import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
DB_PATH = Path(os.getenv("HEALING_DB_PATH", os.getenv("HEAL_DB_PATH", "./healing_history.sqlite")))

DISK_USAGE_WARN_PERCENT = float(os.getenv("HEAL_DISK_WARN", "75.0"))
DISK_USAGE_CRIT_PERCENT = float(os.getenv("HEAL_DISK_CRIT", "85.0"))
MEM_AVAILABLE_MIN_MB = float(os.getenv("HEAL_MEM_MIN_MB", "1500.0"))
SWAP_USED_MAX_MB = float(os.getenv("HEAL_SWAP_MAX_MB", "4000.0"))

MAX_REMEDIATIONS_PER_RUN = int(os.getenv("HEAL_MAX_REMEDIATIONS", "2"))
COOLDOWN_SECONDS = int(os.getenv("HEAL_COOLDOWN_SEC", "20"))
LOG_SCAN_HOURS = int(os.getenv("HEAL_LOG_SCAN_HOURS", "24"))

LLM_CALL_TIMEOUT = float(os.getenv("LLM_CALL_TIMEOUT", "45.0"))
CIRCUIT_BREAKER_RECOVERY_TIMEOUT = float(os.getenv("CIRCUIT_BREAKER_RECOVERY_TIMEOUT", "60.0"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def validate_telegram_config() -> None:
    """Validates that Telegram credentials are provided in the environment."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in the environment.")


LOG_IGNORE_PATTERNS = [
    "Neo.ClientNotification.Statement.UnknownRelationshipTypeWarning",
    "Detection mechanism has observed",
    "HTTP Request: POST https://api.telegram.org",
    "Detected filter using positional arguments",
    "null value eliminated in set function",
]
