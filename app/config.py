"""Environment-backed configuration. No secret ever leaves this module."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODELS = (
    "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,"
    "gemini-3.5-flash,gemini-3.1-pro-preview"
)


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Populate os.environ from a .env file without overwriting real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [
    m.strip()
    for m in os.environ.get("GEMINI_MODELS", DEFAULT_MODELS).split(",")
    if m.strip()
]
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "10"))
LLM_TOTAL_BUDGET_S = float(os.environ.get("LLM_TOTAL_BUDGET_S", "11"))
PORT = int(os.environ.get("PORT", "8000"))

# Judge tolerance is 0.01; we keep our own slack an order of magnitude tighter.
TOLERANCE = 1e-3
# Battery bounds are shrunk by this much inside the LP so float drift in the
# solver can never push a reported state outside a strict bound check.
BOUND_EPS = 1e-6


def llm_available() -> bool:
    return bool(GEMINI_API_KEY)
