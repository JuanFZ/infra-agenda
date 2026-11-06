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
        status_obj = raw.get("status") or {}
        raw_status = (status_obj.get("status") or "").lower() if isinstance(status_obj, dict) else str(status_obj).lower()
        status_unif = STATUS_MAP_CLICKUP.get(raw_status, "pendiente")

        prio_obj = raw.get("priority") or {}
        raw_prio = (prio_obj.get("priority") or "").lower() if isinstance(prio_obj, dict) else str(prio_obj).lower()
        prio_unif = PRIORITY_MAP_CLICKUP.get(raw_prio, "Media")

        # Fechas en milisegundos
        created_ms = raw.get("date_created")
        created_at = datetime.datetime.utcfromtimestamp(int(created_ms) / 1000) if created_ms else datetime.datetime.utcnow()

        updated_ms = raw.get("date_updated")
        updated_at = datetime.datetime.utcfromtimestamp(int(updated_ms) / 1000) if updated_ms else created_at

        closed_ms = raw.get("date_closed")
        closed_at = datetime.datetime.utcfromtimestamp(int(closed_ms) / 1000) if closed_ms else None

        due_ms = raw.get("due_date")
        has_due_time = bool(raw.get("due_date_time"))
        if due_ms:
            due_at = datetime.datetime.utcfromtimestamp(int(due_ms) / 1000)
            # En ClickUp, al seleccionar una fecha sin hora específica (ej. "Hoy"),
            # ClickUp asigna por defecto la madrugada. Si resulta anterior a la creación en el mismo día,
            # o no tiene hora explícita, se fija al final del día (23:59:59) para dar cumplimiento real al día.
            if not has_due_time or (due_at.date() == created_at.date() and due_at < created_at):
                due_at = due_at.replace(hour=23, minute=59, second=59)
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

    def assign_task(self, task_id: str, user_ids: list[int] | int) -> bool:
        """
        Asigna o reasigna uno o varios especialistas a la tarea en ClickUp.
        """
        try:
            url = f"{self.base_url}/task/{task_id}"
            if isinstance(user_ids, (int, str)):
                uids = [int(user_ids)]
            else:
                uids = [int(u) for u in user_ids if u]

            # Consultar asignados actuales para remover únicamente los que no estén en la nueva lista
            res_get = requests.get(url, headers=self.get_headers(), timeout=10)
            current_ids = []
            if res_get.status_code == 200:
                current_ids = [a.get("id") for a in res_get.json().get("assignees", []) if a.get("id")]
            
            payload = {
                "assignees": {
                    "add": uids,
                    "rem": [cid for cid in current_ids if cid not in uids]
                }
            }
            res = requests.put(url, headers=self.get_headers(), json=payload, timeout=10)
            return res.status_code == 200
        except Exception as e:
            print(f"[ClickUpClient] Error asignando tarea {task_id} a usuarios {user_ids}: {e}")
            return False

    def update_task_status(self, task_id: str, new_status: str) -> bool:
        """
        Actualiza el estado de la tarea en ClickUp.
        ClickUp solo cuenta con: 'to do' (pendiente), 'in progress' (curso) y 'complete' (cerrado).
        'espera' no existe en ClickUp.
        """
        if new_status == "espera":
            return False

        status_map = {
            "pendiente": "to do",
            "curso": "in progress",
            "cerrado": "complete"
        }
        target = status_map.get(new_status, new_status)
        try:
            url = f"{self.base_url}/task/{task_id}"
            res = requests.put(url, headers=self.get_headers(), json={"status": target}, timeout=10)
            if res.status_code == 200:
                return True
            # Fallback en caso de que la lista use nombres estándar alternativos
            if target == "to do":
                res = requests.put(url, headers=self.get_headers(), json={"status": "open"}, timeout=10)
            elif target == "complete":
                res = requests.put(url, headers=self.get_headers(), json={"status": "closed"}, timeout=10)
            return res.status_code == 200
        except Exception as e:
            print(f"[ClickUpClient] Error actualizando estado de tarea {task_id} a {new_status}: {e}")
            return False

    def create_task(self, title: str, description: str = "", priority: str = "Media", due_at: datetime.datetime | None = None, user_ids: list[int] | None = None, list_id: str | None = None) -> dict | None:
        """
        Crea una tarea formal en ClickUp mediante su API v2.
        """
        if not list_id:
            list_id = "1600250000002725"  # Lista Action Items en Espacio Infra & BD
        
        url = f"{self.base_url}/list/{list_id}/task"
        prio_map = {
            "Crítica": 1,
            "Alta": 2,
            "Media": 3,
            "Baja": 4
        }
        clickup_prio = prio_map.get(priority, 3)
        
        payload = {
            "name": title,
            "description": description or "",
            "priority": clickup_prio,
            "status": "to do"
        }
        if user_ids:
            payload["assignees"] = [int(u) for u in user_ids if u]
            
        if due_at:
            payload["due_date"] = int(due_at.timestamp() * 1000)
            payload["due_date_time"] = True
            
        try:
            res = requests.post(url, headers=self.get_headers(), json=payload, timeout=12)
            res.raise_for_status()
            data = res.json()
            task_id = data.get("id")
            if task_id and not data.get("url"):
                data["url"] = f"https://app.clickup.com/t/{task_id}"
            return data
        except Exception as e:
            print(f"[ClickUpClient] Error creando tarea '{title}': {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"[ClickUpClient Response] {e.response.text}")
            return None


