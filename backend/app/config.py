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

import secrets
import tempfile
from pathlib import Path


def _load_or_create_auth_secret() -> str:
    env_secret = os.getenv("AUTH_SECRET_KEY", "").strip()
    if env_secret:
        if len(env_secret) < 32:
            print("[Security Warning] AUTH_SECRET_KEY tiene menos de 32 caracteres; se recomienda >= 32 bytes aleatorios.")
        return env_secret
    secret_file = Path(tempfile.gettempdir()) / ".tasks_infra_session_secret"
    try:
        if secret_file.exists():
            stored = secret_file.read_text(encoding="utf-8").strip()
            if len(stored) >= 32:
                return stored
        generated = secrets.token_urlsafe(48)
        secret_file.write_text(generated, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except Exception:
            pass
        return generated
    except Exception:
        return secrets.token_urlsafe(48)


# Seguridad Fase 2: Sesiones firmadas, compatibilidad HTTP (:7070) y TV Kiosko
AUTH_SECRET_KEY = _load_or_create_auth_secret()
SESSION_COOKIE_NAME = "session_token"
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes")
try:
    SESSION_TTL_SECONDS = max(900, int(os.getenv("SESSION_TTL_SECONDS", "43200")))
except ValueError:
    SESSION_TTL_SECONDS = 43200
KIOSK_SECRET = os.getenv("KIOSK_SECRET", "").strip()
KIOSK_ALLOWED_IPS = [ip.strip() for ip in os.getenv("KIOSK_ALLOWED_IPS", "").split(",") if ip.strip()]


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
