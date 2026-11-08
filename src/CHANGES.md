# mdt-local 2.0 - what changed and how to run it

## Run
```
pip install -r requirements-local.txt
copy .env.example ..\.env        # set OPENAI_BASE_URL and MODEL_NAME (local OpenAI-compatible server)
python run_local_mdt.py --patients subject0001 subject0002 --data-dir D:\...\Processed_pateint_data
python -m pytest tests            # 39 tests, no model needed
```
Outputs per run in `mdt_output/<patient_id>/runs/<run_id>/`: `MDT_report_<id>.docx`, `case_facts.json` (every value with its source quote
and check state), `mdt_decision.json`, `automated_checks.json`, `patient_timeline.json`, `transcript.json`,
`chat_history_readable.txt`, `run_metadata.json`.

## Flow
notes -> timeline (cited sentences checked) -> CaseFacts (quotes checked, ECOG/stage/MMR cross-checked) ->
Pathology / Radiology / PatientStatus -> GuidelineEvidence -> GynaeOncologist -> RadiationOncologist -> MedicalOncologist ->
ClinicalTrials -> **MDT chair decision** (schema-validated JSON) -> automated checks -> Word report.

The runner is the orchestrator. Each agent gets a fresh thread containing only a briefing built from THIS patient's source-linked facts and explicit verification states.

## MDT chair (new)
Every specialist's reply is kept verbatim. The chair reads all three plus the source-linked facts, guideline excerpts and trial
candidates and returns: intent, summary, immediate plan, conditional plan/gates, not-recommended options, agreements,
disagreements and how resolved, guideline citations (chunk IDs), trial relevance, open questions, actions with owners, reassessment.
Prompt: `scenarios/default/config/agents.yaml` -> `MDTChair`. The Word report shows the chair's plan AND each specialist's recommendation.

## Review findings -> fixes
| # | Finding | Fix |
|---|---|---|
| 1 | `subject0001` facts hard-coded in prompts/cache | All prompts built from the current patient's CaseFacts (`mdt/prompts.py`); cache shortcuts removed; test asserts no legacy text reaches any prompt |
| 2 | Cached history dropped latest events, "ct" matched "doctor", stale cache | Removed. Full timeline/facts every run |
| 3 | `re.match` ID validation, LLM-supplied patient_id | `fullmatch` + path confinement; patient locked by the runner; agents have no patient-data tool |
| 4 | No grounding checks | Quote verification (timeline + facts), regex cross-checks, deterministic lint, report filled from structured data; `process_prompt` ("may infer") removed |
| 5 | Silent truncation / crashes on local model | Batching by `MDT_MAX_PROMPT_CHARS`; oversize note = error; `finish_reason=length` = error; schema-validated retries; LLM calls throttled |
| 6 | Radiology skipped; Pathology read second-hand text | Both get verified items extracted from raw notes |
| 7 | No chair step | Added (above) |
| 8 | ClinicalTrials dropped negative biomarkers etc. | Fixed prompt/signature; schema verdicts; statuses/location configurable; results labelled candidates |
| 9 | Guideline plumbing | Manifest remapped once per process; sources recorded; chair citations checked against what was retrieved |
| 10 | Report looked signed-off | AI-draft banner (page 1 + every footer), automated-check block, sign-off block, provenance footer (model, prompt hash, guideline index hash) |
| 11 | Azure deployment coupling | Legacy Azure-specific dependencies were removed in 2.0; 2.1 adds a provider-neutral HKU/Azure-compatible gateway and native Azure option, while retaining a local-compatible replacement path. |
| 12 | Config / tests | CLI + env settings, `.env.example`, `.gitignore`, 39 tests |

## Automated checks (never block the report; any ERROR marks it FAILED on the page)
errors: missing specialist opinion, no/empty chair decision, chair cited a guideline chunk that was not retrieved, "undetermined"
with no open questions, another patient's ID in the text. warnings: unverified key facts, cross-check mismatches, record
conflicts, key gaps not listed as open questions, ECOG>=3 with systemic therapy starting now, <90% of timeline sources verified,
no guideline evidence. They check traceability and completeness - NOT clinical appropriateness.

## Not verified here (please check on your machine)
- Run against a real model and your real `Guidelines/` index (only a scripted fake model and a stub index were available).
- `search_guidelines` return shape is unknown to me: chunk IDs are found by scanning for `chunk_id`/`id`. If your tool uses another
  key, every chair citation will fail the check - fix `mdt/guideline_sources.py` once.
- Your server must honour JSON-schema `response_format` (timeline, facts, chair). If not, those steps fail loudly after retries.
- ClinicalTrials.gov: no internet in my sandbox; `MDT_TRIAL_STATUSES` assumes comma-separated values.
- Note files must have names of letters/digits/`-`/`_` (e.g. UUIDs).
- Re-create the template from the original with `python tools/patch_template.py <original.docx> <out.docx>`.

## Before clinical use
Validate retrospectively on past cases against the real MDT outcomes (including deliberately hard ones: stage IV, ECOG 3-4,
missing biomarkers), complete your institution's data-protection / ethics / IT-governance review for the model host, and keep a
named clinician sign-off on every report (the template has the block).


## 2.1 safety and deployment update

### Current HKU/Azure gateway support
- Removed the false "local-only" assumption. The current supported configuration is an HKU-approved Azure OpenAI-compatible gateway using `OPENAI_BASE_URL`, `MODEL_NAME`, `OPENAI_API_KEY`, and optional `AZURE_OPENAI_API_VERSION` / `OPENAI_AUTH_STYLE`.
- Added native Azure OpenAI SDK configuration (`LLM_PROVIDER=azure_openai`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`).
- Kept a provider-neutral OpenAI-compatible route so a local endpoint can replace the gateway later without changing MDT orchestration or clinical code.
- Remote endpoints require HTTPS; local loopback HTTP remains available for local inference/testing.
- Added root-level `.env.example` and `.gitignore`. Credential-bearing `.env` files are excluded from the updated archive.

### Clinical and software safety fixes
- Replaced permissive 50% token-overlap fact verification with conservative claim support; negation is no longer dropped from token checks.
- Added attribute-specific contradiction checks for stage, ECOG, MMR/MSI, ER/PR, p53, POLE, LVSI, margins, invasion, nodal status, cytology, HER2 and residual-disease statements. Explicit contradictions are now suppressed as `unverified` rather than passed downstream as merely inferred.
- Required a source quote to be present in the declared note for fact and timeline verification.
- Strengthened imaging verification to require source quote, date, modality and finding support.
- Rejected symbolic links for patient directories, note directories and individual note files.
- Changed specialist oncology reviews to use the same evidence/context snapshot; they no longer receive earlier specialist opinions. Pathology/radiology review can run independently. Excluded or failed specialists are now treated as missing opinions.
- Gave each execution a unique run directory under `mdt_output/<patient_id>/runs/<run_id>/`, preventing stale decisions from being confused with current-run output.
- Changed the CLI exit code to non-zero for `FAILED` as well as `ERROR` runs. Cross-patient identifier checks now scan every model-generated workflow step and use complete-ID boundaries.
- Added ClinicalTrials.gov v2 pagination with a configurable retrieval cap and bounded screening/candidate counts.

### Validation performed in this environment
- Python compilation passed.
- 44 focused unit tests passed.
- Full end-to-end test execution remains blocked in this container because `semantic-kernel` and `docxtpl` are not installed. The end-to-end tests remain in the project and should be run after installing `requirements-local.txt` in the target environment.
- No live HKU Azure gateway call, real guideline retrieval, ClinicalTrials.gov request, or clinical accuracy validation was performed.

### Clinical-use restriction
This remains a research prototype. Automated evidence checks establish provenance/completeness only; they do not establish clinical correctness or guideline concordance. Every generated report requires review and sign-off by the responsible clinicians.


## 2.2 - merge of 2.0 (mine) and 2.1 (gateway + hardening), plus fixes

Base: 2.1. Everything in 2.1 is kept (gateway/Azure provider settings, HTTPS enforcement, symlink rejection, per-run folders,
conservative fact/timeline/imaging verification, contradiction checks, independent specialist reviews, trial pagination).

### Fixed in 2.2
- **Imaging was withheld.** 2.1 required the imaging modality word (CT/MRI/...) to appear inside the quoted sentence, so findings
  listed under a header such as "CT abdomen/pelvis ..." were marked UNVERIFIED and the Radiology agent was told "Imaging: Not
  stated". The modality may now appear as a whole word in the quote or in the 400 characters of the same note before it. Substring
  matching is gone ("ct" no longer matches "infection"). The stricter rule that the *finding* must be supported by its quote is kept.
- **Timeline clipped in the Word report.** The 2.1 timeline image (5.5 in) overflowed the template column; back to 3.4 in.
- **Red "AI-GENERATED DRAFT - NOT YET REVIEWED BY A CLINICIAN" headline** at the top of the case column was missing; restored
  (a test now checks it is in the page body, not only the footer).
- **A failing 2.1 test** (the specialist-snapshot test also matched the chair's prompt) is fixed.

### New in 2.2 - the chair reviews every specialist before deciding
- The chair's output starts with `specialist_review`: exactly one entry per specialist (Gynae, Radiation, Medical) with the
  stance the chair read, a faithful one-sentence position, how it was used (adopted / partly adopted / not adopted / no usable
  opinion) and why. It is the first field of the JSON so it is written before any decision field.
- **Automated checks** (errors): chair did not review all three exactly once; chair read a stance different from the one the
  specialist declared at the start of its Recommendation; chair marked a missing opinion as adopted.
- **Word order now follows the reasoning:** 1. Specialist recommendations (verbatim, with rationale and needs) ->
  2. MDT chair's review of each specialist -> 3. MDT chair decision. Previously the decision came first.

### Tests
68 tests (unit + end-to-end against the scripted fake model). Not verified: a real gateway/model, the real guideline index,
ClinicalTrials.gov, or clinical accuracy.

### Tuning to expect
2.1's strict rules (full-claim support, 80% description overlap and date match for timeline entries, quote must be in the declared
note) are safe but flag more items "(inferred)" / "[source not verified]". Review the flags on 10-20 real cases before loosening
anything.
