import os
from pathlib import Path
from dotenv import load_dotenv

# Base Directory & Environment Loading
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# Bot & Admin Settings
VERSION = "v4.0.0-beta.3"
BOT_TOKEN = os.getenv("BOT_TOKEN", os.getenv("HST_AGENT_BOT_TOKEN", ""))
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

# Destination Routing for Supergroups & Topics
MAIN_GROUP_ID = int(os.getenv("MAIN_GROUP_ID", "0"))
MY_PROJECT_GROUP_ID = MAIN_GROUP_ID  # Backward-compatible alias
SANDBOX_GROUP_ID = int(os.getenv("SANDBOX_GROUP_ID", "0"))

OPERATIONAL_LOGS_TOPIC_ID = int(os.getenv("OPERATIONAL_LOGS_TOPIC_ID", "10"))
BACKUPS_TOPIC_ID = int(os.getenv("BACKUPS_TOPIC_ID", "20"))
ADMIN_DM_LOGS_TOPIC_ID = int(os.getenv("ADMIN_DM_LOGS_TOPIC_ID", "0"))
REP_LOGS_TOPIC_ID = int(os.getenv("REP_LOGS_TOPIC_ID", "0"))

# Optional Bootstrap Representatives (Loaded dynamically from DB if empty)
REPRESENTATIVES = {}

# Paths
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "agent_bot.db"))
BACKUP_DIR = os.getenv("BACKUP_DIR", str(BASE_DIR / "backups"))

# Public subscription URL configuration. No live-domain fallback is embedded in code.
SUB_DOMAIN = os.getenv("SUB_DOMAIN", "").strip()
SUB_PORT = int(os.getenv("SUB_PORT", "443"))
SUB_PATH = os.getenv("SUB_PATH", "sub").strip("/")

# Ensure local directories exist
db_parent = os.path.dirname(DB_PATH)
if db_parent:
    os.makedirs(db_parent, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
