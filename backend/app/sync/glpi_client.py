import base64
import datetime
import requests
from ..config import GLPI_URL, GLPI_USER, GLPI_PASS, GLPI_APP_TOKEN

STATUS_MAP = {
    1: ("pendiente", "1 - Nuevo"),
    2: ("curso", "2 - En curso (asignado)"),
    3: ("curso", "3 - En curso (planificado)"),
    4: ("espera", "4 - En espera"),
    5: ("cerrado", "5 - Resuelto"),
    6: ("cerrado", "6 - Cerrado")
}

PRIORITY_MAP = {
    1: "Baja",
    2: "Baja",
    3: "Media",
    4: "Alta",
    5: "Crítica",
    6: "Crítica"
}

# Horas de SLA por defecto si GLPI no tiene time_to_resolve configurado
DEFAULT_SLA_HOURS = {
    "Crítica": 4,
    "Alta": 24,
    "Media": 72,
    "Baja": 120
}

class GLPIClient:
    def __init__(self):
        self.base_url = GLPI_URL.rstrip("/")
        self.app_token = GLPI_APP_TOKEN
        self.user = GLPI_USER
        self.password = GLPI_PASS
        self.session_token = None
        self._categories_cache = {}
        self._entities_cache = {}

    def get_auth_headers(self):
        b64_cred = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        return {
            "Authorization": f"Basic {b64_cred}",
            "App-Token": self.app_token,
            "Content-Type": "application/json"
        }

    def init_session(self):
        url = f"{self.base_url}/initSession"
        res = requests.get(url, headers=self.get_auth_headers(), timeout=10)
        res.raise_for_status()
        data = res.json()
        self.session_token = data.get("session_token")
        return self.session_token

    def get_session_headers(self):
        if not self.session_token:
            self.init_session()
        return {
            "Session-Token": self.session_token,
            "App-Token": self.app_token,
            "Content-Type": "application/json"
        }

    def fetch_tickets(self, range_limit="0-100"):
        url = f"{self.base_url}/Ticket?range={range_limit}&order=DESC&sort=date_mod"
        headers = self.get_session_headers()
        res = requests.get(url, headers=headers, timeout=15)
        
        # Si la sesión expiró, reintentar una vez tras renovar
        if res.status_code == 401:
            self.init_session()
            headers = self.get_session_headers()
            res = requests.get(url, headers=headers, timeout=15)

        res.raise_for_status()
        return res.json()

    def fetch_users_map(self) -> dict:
        """Obtiene el diccionario {user_id: 'Nombre Técnico'} de GLPI."""
        try:
            url = f"{self.base_url}/User?range=0-200"
            headers = self.get_session_headers()
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                mapping = {}
                for u in res.json():
                    uid = u.get("id")
                    first = u.get("firstname", "")
                    real = u.get("realname", "")
                    full = f"{first} {real}".strip()
                    mapping[uid] = full or u.get("name") or f"Usuario #{uid}"
                return mapping
        except Exception as e:
            print(f"[GLPI] Error consultando catálogo de usuarios: {e}")
        return {}

    def fetch_ticket_assignees_map(self, users_map: dict) -> dict:
        """Obtiene el mapa {tickets_id: 'Nombre Técnico'} desde la relación Ticket_User (type=2: asignado)."""
        try:
            url = f"{self.base_url}/Ticket_User?range=0-500"
            headers = self.get_session_headers()
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                mapping = {}
                for tu in res.json():
                    if tu.get("type") == 2:  # 2 = Técnico asignado
                        tid = str(tu.get("tickets_id"))
                        uid = tu.get("users_id")
                        mapping[tid] = users_map.get(uid, f"Usuario #{uid}")
                return mapping
        except Exception as e:
            print(f"[GLPI] Error consultando asignaciones Ticket_User: {e}")
        return {}

    def normalize_ticket(self, raw_ticket: dict, assignees_map: dict = None) -> dict:
        ticket_id = str(raw_ticket.get("id"))
        raw_status = int(raw_ticket.get("status", 1))
        status_unif, status_orig = STATUS_MAP.get(raw_status, ("pendiente", f"Estado {raw_status}"))
        
        raw_prio = int(raw_ticket.get("priority", 3))
        prio_unif = PRIORITY_MAP.get(raw_prio, "Media")

        # Fechas
        created_str = raw_ticket.get("date")
        created_at = datetime.datetime.fromisoformat(created_str) if created_str else datetime.datetime.utcnow()
        
        updated_str = raw_ticket.get("date_mod")
        updated_at = datetime.datetime.fromisoformat(updated_str) if updated_str else created_at
        
        closed_str = raw_ticket.get("closedate") or raw_ticket.get("solvedate")
        closed_at = datetime.datetime.fromisoformat(closed_str) if closed_str else None

        # Vencimiento / SLA
        ttr_str = raw_ticket.get("time_to_resolve")
        if ttr_str:
            due_at = datetime.datetime.fromisoformat(ttr_str)
        else:
            sla_hours = DEFAULT_SLA_HOURS.get(prio_unif, 72)
            due_at = created_at + datetime.timedelta(hours=sla_hours)

        # Construir enlace directo
        base_host = self.base_url.replace("/apirest.php", "")
        ticket_url = f"{base_host}/front/ticket.form.php?id={ticket_id}"

        assignee = "Sin asignar"
        if assignees_map and ticket_id in assignees_map:
            assignee = assignees_map[ticket_id]

        return {
            "source": "GLPI",
            "external_id": ticket_id,
            "url": ticket_url,
            "title": raw_ticket.get("name") or f"Ticket #{ticket_id}",
            "category": "Soporte TI / Infraestructura",
            "client_name": f"Entidad #{raw_ticket.get('entities_id', 1)}",
            "client_type": "Interno",
            "assignee_name": assignee,
            "priority": prio_unif,
            "status": status_unif,
            "status_origin": status_orig,
            "created_at": created_at,
            "due_at": due_at,
            "closed_at": closed_at,
            "updated_at": updated_at,
            "synced_at": datetime.datetime.utcnow()
        }

