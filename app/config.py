"""Environment-backed configuration. No secret ever leaves this module."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Google Gemini, newest first. Extra flash tiers are included so a per-model
# daily cap running out does not take the provider down with it.
DEFAULT_GEMINI_MODELS = (
    "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,"
    "gemini-3-flash-preview,gemini-2.5-flash,gemini-2.5-flash-lite,"
    "gemini-3.1-pro-preview"
)

# Groq, a second provider on a completely separate quota. Only chat-completion
# models that passed structured-extraction testing are listed: the whisper
# (speech to text), orpheus (text to speech) and llama-prompt-guard
# (classifier) models on the account cannot do this task.
DEFAULT_GROQ_MODELS = (
    "qwen/qwen3.8-27b,openai/gpt-oss-20b,openai/gpt-oss-120b,groq/compound-mini"
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

def _models(env_name: str, default: str) -> list[str]:
    return [m.strip() for m in os.environ.get(env_name, default).split(",") if m.strip()]


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = _models("GEMINI_MODELS", DEFAULT_GEMINI_MODELS)
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODELS = _models("GROQ_MODELS", DEFAULT_GROQ_MODELS)
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# How many models to race at once. Racing every configured model on every
# request is wasteful and trips per-model rate limits under repeated load, so
# a small fast tier goes first and the rest are only used if that tier fails.
LLM_RACE_SIZE = int(os.environ.get("LLM_RACE_SIZE", "3"))
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "10"))
LLM_TOTAL_BUDGET_S = float(os.environ.get("LLM_TOTAL_BUDGET_S", "7"))
PORT = int(os.environ.get("PORT", "8000"))

# Judge tolerance is 0.01; we keep our own slack an order of magnitude tighter.
TOLERANCE = 1e-3
# Reported values are rounded to this many decimals. HiGHS respects bounds to
# roughly 1e-9 and the judge tolerance is 0.01, so rounding here is what keeps
# the plan clean; the LP needs no artificial safety margin on its bounds.
ROUND_DP = 6


def llm_available() -> bool:
    return bool(GEMINI_API_KEY or GROQ_API_KEY)
