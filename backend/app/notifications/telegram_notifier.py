import urllib.request
import json
from sqlalchemy.orm import Session
from .. import config
from ..models import Item, AlertLog

def send_telegram_message(text: str) -> bool:
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[Telegram Notifier] Error enviando mensaje: {e}")
        return False


def check_and_notify_alerts(db: Session):
    """
    Revisa los tickets activos y envía notificaciones a Telegram:
    - 🟡 SLA entre 75% y 99% (Alerta Preventiva)
    - 🔴 SLA >= 100% o atrasado (Alerta Crítica / Fuera de SLA)
    Usa la tabla AlertLog para garantizar que cada ticket se alerte solo UNA vez por nivel.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    open_items = db.query(Item).filter(Item.status != "cerrado").all()
    for item in open_items:
        sem = item.semaphore
        if sem not in ("amarillo", "rojo"):
            continue

        # Verificar si ya enviamos alerta para este nivel de semáforo
        already_alerted = db.query(AlertLog).filter(
            AlertLog.item_id == item.id,
            AlertLog.semaphore == sem
        ).first()

        if already_alerted:
            continue

        # Formatear fecha de vencimiento
        due_str = item.due_at.strftime("%d-%m-%Y %H:%M") if item.due_at else "No especificada"
        ticket_url = item.url or "http://200.74.215.44:7070/"

        if sem == "amarillo":
            msg = (
                f"🟡 *ALERTA PREVENTIVA · SLA al {item.sla_percent}%*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *{item.source} #{item.external_id}:* {item.title}\n"
                f"🏢 *Cliente / Entidad:* {item.client_name}\n"
                f"👤 *Asignado a:* {item.assignee_name}\n"
                f"⚡ *Prioridad:* {item.priority}\n"
                f"⏰ *Vence:* {due_str}\n"
                f"🔗 [Abrir en {item.source}]({ticket_url})"
            )
        else: # rojo
            msg = (
                f"🚨 *¡ATENCIÓN! TICKET FUERA DE SLA ({item.sla_percent}%)*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *{item.source} #{item.external_id}:* {item.title}\n"
                f"🏢 *Cliente / Entidad:* {item.client_name}\n"
                f"👤 *Asignado a:* {item.assignee_name}\n"
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

