import datetime
from fastapi import FastAPI, Depends, Query, HTTPException
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

# Crear tablas si no existen
Base.metadata.create_all(bind=engine)

SPECIALISTS_MAP = {
    "adonis": {
        "key": "adonis",
        "name": "Adonis Lozada",
        "glpi_id": 8,
        "clickup_id": 49560462,
        "telegram": "@adonislozada"
    },
    "juan": {
        "key": "juan",
        "name": "Juan Fernández",
        "glpi_id": 10,
        "clickup_id": None,
        "telegram": "@Juuan8"
    },
    "yoxaide": {
        "key": "yoxaide",
        "name": "Yoxaide Jiménez",
        "glpi_id": 11,
        "clickup_id": 49560461,
        "telegram": "@Yjimenezg"
    },
    "luis": {
        "key": "luis",
        "name": "Luis Acosta",
        "glpi_id": 9,
        "clickup_id": None,
        "telegram": "@lapsy0"
    }
}

class AssignRequest(BaseModel):
    specialist_key: str | None = None
    specialist_keys: list[str] | None = None

DEFAULT_USERS = [
    {"full_name": "Adonis Lozada", "username": "a.lozada", "email": "a.lozada@sycom.com.ve", "telegram_username": "@adonislozada", "role": "coordinador"},
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

        for u in DEFAULT_USERS:
            exists = db.query(User).filter(User.full_name == u["full_name"]).first()
            if not exists:
                db.add(User(**u))
            else:
                exists.telegram_username = u["telegram_username"]
                exists.role = u["role"]
                exists.username = u["username"]
                exists.email = u["email"]
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
    db: Session = Depends(get_db)
):
    query = db.query(Item)

    if src:
        query = query.filter(Item.source == src)
    if tipo:
        query = query.filter(Item.client_type == tipo)
    if resp:
        query = query.filter(Item.assignee_name.ilike(f"%{resp}%"))
    if prio:
        query = query.filter(Item.priority == prio)
    
    # Política de cerrados: solo mostrar activos o cerrados desde el día de ayer en adelante
    yesterday_start = get_yesterday_start()
    if not status:
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

    if q:
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
        "telegram_username": u.telegram_username,
        "role": u.role
    } for u in users]

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
def assign_item(item_id: int, req: AssignRequest, db: Session = Depends(get_db)):
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
    item.assignee_name = combined_names
    if item.status == "pendiente":
        item.status = "curso"
        item.status_origin = "in progress" if source == "ClickUp" else "En curso (asignado)"
    item.updated_at = datetime.datetime.utcnow()
    item.synced_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(item)

    # Disparar alerta inmediata a Telegram
    try:
        from .notifications.telegram_notifier import send_telegram_message
        mentions = ", ".join(f"{m['name']} ({m['telegram']})" for m in matched_list)
        plural_label = "Responsables" if len(matched_list) > 1 else "Responsable"
        icon = "👥" if len(matched_list) > 1 else "👤"
        msg = (
            f"{icon} <b>Asignación en vivo</b>\n\n"
            f"📌 <b>Asunto:</b> {item.title}\n"
            f"🏷 <b>Origen:</b> {item.source} #{item.external_id}\n"
            f"🎯 <b>{plural_label}:</b> {mentions}\n"
            f"⚡ <i>Asignado desde la Agenda de Infra</i>"
        )
        send_telegram_message(msg)
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
            "status": item.status,
            "status_origin": item.status_origin
        }
    }


