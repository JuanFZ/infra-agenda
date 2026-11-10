import datetime
import html
import json
import os
import requests
from sqlalchemy.orm import Session
from .. import config
from ..models import Item, AlertLog, User

# Zona Horaria de Venezuela (Caracas: UTC-4)
CARACAS_TZ = datetime.timezone(datetime.timedelta(hours=-4))

def get_agenda_deep_link(item_or_id: Item | int | str, view: str = "kanban") -> str:
    """
    Construye la URL con deep linking a la Agenda de Infraestructura,
    apuntando a la vista solicitada (por defecto 'kanban') y activando el modal de la actividad.
    """
    item_id = getattr(item_or_id, "id", item_or_id)
    base_url = (getattr(config, "AGENDA_BASE_URL", None) or os.getenv("AGENDA_BASE_URL") or os.getenv("APP_URL") or "http://localhost:8000").rstrip("/")
    return f"{base_url}/?view={view}&item_id={item_id}"


def format_caracas_time(dt: datetime.datetime | datetime.date | str | None, format_str: str = "%d-%m-%Y %I:%M %p") -> str:
    """Convierte cualquier fecha UTC, local o cadena ISO a la zona horaria oficial de Caracas (UTC-4)."""
    if not dt:
        return "No especificada"
    if isinstance(dt, str):
        s = dt.strip().replace("Z", "+00:00")
        try:
            if len(s) == 10:
                parts = s.split("-")
                dt_obj = datetime.datetime(int(parts[0]), int(parts[1]), int(parts[2]), 0, 0, 0, tzinfo=CARACAS_TZ)
                return dt_obj.strftime(format_str)
            dt = datetime.datetime.fromisoformat(s)
        except Exception:
            return dt
    elif isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime):
        dt = datetime.datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=CARACAS_TZ)
        return dt.strftime(format_str)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(CARACAS_TZ).strftime(format_str)

def now_caracas_str(format_str: str = "%d-%m-%Y %I:%M %p") -> str:
    """Retorna la fecha y hora actual exacta en Caracas (UTC-4)."""
    now_utc = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    return now_utc.astimezone(CARACAS_TZ).strftime(format_str)

def send_telegram_message(text: str, parse_mode: str | None = None) -> bool:
    token = (config.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (config.TELEGRAM_CHAT_ID or "").strip()

    if not token or not chat_id:
        print("[Telegram Notifier] AVISO: TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no están configurados en backend/.env")
        return False

    if not parse_mode:
        if "<b" in text or "<i" in text or "<a " in text or "<code" in text or "</" in text:
            parse_mode = "HTML"
        else:
            parse_mode = "Markdown"

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        res = requests.post(api_url, json=payload, timeout=10)
        if res.status_code == 200:
            return True
        print(f"[Telegram Notifier] Error {res.status_code} enviando mensaje ({parse_mode}): {res.text}")
        
        # Si falló por formato de entidades, reintentar en texto plano para asegurar la entrega
        if res.status_code == 400 and parse_mode:
            payload.pop("parse_mode", None)
            retry_res = requests.post(api_url, json=payload, timeout=10)
            if retry_res.status_code == 200:
                print("[Telegram Notifier] Mensaje entregado con éxito en reintento plano.")
                return True
            print(f"[Telegram Notifier] Falló reintento plano ({retry_res.status_code}): {retry_res.text}")
        return False
    except Exception as e:
        print(f"[Telegram Notifier] Excepción de conexión con API de Telegram: {e}")
        return False

def test_telegram_connection() -> dict:
    """Prueba la conectividad y validez del bot de Telegram configurado."""
    token = (config.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (config.TELEGRAM_CHAT_ID or "").strip()
    if not token or not chat_id:
        return {
            "status": "ERROR",
            "message": "TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID vacíos en el archivo .env",
            "bot_configured": bool(token),
            "chat_configured": bool(chat_id)
        }

    now_str = now_caracas_str('%d-%m-%Y %I:%M %p')
    test_msg = (
        f"🤖 <b>Diagnóstico de Conexión Sycom</b>\n\n"
        f"✅ <i>El bot de notificaciones está conectado y operativo en el servidor.</i>\n"
        f"🕒 <b>Hora Caracas:</b> {now_str}\n"
        f"📡 <i>Agenda de Infraestructura & Base de Datos</i>"
    )
    ok = send_telegram_message(test_msg, parse_mode="HTML")
    return {
        "status": "OK" if ok else "ERROR",
        "message": "Mensaje de prueba enviado exitosamente a Telegram" if ok else "Error al enviar mensaje a Telegram (verifique credenciales y permisos)",
        "bot_configured": bool(token),
        "chat_configured": bool(chat_id)
    }


def resolve_assignee_mention(db: Session, assignee_name: str, is_critical: bool = False) -> str:
    """
    Resuelve el nombre del responsable y le agrega su mención en Telegram (@usuario).
    Si el ticket no tiene responsable asignado, menciona al Coordinador (@adonislozada).
    """
    coord = db.query(User).filter(User.role == "coordinador").first()
    coord_tag = coord.telegram_username if coord and coord.telegram_username else "@adonislozada"

    if not assignee_name or assignee_name.strip() in ("Sin asignar", "Sin Asignar", "Técnico GLPI"):
        return f"⚠️ <b>Sin asignar</b> (Atención Coordinador: {coord_tag})"

    users = db.query(User).filter(User.is_active == 1).all()
    matched_tags = []

    for u in users:
        first_name = u.full_name.split()[0].lower()
        last_name = u.full_name.split()[-1].lower()
        assignee_lower = assignee_name.lower()

        if (first_name in assignee_lower or last_name in assignee_lower or 
            (u.username and u.username.lower() in assignee_lower) or
            (u.email and u.email.lower() in assignee_lower)):
            tag = u.telegram_username if u.telegram_username else u.full_name
            matched_tags.append(f"{html.escape(u.full_name)} ({html.escape(tag)})")

    if matched_tags:
        res = ", ".join(matched_tags)
        # Si es crítico (rojo), también notificamos al coordinador
        if is_critical and coord_tag not in res:
            res += f" · 📢 CC: {coord_tag}"
        return res

    return f"{html.escape(assignee_name)}" + (f" · 📢 CC: {coord_tag}" if is_critical else "")


def check_and_notify_alerts(db: Session):
    """
    Revisa los tickets activos y envía notificaciones a Telegram:
    - 🆕 Nuevo requerimiento asignado (creado en las últimas 4 horas)
    - 🟡 SLA entre 75% y 99% (Alerta Preventiva)
    - 🔴 SLA >= 100% o atrasado (Alerta Crítica / Fuera de SLA)
    Usa la tabla AlertLog para garantizar que cada ticket se alerte solo UNA vez por nivel.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    now = datetime.datetime.utcnow()
    open_items = db.query(Item).filter(Item.status != "cerrado").all()
    for item in open_items:
        # 1. Alerta de NUEVO requerimiento asignado (si fue creado en los últimos 20 minutos para no saturar)
        diff_sec = (now - item.created_at).total_seconds() if item.created_at else 999999
        if -600 < diff_sec < 1200:
            new_alerted = db.query(AlertLog).filter(
                AlertLog.item_id == item.id,
                AlertLog.semaphore == "nuevo"
            ).first()

            if not new_alerted:
                assignee_str = resolve_assignee_mention(db, item.assignee_name, is_critical=False)
                due_str = format_caracas_time(item.due_at, "%d-%m-%Y %I:%M %p") if item.due_at else "No especificada"
                agenda_url = get_agenda_deep_link(item.id, view="kanban")
                safe_title = html.escape(item.title or "")
                safe_source = html.escape(item.source or "")
                safe_ext_id = html.escape(str(item.external_id or ""))
                safe_client = html.escape(item.client_name or "")
                safe_priority = html.escape(item.priority or "")

                msg_nuevo = (
                    f"🆕 <b>NUEVO REQUERIMIENTO ASIGNADO ({safe_source.upper()})</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📋 <b>{safe_source} #{safe_ext_id}:</b> {safe_title}\n"
                    f"🏢 <b>Cliente / Espacio:</b> {safe_client}\n"
                    f"👤 <b>Responsable:</b> {assignee_str}\n"
                    f"⚡ <b>Prioridad:</b> {safe_priority}\n"
                    f"⏰ <b>Vence:</b> {due_str}\n"
                    f"🔗 <a href=\"{agenda_url}\"><b>Abrir Agenda</b></a>"
                )
                ok = send_telegram_message(msg_nuevo, parse_mode="HTML")
                if ok:
                    log_nuevo = AlertLog(item_id=item.id, semaphore="nuevo")
                    db.add(log_nuevo)
                    db.commit()
                    print(f"[Telegram Notifier] Alerta 'nuevo' enviada para #{item.external_id} ({item.source})")

        # 2. Alertas de SLA Preventiva y Crítica
        sem = item.semaphore
        if sem not in ("amarillo", "rojo"):
            continue

        already_alerted = db.query(AlertLog).filter(
            AlertLog.item_id == item.id,
            AlertLog.semaphore == sem
        ).first()

        if already_alerted:
            continue

        is_critical = (sem == "rojo")
        assignee_str = resolve_assignee_mention(db, item.assignee_name, is_critical=is_critical)
        due_str = format_caracas_time(item.due_at, "%d-%m-%Y %I:%M %p") if item.due_at else "No especificada"
        agenda_url = get_agenda_deep_link(item.id, view="kanban")
        safe_title = html.escape(item.title or "")
        safe_source = html.escape(item.source or "")
        safe_ext_id = html.escape(str(item.external_id or ""))
        safe_client = html.escape(item.client_name or "")
        safe_priority = html.escape(item.priority or "")

        if sem == "amarillo":
            msg = (
                f"🟡 <b>ALERTA PREVENTIVA · SLA al {item.sla_percent}%</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 <b>{safe_source} #{safe_ext_id}:</b> {safe_title}\n"
                f"🏢 <b>Cliente / Entidad:</b> {safe_client}\n"
                f"👤 <b>Responsable:</b> {assignee_str}\n"
                f"⚡ <b>Prioridad:</b> {safe_priority}\n"
                f"⏰ <b>Vence:</b> {due_str}\n"
                f"🔗 <a href=\"{agenda_url}\"><b>Abrir Agenda</b></a>"
            )
        else: # rojo
            msg = (
                f"🚨 <b>¡ATENCIÓN! TICKET FUERA DE SLA ({item.sla_percent}%)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 <b>{safe_source} #{safe_ext_id}:</b> {safe_title}\n"
                f"🏢 <b>Cliente / Entidad:</b> {safe_client}\n"
                f"👤 <b>Responsable:</b> {assignee_str}\n"
                f"⚡ <b>Prioridad:</b> {safe_priority}\n"
                f"⏰ <b>Venció:</b> {due_str}\n"
                f"🔗 <a href=\"{agenda_url}\"><b>Abrir Agenda</b></a>"
            )

        ok = send_telegram_message(msg, parse_mode="HTML")
        if ok:
            log = AlertLog(item_id=item.id, semaphore=sem)
            db.add(log)
            db.commit()
            print(f"[Telegram Notifier] Alerta '{sem}' enviada para item #{item.external_id} ({item.source})")



