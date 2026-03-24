import os
from pathlib import Path
from dotenv import load_dotenv

_BASE_DIR = Path(__file__).parent.parent
_ENV_PATH = _BASE_DIR / ".env"

if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)

ZALO_OA_ID     = os.getenv("ZALOOA_ID", "")
ZALO_TOKEN     = os.getenv("ZALOOA_ACCESS_TOKEN", "")

GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL     = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

USE_OLLAMA     = os.getenv("USE_OLLAMA", "false").lower() == "true"
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "llama3.2")

CALENDAR_ID    = os.getenv("GOOGLE_CALENDAR_ID", "primary")

__all__ = [
    "ZALO_OA_ID",
    "ZALO_TOKEN",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "USE_OLLAMA",
    "OLLAMA_MODEL",
    "CALENDAR_ID",
]