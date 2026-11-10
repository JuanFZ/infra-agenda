import datetime
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session
from ..database import SessionLocal
from ..models import Item, SyncLog
from .glpi_client import GLPIClient
from .clickup_client import ClickUpClient
from ..notifications.telegram_notifier import check_and_notify_alerts

def get_yesterday_start():
    now = datetime.datetime.utcnow()
    return (now - datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

def purge_old_closed_items(db: Session):
    """
    Política de retención: Solo conservar tickets/tareas cerradas desde el día de ayer en adelante.
    Cualquier asunto cerrado con fecha anterior a ayer (00:00) se elimina de la base de datos operativa.
    """
    cutoff = get_yesterday_start()
    purged = db.query(Item).filter(
        Item.status == "cerrado",
        or_(
            and_(Item.closed_at != None, Item.closed_at < cutoff),
            and_(Item.closed_at == None, Item.updated_at < cutoff)
        )
    ).all()
    if purged:
        count = len(purged)
        for p in purged:
            db.delete(p)
        db.commit()
        print(f"[Purge] Purgados {count} tickets/tareas cerrados anteriores a ayer ({cutoff.strftime('%Y-%m-%d')}).")

def sync_glpi(db: Session):
    client = GLPIClient()
    yesterday_start = get_yesterday_start()
    try:
        users_map = client.fetch_users_map()
        assignees_map = client.fetch_ticket_assignees_map(users_map)
        entities_map = client.fetch_entities_map()
        raw_tickets = client.fetch_tickets()
        count = 0
        active_ids = set()

        for raw in raw_tickets:
            # En GLPI, si un ticket está en papelera (is_deleted = 1), se descarta y elimina de la BD
            if raw.get("is_deleted") == 1 or raw.get("is_deleted") is True:
                del_id = str(raw.get("id"))
                existing_del = db.query(Item).filter(
                    Item.source == "GLPI",
                    Item.external_id == del_id
                ).first()
                if existing_del:
                    db.delete(existing_del)
                continue

            norm = client.normalize_ticket(raw, assignees_map, entities_map)
            ext_id = norm["external_id"]

            # Política de cerrados: no guardar ni mostrar cerrados anteriores al día de ayer
            if norm["status"] == "cerrado":
                ref_time = norm.get("closed_at") or norm.get("updated_at")
                if ref_time and ref_time < yesterday_start:
                    existing_old = db.query(Item).filter(Item.source == "GLPI", Item.external_id == ext_id).first()
                    if existing_old:
                        db.delete(existing_old)
                    continue

            active_ids.add(ext_id)

            # Buscar si ya existe
            item = db.query(Item).filter(
                Item.source == "GLPI",
                Item.external_id == ext_id
            ).first()

            if item:
                incoming_assignee = (norm.get("assignee_name") or "").strip()
                current_assignee = (item.assignee_name or "").strip()

                has_current_assignee = bool(current_assignee) and current_assignee.lower() not in (
                    "sin asignar", "técnico glpi", "tecnico glpi"
                )

                # La Agenda de Infra es la fuente autoritativa de asignaciones del equipo:
                # si la actividad ya tiene especialistas asignados en la Agenda, se preservan
                # para que ningún ciclo de sincronización borre a Juan, Luis, Yoxaide o Adonis.
                if has_current_assignee:
                    norm["assignee_name"] = current_assignee
                    if item.assigned_at:
                        norm["assigned_at"] = item.assigned_at

                # Preservar fechas de recibimiento y escalada si ya existen
                if item.received_at:
                    norm["received_at"] = item.received_at
                if item.escalated_at:
                    norm["escalated_at"] = item.escalated_at

                # Preservar subproyecto / entidad específica si el ticket ya tenía una asignada en Agenda
                # y GLPI reporta la entidad raíz por defecto (0 o 1)
                existing_client = (item.client_name or "").strip()
                if existing_client and not existing_client.lower().startswith("entidad #"):
                    raw_ent_id = raw.get("entities_id")
                    if raw_ent_id in (0, 1, None, "0", "1") and "microfilm" in existing_client.lower():
                        norm["client_name"] = existing_client
                        norm["project_name"] = item.project_name or existing_client

                # Si tiene responsable y no tenía fecha de asignación o cambió de responsable
                has_assignee = norm.get("assignee_name") and "sin asignar" not in norm["assignee_name"].lower()
                if has_assignee:
                    if not item.assigned_at or item.assignee_name != norm["assignee_name"]:
                        item.assigned_at = norm.get("updated_at") or norm.get("created_at") or datetime.datetime.utcnow()
                else:
                    item.assigned_at = None

                for k, v in norm.items():
                    setattr(item, k, v)
            else:
                has_assignee = norm.get("assignee_name") and "sin asignar" not in norm["assignee_name"].lower()
                if has_assignee:
                    norm["assigned_at"] = norm.get("updated_at") or norm.get("created_at") or datetime.datetime.utcnow()
                norm.setdefault("received_at", norm.get("created_at"))
                norm.setdefault("escalated_at", norm.get("created_at"))
                item = Item(**norm)
                db.add(item)
            count += 1
        
        # Si un ticket fue eliminado por completo de GLPI (no devuelto por la API), eliminarlo de la BD
        purged = db.query(Item).filter(
            Item.source == "GLPI",
            ~Item.external_id.in_(active_ids)
        ).all()
        for p in purged:
            db.delete(p)

        db.commit()
        log = SyncLog(source="GLPI", status="OK", records_synced=count, message=f"Sincronizados {count} tickets con éxito.")
        db.add(log)
        db.commit()
        return {"status": "OK", "count": count}
    except Exception as e:
        db.rollback()
        err_msg = str(e)
        print(f"[Sync GLPI] Error: {err_msg}")
        log = SyncLog(source="GLPI", status="ERROR", records_synced=0, message=err_msg)
        db.add(log)
        db.commit()
        return {"status": "ERROR", "message": err_msg}

def sync_clickup(db: Session):
    client = ClickUpClient()
    yesterday_start = get_yesterday_start()
    try:
        raw_tasks = client.fetch_tasks()
        count = 0
        active_ids = set()
        for raw in raw_tasks:
            norm = client.normalize_task(raw)
            ext_id = norm["external_id"]

            # Política de cerrados: no guardar ni mostrar cerrados anteriores al día de ayer
            if norm["status"] == "cerrado":
                ref_time = norm.get("closed_at") or norm.get("updated_at")
                if ref_time and ref_time < yesterday_start:
                    existing_old = db.query(Item).filter(Item.source == "ClickUp", Item.external_id == ext_id).first()
                    if existing_old:
                        db.delete(existing_old)
                    continue

            active_ids.add(ext_id)

            item = db.query(Item).filter(
                Item.source == "ClickUp",
                Item.external_id == ext_id
            ).first()

            if item:
                incoming_assignee = (norm.get("assignee_name") or "").strip()
                current_assignee = (item.assignee_name or "").strip()

                has_current_assignee = bool(current_assignee) and current_assignee.lower() not in (
                    "sin asignar", "técnico glpi", "tecnico glpi"
                )

                # La Agenda de Infra es la fuente autoritativa de asignaciones del equipo:
                # si la actividad ya tiene especialistas asignados en la Agenda, se preservan
                # para que ningún ciclo de sincronización borre a Juan, Luis, Yoxaide o Adonis.
                if has_current_assignee:
                    norm["assignee_name"] = current_assignee
                    if item.assigned_at:
                        norm["assigned_at"] = item.assigned_at

                # Preservar fechas de recibimiento y escalada si ya existen
                if item.received_at:
                    norm["received_at"] = item.received_at
                if item.escalated_at:
                    norm["escalated_at"] = item.escalated_at

                # Preservar subproyecto de ClickUp (ej. SIMF, SGLPAR) si fue asignado desde la Agenda
                existing_client = (item.client_name or "").strip()
                if existing_client and existing_client != "Gestión Sycom":
                    if not norm.get("client_name") or norm.get("client_name") == "Gestión Sycom":
                        norm["client_name"] = existing_client
                        norm["project_name"] = item.project_name or existing_client
                elif item.project_name and not norm.get("project_name"):
                    norm["project_name"] = item.project_name

                # Si tiene responsable y no tenía fecha de asignación o cambió de responsable
                has_assignee = norm.get("assignee_name") and "sin asignar" not in norm["assignee_name"].lower()
                if has_assignee:
                    if not item.assigned_at or item.assignee_name != norm["assignee_name"]:
                        item.assigned_at = norm.get("updated_at") or norm.get("created_at") or datetime.datetime.utcnow()
                else:
                    item.assigned_at = None

                for k, v in norm.items():
                    setattr(item, k, v)
            else:
                has_assignee = norm.get("assignee_name") and "sin asignar" not in norm["assignee_name"].lower()
                if has_assignee:
                    norm["assigned_at"] = norm.get("updated_at") or norm.get("created_at") or datetime.datetime.utcnow()
                norm.setdefault("received_at", norm.get("created_at"))
                norm.setdefault("escalated_at", norm.get("created_at"))
                item = Item(**norm)
                db.add(item)
            count += 1

        # Eliminar tareas que hayan sido borradas en ClickUp
        purged = db.query(Item).filter(
            Item.source == "ClickUp",
            ~Item.external_id.in_(active_ids)
        ).all()
        for p in purged:
            db.delete(p)

        db.commit()
        log = SyncLog(source="ClickUp", status="OK", records_synced=count, message=f"Sincronizadas {count} tareas con éxito.")
        db.add(log)
        db.commit()
        return {"status": "OK", "count": count}
    except Exception as e:
        db.rollback()
        err_msg = str(e)
        print(f"[Sync ClickUp] Error: {err_msg}")
        log = SyncLog(source="ClickUp", status="ERROR", records_synced=0, message=err_msg)
        db.add(log)
        db.commit()
        return {"status": "ERROR", "message": err_msg}

def sync_all():
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Iniciando ciclo de sincronización (GLPI + ClickUp)...")
    db = SessionLocal()
    try:
        res_glpi = sync_glpi(db)
        res_cu = sync_clickup(db)
        # Purgar cualquier residuo de tareas cerradas anteriores a ayer
        purge_old_closed_items(db)
        print(f"[Sync Completado] GLPI: {res_glpi.get('status')} ({res_glpi.get('count', 0)}), ClickUp: {res_cu.get('status')} ({res_cu.get('count', 0)})")
        # Revisar y enviar alertas automáticas a Telegram
        check_and_notify_alerts(db)
    finally:
        db.close()

