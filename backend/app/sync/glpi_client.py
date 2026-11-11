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

# Mapeo de prioridades GLPI
PRIORITY_MAP = {
    1: "Baja",
    2: "Baja",
    3: "Media",
    4: "Alta",
    5: "Crítica",
    6: "Crítica"
}

DEFAULT_ENTITIES_MAP = {
    0: "SYCOM",
    1: "SYCOM",
    2: "Microfilms Center"
}

class GLPIClient:
    _shared_entities_cache = dict(DEFAULT_ENTITIES_MAP)
    _entities_fetched = False

    def __init__(self):
        self.base_url = GLPI_URL.rstrip("/")
        self.app_token = GLPI_APP_TOKEN
        self.user = GLPI_USER
        self.password = GLPI_PASS
        self.session_token = None
        self._categories_cache = {}
        self._entities_cache = dict(GLPIClient._shared_entities_cache)

    @staticmethod
    def clean_entity_name(raw_name: str | None) -> str:
        if not raw_name:
            return ""
        text = str(raw_name).strip()
        if ">" in text:
            text = text.split(">")[-1].strip()
        low = text.lower()
        if "microfilm" in low:
            return "Microfilms Center"
        if "sycom" in low or low in ("root entity", "entidad raíz", "entidad raiz", "root", "entidad #1", "entidad #0"):
            return "SYCOM"
        return text

    def fetch_entities_map(self) -> dict:
        """Obtiene el diccionario {entity_id: 'Nombre Entidad'} desde GLPI (/Entity)."""
        mapping = dict(DEFAULT_ENTITIES_MAP)
        try:
            url = f"{self.base_url}/Entity?range=0-200"
            headers = self.get_session_headers()
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200 and isinstance(res.json(), list):
                for ent in res.json():
                    if not isinstance(ent, dict):
                        continue
                    eid = ent.get("id")
                    if eid is None:
                        continue
                    raw_name = ent.get("name") or ent.get("completename") or ""
                    clean_name = self.clean_entity_name(raw_name)
                    if clean_name:
                        mapping[int(eid)] = clean_name
                GLPIClient._entities_fetched = True
        except Exception as e:
            print(f"[GLPI] Error consultando catálogo de entidades: {e}")
        self._entities_cache = mapping
        GLPIClient._shared_entities_cache = dict(mapping)
        return mapping

    def resolve_entity_name(self, raw_ticket: dict, entities_map: dict = None) -> str:
        """Resuelve el nombre real de la entidad GLPI (ej. SYCOM o Microfilms Center)."""
        lookup = entities_map if isinstance(entities_map, dict) and entities_map else self._entities_cache
        raw_entity = raw_ticket.get("entities_id")

        for field in ("entity_name", "entities_name", "completename"):
            val = raw_ticket.get(field)
            if val and isinstance(val, str):
                cleaned = self.clean_entity_name(val)
                if cleaned:
                    return cleaned

        if isinstance(raw_entity, str) and not raw_entity.strip().isdigit():
            cleaned = self.clean_entity_name(raw_entity)
            if cleaned:
                return cleaned

        try:
            eid = int(raw_entity) if raw_entity is not None else 1
        except (ValueError, TypeError):
            eid = 1

        title_and_content = f"{raw_ticket.get('name') or ''} {raw_ticket.get('content') or ''}".lower()
        if "microfilm" in title_and_content:
            return "Microfilms Center"

        if lookup and eid in lookup:
            return lookup[eid]
        if lookup and str(eid) in lookup:
            return lookup[str(eid)]
        return DEFAULT_ENTITIES_MAP.get(eid, f"Entidad #{eid}")

    def resolve_entity_id(self, client_name: str | None, entities_map: dict = None) -> int | None:
        """Resuelve el ID numérico de entidad en GLPI a partir del nombre del cliente/entidad."""
        if not client_name or not str(client_name).strip():
            return None
        clean_target = self.clean_entity_name(client_name)
        target_low = clean_target.lower()

        lookup = entities_map if isinstance(entities_map, dict) and entities_map else self._entities_cache
        for eid, ename in (lookup or {}).items():
            try:
                eid_int = int(eid)
            except (ValueError, TypeError):
                continue
            if eid_int == 0 and 1 in (lookup or {}):
                continue
            ename_low = str(ename).lower()
            if ename_low == target_low or ("microfilm" in target_low and "microfilm" in ename_low) or ("sycom" in target_low and "sycom" in ename_low):
                return eid_int

        if "microfilm" in target_low:
            return 2
        if "sycom" in target_low:
            return 1
        return None

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

    def normalize_ticket(self, raw_ticket: dict, assignees_map: dict = None, entities_map: dict = None) -> dict:
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

        # Vencimiento / Fecha límite nativa
        ttr_str = raw_ticket.get("time_to_resolve")
        if ttr_str:
            try:
                due_at = datetime.datetime.fromisoformat(ttr_str)
            except Exception:
                due_at = None
        else:
            due_at = None

        # Construir enlace directo
        base_host = self.base_url.replace("/apirest.php", "")
        ticket_url = f"{base_host}/front/ticket.form.php?id={ticket_id}"

        assignee = "Sin asignar"
        if assignees_map and ticket_id in assignees_map:
            assignee = assignees_map[ticket_id]

        entity_name = self.resolve_entity_name(raw_ticket, entities_map)

        return {
            "source": "GLPI",
            "external_id": ticket_id,
            "url": ticket_url,
            "title": raw_ticket.get("name") or f"Ticket #{ticket_id}",
            "category": "Soporte TI / Infraestructura",
            "client_name": entity_name,
            "project_name": entity_name,
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

    def assign_ticket(self, ticket_id: str, user_ids: list[int] | int) -> bool:
        """
        Asigna o reasigna uno o varios técnicos (user_ids) al ticket en GLPI.
        1. Consulta Ticket_User para ver si ya tiene técnicos asignados (type=2).
        2. Elimina las relaciones técnicas anteriores para evitar duplicados.
        3. Registra cada nuevo técnico en Ticket_User con type=2.
        4. Si el ticket estaba en 'Nuevo' (status=1), lo pasa a 'En curso (asignado)' (status=2).
        """
        try:
            headers = self.get_session_headers()
            tid = int(ticket_id)
            if isinstance(user_ids, (int, str)):
                uids = [int(user_ids)]
            else:
                uids = [int(u) for u in user_ids if u]
            
            # 1. Buscar y remover técnicos asignados previos
            res_tu = requests.get(f"{self.base_url}/Ticket/{tid}/Ticket_User", headers=headers, timeout=10)
            if res_tu.status_code == 200:
                for tu in res_tu.json():
                    if tu.get("type") == 2:
                        old_rel_id = tu.get("id")
                        requests.delete(f"{self.base_url}/Ticket/{tid}/Ticket_User/{old_rel_id}?force_purge=true", headers=headers, timeout=10)
            
            # 2. Registrar las nuevas relaciones de técnicos asignados
            success = True
            for uid in uids:
                payload = {
                    "input": {
                        "tickets_id": tid,
                        "users_id": uid,
                        "type": 2
                    }
                }
                res_add = requests.post(f"{self.base_url}/Ticket/{tid}/Ticket_User", headers=headers, json=payload, timeout=10)
                if res_add.status_code not in [200, 201]:
                    success = False
            
            # 3. Pasar a estado 'En curso' si estaba en 'Nuevo'
            requests.put(f"{self.base_url}/Ticket/{tid}", headers=headers, json={"input": {"id": tid, "status": 2}}, timeout=10)
            
            return success
        except Exception as e:
            print(f"[GLPIClient] Error asignando ticket {ticket_id} a usuarios {user_ids}: {e}")
            return False

    def update_ticket_status(self, ticket_id: str, new_status: str) -> bool:
        """
        Actualiza el estado del ticket en GLPI.
        new_status: 'pendiente', 'curso', 'espera', 'cerrado'
        GLPI status IDs:
        1: Nuevo (pendiente)
        2: En curso (asignado) (curso)
        4: En espera (espera)
        5: Resuelto (cerrado)
        """
        status_id_map = {
            "pendiente": 1,
            "curso": 2,
            "espera": 4,
            "cerrado": 5
        }
        target_id = status_id_map.get(new_status, 2)
        try:
            headers = self.get_session_headers()
            tid = int(ticket_id)
            payload = {"input": {"id": tid, "status": target_id}}
            res = requests.put(f"{self.base_url}/Ticket/{tid}", headers=headers, json=payload, timeout=10)
            return res.status_code in [200, 201]
        except Exception as e:
            print(f"[GLPIClient] Error actualizando estado de ticket {ticket_id} a {new_status}: {e}")
            return False

    def create_ticket(self, title: str, description: str = "", priority: str = "Media", due_at: datetime.datetime | None = None, user_ids: list[int] | None = None, client_name: str | None = None, entities_id: int | None = None) -> dict | None:
        """
        Crea un ticket en GLPI mediante la API REST.
        """
        prio_map = {
            "Baja": 2,
            "Media": 3,
            "Alta": 4,
            "Crítica": 5
        }
        glpi_prio = prio_map.get(priority, 3)
        input_data = {
            "name": title,
            "content": description or title,
            "priority": glpi_prio,
            "urgency": glpi_prio,
            "impact": glpi_prio,
            "status": 1  # 1: Nuevo
        }
        if due_at:
            input_data["time_to_resolve"] = due_at.strftime("%Y-%m-%d %H:%M:%S")

        resolved_eid = entities_id if entities_id is not None else self.resolve_entity_id(client_name)
        if resolved_eid is not None:
            input_data["entities_id"] = int(resolved_eid)

        try:
            headers = self.get_session_headers()
            url = f"{self.base_url}/Ticket"
            res = requests.post(url, headers=headers, json={"input": input_data}, timeout=12)
            if res.status_code == 401:
                self.init_session()
                headers = self.get_session_headers()
                res = requests.post(url, headers=headers, json={"input": input_data}, timeout=12)
            if res.status_code in (400, 403) and "entities_id" in input_data:
                fallback_input = {k: v for k, v in input_data.items() if k != "entities_id"}
                res = requests.post(url, headers=headers, json={"input": fallback_input}, timeout=12)
            
            res.raise_for_status()
            data = res.json()
            new_id = data.get("id")
            if not new_id:
                return None
            
            # Asignar técnicos si se indicaron
            if user_ids:
                self.assign_ticket(str(new_id), user_ids)
            
            base_web = self.base_url.replace("/apirest.php", "")
            ticket_url = f"{base_web}/front/ticket.form.php?id={new_id}"
            
            return {
                "id": str(new_id),
                "url": ticket_url,
                "message": data.get("message")
            }
        except Exception as e:
            print(f"[GLPIClient] Error creando ticket '{title}': {e}")
            if hasattr(e, "response") and e.response is not None:
                print(f"[GLPIClient Response] {e.response.text}")
            return None

    def delete_ticket(self, ticket_id: str) -> bool:
        """
        Elimina o purga el ticket en GLPI mediante su API REST.
        """
        try:
            headers = self.get_session_headers()
            tid = int(ticket_id)
            res = requests.delete(f"{self.base_url}/Ticket/{tid}?force_purge=true", headers=headers, timeout=12)
            if res.status_code in [200, 201, 204]:
                return True
            res2 = requests.delete(f"{self.base_url}/Ticket/{tid}", headers=headers, timeout=12)
            return res2.status_code in [200, 201, 204]
        except Exception as e:
            print(f"[GLPIClient] Error eliminando ticket {ticket_id}: {e}")
            return False



