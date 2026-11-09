import datetime
import hashlib
from fastapi import FastAPI, Depends, Query, HTTPException, Header, Response
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from .database import engine, Base, get_db, SessionLocal
from .models import Item, SyncLog, AlertLog, User
from .config import SYNC_INTERVAL_MINUTES
from .sync.synchronizer import sync_all, purge_old_closed_items, get_yesterday_start
from .sync.glpi_client import GLPIClient
from .sync.clickup_client import ClickUpClient
from .notifications.telegram_notifier import CARACAS_TZ, format_caracas_time, now_caracas_str
from .reports.report_generator import get_report_data, generate_excel_report, generate_executive_html

# Crear tablas si no existen
Base.metadata.create_all(bind=engine)

def hash_password(password: str) -> str:
    salt = "sycom_infra_salt_2026"
    return hashlib.sha256(f"{salt}{password}".encode("utf-8")).hexdigest()

def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    if not hashed_password:
        return False
    return hash_password(plain_password) == hashed_password

def ensure_db_schema():
    try:
        with engine.connect() as conn:
            cursor = conn.connection.cursor()
            cursor.execute("PRAGMA table_info(items)")
            cols = [row[1] for row in cursor.fetchall()]
            if "assigned_at" not in cols:
                cursor.execute("ALTER TABLE items ADD COLUMN assigned_at DATETIME")
            
            cursor.execute("PRAGMA table_info(users)")
            u_cols = [row[1] for row in cursor.fetchall()]
            if "password_hash" not in u_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN password_hash VARCHAR(200)")
            if "last_login" not in u_cols:
                cursor.execute("ALTER TABLE users ADD COLUMN last_login DATETIME")
            conn.connection.commit()
    except Exception as e:
        print(f"[DB Schema Check] {e}")

ensure_db_schema()

def verify_coordinator_role(x_user_role: str | None = Header(None, alias="X-User-Role")):
    """Valida que la solicitud provenga de un usuario con rol Coordinador o Administrador."""
    if not x_user_role or x_user_role.strip().lower() not in ("coordinador", "admin", "administrador"):
        raise HTTPException(
            status_code=403,
            detail="Acceso restringido: Solo el rol Coordinador / Administrador tiene permisos para crear, asignar, mover estados o eliminar actividades."
        )
    return x_user_role

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
        "clickup_id": None,
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
        "clickup_id": None,
        "telegram": "@lapsy0",
        "role": "Especialista"
    }
}

class AssignRequest(BaseModel):
    specialist_key: str | None = None
    specialist_keys: list[str] | None = None

class StatusUpdateRequest(BaseModel):
    status: str

class CreateItemRequest(BaseModel):
    source: str
    title: str
    description: str | None = None
    priority: str = "Media"
    specialist_keys: list[str] | None = None
    due_at: str | None = None

class LoginRequest(BaseModel):
    username: str
    password: str

class ChangePasswordRequest(BaseModel):
    username: str
    old_password: str
    new_password: str

DEFAULT_USERS = [
    {"full_name": "Adonis Lozada", "username": "a.lozada", "email": "a.lozada@sycom.com.ve", "telegram_username": "@adonislozada", "role": "coordinador"},
    {"full_name": "Administrador", "username": "admin", "email": "admin@sycom.com.ve", "telegram_username": "@adonislozada", "role": "coordinador"},
    {"full_name": "Juan Fernández", "username": "j.fernandez", "email": "j.fernandez@sycom.com.ve", "telegram_username": "@Juuan8", "role": "especialista"},
    {"full_name": "Yoxaide Jiménez", "username": "y.jimenez", "email": "y.jimenez@sycom.com.ve", "telegram_username": "@Yjimenezg", "role": "especialista"},
    {"full_name": "Luis Acosta", "username": "l.acosta", "email": "l.acosta@sycom.com.ve", "telegram_username": "@lapsy0", "role": "especialista"},
    {"full_name": "Analista de Operaciones", "username": "analista", "email": "analista@sycom.com.ve", "telegram_username": None, "role": "analista"},
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
    version="1.0.0"
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

    # Control de visibilidad por perfil:
    # El usuario regular solo ve las asignadas a él y las que están sin asignar (NO las de los demás)
    role = (x_user_role.strip().lower()) if isinstance(x_user_role, str) else ""
    if role and role not in ("coordinador", "admin", "administrador") and isinstance(x_user_name, str) and x_user_name:
        clean_name = x_user_name.strip()
        first_name = clean_name.split()[0] if clean_name else ""
        query = query.filter(
            or_(
                Item.assignee_name == None,
                Item.assignee_name == "",
                Item.assignee_name.ilike("%sin asignar%"),
                Item.assignee_name.ilike("%técnico glpi%"),
                Item.assignee_name.ilike(f"%{clean_name}%"),
                Item.assignee_name.ilike(f"%{first_name}%")
            )
        )

    if src and isinstance(src, str):
        query = query.filter(Item.source == src)
    if tipo and isinstance(tipo, str):
        query = query.filter(Item.client_type == tipo)
    if resp and isinstance(resp, str):
        query = query.filter(Item.assignee_name.ilike(f"%{resp}%"))
    if prio and isinstance(prio, str):
        query = query.filter(Item.priority == prio)
    
    # Política de cerrados: solo mostrar activos o cerrados desde el día de ayer en adelante
    yesterday_start = get_yesterday_start()
    if not status or not isinstance(status, str):
        query = query.filter(
            or_(
                Item.status != "cerrado",
                Item.closed_at >= yesterday_start,
                and_(Item.closed_at == None, Item.updated_at >= yesterday_start)
            )
        )
    elif status == "cerrado":
        query = query.filter(
            Item.status == "cerrado",
            or_(
                Item.closed_at >= yesterday_start,
                and_(Item.closed_at == None, Item.updated_at >= yesterday_start)
            )
        )
    else:
        query = query.filter(Item.status == status)

    if q and isinstance(q, str):
        query = query.filter(Item.title.ilike(f"%{q}%"))

    items = query.order_by(Item.updated_at.desc()).all()

    results = []
    for item in items:
        results.append({
            "id": item.id,
            "source": item.source,
            "external_id": item.external_id,
            "url": item.url,
            "title": item.title,
            "category": item.category,
            "client_name": item.client_name,
            "client_type": item.client_type,
            "assignee_name": item.assignee_name,
            "priority": item.priority,
            "status": item.status,
            "status_origin": item.status_origin,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "assigned_at": item.assigned_at.isoformat() if item.assigned_at else None,
            "due_at": item.due_at.isoformat() if item.due_at else None,
            "closed_at": item.closed_at.isoformat() if item.closed_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            "synced_at": item.synced_at.isoformat() if item.synced_at else None,
            "is_overdue": item.is_overdue,
            "sla_percent": item.sla_percent,
            "semaphore": item.semaphore,
            "duration_human": item.duration_human,
            "time_sla_label": item.time_sla_label,
            "sla_remaining_human": item.sla_remaining_human
        })
    return results

@app.get("/api/kpis")
def get_kpis(db: Session = Depends(get_db)):
    yesterday_start = get_yesterday_start()
    all_items = db.query(Item).filter(
        or_(
            Item.status != "cerrado",
            Item.closed_at >= yesterday_start,
            and_(Item.closed_at == None, Item.updated_at >= yesterday_start)
        )
    ).all()
    now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day, 0, 0, 0)
    today_end = datetime.datetime(now.year, now.month, now.day, 23, 59, 59)
    seven_days_ago = now - datetime.timedelta(days=7)

    pendientes = sum(1 for i in all_items if i.status == "pendiente")
    curso = sum(1 for i in all_items if i.status == "curso")
    espera = sum(1 for i in all_items if i.status == "espera")
    cerrados = [i for i in all_items if i.status == "cerrado"]
    
    atrasados = sum(1 for i in all_items if i.is_overdue)
    vencen_hoy = sum(1 for i in all_items if i.status != "cerrado" and i.due_at and today_start <= i.due_at <= today_end)
    cerrados_7d = sum(1 for i in cerrados if i.closed_at and i.closed_at >= seven_days_ago)

    # Cumplimiento SLA: cerrados dentro del plazo / total cerrados
    if cerrados:
        dentro_sla = sum(1 for i in cerrados if i.closed_at and i.due_at and i.closed_at <= i.due_at)
        cumplimiento_sla = round((dentro_sla / len(cerrados)) * 100.0, 1)
    else:
        cumplimiento_sla = 100.0

    last_log = db.query(SyncLog).order_by(SyncLog.created_at.desc()).first()

    return {
        "pendientes": pendientes,
        "en_curso": curso,
        "en_espera": espera,
        "atrasados": atrasados,
        "vencen_hoy": vencen_hoy,
        "cerrados_7d": cerrados_7d,
        "cumplimiento_sla": cumplimiento_sla,
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
            workload[resp] = {"name": resp, "total": 0, "criticos": 0, "atrasados": 0}
        workload[resp]["total"] += 1
        if item.priority == "Crítica":
            workload[resp]["criticos"] += 1
        if item.is_overdue:
            workload[resp]["atrasados"] += 1
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
        "user": {
            "id": user.id,
            "full_name": user.full_name,
            "username": user.username,
            "email": user.email,
            "telegram_username": user.telegram_username,
            "role": user.role
        }
    }

@app.get("/api/auth/users")
def get_auth_users(db: Session = Depends(get_db)):
    """Retorna los usuarios activos para el selector rápido del formulario de acceso."""
    users = db.query(User).filter(User.is_active == 1).all()
    return [{
        "id": u.id,
        "full_name": u.full_name,
        "username": u.username,
        "email": u.email,
        "role": u.role
    } for u in users]

@app.post("/api/auth/change-password")
def change_password(req: ChangePasswordRequest, db: Session = Depends(get_db)):
    u_str = req.username.strip()
    user = db.query(User).filter(
        or_(
            User.username.ilike(u_str),
            User.email.ilike(u_str),
            User.full_name.ilike(u_str)
        )
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if not verify_password(req.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="La contraseña actual es incorrecta.")
    if len(req.new_password) < 4:
        raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 4 caracteres.")
    
    user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"status": "ok", "message": "Contraseña actualizada con éxito"}

@app.get("/api/clients")
def get_clients_summary(db: Session = Depends(get_db)):
    yesterday_start = get_yesterday_start()
    items = db.query(Item).filter(
        or_(
            Item.status != "cerrado",
            Item.closed_at >= yesterday_start,
            and_(Item.closed_at == None, Item.updated_at >= yesterday_start)
        )
    ).all()
    clients = {}
    for item in items:
        c_name = item.client_name or "Sin cliente"
        if c_name not in clients:
            clients[c_name] = {
                "name": c_name,
                "type": item.client_type,
                "abiertos": 0,
                "atrasados": 0,
                "criticos": 0,
                "cerrados": 0,
                "dentro_sla": 0
            }
        if item.status != "cerrado":
            clients[c_name]["abiertos"] += 1
            if item.is_overdue:
                clients[c_name]["atrasados"] += 1
            if item.priority == "Crítica":
                clients[c_name]["criticos"] += 1
        else:
            clients[c_name]["cerrados"] += 1
            if item.closed_at and item.due_at and item.closed_at <= item.due_at:
                clients[c_name]["dentro_sla"] += 1

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
    source = item.source
    ext_id = item.external_id

    if source == "GLPI":
        client = GLPIClient()
        glpi_ids = [m["glpi_id"] for m in matched_list if m.get("glpi_id")]
        success = client.assign_ticket(ext_id, glpi_ids)
    elif source == "ClickUp":
        client = ClickUpClient()
        clickup_ids = [m["clickup_id"] for m in matched_list if m.get("clickup_id")]
        if clickup_ids:
            success = client.assign_task(ext_id, clickup_ids)
        else:
            success = True

    if not success:
        raise HTTPException(status_code=500, detail=f"No se pudo sincronizar la asignación en {source}")

    # Actualizar estado y responsable en la base de datos operativa
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
        msg = (
            f"{icon} <b>Asignación en vivo</b>\n\n"
            f"📌 <b>Asunto:</b> {safe_title}\n"
            f"🏷 <b>Origen:</b> {safe_source} #{item.external_id}\n"
            f"🎯 <b>{plural_label}:</b> {mentions}\n"
            f"🕒 <b>Incorporado a la actividad:</b> {now_str}\n"
            f"⚡ <i>Asignado desde la Agenda de Infra</i>"
        )
        send_telegram_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"[Telegram Assign Alert Error] {e}")

    return {
        "status": "OK",
        "message": f"Asignado exitosamente a {combined_names}",
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

    role = (x_user_role.strip().lower()) if isinstance(x_user_role, str) else ""
    if role in ("coordinador", "admin", "administrador"):
        pass  # Coordinador / Administrador tiene autorización total sobre cualquier actividad
    elif role == "especialista":
        # Especialista solo puede cambiar de estado sus propias actividades asignadas
        user_name_clean = (x_user_name.strip().lower()) if isinstance(x_user_name, str) else ""
        first_name = user_name_clean.split()[0] if user_name_clean else ""
        assignee_clean = (item.assignee_name or "").lower()
        if not user_name_clean or (user_name_clean not in assignee_clean and first_name not in assignee_clean):
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

    # ClickUp solo admite TO DO (pendiente), IN PROGRESS (curso) y COMPLETE (cerrado)
    if source == "ClickUp" and new_st == "espera":
        raise HTTPException(
            status_code=400, 
            detail="ClickUp no admite el estado 'En espera'. Solo admite: Pendiente (TO DO), En curso (IN PROGRESS) y Cerrado (COMPLETE). 'En espera' es exclusivo de GLPI."
        )

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

    if new_st == "cerrado":
        item.closed_at = now_utc
        item.status_origin = "5 - Resuelto" if source == "GLPI" else "complete"
    elif new_st == "curso":
        item.closed_at = None
        item.status_origin = "2 - En curso (asignado)" if source == "GLPI" else "in progress"
    elif new_st == "espera":
        item.closed_at = None
        item.status_origin = "4 - En espera" if source == "GLPI" else "waiting"
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
        msg = (
            f"📋 <b>Cambio de estado en Tablero Kanban</b>\n\n"
            f"📌 <b>Asunto:</b> {safe_title}\n"
            f"🏷 <b>Origen:</b> {safe_source} #{item.external_id}\n"
            f"📊 <b>Nuevo estado:</b> {status_labels.get(new_st, new_st)} ({safe_status_orig})\n"
            f"👤 <b>Responsable:</b> {safe_assignee}\n"
            f"🕒 <b>Hora:</b> {now_str}\n"
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
            "closed_at": item.closed_at.isoformat() if item.closed_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None
        }
    }

@app.post("/api/items")
def create_item(req: CreateItemRequest, db: Session = Depends(get_db), _role: str = Depends(verify_coordinator_role)):
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

    # Procesar fecha límite (SLA)
    now_utc = datetime.datetime.utcnow()
    due_dt = None
    if req.due_at:
        try:
            clean_due = req.due_at.replace("Z", "+00:00")
            due_dt = datetime.datetime.fromisoformat(clean_due)
            if due_dt.tzinfo is not None:
                due_dt = due_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        except Exception as e:
            print(f"[CreateItem] Error parseando due_at '{req.due_at}': {e}")
            due_dt = None

    if not due_dt:
        sla_hours = {"Crítica": 4, "Alta": 24, "Media": 72, "Baja": 120}.get(priority, 72)
        due_dt = now_utc + datetime.timedelta(hours=sla_hours)

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
    client_name = "Gestión Sycom" if source == "ClickUp" else "Interno Sycom"
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
            user_ids=glpi_ids
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
        category=category,
        client_name=client_name,
        client_type="Interno",
        assignee_name=combined_names,
        priority=priority,
        status="pendiente",
        status_origin=status_origin,
        created_at=now_utc,
        assigned_at=now_utc if matched_list else None,
        due_at=due_dt,
        updated_at=now_utc,
        synced_at=now_utc
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    # Disparar notificación a Telegram
    try:
        import html
        from .notifications.telegram_notifier import send_telegram_message
        mentions = ", ".join(f"{html.escape(m['name'])} ({m['telegram']})" for m in matched_list) if matched_list else "⚠️ <b>Sin asignar</b> (Atención: @adonislozada)"
        plural_label = "Responsables" if len(matched_list) > 1 else "Responsable"
        due_str = format_caracas_time(due_dt, '%d-%m-%Y %I:%M %p') if due_dt else "Sin límite"
        now_str = now_caracas_str('%d-%m-%Y %I:%M %p')
        safe_title = html.escape(title)
        safe_source = html.escape(source)
        safe_desc = f"\n📝 <b>Descripción:</b>\n{html.escape(req.description.strip())}\n" if req.description and req.description.strip() else ""
        prio_icon = "🔥" if priority == "Crítica" else ("⚡" if priority == "Alta" else "📌")
        msg = (
            f"✨ <b>Nueva Actividad Creada</b>\n\n"
            f"📌 <b>Asunto:</b> {safe_title}\n"
            f"🏷 <b>Origen:</b> {safe_source} #{external_id}\n"
            f"{prio_icon} <b>Prioridad:</b> {priority}\n"
            f"🎯 <b>{plural_label}:</b> {mentions}\n"
            f"⏳ <b>Límite SLA:</b> {due_str}\n"
            f"🕒 <b>Hora registro:</b> {now_str}\n"
            f"{safe_desc}\n"
            f"🔗 <a href='{url}'>Abrir en {safe_source}</a>"
        )
        send_telegram_message(msg, parse_mode="HTML")
    except Exception as e:
        print(f"[Telegram Create Notification Error] {e}")

    return {
        "status": "OK",
        "message": "Actividad creada con éxito",
        "item": {
            "id": item.id,
            "external_id": item.external_id,
            "source": item.source,
            "title": item.title,
            "assignee_name": item.assignee_name,
            "priority": item.priority,
            "status": item.status,
            "url": item.url
        }
    }


@app.delete("/api/items/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db), _role: str = Depends(verify_coordinator_role)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Actividad no encontrada")

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
        msg = (
            f"🗑 <b>Actividad Eliminada</b>\n\n"
            f"📌 <b>Asunto:</b> {html.escape(title)}\n"
            f"🏷 <b>Origen:</b> {html.escape(source)} #{ext_id}\n"
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
# REPORTES EJECUTIVOS (M9) - EXCEL & PDF
# ==============================================================================

def _resolve_report_filters(start_date, end_date, specialist, x_user_role, x_user_name, role_param, user_param):
    role = (x_user_role if isinstance(x_user_role, str) else (role_param if isinstance(role_param, str) else "")).strip().lower()
    user_name = (x_user_name if isinstance(x_user_name, str) else (user_param if isinstance(user_param, str) else "")).strip()
    spec = specialist.strip() if isinstance(specialist, str) and specialist.strip() else None

    # Si el usuario no es coordinador ni admin, restringir al especialista autenticado
    if role and role not in ("coordinador", "admin", "administrador") and user_name:
        spec = user_name

    s_date = start_date.strip() if isinstance(start_date, str) and start_date.strip() else None
    e_date = end_date.strip() if isinstance(end_date, str) and end_date.strip() else None
    return s_date, e_date, spec


@app.get("/api/reports/data")
def api_report_data(
    start_date: str = Query(None, description="Fecha inicio YYYY-MM-DD"),
    end_date: str = Query(None, description="Fecha fin YYYY-MM-DD"),
    specialist: str = Query(None, description="Filtrar por especialista"),
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user"),
):
    """Retorna los datos consolidados para reportes ejecutivos en formato JSON."""
    s_date, e_date, spec = _resolve_report_filters(
        start_date, end_date, specialist, x_user_role, x_user_name, role_param, user_param
    )
    data = get_report_data(
        db=db,
        start_date=s_date,
        end_date=e_date,
        specialist_filter=spec
    )
    return data


@app.get("/api/reports/excel")
def api_report_excel(
    start_date: str = Query(None, description="Fecha inicio YYYY-MM-DD"),
    end_date: str = Query(None, description="Fecha fin YYYY-MM-DD"),
    specialist: str = Query(None, description="Filtrar por especialista"),
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user"),
):
    """Genera y descarga el archivo Excel (.xlsx) corporativo con KPIs y métricas."""
    s_date, e_date, spec = _resolve_report_filters(
        start_date, end_date, specialist, x_user_role, x_user_name, role_param, user_param
    )
    data = get_report_data(
        db=db,
        start_date=s_date,
        end_date=e_date,
        specialist_filter=spec
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
    db: Session = Depends(get_db),
    x_user_role: str | None = Header(None, alias="X-User-Role"),
    x_user_name: str | None = Header(None, alias="X-User-Name"),
    role_param: str = Query(None, alias="role"),
    user_param: str = Query(None, alias="user"),
):
    """Genera la vista ejecutiva HTML lista para imprimir o guardar como PDF en alta resolución."""
    s_date, e_date, spec = _resolve_report_filters(
        start_date, end_date, specialist, x_user_role, x_user_name, role_param, user_param
    )
    data = get_report_data(
        db=db,
        start_date=s_date,
        end_date=e_date,
        specialist_filter=spec
    )

    html_content = generate_executive_html(data)
    return HTMLResponse(content=html_content)




