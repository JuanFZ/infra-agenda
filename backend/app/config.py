import os
from pathlib import Path
from dotenv import load_dotenv

# Cargar .env desde el directorio raíz de backend
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

GLPI_URL = os.getenv("GLPI_URL", "http://192.168.100.249/apirest.php")
GLPI_USER = os.getenv("GLPI_USER", "glpi")
GLPI_PASS = os.getenv("GLPI_PASS", "glpi")
GLPI_APP_TOKEN = os.getenv("GLPI_APP_TOKEN", "")

CLICKUP_TOKEN = os.getenv("CLICKUP_TOKEN", "")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dashboard.db")
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "1"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

