import os
import sys
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, os.path.abspath('backend'))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.models import Item, EscalationDocument, User
from app.main import app, VALID_ESCALATION_CATEGORIES, ensure_db_schema

TEST_DB_URL = "sqlite:///./test_temp.db"
test_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

class TestEscalationFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=test_engine)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        test_engine.dispose()
        Base.metadata.drop_all(bind=test_engine)
        if os.path.exists("./test_temp.db"):
            try:
                os.remove("./test_temp.db")
            except Exception:
                pass

    def setUp(self):
        db = TestingSessionLocal()
        db.query(Item).delete()
        db.query(EscalationDocument).delete()
        db.commit()
        db.close()

    def test_01_categories_validation(self):
        expected_cats = ["CPU", "RAM", "Disco", "Antivirus", "PostgreSQL", "Servicios"]
        self.assertEqual(VALID_ESCALATION_CATEGORIES, expected_cats)

        # Test creating escalation with each category
        for cat in expected_cats:
            res = self.client.post("/api/escalations", json={
                "title": f"Escalamiento {cat}",
                "category": cat,
                "doc_url": f"https://docs.google.com/document/d/{cat}",
                "notes": f"Notas para {cat}",
                "recommendations": f"Recs iniciales para {cat}"
            }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
            self.assertEqual(res.status_code, 200)
            data = res.json()["document"]
            self.assertEqual(data["category"], cat)
            self.assertEqual(data["recommendations"], f"Recs iniciales para {cat}")

        # Test fallback category
        res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Categoria Invalida",
            "category": "CualquierCosa",
            "doc_url": "https://docs.google.com/document/d/xyz"
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["document"]["category"], "Servicios")

    def test_02_update_escalation(self):
        # Create an escalation
        res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Base",
            "category": "RAM",
            "doc_url": "https://docs.google.com/document/d/base",
            "notes": "Notas base",
            "recommendations": "Recs base"
        })
        esc_id = res.json()["document"]["id"]

        # Update category, title, notes, recommendations, url
        put_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "title": "Escalamiento Editado",
            "category": "PostgreSQL",
            "doc_url": "https://docs.google.com/document/d/edited",
            "notes": "Notas actualizadas",
            "recommendations": "Recs actualizadas"
        })
        self.assertEqual(put_res.status_code, 200)
        updated = put_res.json()["document"]
        self.assertEqual(updated["title"], "Escalamiento Editado")
        self.assertEqual(updated["category"], "PostgreSQL")

        # Verify through GET
        get_res = self.client.get("/api/escalations")
        docs = get_res.json()
        doc = next(d for d in docs if d["id"] == esc_id)
        self.assertEqual(doc["title"], "Escalamiento Editado")
        self.assertEqual(doc["category"], "PostgreSQL")
        self.assertEqual(doc["recommendations"], "Recs actualizadas")

    @patch("app.sync.clickup_client.ClickUpClient.create_task")
    def test_03_create_item_with_escalation_link_and_description(self, mock_cu):
        mock_cu.return_value = {"id": "cu123", "url": "https://app.clickup.com/t/cu123"}

        # Create escalation first
        esc_res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Para Item",
            "category": "CPU",
            "doc_url": "https://docs.google.com/document/d/item1"
        })
        esc_id = esc_res.json()["document"]["id"]

        # Create item linked to escalation with description
        item_res = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Actividad vinculada a CPU",
            "description": "Detalle técnico de la actividad",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(item_res.status_code, 200)
        item_data = item_res.json()["item"]
        self.assertEqual(item_data["escalation_id"], esc_id)

        # Verify through database and GET /api/items
        db = TestingSessionLocal()
        item_db = db.query(Item).filter(Item.id == item_data["id"]).first()
        self.assertEqual(item_db.description, "Detalle técnico de la actividad")
        self.assertEqual(item_db.escalation_id, esc_id)
        db.close()

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_04_close_item_with_recommendations_and_escalation(self, mock_cu):
        mock_cu.return_value = True

        db = TestingSessionLocal()
        esc = EscalationDocument(
            title="Escalamiento Cierre",
            category="Disco",
            doc_url="https://docs.google.com/document/d/disco1",
            recommendations=None
        )
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu999",
            title="Optimizar discos del servidor",
            status="curso",
            escalation_id=esc_id,
            assignee_name="Juan Fernández"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Close item as assigned specialist with recommendations (using URL-encoded header for non-ascii name)
        spec_name_encoded = urllib.parse.quote("Juan Fernández")
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Se liberaron 50GB purgando logs antiguos.",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "especialista", "X-User-Name": spec_name_encoded})
        self.assertEqual(close_res.status_code, 200)

        # Check item has recommendations
        db = TestingSessionLocal()
        item_db = db.query(Item).filter(Item.id == item_id).first()
        self.assertEqual(item_db.status, "cerrado")
        self.assertEqual(item_db.recommendations, "Se liberaron 50GB purgando logs antiguos.")

        # Check escalation document has recommendations appended
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNotNone(esc_db.recommendations)
        self.assertIn("Se liberaron 50GB purgando logs antiguos.", esc_db.recommendations)
        self.assertIn("ClickUp #cu999", esc_db.recommendations)

        # Close again: should not create duplicate
        close_again = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Se liberaron 50GB purgando logs antiguos.",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "especialista", "X-User-Name": spec_name_encoded})
        self.assertEqual(close_again.status_code, 200)
        db.refresh(esc_db)
        # Verify it occurs only once
        self.assertEqual(esc_db.recommendations.count("Se liberaron 50GB purgando logs antiguos."), 1)
        db.close()

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_05_close_item_unlinking_or_changing_escalation(self, mock_cu):
        mock_cu.return_value = True

        db = TestingSessionLocal()
        esc1 = EscalationDocument(title="Escalamiento 1", category="RAM", doc_url="https://docs.google.com/1")
        esc2 = EscalationDocument(title="Escalamiento 2", category="PostgreSQL", doc_url="https://docs.google.com/2")
        db.add_all([esc1, esc2])
        db.commit()
        esc1_id, esc2_id = esc1.id, esc2.id

        # Item initially linked to esc1
        item = Item(source="ClickUp", external_id="cu888", title="Tarea BD", status="curso", escalation_id=esc1_id, assignee_name="Adonis Lozada")
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Close item, changing escalation to esc2 as coordinator
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Recs para esc2",
            "escalation_id": esc2_id
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(close_res.status_code, 200)

        db = TestingSessionLocal()
        esc1_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc1_id).first()
        esc2_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc2_id).first()
        item_db = db.query(Item).filter(Item.id == item_id).first()

        self.assertEqual(item_db.escalation_id, esc2_id)
        self.assertIsNone(esc1_db.recommendations)
        self.assertIsNotNone(esc2_db.recommendations)
        self.assertIn("Recs para esc2", esc2_db.recommendations)
        db.close()

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_06_close_item_unlinking_to_none(self, mock_cu):
        """EDGE CASE: Item was linked to esc1, but user closes it with escalation_id=0 (unlinked)."""
        mock_cu.return_value = True

        db = TestingSessionLocal()
        esc1 = EscalationDocument(title="Escalamiento Unlink", category="Antivirus", doc_url="https://docs.google.com/av")
        db.add(esc1)
        db.commit()
        esc1_id = esc1.id

        item = Item(source="ClickUp", external_id="cu777", title="Tarea AV", status="curso", escalation_id=esc1_id, assignee_name="Adonis Lozada")
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # User unlinks escalation upon closing: sends escalation_id=0
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Recomendacion suelta sin escalamiento",
            "escalation_id": 0
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(close_res.status_code, 200)

        db = TestingSessionLocal()
        item_db = db.query(Item).filter(Item.id == item_id).first()
        esc1_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc1_id).first()

        self.assertIsNone(item_db.escalation_id)
        self.assertIsNone(esc1_db.recommendations)
        db.close()

    def test_07_specialist_and_coordinator_edition_via_patch(self):
        """Test that specialists can link/unlink escalation and write recommendations via PATCH /api/items/{id}."""
        db = TestingSessionLocal()
        esc = EscalationDocument(title="Escalamiento Parche", category="Servicios", doc_url="https://docs.google.com/serv")
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu666",
            title="Tarea Parche Especialista",
            status="curso",
            assignee_name="Yoxaide Jiménez",
            priority="Media"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        spec_header = {
            "X-User-Role": "especialista",
            "X-User-Name": urllib.parse.quote("Yoxaide Jiménez")
        }

        # Specialist links item to escalation and adds recommendations
        patch_res = self.client.patch(f"/api/items/{item_id}", json={
            "escalation_id": esc_id,
            "recommendations": "Recomendación preventiva añadida por Yoxaide"
        }, headers=spec_header)
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["item"]["escalation_id"], esc_id)

        # Specialist attempts to modify priority -> 403 Forbidden
        denied_res = self.client.patch(f"/api/items/{item_id}", json={
            "priority": "Crítica"
        }, headers=spec_header)
        self.assertEqual(denied_res.status_code, 403)

        # Non-assigned specialist attempts to edit -> 403 Forbidden
        other_spec = {
            "X-User-Role": "especialista",
            "X-User-Name": "Luis Acosta"
        }
        denied_res2 = self.client.patch(f"/api/items/{item_id}", json={
            "recommendations": "Intrusión"
        }, headers=other_spec)
        self.assertEqual(denied_res2.status_code, 403)

        # Coordinator can modify anything
        coord_res = self.client.patch(f"/api/items/{item_id}", json={
            "priority": "Alta",
            "description": "Nueva descripcion por coordinador"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(coord_res.status_code, 200)
        self.assertEqual(coord_res.json()["item"]["priority"], "Alta")

        # Verify escalation recommendations were updated
        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIn("Recomendación preventiva añadida por Yoxaide", esc_db.recommendations)
        db.close()

if __name__ == "__main__":
    unittest.main()
