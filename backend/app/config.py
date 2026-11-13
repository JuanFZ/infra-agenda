import os
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv

# Cargar .env desde el directorio raíz de backend
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

GLPI_URL = os.getenv("GLPI_URL", "http://192.168.100.249/apirest.php")
GLPI_USER = os.getenv("GLPI_USER", "")
GLPI_PASS = os.getenv("GLPI_PASS", "")
GLPI_APP_TOKEN = os.getenv("GLPI_APP_TOKEN", "")

CLICKUP_TOKEN = os.getenv("CLICKUP_TOKEN", "")
CLICKUP_TEAM_ID = os.getenv("CLICKUP_TEAM_ID", "")
CLICKUP_SPACE_ID = os.getenv("CLICKUP_SPACE_ID", "55559035")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dashboard.db")
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "1"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# URL Base de la aplicación Agenda para deep linking en notificaciones (ej: http://localhost:7070)
AGENDA_BASE_URL = (os.getenv("AGENDA_BASE_URL") or os.getenv("APP_URL") or "http://localhost:8000").strip().rstrip("/")

# Contención Fase 1: Swagger/OpenAPI deshabilitado por defecto en producción y CORS acotado al origen real
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").strip().lower() in ("1", "true", "yes")
DEFAULT_INIT_PASSWORD = os.getenv("DEFAULT_INIT_PASSWORD", "").strip()


def _build_cors_origins() -> list[str]:
    origins: list[str] = []
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
    for item in raw.split(","):
        cleaned = item.strip().rstrip("/")
        if cleaned and cleaned != "*" and cleaned not in origins:
            origins.append(cleaned)

    parsed_base = urlparse(AGENDA_BASE_URL)
    if parsed_base.scheme in ("http", "https") and parsed_base.netloc:
        base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        if base_origin not in origins:
            origins.append(base_origin)

    for local_origin in (
        "http://localhost",
        "http://localhost:80",
        "http://localhost:7070",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:80",
        "http://127.0.0.1:7070",
        "http://127.0.0.1:8000",
    ):
        if local_origin not in origins:
            origins.append(local_origin)

    return origins


CORS_ALLOWED_ORIGINS = _build_cors_origins()
