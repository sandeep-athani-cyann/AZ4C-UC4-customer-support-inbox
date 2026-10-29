"""Paths and settings, all read from the environment (code/.env is loaded by __main__)."""
import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = CODE_DIR.parent


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or REPO_DIR).resolve()


def db_path() -> Path:
    return data_dir() / "data" / "store.sqlite"


def docs_dir() -> Path:
    return data_dir() / "docs"


def mock_dir() -> Path:
    """mock_system/ at the repository root (COMMON.md). MOCK_DIR overrides it."""
    return Path(os.environ.get("MOCK_DIR") or REPO_DIR / "mock_system").resolve()


RUNS_DIR = CODE_DIR / "runs"      # paused runs waiting for approval
TRACES_DIR = CODE_DIR / "traces"  # one JSONL trace per run


def use_llm() -> bool:
    return os.environ.get("USE_LLM", "1").strip().lower() not in ("0", "false", "no", "")
