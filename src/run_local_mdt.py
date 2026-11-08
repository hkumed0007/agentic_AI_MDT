"""AI-assisted gynae-oncology MDT pipeline (institutional gateway or local endpoint).

Per patient:  notes -> timeline (source-checked) -> CaseFacts (quote-verified) -> Radiology / Pathology / PatientStatus ->
GuidelineEvidence -> GynaeOncologist -> RadiationOncologist -> MedicalOncologist -> ClinicalTrials -> MDT chair decision ->
automated checks -> Word report. Every prompt is built from this patient's source-linked facts with explicit verification states.

    python run_local_mdt.py --patients subject0001 subject0002
    python run_local_mdt.py --all --data-dir D:\\path\\to\\Processed_patient_data
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import sys
import traceback
import uuid
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from data_models.chat_context import ChatContext                      # noqa: E402
from data_models.mdt_decision import Issue                            # noqa: E402
from mdt import prompts                                               # noqa: E402
from mdt.agents import build_agent, load_agent_configs, run_agent     # noqa: E402
from mdt.chair import build_chair_input, run_chair                    # noqa: E402
from mdt.facts import extract_case_facts                              # noqa: E402
from mdt.lint import lint, overall_status                             # noqa: E402
from mdt.llm import configure_concurrency, create_chat_service        # noqa: E402
from mdt.notes import LocalNoteStore, validate_id                     # noqa: E402
from mdt.opinions import parse_opinion                                # noqa: E402
from mdt.report import ReportInputs, render_report                    # noqa: E402
from mdt.timeline import build_timeline, format_timeline, verify_timeline  # noqa: E402
from mdt_config import PIPELINE_VERSION, Settings, file_fingerprint, load_settings  # noqa: E402

logger = logging.getLogger("mdt")
EXPERTS = ("GynaeOncologist", "RadiationOncologist", "MedicalOncologist")


def _guideline_index_hash() -> str:
    p = Path(os.environ.get("GYNAE_GUIDELINE_MANIFEST") or SRC_DIR.parent / "Guidelines" / "kb_build" / "manifest.jsonl")
    if not p.exists():
        return "n/a"
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o)),
                    encoding="utf-8")


async def run_patient(settings: Settings, patient_id: str, other_ids: list[str], store: LocalNoteStore, cfgs: dict) -> dict:
    validate_id(patient_id)
    # Each execution gets an isolated immutable output directory. A failed run
    # can never leave an old mdt_decision.json looking like the current result.
    run_id = f"{dt.datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    out = settings.output_dir / patient_id / "runs" / run_id
    out.mkdir(parents=True, exist_ok=False)
    transcript: list[dict] = []
    run_issues: list[Issue] = []
    excluded = {s.strip() for s in os.environ.get("EXCLUDED_AGENTS", "").split(",") if s.strip()}

    def log_step(role: str, text: str) -> None:
        transcript.append({"role": role, "text": text})
        logger.info("[%s] done", role)

    service = create_chat_service(settings)
    bundle = store.load(patient_id)
    chat_ctx = ChatContext(conversation_id=run_id, patient_id=patient_id)

    # 1. timeline, every cited sentence checked against the notes
    timeline = await build_timeline(service, bundle, patient_id, settings.max_prompt_chars)
    tl_check = verify_timeline(timeline, bundle)
    _write_json(out / "patient_timeline.json", timeline)
    _write_json(out / "timeline_verification.json", {"verified": tl_check.verified_sources, "total": tl_check.total_sources,
                                                      "flagged_entries": tl_check.flagged_entries})
    log_step("PatientHistory", format_timeline(timeline, tl_check))

    # 2. case facts, every quote checked against the notes
    facts = await extract_case_facts(service, bundle, patient_id, settings.max_prompt_chars)
    _write_json(out / "case_facts.json", facts)
    log_step("CaseFacts", facts.briefing(include_imaging=True))

    # 3. agents, each with a curated briefing (fresh thread per call)
    outputs: dict[str, str] = {}

    async def step(name: str, prompt: str) -> str:
        if name in excluded:
            outputs[name] = "(agent excluded for this run)"
            return outputs[name]
        try:
            agent = build_agent(cfgs[name], settings, chat_ctx)
            outputs[name] = await run_agent(agent, prompt, attempts=settings.agent_attempts)
        except Exception as exc:
            logger.error("%s failed: %s", name, exc)
            outputs[name] = f"[{name} FAILED: {exc}]"
            run_issues.append(Issue(severity="error", code="AGENT_FAILED", message=f"{name} failed: {exc}"))
        log_step(name, outputs[name])
        return outputs[name]

    # Evidence-specialist passes use the same locked fact snapshot. They do not
    # review each other's conclusions, which reduces downstream anchoring.
    path, rad = await asyncio.gather(
        step("Pathology", prompts.pathology_prompt(facts)),
        step("Radiology", prompts.radiology_prompt(facts)),
    )
    status = await step("PatientStatus", prompts.status_prompt(facts, path, rad))
    guide = await step("GuidelineEvidence", prompts.guideline_prompt(facts, status))
    trials = await step("ClinicalTrials", prompts.trials_prompt(facts))

    # All three clinical specialties receive the identical evidence/context set.
    # No specialty is given an earlier specialty's opinion as an input.
    base_ctx = {"Pathology": path, "Radiology": rad, "PatientStatus": status,
                "GuidelineEvidence": guide, "ClinicalTrials": trials}
    await asyncio.gather(*(step(role, prompts.expert_prompt(facts, base_ctx)) for role in EXPERTS))

    opinions = {r: parse_opinion(r, outputs.get(r, "")) for r in EXPERTS}
    for r in EXPERTS:
        if outputs.get(r, "").startswith(f"[{r} FAILED") or outputs.get(r, "").startswith("(agent excluded"):
            opinions[r] = parse_opinion(r, "")

    # 4. MDT chair
    decision = None
    try:
        chair_in = build_chair_input(facts, opinions, {"Pathology": path, "Radiology": rad, "PatientStatus": status},
                                     chat_ctx.retrieved_guideline_sources, trials, settings.max_prompt_chars)
        decision = await run_chair(service, cfgs["MDTChair"]["instructions"], chair_in, settings.chair_attempts)
        _write_json(out / "mdt_decision.json", decision)
        log_step("MDTChair", decision.model_dump_json(indent=2))
    except Exception as exc:
        logger.error("MDT chair failed: %s", exc)
        run_issues.append(Issue(severity="error", code="CHAIR_FAILED", message=f"MDT chair failed: {exc}"))
        log_step("MDTChair", f"[FAILED: {exc}]")

    # 5. automated checks
    # Scan every model-generated step, not just final specialty opinions, for cross-patient identifiers.
    scan = [facts.briefing(include_imaging=True)] + [t.get("text", "") for t in transcript] + ([decision.model_dump_json()] if decision else [])
    issues = run_issues + lint(decision, facts, opinions, {s["chunk_id"] for s in chat_ctx.retrieved_guideline_sources},
                               tl_check, [p for p in other_ids if p != patient_id], scan)
    status_label = overall_status(issues)
    _write_json(out / "automated_checks.json", {"status": status_label, "issues": issues})

    # 6. report
    meta = {"run_id": run_id, "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "pipeline": PIPELINE_VERSION, "model": settings.model_name,
            "llm_provider": getattr(settings, "llm_provider", "openai_compatible"),
            "prompts": file_fingerprint(settings.agents_yaml), "guideline_index": _guideline_index_hash(),
            "clinical_release_status": "NOT_FOR_CLINICAL_USE_PENDING_NAMED_MDT_SIGN_OFF"}
    trial_err = next((e for e in chat_ctx.tool_errors if "trial" in e), "")
    docx = render_report(
        ReportInputs(patient_id=patient_id, facts=facts, timeline=timeline, timeline_check=tl_check, opinions=opinions,
                     decision=decision, issues=issues, status=status_label,
                     guideline_sources=chat_ctx.retrieved_guideline_sources, trial_candidates=chat_ctx.trial_candidates,
                     trial_error=trial_err, run_meta=meta),
        settings.template_path, out / f"MDT_report_{patient_id}.docx", out / "_timeline_images")
    _write_json(out / "run_metadata.json", meta)
    _write_json(out / "transcript.json", transcript)
    (out / "chat_history_readable.txt").write_text(
        "\n\n".join(f"===== {t['role']} =====\n{t['text']}" for t in transcript), encoding="utf-8")
    return {"patient_id": patient_id, "run_id": run_id, "run_dir": str(out), "status": status_label, "report": str(docx), "issues": len(issues)}


async def main(argv: list[str] | None = None) -> int:
    settings = load_settings(argv)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(settings.output_dir / "run.log", encoding="utf-8")])
    configure_concurrency(settings.llm_concurrency)
    store = LocalNoteStore(settings.patient_data_dir)
    cfgs = load_agent_configs(settings.agents_yaml)
    all_ids = store.patients()
    results = []
    for pid in settings.patients:
        logger.info("=== %s ===", pid)
        try:
            results.append(await run_patient(settings, pid, all_ids, store, cfgs))
        except Exception as exc:
            logger.error("Patient %s failed: %s\n%s", pid, exc, traceback.format_exc())
            results.append({"patient_id": pid, "status": "ERROR", "error": str(exc)})
    print("\nSummary")
    for r in results:
        print(f"  {r['patient_id']}: {r['status']}  {r.get('report', r.get('error', ''))}")
    return 1 if any(r["status"] in {"ERROR", "FAILED"} for r in results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
