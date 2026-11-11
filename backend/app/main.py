import os
import uuid
import re
import base64
import datetime
import hashlib
import unicodedata
import urllib.parse
from fastapi import FastAPI, Depends, Query, HTTPException, Header, Response
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import or_, and_, inspect, text
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from .database import engine, Base, get_db, SessionLocal
from .models import Item, SyncLog, AlertLog, User, GeneratedReport, EscalationDocument, IBP
from .config import SYNC_INTERVAL_MINUTES
from .sync.synchronizer import sync_all, purge_old_closed_items, get_yesterday_start
from .sync.glpi_client import GLPIClient
from .sync.clickup_client import ClickUpClient
from .notifications.telegram_notifier import CARACAS_TZ, format_caracas_time, now_caracas_str, test_telegram_connection, get_agenda_deep_link
from .reports.report_generator import get_report_data, generate_excel_report, generate_executive_html

# Directorio de almacenamiento permanente para documentos de escalamiento (PDFs)
UPLOAD_ESCALATIONS_DIR = os.path.abspath(
    os.getenv("UPLOAD_ESCALATIONS_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads", "escalations"))
)
os.makedirs(UPLOAD_ESCALATIONS_DIR, exist_ok=True)

# Crear tablas si no existen
Base.metadata.create_all(bind=engine)

def hash_password(password: str) -> str:
    salt = "sycom_infra_salt_2026"
    return hashlib.sha256(f"{salt}{password}".encode("utf-8")).hexdigest()

def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    if not hashed_password:
        return False
    return hash_password(plain_password) == hashed_password

# Catálogo oficial de las 34 IBPs (Identificación Bancaria del Participante) de la plataforma SIMF
IBP_INVENTORY = [
    {"code": "0000", "name": "BTC", "label": "0000 - BTC"},
    {"code": "0001", "name": "BCV", "label": "0001 - BCV"},
    {"code": "0102", "name": "BDV", "label": "0102 - BDV"},
    {"code": "0104", "name": "BVC", "label": "0104 - BVC"},
    {"code": "0105", "name": "MERCANTIL", "label": "0105 - MERCANTIL"},
    {"code": "0108", "name": "PROVINCIAL", "label": "0108 - PROVINCIAL"},
    {"code": "0114", "name": "BANCARIBE", "label": "0114 - BANCARIBE"},
    {"code": "0115", "name": "EXTERIOR", "label": "0115 - EXTERIOR"},
    {"code": "0128", "name": "CARONÍ", "label": "0128 - CARONÍ"},
    {"code": "0134", "name": "BANESCO", "label": "0134 - BANESCO"},
    {"code": "0137", "name": "SOFITASA", "label": "0137 - SOFITASA"},
    {"code": "0138", "name": "PLAZA", "label": "0138 - PLAZA"},
    {"code": "0145", "name": "BANCOEX", "label": "0145 - BANCOEX"},
    {"code": "0146", "name": "BANGENTE", "label": "0146 - BANGENTE"},
    {"code": "0151", "name": "BFC", "label": "0151 - BFC"},
    {"code": "0152", "name": "BANDES", "label": "0152 - BANDES"},
    {"code": "0156", "name": "100%BANCO", "label": "0156 - 100%BANCO"},
    {"code": "0157", "name": "DELSUR", "label": "0157 - DELSUR"},
    {"code": "0163", "name": "TESORO", "label": "0163 - TESORO"},
    {"code": "0166", "name": "AGRÍCOLA", "label": "0166 - AGRÍCOLA"},
    {"code": "0168", "name": "BANCRECER", "label": "0168 - BANCRECER"},
    {"code": "0169", "name": "R4", "label": "0169 - R4"},
    {"code": "0171", "name": "ACTIVO", "label": "0171 - ACTIVO"},
    {"code": "0172", "name": "BANCAMIGA", "label": "0172 - BANCAMIGA"},
    {"code": "0173", "name": "BID", "label": "0173 - BID"},
    {"code": "0174", "name": "BANPLUS", "label": "0174 - BANPLUS"},
    {"code": "0175", "name": "BDT", "label": "0175 - BDT"},
    {"code": "0177", "name": "BANFANB", "label": "0177 - BANFANB"},
    {"code": "0178", "name": "N58", "label": "0178 - N58"},
    {"code": "0191", "name": "BNC", "label": "0191 - BNC"},
    {"code": "0601", "name": "IMCP", "label": "0601 - IMCP"},
    {"code": "0882", "name": "Suiche7B (Consorcio)", "label": "0882 - Suiche7B (Consorcio)"},
    {"code": "0883", "name": "CONEXUS (Consorcio)", "label": "0883 - CONEXUS (Consorcio)"},
    {"code": "2017", "name": "ONT", "label": "2017 - ONT"}
]

IBP_CODE_MAP = {x["code"]: x["label"] for x in IBP_INVENTORY}

def _clean_ibp_str(s: str | None) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()

IBP_ALIASES = {
    "btc": "0000",
    "bcv": "0001",
    "banco central": "0001",
    "banco central de venezuela": "0001",
    "bdv": "0102",
    "banco de venezuela": "0102",
    "venezuela": "0102",
    "bvc": "0104",
    "banco venezolano de credito": "0104",
    "venezolano de credito": "0104",
    "venezolano": "0104",
    "mercantil": "0105",
    "banco mercantil": "0105",
    "provincial": "0108",
    "bbva": "0108",
    "bbva provincial": "0108",
    "banco provincial": "0108",
    "bancaribe": "0114",
    "banco del caribe": "0114",
    "caribe": "0114",
    "exterior": "0115",
    "banco exterior": "0115",
    "caroni": "0128",
    "banco caroni": "0128",
    "banesco": "0134",
    "banco banesco": "0134",
    "sofitasa": "0137",
    "banco sofitasa": "0137",
    "plaza": "0138",
    "banco plaza": "0138",
    "bancoex": "0145",
    "banco de comercio exterior": "0145",
    "bangente": "0146",
    "bfc": "0151",
    "banco fondo comun": "0151",
    "fondo comun": "0151",
    "bandes": "0152",
    "100%banco": "0156",
    "100% banco": "0156",
    "100 banco": "0156",
    "100banco": "0156",
    "delsur": "0157",
    "del sur": "0157",
    "banco del sur": "0157",
    "tesoro": "0163",
    "banco del tesoro": "0163",
    "agricola": "0166",
    "banco agricola": "0166",
    "banco agricola de venezuela": "0166",
    "bancrecer": "0168",
    "r4": "0169",
    "activo": "0171",
    "banco activo": "0171",
    "bancamiga": "0172",
    "bid": "0173",
    "banplus": "0174",
    "banco banplus": "0174",
    "bdt": "0175",
    "bicentenario": "0175",
    "banco bicentenario": "0175",
    "banco digital de los trabajadores": "0175",
    "banfanb": "0177",
    "n58": "0178",
    "bnc": "0191",
    "banco nacional de credito": "0191",
    "imcp": "0601",
    "suiche": "0882",
    "suiche7b": "0882",
    "suiche 7b": "0882",
    "suiche7b (consorcio)": "0882",
    "conexus": "0883",
    "conexus (consorcio)": "0883",
    "ont": "2017"
}

def normalize_ibp(val: str | None) -> str | None:
    """Normaliza cualquier código, nombre, etiqueta o alias de IBP a su formato canónico oficial SIMF 'XXXX - NOMBRE'."""
    if not val:
        return None
    val_clean = str(val).strip()
    if not val_clean:
        return None

    # 1. Búsqueda directa por código numérico (ej. '0134', '134', '1' -> '0001')
    if val_clean.isdigit() and len(val_clean) <= 4:
        code_pad = val_clean.zfill(4)
        if code_pad in IBP_CODE_MAP:
            return IBP_CODE_MAP[code_pad]

    cleaned = _clean_ibp_str(val_clean)

    # 2. Búsqueda en alias populares y nombres institucionales
    if cleaned in IBP_ALIASES:
        return IBP_CODE_MAP[IBP_ALIASES[cleaned]]

    # 3. Coincidencia exacta con catálogo oficial
    for item in IBP_INVENTORY:
        item_clean_name = _clean_ibp_str(item["name"])
        item_clean_label = _clean_ibp_str(item["label"])
        if cleaned == item_clean_label or cleaned == item["code"] or cleaned == item_clean_name:
            return item["label"]
        if cleaned.startswith(item["code"] + " ") or cleaned.startswith(item["code"] + "-"):
            return item["label"]

    # 4. Coincidencia por alias parcial (ej. 'consorcio suiche 7b', 'banco banesco c.a.')
    for alias_key, code in sorted(IBP_ALIASES.items(), key=lambda x: -len(x[0])):
        if len(alias_key) >= 3 and alias_key in cleaned:
            return IBP_CODE_MAP[code]

    # 5. Coincidencia por nombre en inventario
    for item in IBP_INVENTORY:
        item_clean_name = _clean_ibp_str(item["name"])
        if len(item_clean_name) >= 3 and item_clean_name in cleaned:
            return item["label"]

    return val_clean

def resolve_ibp(db: Session, raw_ibp: str | None, raw_ibp_id: int | None) -> tuple[str | None, int | None]:
    """Resuelve y sincroniza la cadena canónica de IBP y el ID relacional en ibps."""
    if (not raw_ibp or not str(raw_ibp).strip()) and (not raw_ibp_id or raw_ibp_id <= 0):
        return None, None
    clean_ibp = normalize_ibp(raw_ibp) if raw_ibp else None
    matched_ibp = None
    if raw_ibp_id and raw_ibp_id > 0:
        matched_ibp = db.query(IBP).filter(IBP.is_active == 1, IBP.id == raw_ibp_id).first()
        if matched_ibp and not clean_ibp:
            clean_ibp = matched_ibp.name if " - " in matched_ibp.name else (normalize_ibp(matched_ibp.code) or matched_ibp.name)
    if not matched_ibp and clean_ibp:
        code_part = clean_ibp.split(" - ")[0] if " - " in clean_ibp else clean_ibp
        matched_ibp = db.query(IBP).filter(IBP.is_active == 1, IBP.code == code_part).first()
        if not matched_ibp:
            matched_ibp = db.query(IBP).filter(IBP.is_active == 1, or_(IBP.name == clean_ibp, IBP.code == clean_ibp, IBP.name.ilike(f"%{clean_ibp}%"))).first()
    return clean_ibp, (matched_ibp.id if matched_ibp else None)

def ensure_db_schema():
    try:
        inspector = inspect(engine)
        table_names = inspector.get_table_names()
        is_sqlite = engine.dialect.name == "sqlite"
        dt_type = "DATETIME" if is_sqlite else "TIMESTAMP"

        with engine.begin() as conn:
            # 1. items
            if "items" in table_names:
                cols = [c["name"] for c in inspector.get_columns("items")]
                if "assigned_at" not in cols:
                    conn.execute(text(f"ALTER TABLE items ADD COLUMN assigned_at {dt_type}"))
                if "received_at" not in cols:
                    conn.execute(text(f"ALTER TABLE items ADD COLUMN received_at {dt_type}"))
                if "escalated_at" not in cols:
                    conn.execute(text(f"ALTER TABLE items ADD COLUMN escalated_at {dt_type}"))
                if "escalation_id" not in cols:
                    conn.execute(text("ALTER TABLE items ADD COLUMN escalation_id INTEGER"))
                if "recommendations" not in cols:
                    conn.execute(text("ALTER TABLE items ADD COLUMN recommendations TEXT"))
                if "description" not in cols:
                    conn.execute(text("ALTER TABLE items ADD COLUMN description TEXT"))
                if "project_name" not in cols:
                    conn.execute(text("ALTER TABLE items ADD COLUMN project_name VARCHAR(150)"))
                if "created_by" not in cols:
                    conn.execute(text("ALTER TABLE items ADD COLUMN created_by VARCHAR(150)"))

            # 2. users
            if "users" in table_names:
                u_cols = [c["name"] for c in inspector.get_columns("users")]
                if "password_hash" not in u_cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(200)"))
                if "last_login" not in u_cols:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN last_login {dt_type}"))
                if "avatar_data" not in u_cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN avatar_data TEXT"))
                if "phone" not in u_cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(50)"))
                if "job_title" not in u_cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN job_title VARCHAR(100)"))
                if "bio" not in u_cols:
                    conn.execute(text("ALTER TABLE users ADD COLUMN bio VARCHAR(255)"))

            # 3. escalation_documents
            if "escalation_documents" in table_names:
                esc_cols = [c["name"] for c in inspector.get_columns("escalation_documents")]
                if "doc_url" not in esc_cols:
                    conn.execute(text("ALTER TABLE escalation_documents ADD COLUMN doc_url VARCHAR(1000)"))
                if "category" not in esc_cols:
                    conn.execute(text("ALTER TABLE escalation_documents ADD COLUMN category VARCHAR(100) DEFAULT 'Servicios'"))
                if "recommendations" not in esc_cols:
                    conn.execute(text("ALTER TABLE escalation_documents ADD COLUMN recommendations TEXT"))
                if "ibp_id" not in esc_cols:
                    conn.execute(text("ALTER TABLE escalation_documents ADD COLUMN ibp_id INTEGER"))
                if "ibp" not in esc_cols:
                    conn.execute(text("ALTER TABLE escalation_documents ADD COLUMN ibp VARCHAR(100)"))
                if "received_at" not in esc_cols:
                    conn.execute(text(f"ALTER TABLE escalation_documents ADD COLUMN received_at {dt_type}"))
                if "escalated_at" not in esc_cols:
                    conn.execute(text(f"ALTER TABLE escalation_documents ADD COLUMN escalated_at {dt_type}"))
                conn.execute(text("UPDATE escalation_documents SET received_at = created_at WHERE received_at IS NULL"))
                conn.execute(text("UPDATE escalation_documents SET escalated_at = COALESCE(received_at, created_at) WHERE escalated_at IS NULL"))

        # Poblar catálogo oficial de las 34 IBPs (Participantes SIMF) si no existen
        seed_db = SessionLocal()
        try:
            existing = seed_db.query(IBP).all()
            existing_codes = {x.code for x in existing if x.code}
            existing_names = {x.name for x in existing if x.name}
            to_add = []
            for item in IBP_INVENTORY:
                if item["code"] not in existing_codes and item["label"] not in existing_names and item["name"] not in existing_names:
                    to_add.append(IBP(
                        name=item["label"],
                        code=item["code"],
                        description=f"Participante SIMF: {item['label']}",
                        environment="Producción",
                        is_active=1
                    ))
                else:
                    for x in existing:
                        if x.code == item["code"]:
                            x.is_active = 1
                            x.name = item["label"]
            if to_add:
                seed_db.add_all(to_add)
            seed_db.commit()
        except Exception as seed_err:
            print(f"[Seed IBP Warning] {seed_err}")
            seed_db.rollback()
        finally:
            seed_db.close()
    except Exception as e:
        print(f"[DB Schema Check] {e}")

ensure_db_schema()

def _repair_clickup_assignees():
    """Restaura la asignación histórica de especialistas en tareas de ClickUp y normaliza estados 'espera' en ClickUp y nombres de entidades GLPI."""
    try:
        db = SessionLocal()
        repaired = False
        target_ids = ["1dfdcycw8uu", "1dfdcycw8wm"]
        for ext_id in target_ids:
            it = db.query(Item).filter(Item.external_id == ext_id, Item.source == "ClickUp").first()
            if it and (not it.assignee_name or it.assignee_name.lower() in ("sin asignar", "")):
                it.assignee_name = "Juan Fernández"
                if not it.assigned_at:
                    it.assigned_at = it.created_at or datetime.datetime.utcnow()
                repaired = True

        # Normalizar tickets GLPI que tengan 'Entidad #1' / 'Entidad #0' / 'Entidad #2' o menciones de Microfilms
        glpi_items = db.query(Item).filter(Item.source == "GLPI").all()
        for it in glpi_items:
            raw_ent = (it.client_name or "").strip()
            text_blob = f"{it.title or ''} {it.description or ''}".lower()
            if "microfilm" in text_blob or "microfilm" in raw_ent.lower() or raw_ent == "Entidad #2":
                resolved_ent = "Microfilms Center"
            elif raw_ent.startswith("Entidad #"):
                resolved_ent = GLPIClient.clean_entity_name(raw_ent)
                if resolved_ent == raw_ent and raw_ent in ("Entidad #0", "Entidad #1"):
                    resolved_ent = "SYCOM"
            else:
                resolved_ent = raw_ent
            if resolved_ent and resolved_ent != raw_ent:
                it.client_name = resolved_ent
                if not it.project_name or it.project_name.startswith("Entidad #"):
                    it.project_name = resolved_ent
                repaired = True
            elif resolved_ent and not it.project_name:
                it.project_name = resolved_ent
                repaired = True

        if repaired:
            db.commit()

        # Normalizar registros de escalamientos que se hayan guardado con URL encoding (%20)
        encoded_docs = db.query(EscalationDocument).filter(EscalationDocument.uploaded_by.like("%20%")).all()
        for doc in encoded_docs:
            doc.uploaded_by = urllib.parse.unquote(doc.uploaded_by).strip()
            db.commit()
        db.close()
    except Exception as e:
        print(f"[Repair Assignees Error] {e}")

_repair_clickup_assignees()

import unicodedata
import urllib.parse


def strip_accents_str(val: str | None) -> str:
    if not val:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", str(val))
        if unicodedata.category(c) != "Mn"
    ).lower().strip()


def decode_header_val(raw_val: str | None) -> str:
    if not raw_val or not isinstance(raw_val, str):
        return ""
    return urllib.parse.unquote(raw_val).strip()


def is_item_assigned_to_user_name(assignee_name: str | None, user_name_raw: str | None) -> bool:
    """Comprueba si un usuario (por nombre completo, primer nombre, apellido o username) figura en assignee_name."""
    user_clean = decode_header_val(user_name_raw)
    if not user_clean:
        return False
    assignee_raw = (assignee_name or "").strip()
    assignee_lower = assignee_raw.lower()
    if not assignee_raw or "sin asignar" in assignee_lower or "técnico glpi" in assignee_lower or "tecnico glpi" in assignee_lower:
        return False
    assignee_norm = strip_accents_str(assignee_raw)
    user_norm = strip_accents_str(user_clean)
    parts = [p for p in user_norm.replace(".", " ").split() if len(p) >= 3]
    first_norm = parts[0] if parts else user_norm
    if user_norm and user_norm in assignee_norm:
        return True
    if first_norm and len(first_norm) >= 3 and first_norm in assignee_norm:
        return True
    for spec in SPECIALISTS_MAP.values():
        spec_norm = strip_accents_str(spec["name"])
        spec_user = strip_accents_str(spec["email"].split("@")[0])
        if user_norm in (spec_norm, spec["key"], spec_user) or (first_norm and first_norm in spec_norm):
            if spec["key"] in assignee_norm or spec_norm in assignee_norm:
                return True
    return False


def is_user_creator_of_item(created_by: str | None, user_name_raw: str | None) -> bool:
    """Comprueba si un usuario coincide con el creador (created_by) de una actividad."""
    user_clean = decode_header_val(user_name_raw)
    creator_clean = decode_header_val(created_by)
    if not user_clean or not creator_clean:
        return False
    user_norm = strip_accents_str(user_clean)
    creator_norm = strip_accents_str(creator_clean)
    if user_norm and creator_norm and (user_norm in creator_norm or creator_norm in user_norm):
        return True
    parts = [p for p in user_norm.replace(".", " ").split() if len(p) >= 3]
    first_norm = parts[0] if parts else user_norm
    if first_norm and len(first_norm) >= 3 and first_norm in creator_norm:
        return True
    for spec in SPECIALISTS_MAP.values():
        spec_norm = strip_accents_str(spec["name"])
        spec_user = strip_accents_str(spec["email"].split("@")[0])
        if user_norm in (spec_norm, spec["key"], spec_user) or (first_norm and first_norm in spec_norm):
            if spec["key"] in creator_norm or spec_norm in creator_norm or spec_user in creator_norm:
                return True
    return False


def is_item_visible_to_user(item: Item, role_raw: str | None, user_name_raw: str | None) -> bool:
    """Coordinador/Admin ve todas; Especialista/Analista ve sus asignadas y las sin asignar."""
    role = decode_header_val(role_raw).lower()
    user_clean = decode_header_val(user_name_raw)
    if not role or role in ("coordinador", "admin", "administrador"):
        return True
    if not user_clean:
        return True
    assignee_raw = (item.assignee_name or "").strip()
    assignee_lower = assignee_raw.lower()
    if not assignee_raw or "sin asignar" in assignee_lower or "técnico glpi" in assignee_lower or "tecnico glpi" in assignee_lower:
        return True
    return is_item_assigned_to_user_name(assignee_raw, user_clean)


def verify_coordinator_role(x_user_role: str | None = Header(None, alias="X-User-Role")):
    """Valida que la solicitud provenga de un usuario con rol Coordinador o Administrador."""
    role_clean = decode_header_val(x_user_role).lower()
    if not role_clean or role_clean not in ("coordinador", "admin", "administrador"):
        raise HTTPException(
            status_code=403,
            detail="Acceso restringido: Solo el rol Coordinador / Administrador tiene permisos para crear, asignar, mover estados o eliminar actividades."
        )
    return role_clean


def verify_coordinator_or_specialist_role(x_user_role: str | None = Header(None, alias="X-User-Role")):
    """Valida que la solicitud provenga de un usuario con rol Coordinador, Administrador o Especialista."""
    role_clean = decode_header_val(x_user_role).lower()
    if not role_clean or role_clean not in ("coordinador", "admin", "administrador", "especialista", "specialist"):
        raise HTTPException(
            status_code=403,
            detail="Acceso restringido: Solo Coordinadores y Especialistas tienen permisos para crear actividades."
        )
    return role_clean

SPECIALISTS_MAP = {
    "adonis": {
        "key": "adonis",
        "name": "Adonis Lozada",
        "email": "a.lozada@sycom.com.ve",
        "glpi_id": 8,
        "clickup_id": 49560462,
        "telegram": "@adonislozada",
        "role": "Coordinador"
    },
    "juan": {
        "key": "juan",
        "name": "Juan Fernández",
        "email": "j.fernandez@sycom.com.ve",
        "glpi_id": 10,
        "clickup_id": 107105518,
        "telegram": "@Juuan8",
        "role": "Especialista"
    },
    "yoxaide": {
        "key": "yoxaide",
        "name": "Yoxaide Jiménez",
        "email": "y.jimenez@sycom.com.ve",
        "glpi_id": 11,
        "clickup_id": 49560461,
        "telegram": "@Yjimenezg",
        "role": "Especialista"
    },
    "luis": {
        "key": "luis",
        "name": "Luis Acosta",
        "email": "l.acosta@sycom.com.ve",
        "glpi_id": 9,
        "clickup_id": 107105519,
        "telegram": "@lapsy0",
        "role": "Especialista"
    }
}

class AssignRequest(BaseModel):
    specialist_key: str | None = None
    specialist_keys: list[str] | None = None

class StatusUpdateRequest(BaseModel):
    status: str
    recommendations: str | None = None
    escalation_id: int | None = None

class CreateItemRequest(BaseModel):
    source: str
    title: str
    description: str | None = None
    priority: str = "Media"
    specialist_keys: list[str] | None = None
    received_at: str | None = None   # Fecha y hora en que la solicitud llegó a Sycom
    escalated_at: str | None = None  # Fecha y hora en que fue escalada a Infra & BD
    due_at: str | None = None        # Opcional, calculado automáticamente por prioridad a partir de escalated_at
    escalation_id: int | None = None
    project_name: str | None = None  # SIMF, SGLPAR, etc. para ClickUp
    client_name: str | None = None   # Sycom, MicroFilms Center, etc. para GLPI

class UpdateItemRequest(BaseModel):
    title: str | None = None
    priority: str | None = None
    description: str | None = None
    escalation_id: int | None = None
    recommendations: str | None = None
    project_name: str | None = None
    client_name: str | None = None
    received_at: str | None = None
    escalated_at: str | None = None


def parse_iso_utc(raw_str: str | None) -> datetime.datetime | None:
    if not raw_str or not str(raw_str).strip():
        return None
    try:
        clean_val = str(raw_str).strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(clean_val)
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt
    except Exception as e:
        print(f"[ParseISO] Error parseando '{raw_str}': {e}")
        return None

class LoginRequest(BaseModel):
    username: str
    password: str

class ChangePasswordRequest(BaseModel):
    username: str
    old_password: str
    new_password: str

class UpdateUserProfileRequest(BaseModel):
    full_name: str | None = None
    email: str | None = None
    telegram_username: str | None = None
    phone: str | None = None
    job_title: str | None = None
    bio: str | None = None
    avatar_data: str | None = None

DEFAULT_USERS = [
    {"full_name": "Adonis Lozada", "username": "a.lozada", "email": "a.lozada@sycom.com.ve", "telegram_username": "@adonislozada", "role": "coordinador"},
    {"full_name": "Administrador", "username": "admin", "email": "admin@sycom.com.ve", "telegram_username": "@adonislozada", "role": "coordinador"},
    {"full_name": "Juan Fernández", "username": "j.fernandez", "email": "j.fernandez@sycom.com.ve", "telegram_username": "@Juuan8", "role": "especialista"},
    {"full_name": "Yoxaide Jiménez", "username": "y.jimenez", "email": "y.jimenez@sycom.com.ve", "telegram_username": "@Yjimenezg", "role": "especialista"},
    {"full_name": "Luis Acosta", "username": "l.acosta", "email": "l.acosta@sycom.com.ve", "telegram_username": "@lapsy0", "role": "especialista"},
]

def init_users():
    db = SessionLocal()
    try:
        # Purgar cualquier usuario no perteneciente al equipo operativo
        db.query(User).filter(
            (User.username == "r.bravo") | 
            (User.full_name == "Richard Bravo") | 
            (User.email == "r.bravo@sycom.com.ve")
        ).delete()
        db.commit()

        default_pw_hash = hash_password("sycom2026")
        for u in DEFAULT_USERS:
            exists = db.query(User).filter(
                or_(
                    User.username == u["username"],
                    User.full_name == u["full_name"],
                    User.email == u["email"]
                )
            ).first()
            if not exists:
                db.add(User(**u, password_hash=default_pw_hash))
            else:
                exists.telegram_username = u["telegram_username"]
                exists.role = u["role"]
                exists.username = u["username"]
                exists.email = u["email"]
                if not exists.password_hash:
                    exists.password_hash = default_pw_hash
        db.commit()
    finally:
        db.close()

app = FastAPI(
    title="Agenda de Infra - Infraestructura y BD",
    description="API de consolidación y sincronización en tiempo real de GLPI y ClickUp",
    version="2.0.0"
)

# Permitir CORS para que el dashboard web pueda consumir la API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Planificador en segundo plano (cada 2 minutos según requerimiento)
scheduler = BackgroundScheduler()

@app.on_event("startup")
def on_startup():
    # Auto-migrar datos si es la primera vez que se inicia en PostgreSQL y existe dashboard.db
    try:
        try:
            from .migrate_sqlite_to_pg import auto_migrate_if_needed
        except ImportError:
            from migrate_sqlite_to_pg import auto_migrate_if_needed
        auto_migrate_if_needed()
    except Exception as mig_err:
        print(f"[Startup Migration Check] {mig_err}")

    # Inicializar usuarios del equipo
    init_users()
    # Purgar cerrados anteriores al día de ayer
    db = SessionLocal()
    try:
        purge_old_closed_items(db)
    finally:
        db.close()
    # Sincronización inicial al arrancar
    sync_all()
    # Programar sincronización periódica
    scheduler.add_job(sync_all, "interval", minutes=SYNC_INTERVAL_MINUTES, id="sync_job")
    scheduler.start()

@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown()

@app.get("/api/health")
def health_check():
    return {"status": "ok", "timestamp": datetime.datetime.utcnow().isoformat()}

@app.post("/api/sync")
def trigger_sync():
    """Permite forzar una sincronización inmediata desde el dashboard."""
    sync_all()
    return {"message": "Sincronización completada con éxito"}

def to_iso_z(dt: datetime.datetime | None) -> str | None:
    if not dt:
        return None
    s = dt.isoformat()
    return s if s.endswith("Z") or ("+" in s or "-" in s[10:]) else s + "Z"

def serialize_item(item: Item) -> dict:
    """Serializa una actividad con sus campos cronológicos puros (received_at, escalated_at, duración)."""
    rec_dt = item.received_at or item.created_at
    esc_dt = item.escalated_at or item.received_at or item.created_at
    
    is_recent_closed = False
    if item.status == "cerrado":
        ref_dt = item.closed_at or item.updated_at
        if ref_dt:
            diff_secs = (datetime.datetime.utcnow() - ref_dt).total_seconds()
            is_recent_closed = bool(0 <= diff_secs <= 24 * 3600)

    return {
        "id": item.id,
        "source": item.source,
        "external_id": item.external_id,
        "url": item.url,
        "title": item.title,
        "category": item.category,
        "client_name": item.client_name,
        "project_name": getattr(item, "project_name", item.client_name) or item.client_name,
        "client_type": item.client_type,
        "assignee_name": item.assignee_name,
        "priority": item.priority,
        "status": item.status,
        "status_origin": item.status_origin,
        "created_by": getattr(item, "created_by", None),
        "created_at": to_iso_z(item.created_at),
        "received_at": to_iso_z(rec_dt),
        "escalated_at": to_iso_z(esc_dt),
        "assigned_at": to_iso_z(item.assigned_at),
        "due_at": to_iso_z(item.due_at),
        "closed_at": to_iso_z(item.closed_at),
        "updated_at": to_iso_z(item.updated_at),
        "synced_at": to_iso_z(item.synced_at),
        "description": item.description,
        "escalation_id": item.escalation_id,
        "recommendations": item.recommendations,
        "duration_human": item.duration_human,
        "time_attention_label": item.time_attention_label,
        "is_recent_closed": is_recent_closed
    }

@app.get("/api/items")
def get_items(
    src: str = Query(None, description="GLPI o ClickUp"),
    tipo: str = Query(None, description="Interno o Externo"),
    resp: str = Query(None, description="Nombre del responsable"),
    prio: str = Query(None, description="Crítica, Alta, Media, Baja"),
    status: str = Query(None, description="pendiente, curso, espera, cerrado"),
    q: str = Query(None, description="Búsqueda por texto en título"),
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name")
):
    query = db.query(Item)

    if src and isinstance(src, str):
        query = query.filter(Item.source == src)
    if tipo and isinstance(tipo, str):
        query = query.filter(Item.client_type == tipo)
    if resp and isinstance(resp, str):
        resp_clean = decode_header_val(resp)
        query = query.filter(Item.assignee_name.ilike(f"%{resp_clean}%"))
    if prio and isinstance(prio, str):
        query = query.filter(Item.priority == prio)
    
    # Desacoplado: el Resumen conserva todo el histórico de actividades cerradas.
    # El filtro de 24 horas solo aplica visualmente a la columna 'Cerrados' del Kanban en frontend.
    if status and isinstance(status, str):
        query = query.filter(Item.status == status)

    if q and isinstance(q, str):
        query = query.filter(Item.title.ilike(f"%{q}%"))

    items = query.order_by(Item.updated_at.desc()).all()
    # Control de visibilidad por perfil (con decodificación URL y normalización de acentos):
    # El usuario regular solo ve las asignadas a él y las que están sin asignar (NO las de los demás)
    visible_items = [item for item in items if is_item_visible_to_user(item, x_user_role, x_user_name)]
    return [serialize_item(item) for item in visible_items]

@app.get("/api/items/{item_id}")
def get_item_by_id(
    item_id: str,
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user")
):
    """Obtiene una actividad individual por su ID de BD o ID externo (deep linking)."""
    item = None
    if item_id.isdigit():
        item = db.query(Item).filter(Item.id == int(item_id)).first()
    if not item:
        item = db.query(Item).filter(Item.external_id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail=f"Actividad #{item_id} no encontrada.")

    # Control de visibilidad por perfil
    role_raw = x_user_role if isinstance(x_user_role, str) else (role_param if isinstance(role_param, str) else "")
    user_name_raw = x_user_name if isinstance(x_user_name, str) else (user_param if isinstance(user_param, str) else "")
    if not is_item_visible_to_user(item, role_raw, user_name_raw):
        raise HTTPException(status_code=403, detail="No tienes permisos para visualizar esta actividad.")

    return serialize_item(item)


@app.get("/api/kpis")
def get_kpis(db: Session = Depends(get_db)):
    all_items = db.query(Item).all()
    now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day, 0, 0, 0)
    today_end = datetime.datetime(now.year, now.month, now.day, 23, 59, 59)
    seven_days_ago = now - datetime.timedelta(days=7)

    pendientes = sum(1 for i in all_items if i.status == "pendiente")
    curso = sum(1 for i in all_items if i.status == "curso")
    espera = sum(1 for i in all_items if i.status == "espera")
    cerrados = [i for i in all_items if i.status == "cerrado"]
    cerrados_7d = sum(1 for i in cerrados if i.closed_at and i.closed_at >= seven_days_ago)

    last_log = db.query(SyncLog).order_by(SyncLog.created_at.desc()).first()

    return {
        "total_actividades": len(all_items),
        "pendientes": pendientes,
        "en_curso": curso,
        "en_espera": espera,
        "cerrados": len(cerrados),
        "cerrados_7d": cerrados_7d,
        "total_activos": pendientes + curso + espera,
        "total_registrados": len(all_items),
        "last_sync": last_log.created_at.isoformat() if last_log else None,
        "last_sync_status": last_log.status if last_log else "N/A"
    }

@app.get("/api/team")
def get_team_workload(db: Session = Depends(get_db)):
    active_items = db.query(Item).filter(Item.status != "cerrado").all()
    workload = {}
    for item in active_items:
        resp = item.assignee_name or "Sin asignar"
        if resp not in workload:
            workload[resp] = {"name": resp, "total": 0, "criticos": 0, "en_curso": 0, "en_espera": 0, "pendientes": 0}
        workload[resp]["total"] += 1
        if item.priority == "Crítica":
            workload[resp]["criticos"] += 1
        if item.status == "curso":
            workload[resp]["en_curso"] += 1
        elif item.status == "espera":
            workload[resp]["en_espera"] += 1
        elif item.status == "pendiente":
            workload[resp]["pendientes"] += 1
    return list(workload.values())

@app.get("/api/users")
def get_users(db: Session = Depends(get_db)):
    users = db.query(User).filter(User.is_active == 1).all()
    return [{
        "id": u.id,
        "full_name": u.full_name,
        "username": u.username,
        "email": u.email,
        "telegram_username": u.telegram_username,
        "role": u.role
    } for u in users]

def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "full_name": user.full_name,
        "username": user.username,
        "email": user.email,
        "telegram_username": user.telegram_username,
        "role": user.role,
        "phone": getattr(user, "phone", None),
        "job_title": getattr(user, "job_title", None),
        "bio": getattr(user, "bio", None),
        "avatar_data": getattr(user, "avatar_data", None),
        "last_login": user.last_login.isoformat() if user.last_login else None
    }

@app.post("/api/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    u_str = req.username.strip()
    user = db.query(User).filter(
        or_(
            User.username.ilike(u_str),
            User.email.ilike(u_str),
            User.full_name.ilike(u_str)
        )
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado o no registrado.")
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Usuario inactivo en la plataforma.")
        
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Contraseña incorrecta.")
        
    user.last_login = datetime.datetime.utcnow()
    db.commit()
    
    return {
        "status": "ok",
        "user": _serialize_user(user)
    }

@app.get("/api/auth/users")
def get_auth_users(db: Session = Depends(get_db)):
    """Retorna los usuarios activos para el selector rápido del formulario de acceso."""
    users = db.query(User).filter(User.is_active == 1).all()
    return [_serialize_user(u) for u in users]

def find_user_by_identifier(
    db: Session,
    target_raw: str | None = None,
    username_param: str | None = None,
    x_user_username: str | None = None
) -> User | None:
    """Busca un usuario por username, email o nombre completo, con tolerancia a acentos y codificación URL."""
    # 1. Búsqueda prioritaria por username explícito (parámetro query o header)
    u_candidate = username_param or x_user_username
    if u_candidate and str(u_candidate).strip():
        u_clean = decode_header_val(u_candidate).strip()
        user = db.query(User).filter(User.username.ilike(u_clean)).first()
        if user:
            return user
        u_norm = strip_accents_str(u_clean)
        for u in db.query(User).all():
            if strip_accents_str(u.username) == u_norm:
                return u

    if not target_raw:
        return None
    target = decode_header_val(target_raw).strip()
    if not target:
        return None

    # 2. Búsqueda directa SQL
    user = db.query(User).filter(
        or_(
            User.username.ilike(target),
            User.email.ilike(target),
            User.full_name.ilike(target)
        )
    ).first()
    if user:
        return user

    # 3. Búsqueda en memoria con normalización de acentos (para SQLite y variaciones)
    target_norm = strip_accents_str(target)
    all_users = db.query(User).all()
    for u in all_users:
        if strip_accents_str(u.username) == target_norm:
            return u
        if strip_accents_str(u.full_name) == target_norm:
            return u
        if u.email and strip_accents_str(u.email) == target_norm:
            return u

    # 4. Coincidencia parcial si target_norm contiene o está contenido en username o full_name
    for u in all_users:
        u_norm = strip_accents_str(u.username)
        fn_norm = strip_accents_str(u.full_name)
        if (target_norm and target_norm in u_norm) or (fn_norm and target_norm and (target_norm in fn_norm or fn_norm in target_norm)):
            return u

    return None


@app.get("/api/users/me")
def get_current_user_profile(
    db: Session = Depends(get_db),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    x_user_username: str | None = Header(None, alias="X-User-Username"),
    username: str | None = Query(None)
):
    """Consulta el perfil completo del usuario autenticado."""
    user = find_user_by_identifier(db, target_raw=x_user_name, username_param=username, x_user_username=x_user_username)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    return _serialize_user(user)


@app.put("/api/users/me")
def update_current_user_profile(
    req: UpdateUserProfileRequest,
    db: Session = Depends(get_db),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    x_user_username: str | None = Header(None, alias="X-User-Username"),
    username: str | None = Query(None)
):
    """Actualiza los datos personales y foto de perfil del usuario autenticado."""
    user = find_user_by_identifier(db, target_raw=x_user_name, username_param=username, x_user_username=x_user_username)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")

    if req.full_name is not None and req.full_name.strip():
        user.full_name = req.full_name.strip()
    if req.email is not None and req.email.strip():
        user.email = req.email.strip()
    if req.telegram_username is not None:
        user.telegram_username = req.telegram_username.strip()
    if req.phone is not None:
        user.phone = req.phone.strip()
    if req.job_title is not None:
        user.job_title = req.job_title.strip()
    if req.bio is not None:
        user.bio = req.bio.strip()
    if req.avatar_data is not None:
        user.avatar_data = req.avatar_data

    db.commit()
    db.refresh(user)
    return {
        "status": "ok",
        "message": "Datos personales y perfil actualizados con éxito",
        "user": _serialize_user(user)
    }


@app.post("/api/auth/change-password")
def change_password(req: ChangePasswordRequest, db: Session = Depends(get_db)):
    u_str = req.username.strip()
    user = find_user_by_identifier(db, target_raw=u_str, username_param=u_str, x_user_username=u_str)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if not verify_password(req.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="La contraseña actual es incorrecta.")
    
    new_pw = req.new_password
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 6 caracteres.")
    if len(new_pw) > 15:
        raise HTTPException(status_code=400, detail="La nueva contraseña no debe exceder los 15 caracteres.")
    
    special_chars = set("$&.*@#%!_?+-=()[]{}<>,:;|/\\^~`")
    if not any(c in special_chars for c in new_pw):
        raise HTTPException(status_code=400, detail="La nueva contraseña debe contener al menos un caracter especial ($&.*).")
    
    user.password_hash = hash_password(new_pw)
    db.commit()
    return {"status": "ok", "message": "Contraseña actualizada con éxito"}

@app.get("/api/clients")
def get_clients_summary(db: Session = Depends(get_db)):
    items = db.query(Item).all()
    clients = {}
    for item in items:
        c_name = item.client_name or "Sin cliente"
        if c_name not in clients:
            clients[c_name] = {
                "name": c_name,
                "type": item.client_type,
                "total": 0,
                "abiertos": 0,
                "en_curso": 0,
                "en_espera": 0,
                "pendientes": 0,
                "criticos": 0,
                "cerrados": 0
            }
        clients[c_name]["total"] += 1
        if item.status != "cerrado":
            clients[c_name]["abiertos"] += 1
            if item.status == "curso":
                clients[c_name]["en_curso"] += 1
            elif item.status == "espera":
                clients[c_name]["en_espera"] += 1
            elif item.status == "pendiente":
                clients[c_name]["pendientes"] += 1
            if item.priority == "Crítica":
                clients[c_name]["criticos"] += 1
        else:
            clients[c_name]["cerrados"] += 1

    return list(clients.values())

@app.post("/api/items/{item_id}/assign")
def assign_item(item_id: int, req: AssignRequest, db: Session = Depends(get_db), _role: str = Depends(verify_coordinator_role)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item no encontrado")

    # Obtener lista de especialistas solicitados
    keys = []
    if req.specialist_keys:
        keys = [k.strip().lower() for k in req.specialist_keys if k and k.strip()]
    elif req.specialist_key:
        keys = [req.specialist_key.strip().lower()]

    if not keys:
        raise HTTPException(status_code=400, detail="Debe indicar al menos un especialista.")

    matched_list = []
    seen = set()
    for spec_key in keys:
        matched = None
        for k, v in SPECIALISTS_MAP.items():
            if k == spec_key or k in spec_key or v["name"].lower() in spec_key:
                matched = v
                break
        if matched and matched["key"] not in seen:
            seen.add(matched["key"])
            matched_list.append(matched)

    if not matched_list:
        raise HTTPException(status_code=400, detail="Ninguno de los especialistas indicados fue reconocido.")

    success = False
    sync_warning = None
    source = item.source
    ext_id = item.external_id

    if source == "GLPI":
        client = GLPIClient()
        glpi_ids = [m["glpi_id"] for m in matched_list if m.get("glpi_id")]
        success = client.assign_ticket(ext_id, glpi_ids)
        if not success:
            sync_warning = "Asignado en Agenda de Infra. Aviso GLPI: No se pudo sincronizar el técnico directamente en GLPI."
    elif source == "ClickUp":
        client = ClickUpClient()
        clickup_ids = [m["clickup_id"] for m in matched_list if m.get("clickup_id")]
        if clickup_ids:
            success = client.assign_task(ext_id, clickup_ids)
            if not success:
                sync_warning = (
                    "Asignado en Agenda de Infra y notificado por Telegram. "
                    "Aviso ClickUp: El espacio o lista en ClickUp es privado; para que ClickUp también lo refleje en su web, "
                    "el administrador debe añadir al especialista como miembro del espacio en ClickUp."
                )
        else:
            success = True

    # Actualizar estado y responsable de forma garantizada en la base de datos operativa
    combined_names = ", ".join(m["name"] for m in matched_list)
    now_utc = datetime.datetime.utcnow()
    item.assignee_name = combined_names
    item.assigned_at = now_utc
    if item.status == "pendiente":
        item.status = "curso"
        item.status_origin = "in progress" if source == "ClickUp" else "En curso (asignado)"
    item.updated_at = now_utc
    item.synced_at = now_utc
    db.commit()
    db.refresh(item)

    # Disparar alerta inmediata a Telegram
    try:
        import html
        from .notifications.telegram_notifier import send_telegram_message
        mentions = ", ".join(f"{html.escape(m['name'])} ({m['telegram']})" for m in matched_list)
        plural_label = "Responsables" if len(matched_list) > 1 else "Responsable"
        icon = "👥" if len(matched_list) > 1 else "👤"
        now_str = now_caracas_str('%d-%m-%Y %I:%M %p')
        safe_title = html.escape(item.title or "")
        safe_source = html.escape(item.source or "")
        agenda_url = get_agenda_deep_link(item.id, view="kanban")
        msg = (
            f"{icon} <b>Asignación en vivo</b>\n\n"
            f"📌 <b>Asunto:</b> {safe_title}\n"
            f"🏷 <b>Origen:</b> {safe_source} #{item.external_id}\n"
            f"🎯 <b>{plural_label}:</b> {mentions}\n"
            f"🕒 <b>Incorporado a la actividad:</b> {now_str}\n\n"
            f"🔗 <a href=\"{agenda_url}\"><b>Abrir Agenda</b></a>\n\n"
            f"⚡ <i>Asignado desde la Agenda de Infra</i>"
        )
        send_telegram_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"[Telegram Assign Alert Error] {e}")

    return {
        "status": "OK",
        "message": f"Asignado exitosamente a {combined_names}",
        "sync_warning": sync_warning,
        "item": {
            "id": item.id,
            "external_id": item.external_id,
            "source": item.source,
            "title": item.title,
            "assignee_name": item.assignee_name,
            "assigned_at": item.assigned_at.isoformat() if item.assigned_at else None,
            "status": item.status,
            "status_origin": item.status_origin
        }
    }

@app.post("/api/items/{item_id}/status")
def update_item_status(
    item_id: int, 
    req: StatusUpdateRequest, 
    db: Session = Depends(get_db), 
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name")
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item no encontrado")

    role = decode_header_val(x_user_role).lower()
    if role in ("coordinador", "admin", "administrador"):
        pass  # Coordinador / Administrador tiene autorización total sobre cualquier actividad
    elif role == "especialista":
        # Especialista solo puede cambiar de estado sus propias actividades asignadas
        if not is_item_assigned_to_user_name(item.assignee_name, x_user_name):
            raise HTTPException(
                status_code=403,
                detail="Permiso denegado: Como Especialista, solo puedes actualizar el estado de tus propias actividades asignadas."
            )
    else:
        raise HTTPException(
            status_code=403,
            detail="Permiso denegado: El rol Analista tiene permisos exclusivos de consulta y análisis."
        )

    new_st = req.status.lower().strip()
    if new_st not in ["pendiente", "curso", "espera", "cerrado"]:
        raise HTTPException(status_code=400, detail=f"Estado '{req.status}' no válido. Use: pendiente, curso, espera, cerrado")

    source = item.source
    ext_id = item.external_id

    success = False

    if source == "GLPI":
        client = GLPIClient()
        success = client.update_ticket_status(ext_id, new_st)
    elif source == "ClickUp":
        client = ClickUpClient()
        success = client.update_task_status(ext_id, new_st)

    if not success:
        raise HTTPException(status_code=500, detail=f"No se pudo sincronizar el estado en {source}")

    # Actualizar estado en la base de datos operativa
    now_utc = datetime.datetime.utcnow()
    item.status = new_st
    item.updated_at = now_utc
    item.synced_at = now_utc

    if req.escalation_id is not None:
        item.escalation_id = req.escalation_id if req.escalation_id > 0 else None
        if item.escalation_id:
            linked_esc_status = db.query(EscalationDocument).filter(EscalationDocument.id == item.escalation_id).first()
            if linked_esc_status:
                esc_rec_dt = linked_esc_status.received_at or linked_esc_status.created_at
                esc_esc_dt = linked_esc_status.escalated_at or esc_rec_dt
                if esc_rec_dt:
                    item.received_at = esc_rec_dt
                if esc_esc_dt:
                    item.escalated_at = esc_esc_dt

    if new_st == "cerrado":
        item.closed_at = now_utc
        item.status_origin = "5 - Resuelto" if source == "GLPI" else "complete"

        # Registrar recomendaciones y anexarlas al escalamiento vinculado si aplica
        rec_clean = (req.recommendations or "").strip()
        if not rec_clean and item.recommendations:
            rec_clean = item.recommendations.strip()

        if rec_clean:
            item.recommendations = rec_clean

            target_esc_id = item.escalation_id
            if target_esc_id:
                esc = db.query(EscalationDocument).filter(EscalationDocument.id == target_esc_id).first()
                if esc:
                    esc_time_str = now_caracas_str('%d-%m-%Y %I:%M %p')
                    entry = f"• [{esc_time_str}] {item.source} #{item.external_id} - {item.title}:\n{rec_clean}"
                    if esc.recommendations and esc.recommendations.strip():
                        item_ref = f"{item.source} #{item.external_id}"
                        if rec_clean not in esc.recommendations or item_ref not in esc.recommendations:
                            esc.recommendations = esc.recommendations.strip() + "\n\n" + entry
                    else:
                        esc.recommendations = entry
    elif new_st == "curso":
        item.closed_at = None
        item.status_origin = "2 - En curso (asignado)" if source == "GLPI" else "in progress"
    elif new_st == "espera":
        item.closed_at = None
        item.status_origin = "4 - En espera" if source == "GLPI" else "en espera"
    elif new_st == "pendiente":
        item.closed_at = None
        item.status_origin = "1 - Nuevo" if source == "GLPI" else "to do"

    db.commit()
    db.refresh(item)

    # Disparar alerta en Telegram
    try:
        import html
        from .notifications.telegram_notifier import send_telegram_message
        status_labels = {
            "pendiente": "Pendiente",
            "curso": "En Curso",
            "espera": "En Espera",
            "cerrado": "Cerrado / Resuelto"
        }
        safe_title = html.escape(item.title or "")
        safe_source = html.escape(item.source or "")
        safe_status_orig = html.escape(item.status_origin or "")
        safe_assignee = html.escape(item.assignee_name or "Sin asignar")
        now_str = now_caracas_str('%d-%m-%Y %I:%M %p')
        user_info = f" por {html.escape(x_user_name)}" if x_user_name else ""
        rec_info = f"\n💡 <b>Recomendaciones:</b>\n{html.escape(req.recommendations.strip())}\n" if (new_st == "cerrado" and req.recommendations and req.recommendations.strip()) else ""
        agenda_url = get_agenda_deep_link(item.id, view="kanban")
        msg = (
            f"📋 <b>Cambio de estado en Tablero Kanban</b>\n\n"
            f"📌 <b>Asunto:</b> {safe_title}\n"
            f"🏷 <b>Origen:</b> {safe_source} #{item.external_id}\n"
            f"📊 <b>Nuevo estado:</b> {status_labels.get(new_st, new_st)} ({safe_status_orig})\n"
            f"👤 <b>Responsable:</b> {safe_assignee}\n"
            f"🕒 <b>Hora:</b> {now_str}\n"
            f"{rec_info}\n"
            f"🔗 <a href=\"{agenda_url}\"><b>Abrir Agenda</b></a>\n\n"
            f"⚡ <i>Actualizado{user_info} desde la Agenda</i>"
        )
        send_telegram_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"[Telegram Status Alert Error] {e}")

    return {
        "status": "OK",
        "message": f"Estado cambiado a {new_st}",
        "item": {
            "id": item.id,
            "external_id": item.external_id,
            "source": item.source,
            "title": item.title,
            "assignee_name": item.assignee_name,
            "status": item.status,
            "status_origin": item.status_origin,
            "escalation_id": item.escalation_id,
            "recommendations": item.recommendations,
            "closed_at": item.closed_at.isoformat() if item.closed_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None
        }
    }

@app.post("/api/items")
def create_item(
    req: CreateItemRequest,
    db: Session = Depends(get_db),
    _role: str = Depends(verify_coordinator_or_specialist_role),
    x_user_name: str | None = Header(None, alias="X-User-Name")
):
    creator_clean = decode_header_val(x_user_name)
    if not creator_clean:
        creator_clean = "Coordinador" if _role in ("coordinador", "admin", "administrador") else "Especialista"

    src = req.source.strip()
    if src.lower() in ["clickup", "click_up"]:
        source = "ClickUp"
    elif src.lower() in ["glpi"]:
        source = "GLPI"
    else:
        raise HTTPException(status_code=400, detail="Plataforma de destino no válida. Debe ser 'ClickUp' o 'GLPI'.")

    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="El título o asunto de la actividad es obligatorio.")

    priority = req.priority.strip().capitalize()
    if priority not in ["Baja", "Media", "Alta", "Crítica"]:
        priority = "Media"

    # Procesar fecha de recibimiento en Sycom y fecha de escalada al equipo
    now_utc = datetime.datetime.utcnow()
    linked_esc = None
    if req.escalation_id and req.escalation_id > 0:
        linked_esc = db.query(EscalationDocument).filter(EscalationDocument.id == req.escalation_id).first()

    received_dt = parse_iso_utc(req.received_at)
    if not received_dt and linked_esc:
        received_dt = linked_esc.received_at or linked_esc.created_at
    if not received_dt:
        received_dt = now_utc

    escalated_dt = parse_iso_utc(req.escalated_at)
    if not escalated_dt and linked_esc:
        escalated_dt = linked_esc.escalated_at or linked_esc.received_at or linked_esc.created_at
    if not escalated_dt:
        escalated_dt = received_dt

    due_dt = parse_iso_utc(req.due_at)

    # Identificar especialistas solicitados
    matched_list = []
    seen = set()
    if req.specialist_keys:
        for spec_key in req.specialist_keys:
            if not spec_key:
                continue
            k_clean = spec_key.strip().lower()
            matched = None
            for k, v in SPECIALISTS_MAP.items():
                if k == k_clean or k in k_clean or v["name"].lower() in k_clean:
                    matched = v
                    break
            if matched and matched["key"] not in seen:
                seen.add(matched["key"])
                matched_list.append(matched)

    combined_names = ", ".join(m["name"] for m in matched_list) if matched_list else "Sin asignar"

    # Crear en plataforma nativa
    external_id = None
    url = None
    category = "Proyectos y Tareas" if source == "ClickUp" else "Tickets TI"
    subproject = (req.project_name or req.client_name or "").strip()
    if source == "ClickUp":
        client_name = subproject or "SIMF"
    else:
        client_name = subproject or "Sycom"
    project_name = client_name
    status_origin = "to do" if source == "ClickUp" else "1 - Nuevo"

    if source == "ClickUp":
        client = ClickUpClient()
        clickup_ids = [m["clickup_id"] for m in matched_list if m.get("clickup_id")]
        task_data = client.create_task(
            title=title,
            description=req.description or "",
            priority=priority,
            due_at=due_dt,
            user_ids=clickup_ids
        )
        if not task_data or not task_data.get("id"):
            raise HTTPException(status_code=500, detail="Error creando la tarea en la API de ClickUp")
        external_id = str(task_data["id"])
        url = task_data.get("url") or f"https://app.clickup.com/t/{external_id}"
    elif source == "GLPI":
        client = GLPIClient()
        glpi_ids = [m["glpi_id"] for m in matched_list if m.get("glpi_id")]
        ticket_data = client.create_ticket(
            title=title,
            description=req.description or "",
            priority=priority,
            due_at=due_dt,
            user_ids=glpi_ids,
            client_name=client_name
        )
        if not ticket_data or not ticket_data.get("id"):
            raise HTTPException(status_code=500, detail="Error creando el ticket en la API de GLPI")
        external_id = str(ticket_data["id"])
        url = ticket_data.get("url")

    # Guardar en base de datos local
    item = Item(
        source=source,
        external_id=external_id,
        url=url,
        title=title,
        description=req.description,
        created_by=creator_clean,
        category=category,
        client_name=client_name,
        project_name=project_name,
        client_type="Interno",
        assignee_name=combined_names,
        priority=priority,
        status="pendiente",
        status_origin=status_origin,
        created_at=now_utc,
        received_at=received_dt,
        escalated_at=escalated_dt,
        assigned_at=now_utc if matched_list else None,
        due_at=due_dt,
        updated_at=now_utc,
        synced_at=now_utc,
        escalation_id=req.escalation_id if (req.escalation_id and req.escalation_id > 0) else None
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    # Disparar notificación a Telegram
    try:
        import html
        from .notifications.telegram_notifier import send_telegram_message
        mentions = ", ".join(f"{html.escape(m['name'])} ({m['telegram']})" for m in matched_list) if matched_list else "Sin asignar (Atención: @adonislozada)"
        plural_label = "Responsables" if len(matched_list) > 1 else "Responsable"
        now_str = now_caracas_str('%d-%m-%Y %I:%M %p')
        safe_title = html.escape(title)
        safe_source = html.escape(source)
        safe_desc = f"\n📝 <b>Descripción:</b>\n{html.escape(req.description.strip())}\n" if req.description and req.description.strip() else ""
        prio_icon = "🔥" if priority == "Crítica" else ("⚡" if priority == "Alta" else "📌")
        agenda_url = get_agenda_deep_link(item.id, view="kanban")
        link_html = f"\n🔗 <a href=\"{agenda_url}\"><b>Abrir Agenda</b></a>"
        rec_str = format_caracas_time(received_dt, '%d-%m-%Y %I:%M %p')
        esc_str = format_caracas_time(escalated_dt, '%d-%m-%Y %I:%M %p')
        msg = (
            f"✨ <b>Nueva Actividad Creada</b>\n\n"
            f"📌 <b>Asunto:</b> {safe_title}\n"
            f"🏷 <b>Origen:</b> {safe_source} #{external_id}\n"
            f"{prio_icon} <b>Prioridad:</b> {priority}\n"
            f"🎯 <b>{plural_label}:</b> {mentions}\n"
            f"📥 <b>Recibido en Sycom:</b> {rec_str}\n"
            f"🚀 <b>Escalado a Infra & BD:</b> {esc_str}\n"
            f"🕒 <b>Hora registro:</b> {now_str}\n"
            f"{safe_desc}"
            f"{link_html}"
        )
        sent_ok = send_telegram_message(msg, parse_mode="HTML")
        if sent_ok:
            try:
                db.add(AlertLog(item_id=item.id, semaphore="nuevo"))
                db.commit()
            except Exception:
                db.rollback()
            print(f"[CreateItem] Alerta Telegram enviada con éxito para #{external_id}")
        else:
            print(f"[CreateItem] AVISO: Alerta Telegram no pudo entregarse para #{external_id}")
    except Exception as e:
        print(f"[Telegram Create Notification Error] {e}")

    return {
        "status": "OK",
        "message": "Actividad creada con éxito",
        "item": serialize_item(item)
    }


@app.patch("/api/items/{item_id}")
@app.put("/api/items/{item_id}")
def update_item_details(
    item_id: int,
    req: UpdateItemRequest,
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user")
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Actividad no encontrada")

    role = decode_header_val(x_user_role if isinstance(x_user_role, str) else (role_param if isinstance(role_param, str) else "")).lower()
    user_name_raw = (x_user_name if isinstance(x_user_name, str) else (user_param if isinstance(user_param, str) else "")) or ""

    recalc_due = False
    if role in ("coordinador", "admin", "administrador"):
        if req.title is not None and req.title.strip():
            item.title = req.title.strip()
        if req.priority is not None and req.priority.strip():
            item.priority = req.priority.strip()
            recalc_due = True
        if req.description is not None:
            item.description = req.description.strip()
        if req.project_name is not None and req.project_name.strip():
            item.client_name = req.project_name.strip()
            item.project_name = req.project_name.strip()
        elif req.client_name is not None and req.client_name.strip():
            item.client_name = req.client_name.strip()
            item.project_name = req.client_name.strip()
        if req.received_at is not None:
            parsed_rec = parse_iso_utc(req.received_at)
            if parsed_rec:
                item.received_at = parsed_rec
                recalc_due = True
        if req.escalated_at is not None:
            parsed_esc = parse_iso_utc(req.escalated_at)
            if parsed_esc:
                item.escalated_at = parsed_esc
    elif role == "especialista":
        if not is_item_assigned_to_user_name(item.assignee_name, user_name_raw):
            raise HTTPException(
                status_code=403,
                detail="Permiso denegado: Como Especialista, solo puedes actualizar actividades asignadas a ti."
            )
        if req.title is not None or req.priority is not None or req.description is not None or req.project_name is not None or req.client_name is not None or req.received_at is not None or req.escalated_at is not None:
            raise HTTPException(
                status_code=403,
                detail="Permiso denegado: Como Especialista, solo puedes vincular escalamientos o registrar recomendaciones."
            )
    else:
        raise HTTPException(
            status_code=403,
            detail="Permiso denegado: Se requiere rol de Coordinador o Especialista asignado."
        )

    if req.escalation_id is not None:
        item.escalation_id = req.escalation_id if req.escalation_id > 0 else None
        if item.escalation_id:
            linked_esc_status = db.query(EscalationDocument).filter(EscalationDocument.id == item.escalation_id).first()
            if linked_esc_status:
                esc_rec_dt = linked_esc_status.received_at or linked_esc_status.created_at
                esc_esc_dt = linked_esc_status.escalated_at or esc_rec_dt
                if esc_rec_dt and req.received_at is None:
                    item.received_at = esc_rec_dt
                    recalc_due = True
                if esc_esc_dt and req.escalated_at is None:
                    item.escalated_at = esc_esc_dt

    if req.recommendations is not None:
        rec_clean = req.recommendations.strip()
        item.recommendations = rec_clean or None

    # Si la actividad tiene recomendaciones y está vinculada a un escalamiento, sincronizarlas al expediente
    rec_to_sync = (item.recommendations or "").strip()
    if item.escalation_id and rec_to_sync:
        esc = db.query(EscalationDocument).filter(EscalationDocument.id == item.escalation_id).first()
        if esc:
            now_str = now_caracas_str('%d-%m-%Y %I:%M %p')
            entry = f"• [{now_str}] {item.source} #{item.external_id} - {item.title}:\n{rec_to_sync}"
            if esc.recommendations and esc.recommendations.strip():
                item_ref = f"{item.source} #{item.external_id}"
                if rec_to_sync not in esc.recommendations or item_ref not in esc.recommendations:
                    esc.recommendations = esc.recommendations.strip() + "\n\n" + entry
            else:
                esc.recommendations = entry

    item.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(item)
    return {
        "status": "OK",
        "message": "Actividad actualizada con éxito",
        "item": serialize_item(item)
    }


@app.delete("/api/items/{item_id}")
def delete_item(
    item_id: int,
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user")
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Actividad no encontrada")

    role = decode_header_val(x_user_role if isinstance(x_user_role, str) else (role_param if isinstance(role_param, str) else "")).lower()
    user_name_raw = (x_user_name if isinstance(x_user_name, str) else (user_param if isinstance(user_param, str) else "")) or ""

    is_coord = role in ("coordinador", "admin", "administrador")
    is_spec = role in ("especialista", "specialist")

    if not is_coord:
        if not is_spec:
            raise HTTPException(
                status_code=403,
                detail="Acceso restringido: Solo el rol Coordinador o el Especialista que creó la actividad tienen permisos para eliminarla."
            )
        if not is_user_creator_of_item(item.created_by, user_name_raw):
            raise HTTPException(
                status_code=403,
                detail="Permiso denegado: Como Especialista, solo puedes eliminar actividades creadas por ti."
            )

    source = item.source
    ext_id = item.external_id
    title = item.title

    # Intentar eliminar de la plataforma de origen
    if source == "ClickUp":
        client = ClickUpClient()
        client.delete_task(ext_id)
    elif source == "GLPI":
        client = GLPIClient()
        client.delete_ticket(ext_id)

    # Eliminar de la base de datos operativa
    db.delete(item)
    db.commit()

    # Notificar a Telegram
    try:
        import html
        from .notifications.telegram_notifier import send_telegram_message
        now_str = now_caracas_str('%d-%m-%Y %I:%M %p')
        deleter_name = decode_header_val(user_name_raw) or ("Coordinador" if is_coord else "Especialista")
        msg = (
            f"🗑 <b>Actividad Eliminada</b>\n\n"
            f"📌 <b>Asunto:</b> {html.escape(title)}\n"
            f"🏷 <b>Origen:</b> {html.escape(source)} #{ext_id}\n"
            f"👤 <b>Eliminada por:</b> {html.escape(deleter_name)}\n"
            f"🕒 <b>Hora eliminación:</b> {now_str}\n"
            f"⚡ <i>Eliminada desde la Agenda de Infraestructura</i>"
        )
        send_telegram_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"[Telegram Delete Alert Error] {e}")

    return {
        "status": "OK",
        "message": f"Actividad {source} #{ext_id} eliminada con éxito",
        "deleted_id": item_id
    }


# ==============================================================================
# DIAGNÓSTICO NOTIFICACIONES TELEGRAM
# ==============================================================================

@app.get("/api/notifications/telegram-status")
def api_telegram_status():
    """Verifica si las credenciales del bot de Telegram están cargadas en el entorno."""
    from . import config
    token = (config.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (config.TELEGRAM_CHAT_ID or "").strip()
    masked_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else ("Configurado" if token else "NO CONFIGURADO")
    masked_chat = f"{chat_id[:3]}...{chat_id[-2:]}" if len(chat_id) > 5 else ("Configurado" if chat_id else "NO CONFIGURADO")
    return {
        "bot_configured": bool(token),
        "chat_configured": bool(chat_id),
        "bot_token_preview": masked_token,
        "chat_id_preview": masked_chat,
        "status": "READY" if (token and chat_id) else "MISSING_ENV"
    }


@app.post("/api/notifications/test-telegram")
def api_test_telegram(_role: str = Depends(verify_coordinator_role)):
    """Envía un mensaje de prueba al chat de Telegram configurado."""
    res = test_telegram_connection()
    return res


# ==============================================================================
# REPORTES EJECUTIVOS (M9) - EXCEL & PDF
# ==============================================================================

def _resolve_report_filters(start_date, end_date, specialist, x_user_role, x_user_name, role_param, user_param, ibp=None, category=None):
    role = (x_user_role if isinstance(x_user_role, str) else (role_param if isinstance(role_param, str) else "")).strip().lower()

    # Restricción estricta: Solo el rol Coordinador / Administrador puede generar reportes
    if role not in ("coordinador", "admin", "administrador"):
        raise HTTPException(
            status_code=403,
            detail="Acceso restringido: La generación de reportes ejecutivos está reservada exclusivamente para el rol Coordinador."
        )

    spec = specialist.strip() if isinstance(specialist, str) and specialist.strip() else None
    s_date = start_date.strip() if isinstance(start_date, str) and start_date.strip() else None
    e_date = end_date.strip() if isinstance(end_date, str) and end_date.strip() else None
    ibp_val = ibp.strip() if isinstance(ibp, str) and ibp.strip() else None
    cat_val = category.strip() if isinstance(category, str) and category.strip() else None
    return s_date, e_date, spec, ibp_val, cat_val


@app.get("/api/reports/data")
def api_report_data(
    start_date: str = Query(None, description="Fecha inicio YYYY-MM-DD"),
    end_date: str = Query(None, description="Fecha fin YYYY-MM-DD"),
    specialist: str = Query(None, description="Filtrar por especialista"),
    ibp: str = Query(None, description="Filtrar por IBP / Banco Participante"),
    category: str = Query(None, description="Filtrar por Categoría Técnica"),
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user"),
):
    """Retorna los datos consolidados para reportes ejecutivos en formato JSON."""
    s_date, e_date, spec, ibp_val, cat_val = _resolve_report_filters(
        start_date, end_date, specialist, x_user_role, x_user_name, role_param, user_param, ibp, category
    )
    data = get_report_data(
        db=db,
        start_date=s_date,
        end_date=e_date,
        specialist_filter=spec,
        ibp_filter=ibp_val,
        category_filter=cat_val,
    )
    return data


@app.get("/api/reports/excel")
def api_report_excel(
    start_date: str = Query(None, description="Fecha inicio YYYY-MM-DD"),
    end_date: str = Query(None, description="Fecha fin YYYY-MM-DD"),
    specialist: str = Query(None, description="Filtrar por especialista"),
    ibp: str = Query(None, description="Filtrar por IBP / Banco Participante"),
    category: str = Query(None, description="Filtrar por Categoría Técnica"),
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user"),
):
    """Genera y descarga el archivo Excel (.xlsx) corporativo con KPIs y métricas."""
    s_date, e_date, spec, ibp_val, cat_val = _resolve_report_filters(
        start_date, end_date, specialist, x_user_role, x_user_name, role_param, user_param, ibp, category
    )
    data = get_report_data(
        db=db,
        start_date=s_date,
        end_date=e_date,
        specialist_filter=spec,
        ibp_filter=ibp_val,
        category_filter=cat_val,
    )

    excel_buffer = generate_excel_report(data)
    now_tag = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"Informe_Ejecutivo_Sycom_{now_tag}.xlsx"

    return Response(
        content=excel_buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )


@app.get("/api/reports/print-view", response_class=HTMLResponse)
def api_report_print_view(
    start_date: str = Query(None, description="Fecha inicio YYYY-MM-DD"),
    end_date: str = Query(None, description="Fecha fin YYYY-MM-DD"),
    specialist: str = Query(None, description="Filtrar por especialista"),
    ibp: str = Query(None, description="Filtrar por IBP / Banco Participante"),
    category: str = Query(None, description="Filtrar por Categoría Técnica"),
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user"),
):
    """Genera la vista ejecutiva HTML lista para imprimir o guardar como PDF en alta resolución."""
    s_date, e_date, spec, ibp_val, cat_val = _resolve_report_filters(
        start_date, end_date, specialist, x_user_role, x_user_name, role_param, user_param, ibp, category
    )
    data = get_report_data(
        db=db,
        start_date=s_date,
        end_date=e_date,
        specialist_filter=spec,
        ibp_filter=ibp_val,
        category_filter=cat_val,
    )

    html_content = generate_executive_html(data)
    return HTMLResponse(content=html_content)


class SaveReportRequest(BaseModel):
    title: str | None = None
    period_label: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    specialist: str | None = None
    ibp: str | None = None
    category: str | None = None


@app.get("/api/reports/history")
def api_reports_history(
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    role_param: str = Query(None, alias="role")
):
    """Lista el historial de reportes ejecutivos generados (solo Coordinador)."""
    role = (x_user_role if isinstance(x_user_role, str) else (role_param if isinstance(role_param, str) else "")).strip().lower()
    if role not in ("coordinador", "admin", "administrador"):
        raise HTTPException(
            status_code=403,
            detail="Acceso restringido: Solo el Coordinador puede consultar el historial de reportes."
        )

    reports = db.query(GeneratedReport).order_by(GeneratedReport.created_at.desc()).all()

    # Si aún no hay reportes generados, auto-generar el del mes en curso inicial
    if not reports:
        now_dt = datetime.datetime.now(CARACAS_TZ)
        start_str = f"{now_dt.year}-{now_dt.month:02d}-01"
        end_str = f"{now_dt.year}-{now_dt.month:02d}-{now_dt.day:02d}"
        init_data = get_report_data(db, start_date=start_str, end_date=end_str)
        k = init_data.get("kpis", {})
        init_rep = GeneratedReport(
            title=f"Informe Ejecutivo Mensual - {now_dt.strftime('%B %Y').capitalize()}",
            period_label=init_data.get("period_label", "Mes en curso"),
            start_date=start_str,
            end_date=end_str,
            specialist_filter="Todo el equipo",
            generated_by="Adonis Lozada Figueroa",
            total_items=k.get("total_items", 0),
            total_closed=k.get("total_cerrados", 0),
            avg_resolution_time=k.get("avg_resolution_str", "0m"),
            created_at=datetime.datetime.utcnow()
        )
        db.add(init_rep)
        db.commit()
        db.refresh(init_rep)
        reports = [init_rep]

    result = []
    for r in reports:
        result.append({
            "id": r.id,
            "title": r.title,
            "period_label": r.period_label,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "specialist_filter": r.specialist_filter,
            "generated_by": r.generated_by,
            "total_items": r.total_items,
            "total_closed": r.total_closed,
            "avg_resolution_time": r.avg_resolution_time,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "created_at_caracas": format_caracas_time(r.created_at, "%d-%m-%Y %I:%M %p") if r.created_at else "—"
        })
    return result


@app.post("/api/reports/generate-and-save")
def api_reports_generate_and_save(
    req: SaveReportRequest,
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user")
):
    """Genera métricas de un reporte ejecutivo y lo registra en el historial persistente."""
    role = (x_user_role if isinstance(x_user_role, str) else (role_param if isinstance(role_param, str) else "")).strip().lower()
    if role not in ("coordinador", "admin", "administrador"):
        raise HTTPException(
            status_code=403,
            detail="Acceso restringido: Solo el Coordinador puede generar reportes ejecutivos."
        )

    user_name = (x_user_name if isinstance(x_user_name, str) else (user_param if isinstance(user_param, str) else "")).strip() or "Adonis Lozada Figueroa"

    s_date, e_date, spec, ibp_val, cat_val = _resolve_report_filters(
        req.start_date, req.end_date, req.specialist, x_user_role, x_user_name, role_param, user_param, req.ibp, req.category
    )

    data = get_report_data(
        db=db,
        start_date=s_date,
        end_date=e_date,
        specialist_filter=spec,
        ibp_filter=ibp_val,
        category_filter=cat_val,
    )
    k = data.get("kpis", {})

    now_dt = datetime.datetime.now(CARACAS_TZ)
    scope_suffix = []
    if ibp_val:
        scope_suffix.append(f"IBP {ibp_val}")
    if cat_val:
        scope_suffix.append(f"Cat. {cat_val}")
    suffix_str = f" ({' · '.join(scope_suffix)})" if scope_suffix else ""
    default_title = f"Informe Ejecutivo{suffix_str} - {now_dt.strftime('%B %Y').capitalize()}"
    title = (req.title or "").strip() or default_title

    spec_summary = spec or "Todo el equipo"
    if scope_suffix:
        spec_summary = f"{spec_summary} | {' | '.join(scope_suffix)}"

    report_entry = GeneratedReport(
        title=title,
        period_label=data.get("period_label", "Período seleccionado"),
        start_date=s_date,
        end_date=e_date,
        specialist_filter=spec_summary,
        generated_by=user_name,
        total_items=k.get("total_items", 0),
        total_closed=k.get("total_cerrados", 0),
        avg_resolution_time=k.get("avg_resolution_str", "0m"),
        created_at=datetime.datetime.utcnow()
    )
    db.add(report_entry)
    db.commit()
    db.refresh(report_entry)

    return {
        "status": "OK",
        "message": "Reporte generado y guardado en el historial",
        "report": {
            "id": report_entry.id,
            "title": report_entry.title,
            "period_label": report_entry.period_label,
            "start_date": report_entry.start_date,
            "end_date": report_entry.end_date,
            "specialist_filter": report_entry.specialist_filter,
            "generated_by": report_entry.generated_by,
            "total_items": report_entry.total_items,
            "total_closed": report_entry.total_closed,
            "avg_resolution_time": report_entry.avg_resolution_time,
            "created_at_caracas": format_caracas_time(report_entry.created_at, "%d-%m-%Y %I:%M %p")
        },
        "data": data
    }


@app.delete("/api/reports/history/{report_id}")
def api_reports_delete_history(
    report_id: int,
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    role_param: str = Query(None, alias="role")
):
    """Elimina una entrada del historial de reportes (solo Coordinador)."""
    role = (x_user_role if isinstance(x_user_role, str) else (role_param if isinstance(role_param, str) else "")).strip().lower()
    if role not in ("coordinador", "admin", "administrador"):
        raise HTTPException(
            status_code=403,
            detail="Acceso restringido: Solo el Coordinador puede eliminar reportes del historial."
        )

    entry = db.query(GeneratedReport).filter(GeneratedReport.id == report_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Reporte no encontrado en el historial.")

    db.delete(entry)
    db.commit()
    return {"status": "OK", "deleted_id": report_id}


# =============================================================================
# MÓDULO DE ESCALAMIENTOS - REPOSITORIO DOCUMENTAL DE PDFs
# =============================================================================

def format_file_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    else:
        return f"{num_bytes / (1024 * 1024):.2f} MB"


VALID_ESCALATION_CATEGORIES = ["CPU", "RAM", "Disco", "Antivirus", "PostgreSQL", "Servicios", "Infraestructura"]


class CreateEscalationRequest(BaseModel):
    title: str
    doc_url: str
    category: str = "Servicios"
    ibp: str | None = None
    ibp_id: int | None = None
    notes: str | None = None
    recommendations: str | None = None
    received_at: str | None = None
    escalated_at: str | None = None


class UpdateEscalationRequest(BaseModel):
    title: str | None = None
    category: str | None = None
    ibp: str | None = None
    ibp_id: int | None = None
    doc_url: str | None = None
    notes: str | None = None
    recommendations: str | None = None
    received_at: str | None = None
    escalated_at: str | None = None


class CreateIBPRequest(BaseModel):
    name: str
    code: str | None = None
    description: str | None = None
    environment: str = "Producción"


class UploadEscalationRequest(BaseModel):
    title: str | None = None
    filename: str
    file_base64: str
    category: str | None = None
    ibp: str | None = None
    ibp_id: int | None = None
    notes: str | None = None


@app.get("/api/ibps")
def api_get_ibps(db: Session = Depends(get_db)):
    """Lista el catálogo oficial de Identificación Bancaria del Participante (IBP) de la plataforma SIMF."""
    db_ibps = db.query(IBP).filter(IBP.is_active == 1).order_by(IBP.code.asc()).all()
    if db_ibps:
        return [
            {
                "id": i.id,
                "name": i.name,
                "code": i.code,
                "label": i.name if " - " in i.name else (f"{i.code} - {i.name}" if i.code else i.name),
                "description": i.description,
                "environment": i.environment,
                "is_active": i.is_active,
                "created_at": i.created_at.isoformat() if i.created_at else None
            }
            for i in db_ibps
        ]
    return IBP_INVENTORY


@app.post("/api/ibps")
def api_create_ibp(
    req: CreateIBPRequest,
    db: Session = Depends(get_db),
    _role: str = Depends(verify_coordinator_role)
):
    """Crea una nueva Instancia de Base de Datos / Plataforma (IBP) en la BD interna."""
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="El nombre de la IBP es obligatorio.")
    existing = db.query(IBP).filter(IBP.name.ilike(name)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ya existe una IBP registrada con ese nombre.")
    code = (req.code or "").strip().upper() if req.code else name[:20].upper().replace(" ", "_")
    ibp = IBP(
        name=name,
        code=code,
        description=(req.description or "").strip() or None,
        environment=(req.environment or "Producción").strip(),
        is_active=1,
        created_at=datetime.datetime.utcnow()
    )
    db.add(ibp)
    db.commit()
    db.refresh(ibp)
    return {
        "status": "OK",
        "message": "IBP creada exitosamente",
        "ibp": {
            "id": ibp.id,
            "name": ibp.name,
            "code": ibp.code,
            "description": ibp.description,
            "environment": ibp.environment,
            "is_active": ibp.is_active
        }
    }


@app.get("/api/escalations")
def api_get_escalations(db: Session = Depends(get_db)):
    """Lista todos los documentos de escalamiento registrados (con fecha de recibimiento, escalada, autor e IBP asociada)."""
    docs = db.query(EscalationDocument).order_by(EscalationDocument.created_at.desc()).all()
    ibp_map = {i.id: i for i in db.query(IBP).all()}
    result = []
    for d in docs:
        is_gdoc = bool(d.doc_url) or not d.stored_filename
        ibp_obj = ibp_map.get(d.ibp_id) if d.ibp_id else None
        ibp_val = d.ibp or (ibp_obj.name if ibp_obj else "") or ""
        rec_dt = d.received_at or d.created_at
        esc_dt = d.escalated_at or rec_dt
        result.append({
            "id": d.id,
            "title": d.title,
            "category": d.category or "Servicios",
            "ibp": ibp_val,
            "ibp_id": d.ibp_id,
            "ibp_name": ibp_obj.name if ibp_obj else (d.ibp or None),
            "ibp_code": ibp_obj.code if ibp_obj else None,
            "doc_url": d.doc_url or (f"/api/escalations/{d.id}/view" if d.stored_filename else "#"),
            "doc_type": "google_docs" if is_gdoc else "pdf",
            "original_filename": d.original_filename or "Documento Google Docs",
            "stored_filename": d.stored_filename,
            "file_size_bytes": d.file_size_bytes,
            "file_size_formatted": d.file_size_formatted if not is_gdoc else "Google Docs",
            "uploaded_by": d.uploaded_by,
            "uploaded_by_role": d.uploaded_by_role,
            "notes": d.notes or "",
            "recommendations": d.recommendations or "",
            "received_at": rec_dt.isoformat() if rec_dt else None,
            "escalated_at": esc_dt.isoformat() if esc_dt else None,
            "received_at_caracas": format_caracas_time(rec_dt, "%d-%m-%Y %I:%M %p") if rec_dt else "—",
            "escalated_at_caracas": format_caracas_time(esc_dt, "%d-%m-%Y %I:%M %p") if esc_dt else "—",
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "created_at_caracas": format_caracas_time(d.created_at, "%d-%m-%Y %I:%M %p") if d.created_at else "—",
            "view_url": d.doc_url or f"/api/escalations/{d.id}/view",
            "download_url": f"/api/escalations/{d.id}/download" if d.stored_filename else None
        })
    return result


@app.post("/api/escalations")
def api_create_escalation(
    req: CreateEscalationRequest,
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user")
):
    """Registra un nuevo documento de escalamiento mediante enlace oficial a Google Docs o Drive."""
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Debe indicar el código o título del escalamiento.")

    doc_url = (req.doc_url or "").strip()
    if not doc_url:
        raise HTTPException(status_code=400, detail="Debe indicar el enlace (URL) de Google Docs.")

    if not (doc_url.startswith("http://") or doc_url.startswith("https://")):
        doc_url = "https://" + doc_url

    category = (req.category or "").strip()
    if category == "Servicios BD":
        category = "Servicios"
    elif category == "Discos":
        category = "Disco"
    if category not in VALID_ESCALATION_CATEGORIES:
        category = "Servicios"

    role = (decode_header_val(x_user_role) if x_user_role else (decode_header_val(role_param) if role_param else "Coordinador")).capitalize()
    user_name = (decode_header_val(x_user_name) if x_user_name else (decode_header_val(user_param) if user_param else "Adonis Lozada Figueroa")) or "Adonis Lozada Figueroa"

    clean_ibp, ibp_id = resolve_ibp(db, req.ibp, req.ibp_id)
    ibp_obj = db.query(IBP).filter(IBP.id == ibp_id).first() if ibp_id else None

    now_utc = datetime.datetime.utcnow()
    rec_dt = parse_iso_utc(req.received_at) or now_utc
    esc_dt = parse_iso_utc(req.escalated_at) or rec_dt

    doc = EscalationDocument(
        title=title,
        category=category,
        ibp=clean_ibp,
        ibp_id=ibp_id,
        doc_url=doc_url,
        original_filename=f"{title} (Google Docs)",
        stored_filename="",
        file_size_bytes=0,
        file_size_formatted="Google Docs",
        uploaded_by=user_name,
        uploaded_by_role=role,
        notes=(req.notes or "").strip() or None,
        recommendations=(req.recommendations or "").strip() or None,
        received_at=rec_dt,
        escalated_at=esc_dt,
        created_at=now_utc
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # Notificación a Telegram (Google Docs)
    try:
        import html
        from .notifications.telegram_notifier import send_telegram_message, now_caracas_str
        now_str = now_caracas_str("%d-%m-%Y %I:%M %p")
        rec_str = format_caracas_time(rec_dt, "%d-%m-%Y %I:%M %p")
        esc_str = format_caracas_time(esc_dt, "%d-%m-%Y %I:%M %p")
        safe_title = html.escape(title)
        safe_cat = html.escape(category)
        safe_author = html.escape(user_name)
        notes_text = (req.notes or "").strip()
        safe_notes = f"\n📝 <b>Notas:</b> {html.escape(notes_text)}" if notes_text else ""
        recs_text = (req.recommendations or "").strip()
        safe_recs = f"\n💡 <b>Recomendaciones:</b> {html.escape(recs_text)}" if recs_text else ""
        safe_ibp = f"\n🏦 <b>IBP / Banco:</b> {html.escape(clean_ibp)}" if clean_ibp else ""
        link_html = f"\n🔗 <a href=\"{doc_url}\"><b>Abrir en Google Docs</b></a>"

        msg = (
            f"📄 <b>Nuevo Escalamiento Registrado</b>\n\n"
            f"📌 <b>Código / Caso:</b> <code>{safe_title}</code>\n"
            f"📂 <b>Categoría:</b> {safe_cat}"
            f"{safe_ibp}\n"
            f"📥 <b>Recibido en Sycom:</b> {rec_str}\n"
            f"🚀 <b>Escalado a Infra:</b> {esc_str}\n"
            f"👤 <b>Registrado por:</b> {safe_author} ({role})\n"
            f"🕒 <b>Fecha registro:</b> {now_str}"
            f"{safe_notes}"
            f"{safe_recs}"
            f"{link_html}\n\n"
            f"⚡ <i>Agenda de Infraestructura &amp; BD · Soluciones SYCOM</i>"
        )
        send_telegram_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"[Telegram Escalation Notification Error] {e}")

    return {
        "status": "OK",
        "message": "Documento de escalamiento registrado exitosamente.",
        "document": {
            "id": doc.id,
            "title": doc.title,
            "category": doc.category,
            "ibp": doc.ibp or "",
            "ibp_id": doc.ibp_id,
            "ibp_name": ibp_obj.name if ibp_obj else (doc.ibp or None),
            "ibp_code": ibp_obj.code if ibp_obj else None,
            "doc_url": doc.doc_url,
            "doc_type": "google_docs",
            "file_size_formatted": "Google Docs",
            "uploaded_by": doc.uploaded_by,
            "uploaded_by_role": doc.uploaded_by_role,
            "notes": doc.notes or "",
            "recommendations": doc.recommendations or "",
            "received_at": doc.received_at.isoformat() if doc.received_at else None,
            "escalated_at": doc.escalated_at.isoformat() if doc.escalated_at else None,
            "received_at_caracas": format_caracas_time(doc.received_at, "%d-%m-%Y %I:%M %p") if doc.received_at else "—",
            "escalated_at_caracas": format_caracas_time(doc.escalated_at, "%d-%m-%Y %I:%M %p") if doc.escalated_at else "—",
            "created_at_caracas": format_caracas_time(doc.created_at, "%d-%m-%Y %I:%M %p")
        }
    }


@app.put("/api/escalations/{doc_id}")
@app.patch("/api/escalations/{doc_id}")
def api_update_escalation(
    doc_id: int,
    req: UpdateEscalationRequest,
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user")
):
    """Actualiza los datos, fechas cronológicas, categoría o recomendaciones de un escalamiento."""
    doc = db.query(EscalationDocument).filter(EscalationDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento de escalamiento no encontrado.")

    role = (decode_header_val(x_user_role) if x_user_role else (decode_header_val(role_param) if role_param else "Coordinador")).capitalize()
    user_name = (decode_header_val(x_user_name) if x_user_name else (decode_header_val(user_param) if user_param else "Adonis Lozada Figueroa")) or "Adonis Lozada Figueroa"

    if req.title is not None:
        t = req.title.strip()
        if not t:
            raise HTTPException(status_code=400, detail="El código o título no puede estar vacío.")
        doc.title = t

    if req.category is not None:
        c = req.category.strip()
        if c == "Servicios BD":
            c = "Servicios"
        elif c == "Discos":
            c = "Disco"
        if c in VALID_ESCALATION_CATEGORIES:
            doc.category = c

    if "ibp" in req.model_fields_set or "ibp_id" in req.model_fields_set:
        clean_ibp, ibp_id = resolve_ibp(db, req.ibp, req.ibp_id)
        doc.ibp = clean_ibp
        doc.ibp_id = ibp_id

    if req.doc_url is not None:
        u = req.doc_url.strip()
        if not u:
            raise HTTPException(status_code=400, detail="El enlace (URL) no puede estar vacío.")
        if not (u.startswith("http://") or u.startswith("https://")):
            u = "https://" + u
        doc.doc_url = u

    if "notes" in req.model_fields_set:
        doc.notes = req.notes.strip() if req.notes else None

    if "recommendations" in req.model_fields_set:
        doc.recommendations = req.recommendations.strip() if req.recommendations else None

    dates_updated = False
    if req.received_at is not None:
        parsed_rec = parse_iso_utc(req.received_at)
        if parsed_rec:
            doc.received_at = parsed_rec
            dates_updated = True
    if req.escalated_at is not None:
        parsed_esc = parse_iso_utc(req.escalated_at)
        if parsed_esc:
            doc.escalated_at = parsed_esc
            dates_updated = True

    if dates_updated:
        linked_items = db.query(Item).filter(Item.escalation_id == doc.id).all()
        for it in linked_items:
            if doc.received_at:
                it.received_at = doc.received_at
            if doc.escalated_at:
                it.escalated_at = doc.escalated_at

    db.commit()
    db.refresh(doc)

    # Notificación a Telegram por actualización
    try:
        import html
        from .notifications.telegram_notifier import send_telegram_message, now_caracas_str
        now_str = now_caracas_str("%d-%m-%Y %I:%M %p")
        safe_title = html.escape(doc.title)
        safe_cat = html.escape(doc.category or "Servicios BD")
        safe_author = html.escape(user_name)
        notes_text = (doc.notes or "").strip()
        safe_notes = f"\n📝 <b>Notas:</b> {html.escape(notes_text)}" if notes_text else ""
        recs_text = (doc.recommendations or "").strip()
        safe_recs = f"\n💡 <b>Recomendaciones:</b> {html.escape(recs_text)}" if recs_text else ""
        safe_ibp = f"\n🏦 <b>IBP / Banco:</b> {html.escape(doc.ibp)}" if doc.ibp else ""
        link_html = f"\n🔗 <a href=\"{doc.doc_url}\"><b>Abrir en Google Docs</b></a>" if doc.doc_url else ""

        msg = (
            f"✏️ <b>Escalamiento Actualizado</b>\n\n"
            f"📌 <b>Código / Caso:</b> <code>{safe_title}</code>\n"
            f"📂 <b>Categoría:</b> {safe_cat}"
            f"{safe_ibp}\n"
            f"👤 <b>Modificado por:</b> {safe_author} ({role})\n"
            f"🕒 <b>Fecha:</b> {now_str}"
            f"{safe_notes}"
            f"{safe_recs}"
            f"{link_html}\n\n"
            f"⚡ <i>Agenda de Infraestructura &amp; BD · Soluciones SYCOM</i>"
        )
        send_telegram_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"[Telegram Update Escalation Notification Error] {e}")

    ibp_obj = db.query(IBP).filter(IBP.id == doc.ibp_id).first() if doc.ibp_id else None
    rec_dt = doc.received_at or doc.created_at
    esc_dt = doc.escalated_at or rec_dt

    return {
        "status": "OK",
        "message": "Escalamiento actualizado exitosamente.",
        "document": {
            "id": doc.id,
            "title": doc.title,
            "category": doc.category,
            "ibp": doc.ibp or "",
            "ibp_id": doc.ibp_id,
            "ibp_name": ibp_obj.name if ibp_obj else (doc.ibp or None),
            "ibp_code": ibp_obj.code if ibp_obj else None,
            "doc_url": doc.doc_url,
            "doc_type": "google_docs",
            "uploaded_by": doc.uploaded_by,
            "uploaded_by_role": doc.uploaded_by_role,
            "notes": doc.notes or "",
            "recommendations": doc.recommendations or "",
            "received_at": rec_dt.isoformat() if rec_dt else None,
            "escalated_at": esc_dt.isoformat() if esc_dt else None,
            "received_at_caracas": format_caracas_time(rec_dt, "%d-%m-%Y %I:%M %p") if rec_dt else "—",
            "escalated_at_caracas": format_caracas_time(esc_dt, "%d-%m-%Y %I:%M %p") if esc_dt else "—",
            "created_at_caracas": format_caracas_time(doc.created_at, "%d-%m-%Y %I:%M %p")
        }
    }


@app.post("/api/escalations/upload")
def api_upload_escalation(
    req: UploadEscalationRequest,
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name")
):
    """Carga y registra un documento de escalamiento en formato PDF."""
    if not req.filename or not req.file_base64:
        raise HTTPException(status_code=400, detail="Debe suministrar un archivo PDF válido.")

    clean_filename = req.filename.strip()
    if not clean_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se admiten documentos en formato PDF (.pdf).")

    # Decodificar Base64
    raw_b64 = req.file_base64
    if "," in raw_b64:
        raw_b64 = raw_b64.split(",", 1)[1]

    try:
        file_bytes = base64.b64decode(raw_b64)
    except Exception as e:
        raise HTTPException(status_code=400, detail="El contenido del archivo no es un Base64 válido.")

    if len(file_bytes) < 10:
        raise HTTPException(status_code=400, detail="El archivo está vacío o dañado.")

    # Validar firma magic bytes PDF (%PDF-)
    if not file_bytes.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="El archivo suministrado no tiene formato PDF válido.")

    # Limitar tamaño a 50MB
    if len(file_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="El archivo excede el tamaño máximo permitido de 50 MB.")

    # Título por defecto: el suministrado por el usuario (ej. 'SIMF-ES-5260 88920 Venezolano') o el nombre del archivo
    title = (req.title or "").strip()
    if not title:
        title = clean_filename[:-4] if clean_filename.lower().endswith(".pdf") else clean_filename

    # Guardar en disco con nombre seguro único
    safe_base = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', clean_filename)
    unique_prefix = uuid.uuid4().hex[:10]
    stored_name = f"{unique_prefix}_{safe_base}"
    file_path = os.path.join(UPLOAD_ESCALATIONS_DIR, stored_name)

    try:
        with open(file_path, "wb") as f:
            f.write(file_bytes)
    except Exception as e:
        print(f"[Upload Escalation Disk Error] {e}")
        raise HTTPException(status_code=500, detail="Error al guardar el archivo en el servidor de almacenamiento.")

    user_name = decode_header_val(x_user_name) or "Adonis Lozada Figueroa"
    user_role = decode_header_val(x_user_role).capitalize() or "Coordinador"

    clean_ibp, ibp_id = resolve_ibp(db, req.ibp, req.ibp_id)
    ibp_obj = db.query(IBP).filter(IBP.id == ibp_id).first() if ibp_id else None

    category = (req.category or "").strip() if req.category else "Servicios"
    if category == "Servicios BD":
        category = "Servicios"
    elif category == "Discos":
        category = "Disco"
    if category not in VALID_ESCALATION_CATEGORIES:
        category = "Servicios"

    doc = EscalationDocument(
        title=title,
        category=category,
        ibp=clean_ibp,
        ibp_id=ibp_id,
        original_filename=clean_filename,
        stored_filename=stored_name,
        file_size_bytes=len(file_bytes),
        file_size_formatted=format_file_size(len(file_bytes)),
        uploaded_by=user_name,
        uploaded_by_role=user_role,
        notes=(req.notes or "").strip() or None,
        created_at=datetime.datetime.utcnow()
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # Notificación a Telegram
    try:
        import html
        from .notifications.telegram_notifier import send_telegram_message, now_caracas_str
        now_str = now_caracas_str("%d-%m-%Y %I:%M %p")
        safe_title = html.escape(title)
        safe_author = html.escape(user_name)
        safe_file = html.escape(req.filename)
        safe_cat = html.escape(category)
        safe_ibp = f"\n🏦 <b>IBP / Banco:</b> {html.escape(clean_ibp)}" if clean_ibp else ""
        notes_text = (req.notes or "").strip()
        safe_notes = f"\n📝 <b>Notas:</b> {html.escape(notes_text)}" if notes_text else ""

        msg = (
            f"📄 <b>Nuevo Escalamiento Registrado (PDF)</b>\n\n"
            f"📌 <b>Código / Caso:</b> <code>{safe_title}</code>\n"
            f"📂 <b>Categoría:</b> {safe_cat}"
            f"{safe_ibp}\n"
            f"📎 <b>Archivo:</b> {safe_file}\n"
            f"👤 <b>Registrado por:</b> {safe_author} ({user_role})\n"
            f"🕒 <b>Fecha:</b> {now_str}"
            f"{safe_notes}\n\n"
            f"⚡ <i>Agenda de Infraestructura &amp; BD · Soluciones SYCOM</i>"
        )
        send_telegram_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"[Telegram Escalation Notification Error] {e}")

    return {
        "status": "OK",
        "message": "Documento de escalamiento cargado exitosamente.",
        "document": {
            "id": doc.id,
            "title": doc.title,
            "category": doc.category,
            "ibp": doc.ibp or "",
            "ibp_id": doc.ibp_id,
            "ibp_name": ibp_obj.name if ibp_obj else (doc.ibp or None),
            "ibp_code": ibp_obj.code if ibp_obj else None,
            "original_filename": doc.original_filename,
            "file_size_formatted": doc.file_size_formatted,
            "uploaded_by": doc.uploaded_by,
            "uploaded_by_role": doc.uploaded_by_role,
            "notes": doc.notes or "",
            "created_at_caracas": format_caracas_time(doc.created_at, "%d-%m-%Y %I:%M %p"),
            "view_url": f"/api/escalations/{doc.id}/view",
            "download_url": f"/api/escalations/{doc.id}/download"
        }
    }


@app.get("/api/escalations/{doc_id}/view")
def api_view_escalation(doc_id: int, db: Session = Depends(get_db)):
    """Abre el documento de escalamiento: redirige si es Google Docs o muestra PDF inline."""
    doc = db.query(EscalationDocument).filter(EscalationDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento de escalamiento no encontrado.")

    if doc.doc_url:
        return RedirectResponse(url=doc.doc_url)

    if not doc.stored_filename:
        raise HTTPException(status_code=404, detail="El documento no posee un archivo físico asociado.")

    file_path = os.path.join(UPLOAD_ESCALATIONS_DIR, doc.stored_filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="El archivo físico del documento no se encuentra en el servidor.")

    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        filename=doc.original_filename,
        content_disposition_type="inline"
    )


@app.get("/api/escalations/{doc_id}/download")
def api_download_escalation(doc_id: int, db: Session = Depends(get_db)):
    """Fuerza la descarga del PDF o redirige al documento si es Google Docs."""
    doc = db.query(EscalationDocument).filter(EscalationDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento de escalamiento no encontrado.")

    if doc.doc_url:
        return RedirectResponse(url=doc.doc_url)

    if not doc.stored_filename:
        raise HTTPException(status_code=404, detail="El documento no posee un archivo descargable.")

    file_path = os.path.join(UPLOAD_ESCALATIONS_DIR, doc.stored_filename)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="El archivo físico del documento no se encuentra en el servidor.")

    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        filename=doc.original_filename,
        content_disposition_type="attachment"
    )


@app.delete("/api/escalations/{doc_id}")
def api_delete_escalation(
    doc_id: int,
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role")
):
    """Elimina un documento de escalamiento de la BD y borra su archivo de disco."""
    doc = db.query(EscalationDocument).filter(EscalationDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    role = (x_user_role if isinstance(x_user_role, str) else (role_param if isinstance(role_param, str) else "")).strip().lower()
    user_name = (x_user_name.strip().lower()) if isinstance(x_user_name, str) else ""
    doc_author = (doc.uploaded_by or "").strip().lower()

    if role not in ("coordinador", "admin", "administrador") and (not user_name or user_name not in doc_author):
        raise HTTPException(
            status_code=403,
            detail="Permiso denegado: Solo el Coordinador o el autor del documento pueden eliminarlo."
        )

    if doc.stored_filename:
        file_path = os.path.join(UPLOAD_ESCALATIONS_DIR, doc.stored_filename)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"[Delete Escalation File Error] {e}")

    # Notificación a Telegram por eliminación
    try:
        import html
        from .notifications.telegram_notifier import send_telegram_message, now_caracas_str
        now_str = now_caracas_str("%d-%m-%Y %I:%M %p")
        safe_title = html.escape(doc.title)
        remover = html.escape(x_user_name if isinstance(x_user_name, str) and x_user_name.strip() else "Coordinador")

        msg = (
            f"🗑 <b>Escalamiento Eliminado</b>\n\n"
            f"📌 <b>Código / Caso:</b> <code>{safe_title}</code>\n"
            f"👤 <b>Eliminado por:</b> {remover}\n"
            f"🕒 <b>Hora eliminación:</b> {now_str}\n\n"
            f"⚡ <i>Agenda de Infraestructura &amp; BD · Soluciones SYCOM</i>"
        )
        send_telegram_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"[Telegram Delete Escalation Error] {e}")

    # Desvincular actividades que apuntaban a este escalamiento para evitar referencias huérfanas
    items_linked = db.query(Item).filter(Item.escalation_id == doc_id).all()
    for it in items_linked:
        it.escalation_id = None

    db.delete(doc)
    db.commit()
    return {"status": "OK", "deleted_id": doc_id}





