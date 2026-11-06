import datetime
import urllib.request
import json
from sqlalchemy.orm import Session
from .. import config
from ..models import Item, AlertLog, User

def send_telegram_message(text: str, parse_mode: str | None = None) -> bool:
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        return False

    if not parse_mode:
        if "<b" in text or "<i" in text or "<a " in text or "<code" in text or "</" in text:
            parse_mode = "HTML"
        else:
            parse_mode = "Markdown"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[Telegram Notifier] Error enviando mensaje ({parse_mode}): {e}")
        # Si falló por formato, reintentar sin parse_mode para asegurar que el mensaje llegue
        try:
            payload.pop("parse_mode", None)
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as retry_err:
            print(f"[Telegram Notifier] Falló reintento plano: {retry_err}")
            return False


def resolve_assignee_mention(db: Session, assignee_name: str, is_critical: bool = False) -> str:
    """
    Resuelve el nombre del responsable y le agrega su mención en Telegram (@usuario).
    Si el ticket no tiene responsable asignado, menciona al Coordinador (@adonislozada).
    """
    coord = db.query(User).filter(User.role == "coordinador").first()
    coord_tag = coord.telegram_username if coord and coord.telegram_username else "@adonislozada"

    if not assignee_name or assignee_name.strip() in ("Sin asignar", "Sin Asignar", "Técnico GLPI"):
        return f"⚠️ *Sin asignar* (Atención Coordinador: {coord_tag})"

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
            matched_tags.append(f"{u.full_name} ({tag})")

    if matched_tags:
        res = ", ".join(matched_tags)
        # Si es crítico (rojo), también notificamos al coordinador
        if is_critical and coord_tag not in res:
            res += f" · 📢 CC: {coord_tag}"
        return res

    return f"{assignee_name}" + (f" · 📢 CC: {coord_tag}" if is_critical else "")


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
                due_str = item.due_at.strftime("%d-%m-%Y %H:%M") if item.due_at else "No especificada"
                ticket_url = item.url or "http://200.74.215.44:7070/"
                msg_nuevo = (
                    f"🆕 *NUEVO REQUERIMIENTO ASIGNADO ({item.source.upper()})*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📋 *{item.source} #{item.external_id}:* {item.title}\n"
                    f"🏢 *Cliente / Espacio:* {item.client_name}\n"
                    f"👤 *Responsable:* {assignee_str}\n"
                    f"⚡ *Prioridad:* {item.priority}\n"
                    f"⏰ *Vence:* {due_str}\n"
                    f"🔗 [Abrir en {item.source}]({ticket_url})"
                )
                ok = send_telegram_message(msg_nuevo)
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
        due_str = item.due_at.strftime("%d-%m-%Y %H:%M") if item.due_at else "No especificada"
        ticket_url = item.url or "http://200.74.215.44:7070/"

        if sem == "amarillo":
            msg = (
                f"🟡 *ALERTA PREVENTIVA · SLA al {item.sla_percent}%*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *{item.source} #{item.external_id}:* {item.title}\n"
                f"🏢 *Cliente / Entidad:* {item.client_name}\n"
                f"👤 *Responsable:* {assignee_str}\n"
                f"⚡ *Prioridad:* {item.priority}\n"
                f"⏰ *Vence:* {due_str}\n"
                f"🔗 [Abrir en {item.source}]({ticket_url})"
            )
        else: # rojo
            msg = (
                f"🚨 *¡ATENCIÓN! TICKET FUERA DE SLA ({item.sla_percent}%)*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *{item.source} #{item.external_id}:* {item.title}\n"
                f"🏢 *Cliente / Entidad:* {item.client_name}\n"
                f"👤 *Responsable:* {assignee_str}\n"
                f"⚡ *Prioridad:* {item.priority}\n"
                f"⏰ *Venció:* {due_str}\n"
                f"🔗 [Abrir en {item.source}]({ticket_url})"
            )

        ok = send_telegram_message(msg)
        if ok:
            log = AlertLog(item_id=item.id, semaphore=sem)
            db.add(log)
            db.commit()
            print(f"[Telegram Notifier] Alerta '{sem}' enviada para item #{item.external_id} ({item.source})")


