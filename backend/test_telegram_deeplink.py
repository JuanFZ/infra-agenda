import os
import sys
import datetime
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath('backend'))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Item, AlertLog, User
from app import config
from app.notifications.telegram_notifier import (
    get_agenda_deep_link,
    check_and_notify_alerts,
    resolve_assignee_mention,
    send_telegram_message
)
from app.main import app

test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class TestTelegramDeepLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=test_engine)
        cls.orig_override = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        if cls.orig_override:
            app.dependency_overrides[get_db] = cls.orig_override
        else:
            app.dependency_overrides.pop(get_db, None)
        test_engine.dispose()

    def setUp(self):
        self.orig_base_url = config.AGENDA_BASE_URL
        self.orig_bot_token = config.TELEGRAM_BOT_TOKEN
        self.orig_chat_id = config.TELEGRAM_CHAT_ID

        config.AGENDA_BASE_URL = "http://localhost:8000"
        config.TELEGRAM_BOT_TOKEN = "123456:FAKE_TOKEN_FOR_TESTS"
        config.TELEGRAM_CHAT_ID = "-100123456789"

        db = TestingSessionLocal()
        try:
            db.query(AlertLog).delete()
            db.query(Item).delete()
            db.query(User).delete()

            # Coordinador para menciones
            coord = User(
                username="a.lozada",
                full_name="Adonis Lozada",
                email="a.lozada@sycom.com.ve",
                telegram_username="@adonislozada",
                role="coordinador",
                is_active=1
            )
            # Especialista
            spec = User(
                username="j.fernandez",
                full_name="Juan Fernández",
                email="j.fernandez@sycom.com.ve",
                telegram_username="@Juuan8",
                role="especialista",
                is_active=1
            )
            db.add(coord)
            db.add(spec)
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        config.AGENDA_BASE_URL = self.orig_base_url
        config.TELEGRAM_BOT_TOKEN = self.orig_bot_token
        config.TELEGRAM_CHAT_ID = self.orig_chat_id

    def test_01_get_agenda_deep_link_defaults(self):
        link = get_agenda_deep_link(42)
        self.assertEqual(link, "http://localhost:8000/?view=kanban&item_id=42")

    def test_02_get_agenda_deep_link_custom_view(self):
        link = get_agenda_deep_link(42, view="resumen")
        self.assertEqual(link, "http://localhost:8000/?view=resumen&item_id=42")

    def test_03_get_agenda_deep_link_item_instance(self):
        mock_item = MagicMock()
        mock_item.id = 101
        link = get_agenda_deep_link(mock_item)
        self.assertEqual(link, "http://localhost:8000/?view=kanban&item_id=101")

    def test_04_get_agenda_deep_link_custom_base_url_and_strip_slash(self):
        config.AGENDA_BASE_URL = "http://200.74.215.44:7070/"
        link = get_agenda_deep_link(55)
        self.assertEqual(link, "http://200.74.215.44:7070/?view=kanban&item_id=55")

    def test_04b_get_agenda_deep_link_none_item(self):
        link = get_agenda_deep_link(None, view="kanban")
        self.assertEqual(link, "http://localhost:8000/?view=kanban")

    def test_04c_get_agenda_deep_link_whitespace_base_url(self):
        config.AGENDA_BASE_URL = "   https://agenda.sycom.com.ve/   "
        link = get_agenda_deep_link(99)
        self.assertEqual(link, "https://agenda.sycom.com.ve/?view=kanban&item_id=99")

    @patch("app.notifications.telegram_notifier.send_telegram_message")
    def test_05_check_and_notify_nuevo_uses_agenda_link(self, mock_send):
        mock_send.return_value = True
        db = TestingSessionLocal()
        try:
            now = datetime.datetime.utcnow()
            item = Item(
                external_id="GLPI-9901",
                source="GLPI",
                title="Falla en Servidor de Aplicaciones",
                client_name="Banco Digital",
                priority="Alta",
                status="pendiente",
                created_at=now - datetime.timedelta(minutes=5),
                due_at=now + datetime.timedelta(hours=4)
            )
            db.add(item)
            db.commit()
            db.refresh(item)

            check_and_notify_alerts(db)

            self.assertTrue(mock_send.called)
            args, kwargs = mock_send.call_args
            msg_text = args[0]
            parse_mode = kwargs.get("parse_mode")

            self.assertEqual(parse_mode, "HTML")
            self.assertIn("<b>Abrir Agenda</b>", msg_text)
            self.assertIn(f"http://localhost:8000/?view=kanban&item_id={item.id}", msg_text)
            self.assertNotIn("Abrir en GLPI", msg_text)
            self.assertNotIn("Abrir en ClickUp", msg_text)

            # Verificar que se registró en AlertLog
            log = db.query(AlertLog).filter(AlertLog.item_id == item.id, AlertLog.semaphore == "nuevo").first()
            self.assertIsNotNone(log)
        finally:
            db.close()

    @patch("app.notifications.telegram_notifier.send_telegram_message")
    def test_06_check_and_notify_amarillo_uses_agenda_link(self, mock_send):
        mock_send.return_value = True
        db = TestingSessionLocal()
        try:
            now = datetime.datetime.utcnow()
            # Total 100 min, elapsed 80 min -> 80% SLA -> amarillo
            item = Item(
                external_id="CU-8821",
                source="ClickUp",
                title="Migración de Base de Datos PostgreSQL",
                client_name="Sycom Core",
                priority="Crítica",
                status="curso",
                created_at=now - datetime.timedelta(minutes=80),
                due_at=now + datetime.timedelta(minutes=20)
            )
            db.add(item)
            db.commit()
            db.refresh(item)

            check_and_notify_alerts(db)

            self.assertTrue(mock_send.called)
            args, kwargs = mock_send.call_args
            msg_text = args[0]

            self.assertIn("<b>Abrir Agenda</b>", msg_text)
            self.assertIn(f"http://localhost:8000/?view=kanban&item_id={item.id}", msg_text)
            self.assertNotIn("Abrir en ClickUp", msg_text)
            self.assertNotIn("Abrir en GLPI", msg_text)
        finally:
            db.close()

    @patch("app.notifications.telegram_notifier.send_telegram_message")
    def test_07_check_and_notify_rojo_uses_agenda_link(self, mock_send):
        mock_send.return_value = True
        db = TestingSessionLocal()
        try:
            now = datetime.datetime.utcnow()
            # Total 100 min, elapsed 120 min -> 120% SLA -> rojo
            item = Item(
                external_id="CU-8899",
                source="ClickUp",
                title="Caída de enlace VPN",
                client_name="Cliente Externo",
                priority="Crítica",
                status="curso",
                created_at=now - datetime.timedelta(minutes=120),
                due_at=now - datetime.timedelta(minutes=20)
            )
            db.add(item)
            db.commit()
            db.refresh(item)

            check_and_notify_alerts(db)

            self.assertTrue(mock_send.called)
            args, kwargs = mock_send.call_args
            msg_text = args[0]

            self.assertIn("<b>Abrir Agenda</b>", msg_text)
            self.assertIn(f"http://localhost:8000/?view=kanban&item_id={item.id}", msg_text)
            self.assertNotIn("Abrir en ClickUp", msg_text)
            self.assertNotIn("Abrir en GLPI", msg_text)
        finally:
            db.close()

    @patch("app.notifications.telegram_notifier.send_telegram_message")
    def test_08_create_item_telegram_notification_link(self, mock_send):
        mock_send.return_value = True

        headers = {
            "x-user-role": "coordinador",
            "x-user-name": "Adonis Lozada"
        }
        payload = {
            "title": "Actividad de prueba para deep link",
            "source": "ClickUp",
            "client_name": "Banco Mercantil",
            "priority": "Alta",
            "sla_hours": 4,
            "description": "Verificando enlace directo a la agenda",
            "url": "https://app.clickup.com/t/99999",
            "assignee_name": "Juan Fernández"
        }

        res = self.client.post("/api/items", json=payload, headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        item_id = data["item"]["id"]

        self.assertTrue(mock_send.called)
        args, kwargs = mock_send.call_args
        msg_text = args[0]

        self.assertIn("<b>Abrir Agenda</b>", msg_text)
        self.assertIn(f"http://localhost:8000/?view=kanban&item_id={item_id}", msg_text)
        self.assertNotIn("Abrir en ClickUp", msg_text)
        self.assertNotIn("Abrir en GLPI", msg_text)

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status", return_value=True)
    @patch("app.notifications.telegram_notifier.send_telegram_message")
    def test_09_status_change_telegram_notification_link(self, mock_send, mock_update_task):
        mock_send.return_value = True
        db = TestingSessionLocal()
        try:
            item = Item(
                external_id="CU-3344",
                source="ClickUp",
                title="Mantenimiento preventivo",
                client_name="Interno",
                priority="Media",
                status="pendiente"
            )
            db.add(item)
            db.commit()
            db.refresh(item)
            item_id = item.id
        finally:
            db.close()

        headers = {
            "x-user-role": "coordinador",
            "x-user-name": "Adonis Lozada"
        }
        res = self.client.post(f"/api/items/{item_id}/status", json={"status": "curso"}, headers=headers)
        self.assertEqual(res.status_code, 200)

        self.assertTrue(mock_send.called)
        args, kwargs = mock_send.call_args
        msg_text = args[0]

        self.assertIn("<b>Abrir Agenda</b>", msg_text)
        self.assertIn(f"http://localhost:8000/?view=kanban&item_id={item_id}", msg_text)

    @patch("app.sync.clickup_client.ClickUpClient.assign_task", return_value=True)
    @patch("app.notifications.telegram_notifier.send_telegram_message")
    def test_10_assign_item_telegram_notification_link(self, mock_send, mock_assign_task):
        mock_send.return_value = True
        db = TestingSessionLocal()
        try:
            item = Item(
                external_id="CU-7711",
                source="ClickUp",
                title="Revisión de logs",
                client_name="Sycom",
                priority="Baja",
                status="pendiente"
            )
            db.add(item)
            db.commit()
            db.refresh(item)
            item_id = item.id
        finally:
            db.close()

        headers = {
            "x-user-role": "coordinador",
            "x-user-name": "Adonis Lozada"
        }
        res = self.client.post(
            f"/api/items/{item_id}/assign",
            json={"specialist_keys": ["juan"]},
            headers=headers
        )
        self.assertEqual(res.status_code, 200)

        self.assertTrue(mock_send.called)
        args, kwargs = mock_send.call_args
        msg_text = args[0]

        self.assertIn("<b>Abrir Agenda</b>", msg_text)
        self.assertIn(f"http://localhost:8000/?view=kanban&item_id={item_id}", msg_text)

    def test_11_get_single_item_by_pk_and_external_id(self):
        db = TestingSessionLocal()
        try:
            item = Item(
                external_id="CU-DETAIL-99",
                source="ClickUp",
                title="Detalle de actividad",
                client_name="Sycom",
                priority="Media",
                status="pendiente"
            )
            db.add(item)
            db.commit()
            db.refresh(item)
            item_id = item.id
        finally:
            db.close()

        # Fetch by PK
        res_pk = self.client.get(f"/api/items/{item_id}")
        self.assertEqual(res_pk.status_code, 200)
        data_pk = res_pk.json()
        self.assertEqual(data_pk["id"], item_id)
        self.assertEqual(data_pk["external_id"], "CU-DETAIL-99")

        # Fetch by external_id
        res_ext = self.client.get("/api/items/CU-DETAIL-99")
        self.assertEqual(res_ext.status_code, 200)
        data_ext = res_ext.json()
        self.assertEqual(data_ext["id"], item_id)

    def test_12_get_single_item_closed_past_yesterday(self):
        """Verifica que un item cerrado antiguo (excluido de GET /api/items) pueda abrirse por deep link."""
        db = TestingSessionLocal()
        try:
            old_date = datetime.datetime.utcnow() - datetime.timedelta(days=15)
            item = Item(
                external_id="CU-OLD-ARCHIVED",
                source="ClickUp",
                title="Actividad cerrada hace dos semanas",
                client_name="Sycom",
                priority="Baja",
                status="cerrado",
                created_at=old_date - datetime.timedelta(days=1),
                closed_at=old_date
            )
            db.add(item)
            db.commit()
            db.refresh(item)
            item_id = item.id
        finally:
            db.close()

        # Verify it is omitted from default GET /api/items
        list_res = self.client.get("/api/items")
        self.assertEqual(list_res.status_code, 200)
        item_ids_in_list = [x["id"] for x in list_res.json()]
        self.assertNotIn(item_id, item_ids_in_list)

        # But CAN be retrieved directly via GET /api/items/{id} for deep linking
        detail_res = self.client.get(f"/api/items/{item_id}")
        self.assertEqual(detail_res.status_code, 200)
        self.assertEqual(detail_res.json()["title"], "Actividad cerrada hace dos semanas")

    def test_13_get_single_item_not_found(self):
        res = self.client.get("/api/items/99999999")
        self.assertEqual(res.status_code, 404)

    def test_14_get_single_item_permissions_specialist(self):
        db = TestingSessionLocal()
        try:
            item_private = Item(
                external_id="CU-OTHER-SPECIALIST",
                source="ClickUp",
                title="Actividad confidencial de otro especialista",
                assignee_name="Pedro Pérez",
                client_name="Sycom",
                priority="Media",
                status="curso"
            )
            db.add(item_private)
            db.commit()
            db.refresh(item_private)
            priv_id = item_private.id
        finally:
            db.close()

        # Juan Fernández requests Pedro's item -> 403 Forbidden
        import urllib.parse
        headers_juan = {
            "x-user-role": "especialista",
            "x-user-name": urllib.parse.quote("Juan Fernández")
        }
        res_denied = self.client.get(f"/api/items/{priv_id}", headers=headers_juan)
        self.assertEqual(res_denied.status_code, 403)

        # Coordinator requests Pedro's item -> 200 OK
        headers_coord = {
            "x-user-role": "coordinador",
            "x-user-name": urllib.parse.quote("Adonis Lozada")
        }
        res_allowed = self.client.get(f"/api/items/{priv_id}", headers=headers_coord)
        self.assertEqual(res_allowed.status_code, 200)


if __name__ == "__main__":
    unittest.main()
