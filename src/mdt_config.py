"""Configuration for the MDT pipeline.

The model endpoint is intentionally provider-neutral. The current deployment can
use an institution-approved HKU/Azure OpenAI-compatible gateway; a local
OpenAI-compatible server can be selected later without changing clinical code.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent
PIPELINE_VERSION = "mdt-clinical-workflow-2.2"

_LEGACY_DATA_DIR = r"D:\Azure_checks\Agentic_AI_MDT_data\Processed_pateint_data"


@dataclass(frozen=True)
class Settings:
    patient_data_dir: Path
    output_dir: Path
    patients: tuple[str, ...]
    scenario: str
    base_url: str
    model_name: str
    api_key: str
    client_max_retries: int
    max_prompt_chars: int
    llm_concurrency: int
    agent_attempts: int
    chair_attempts: int
    guideline_max_sources: int
    trial_max: int
    trial_statuses: str
    trial_location: str
    llm_provider: str = "openai_compatible"  # openai_compatible | azure_openai | local
    azure_endpoint: str = ""
    azure_api_version: str = ""
    auth_style: str = "bearer"               # bearer | api-key (gateway-specific)
    trial_search_limit: int = 100

    @property
    def scenario_dir(self) -> Path:
        return SRC_DIR / "scenarios" / self.scenario

    @property
    def template_path(self) -> Path:
        return self.scenario_dir / "templates" / "tumor_board_template.docx"

    @property
    def agents_yaml(self) -> Path:
        return self.scenario_dir / "config" / "agents.yaml"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _validate_http_endpoint(url: str, provider: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise SystemExit("The model endpoint must be a complete http(s) URL.")
    host = (parsed.hostname or "").lower()
    is_loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not is_loopback and os.environ.get("ALLOW_INSECURE_LLM_HTTP", "0") != "1":
        raise SystemExit("Remote model endpoints must use HTTPS. HTTP is allowed only for loopback/local testing.")
    if provider == "azure_openai" and parsed.scheme != "https" and not is_loopback:
        raise SystemExit("A remote Azure OpenAI endpoint must use HTTPS.")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run the AI-assisted gynae-oncology MDT workflow.")
    p.add_argument("--patients", nargs="+", help="Patient IDs (folder names under the data dir).")
    p.add_argument("--all", action="store_true", help="Run every patient folder found in the data dir.")
    p.add_argument("--data-dir", help="Folder containing <patient_id>/clinical_notes/*.json")
    p.add_argument("--output-dir", help="Where per-patient run outputs are written.")
    p.add_argument("--env", help="Path to a .env file (default: <project>/.env)")
    return p


def load_settings(argv: list[str] | None = None) -> Settings:
    args = build_arg_parser().parse_args(argv)

    from dotenv import load_dotenv
    env_path = Path(args.env) if args.env else PROJECT_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)

    provider = (os.environ.get("LLM_PROVIDER") or "openai_compatible").strip().lower()
    if provider == "local":
        provider = "openai_compatible"
        os.environ.setdefault("OPENAI_AUTH_STYLE", "bearer")
    if provider not in {"openai_compatible", "azure_openai"}:
        raise SystemExit("LLM_PROVIDER must be 'openai_compatible' or 'azure_openai' (use openai_compatible for the local replacement later).")

    model_name = (os.environ.get("MODEL_NAME") or os.environ.get("OPENAI_MODEL") or "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "").strip()
    auth_style = (os.environ.get("OPENAI_AUTH_STYLE") or ("api-key" if api_version else "bearer")).strip().lower()

    if auth_style not in {"bearer", "api-key"}:
        raise SystemExit("OPENAI_AUTH_STYLE must be 'bearer' or 'api-key'.")
    if not model_name:
        raise SystemExit("Set MODEL_NAME to the deployed model name (or Azure deployment name).")
    if provider == "azure_openai":
        if not azure_endpoint or not api_version or not api_key:
            raise SystemExit("For LLM_PROVIDER=azure_openai, set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_VERSION and OPENAI_API_KEY.")
        base_url = azure_endpoint
        _validate_http_endpoint(azure_endpoint, provider)
    else:
        if not base_url:
            raise SystemExit("Set OPENAI_BASE_URL to the approved HKU/Azure OpenAI gateway URL (or local endpoint when switched later).")
        if not api_key and not urlparse(base_url).hostname in {"localhost", "127.0.0.1", "::1"}:
            raise SystemExit("OPENAI_API_KEY is required for a remote model gateway.")
        _validate_http_endpoint(base_url, provider)

    data_dir = Path(args.data_dir or os.environ.get("MDT_PATIENT_DATA_DIR") or _LEGACY_DATA_DIR)
    if not data_dir.is_dir():
        raise SystemExit(f"Patient data dir not found: {data_dir} (use --data-dir or MDT_PATIENT_DATA_DIR)")
    out_dir = Path(args.output_dir or os.environ.get("MDT_OUTPUT_DIR") or PROJECT_DIR / "mdt_output")

    if args.all:
        patients = tuple(sorted(p.name for p in data_dir.iterdir() if p.is_dir() and not p.is_symlink() and (p / "clinical_notes").is_dir()))
    else:
        patients = tuple(args.patients or [s.strip() for s in os.environ.get("MDT_PATIENTS", "").split(",") if s.strip()])
    if not patients:
        raise SystemExit("No patients given: use --patients subject0001 [...] or --all")

    default_concurrency = 1 if urlparse(base_url).hostname in {"localhost", "127.0.0.1", "::1"} else 3
    return Settings(
        patient_data_dir=data_dir,
        output_dir=out_dir,
        patients=patients,
        scenario=os.environ.get("SCENARIO", "default"),
        base_url=base_url.rstrip("/"),
        model_name=model_name,
        api_key=api_key or "local",
        client_max_retries=_env_int("OPENAI_MAX_RETRIES", 3),
        max_prompt_chars=_env_int("MDT_MAX_PROMPT_CHARS", 20000),
        llm_concurrency=max(1, _env_int("MDT_LLM_CONCURRENCY", default_concurrency)),
        agent_attempts=max(1, _env_int("MDT_AGENT_ATTEMPTS", 3)),
        chair_attempts=max(1, _env_int("MDT_CHAIR_ATTEMPTS", 3)),
        guideline_max_sources=max(1, min(8, _env_int("MDT_GUIDELINE_MAX_SOURCES", 4))),
        trial_max=max(1, _env_int("MDT_TRIAL_MAX", 15)),
        trial_statuses=os.environ.get("MDT_TRIAL_STATUSES", "RECRUITING").strip(),
        trial_location=os.environ.get("MDT_TRIAL_LOCATION", "").strip(),
        llm_provider=provider,
        azure_endpoint=azure_endpoint,
        azure_api_version=api_version,
        auth_style=auth_style,
        trial_search_limit=max(1, min(1000, _env_int("MDT_TRIAL_SEARCH_LIMIT", 100))),
    )


def file_fingerprint(*paths: Path) -> str:
    """Short hash of prompt/config files, stamped on every report so a result can be traced to its prompts."""
    h = hashlib.sha256()
    for p in paths:
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:8]
