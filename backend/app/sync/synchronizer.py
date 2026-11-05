import datetime
from sqlalchemy.orm import Session
from ..database import SessionLocal
from ..models import Item, SyncLog
from .glpi_client import GLPIClient
from .clickup_client import ClickUpClient

def sync_glpi(db: Session):
    client = GLPIClient()
    try:
        raw_tickets = client.fetch_tickets()
        count = 0
        for raw in raw_tickets:
            norm = client.normalize_ticket(raw)
            # Buscar si ya existe
            item = db.query(Item).filter(
                Item.source == "GLPI",
                Item.external_id == norm["external_id"]
            ).first()

            if item:
                for k, v in norm.items():
                    setattr(item, k, v)
            else:
                item = Item(**norm)
                db.add(item)
            count += 1
        
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
    try:
        raw_tasks = client.fetch_tasks()
        count = 0
        for raw in raw_tasks:
            norm = client.normalize_task(raw)
            item = db.query(Item).filter(
                Item.source == "ClickUp",
                Item.external_id == norm["external_id"]
            ).first()

            if item:
                for k, v in norm.items():
                    setattr(item, k, v)
            else:
                item = Item(**norm)
                db.add(item)
            count += 1

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
        print(f"[Sync Completado] GLPI: {res_glpi.get('status')} ({res_glpi.get('count', 0)}), ClickUp: {res_cu.get('status')} ({res_cu.get('count', 0)})")
    finally:
        db.close()

