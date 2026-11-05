import datetime
import requests
from ..config import CLICKUP_TOKEN, CLICKUP_TEAM_ID, CLICKUP_SPACE_ID

STATUS_MAP_CLICKUP = {
    "to do": "pendiente",
    "open": "pendiente",
    "abierto": "pendiente",
    "in progress": "curso",
    "en curso": "curso",
    "doing": "curso",
    "waiting": "espera",
    "en espera": "espera",
    "blocked": "espera",
    "bloqueado": "espera",
    "complete": "cerrado",
    "closed": "cerrado",
    "completado": "cerrado",
    "done": "cerrado"
}

PRIORITY_MAP_CLICKUP = {
    "urgent": "Crítica",
    "high": "Alta",
    "normal": "Media",
    "low": "Baja"
}

class ClickUpClient:
    def __init__(self):
        self.token = CLICKUP_TOKEN
        self.team_id = CLICKUP_TEAM_ID
        self.base_url = "https://api.clickup.com/api/v2"

    def get_headers(self):
        return {
            "Authorization": self.token,
            "Content-Type": "application/json"
        }

    def fetch_tasks(self):
        if not self.token or not self.team_id:
            return []
        all_tasks = []
        page = 0
        space_param = f"&space_ids%5B%5D={CLICKUP_SPACE_ID}" if CLICKUP_SPACE_ID else ""
        while True:
            url = f"{self.base_url}/team/{self.team_id}/task?include_closed=true&subtasks=true{space_param}&page={page}"
            try:
                res = requests.get(url, headers=self.get_headers(), timeout=15)
                res.raise_for_status()
                data = res.json()
                tasks = data.get("tasks", [])
                if not tasks:
                    break
                all_tasks.extend(tasks)
                if len(tasks) < 100:
                    break
                page += 1
            except Exception as e:
                print(f"[ClickUpClient] Error fetching tasks page {page}: {e}")
                break
        return all_tasks

    def normalize_task(self, raw: dict) -> dict:
        task_id = raw.get("id")
        raw_status = (raw.get("status", {}).get("status") or "").lower()
        status_unif = STATUS_MAP_CLICKUP.get(raw_status, "pendiente")

        raw_prio = (raw.get("priority", {}).get("priority") or "").lower()
        prio_unif = PRIORITY_MAP_CLICKUP.get(raw_prio, "Media")

        # Fechas en milisegundos
        created_ms = raw.get("date_created")
        created_at = datetime.datetime.utcfromtimestamp(int(created_ms) / 1000) if created_ms else datetime.datetime.utcnow()

        updated_ms = raw.get("date_updated")
        updated_at = datetime.datetime.utcfromtimestamp(int(updated_ms) / 1000) if updated_ms else created_at

        closed_ms = raw.get("date_closed")
        closed_at = datetime.datetime.utcfromtimestamp(int(closed_ms) / 1000) if closed_ms else None

        due_ms = raw.get("due_date")
        if due_ms:
            due_at = datetime.datetime.utcfromtimestamp(int(due_ms) / 1000)
        else:
            # Si no tiene due_date, calculamos SLA por prioridad
            sla_hours = {"Crítica": 4, "Alta": 24, "Media": 72, "Baja": 120}.get(prio_unif, 72)
            due_at = created_at + datetime.timedelta(hours=sla_hours)

        # Responsables
        assignees = raw.get("assignees", [])
        names = [a.get("username") or a.get("email") for a in assignees if a.get("username") or a.get("email")]
        assignee_names = ", ".join(names) or "Sin asignar"

        return {
            "source": "ClickUp",
            "external_id": task_id,
            "url": raw.get("url") or f"https://app.clickup.com/t/{task_id}",
            "title": raw.get("name") or f"Tarea #{task_id}",
            "category": "Proyectos y Tareas",
            "client_name": "Gestión Sycom",
            "client_type": "Interno",
            "assignee_name": assignee_names,
            "priority": prio_unif,
            "status": status_unif,
            "status_origin": raw_status,
            "created_at": created_at,
            "due_at": due_at,
            "closed_at": closed_at,
            "updated_at": updated_at,
            "synced_at": datetime.datetime.utcnow()
        }

