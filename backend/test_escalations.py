import os
import sys
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, os.path.abspath('backend'))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Item, EscalationDocument, User, IBP
from app.main import app, VALID_ESCALATION_CATEGORIES, ensure_db_schema, IBP_INVENTORY, normalize_ibp, hash_password

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

app.dependency_overrides[get_db] = override_get_db

class TestEscalationFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=test_engine)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        test_engine.dispose()

    def setUp(self):
        db = TestingSessionLocal()
        try:
            db.query(Item).delete()
            db.query(EscalationDocument).delete()
            db.commit()
        finally:
            db.close()

    def test_01_categories_validation(self):
        expected_cats = ["CPU", "RAM", "Disco", "Antivirus", "PostgreSQL", "Servicios", "Infraestructura"]
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

        # Test fallback category for invalid input
        res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Categoria Invalida",
            "category": "CualquierCosa",
            "doc_url": "https://docs.google.com/document/d/xyz"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["document"]["category"], "Servicios")

        # Test legacy category normalization ("Servicios BD" -> "Servicios", "Discos" -> "Disco")
        res_serv = self.client.post("/api/escalations", json={
            "title": "Escalamiento Legacy Servicios",
            "category": "Servicios BD",
            "doc_url": "https://docs.google.com/document/d/serv1"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(res_serv.status_code, 200)
        self.assertEqual(res_serv.json()["document"]["category"], "Servicios")

        res_disc = self.client.post("/api/escalations", json={
            "title": "Escalamiento Legacy Disco",
            "category": "Discos",
            "doc_url": "https://docs.google.com/document/d/disc1"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(res_disc.status_code, 200)
        self.assertEqual(res_disc.json()["document"]["category"], "Disco")

    def test_02_update_escalation(self):
        # Create an escalation
        res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Base",
            "category": "RAM",
            "doc_url": "https://docs.google.com/document/d/base",
            "notes": "Notas base",
            "recommendations": "Recs base"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        esc_id = res.json()["document"]["id"]

        # Update category, title, notes, recommendations, url
        put_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "title": "Escalamiento Editado",
            "category": "PostgreSQL",
            "doc_url": "https://docs.google.com/document/d/edited",
            "notes": "Notas actualizadas",
            "recommendations": "Recs actualizadas"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(put_res.status_code, 200)
        updated = put_res.json()["document"]
        self.assertEqual(updated["title"], "Escalamiento Editado")
        self.assertEqual(updated["category"], "PostgreSQL")

        # Verify through GET
        get_res = self.client.get(
            "/api/escalations",
            headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"}
        )
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
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
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
        esc1 = EscalationDocument(title="Escalamiento Unlink", category="Servicios", doc_url="https://docs.google.com/av")
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

    def test_08_ibp_catalog_and_escalation_linkage(self):
        """Test IBP catalog management and linking escalations to IBPs."""
        db = TestingSessionLocal()
        # Ensure at least standard IBPs exist in test db
        if db.query(IBP).count() == 0:
            db.add_all([
                IBP(name="Banesco", code="BANESCO", description="Instancia Banesco"),
                IBP(name="Banco Mercantil", code="MERCANTIL", description="Instancia Mercantil")
            ])
            db.commit()
        db.close()

        # 1. GET /api/ibps
        get_ibps_res = self.client.get(
            "/api/ibps",
            headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"}
        )
        self.assertEqual(get_ibps_res.status_code, 200)
        ibps = get_ibps_res.json()
        self.assertGreater(len(ibps), 0)
        banesco = next((i for i in ibps if i.get("code") in ("0134", "BANESCO") or "BANESCO" in (i.get("name") or "").upper()), None)
        self.assertIsNotNone(banesco)

        # 2. POST /api/ibps - Coordinator creates new IBP
        create_ibp_res = self.client.post("/api/ibps", json={
            "name": "Banco Plaza",
            "code": "PLAZA",
            "description": "Instancia Banco Plaza C.A."
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(create_ibp_res.status_code, 200)
        created_ibp = create_ibp_res.json()["ibp"]
        self.assertEqual(created_ibp["name"], "Banco Plaza")
        self.assertEqual(created_ibp["code"], "PLAZA")
        plaza_id = created_ibp["id"]

        # Duplicate check -> 400
        dup_res = self.client.post("/api/ibps", json={
            "name": "Banco Plaza"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(dup_res.status_code, 400)

        # 3. Create Escalation linked to IBP
        esc_res = self.client.post("/api/escalations", json={
            "title": "SIMF-ES-8899 Banco Plaza Escalation",
            "category": "PostgreSQL",
            "ibp_id": plaza_id,
            "doc_url": "https://docs.google.com/document/d/plaza8899",
            "notes": "Escalamiento en servidor srvbdplaza",
            "recommendations": "Revisar max_connections"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(esc_res.status_code, 200)
        esc_doc = esc_res.json()["document"]
        self.assertEqual(esc_doc["ibp_id"], plaza_id)
        self.assertEqual(esc_doc["ibp_name"], "Banco Plaza")
        self.assertEqual(esc_doc["ibp_code"], "PLAZA")
        esc_id = esc_doc["id"]

        # 4. Verify in GET /api/escalations
        list_res = self.client.get(
            "/api/escalations",
            headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"}
        )
        self.assertEqual(list_res.status_code, 200)
        found = next(d for d in list_res.json() if d["id"] == esc_id)
        self.assertEqual(found["ibp_id"], plaza_id)
        self.assertEqual(found["ibp_name"], "Banco Plaza")

        # 5. Update Escalation changing IBP to Banesco
        banesco_id = banesco["id"]
        put_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp_id": banesco_id,
            "category": "CPU"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(put_res.status_code, 200)
        updated_doc = put_res.json()["document"]
        self.assertEqual(updated_doc["ibp_id"], banesco_id)
        self.assertEqual(updated_doc["ibp_name"], banesco["name"])
        self.assertIn("BANESCO", updated_doc["ibp_name"].upper())
        self.assertEqual(updated_doc["category"], "CPU")

        # 6. Unlink IBP (set ibp_id = 0)
        unlink_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp_id": 0
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(unlink_res.status_code, 200)
        unlinked_doc = unlink_res.json()["document"]
        self.assertIsNone(unlinked_doc["ibp_id"])
        self.assertIsNone(unlinked_doc["ibp_name"])

    @patch("app.sync.clickup_client.ClickUpClient.create_task")
    @patch("app.sync.glpi_client.GLPIClient.create_ticket")
    def test_09_create_item_with_platform_subprojects(self, mock_glpi, mock_cu):
        """Test creating activities with project selector (SIMF, SGLPAR) for ClickUp and (Sycom, MicroFilms Center) for GLPI."""
        mock_cu.return_value = {"id": "cu_sub1", "url": "https://app.clickup.com/t/cu_sub1"}
        mock_glpi.return_value = {"id": "glpi_sub2", "url": "https://glpi.sycom.com.ve/ticket/glpi_sub2"}

        # 1. ClickUp with SIMF
        cu_res1 = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Optimización SIMF motor de pagos",
            "project_name": "SIMF"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(cu_res1.status_code, 200)
        item1 = cu_res1.json()["item"]
        self.assertEqual(item1["client_name"], "SIMF")
        self.assertEqual(item1["project_name"], "SIMF")

        # 2. ClickUp with SGLPAR
        mock_cu.return_value = {"id": "cu_sub2", "url": "https://app.clickup.com/t/cu_sub2"}
        cu_res2 = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Ajuste SGLPAR conciliaciones",
            "project_name": "SGLPAR"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(cu_res2.status_code, 200)
        item2 = cu_res2.json()["item"]
        self.assertEqual(item2["client_name"], "SGLPAR")

        # 3. GLPI with Sycom
        glpi_res1 = self.client.post("/api/items", json={
            "source": "GLPI",
            "title": "Ticket Soporte Servidores Sycom",
            "client_name": "Sycom"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(glpi_res1.status_code, 200)
        item3 = glpi_res1.json()["item"]
        self.assertEqual(item3["client_name"], "Sycom")

        # 4. GLPI with MicroFilms Center
        mock_glpi.return_value = {"id": "glpi_sub4", "url": "https://glpi.sycom.com.ve/ticket/glpi_sub4"}
        glpi_res2 = self.client.post("/api/items", json={
            "source": "GLPI",
            "title": "Ticket Escaneo MicroFilms Center",
            "client_name": "MicroFilms Center"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(glpi_res2.status_code, 200)
        item4 = glpi_res2.json()["item"]
        self.assertEqual(item4["client_name"], "MicroFilms Center")

        # 5. Coordinator updates project_name via PATCH
        patch_res = self.client.patch(f"/api/items/{item1['id']}", json={
            "project_name": "SIMF-Core"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["item"]["project_name"], "SIMF-Core")
        self.assertEqual(patch_res.json()["item"]["client_name"], "SIMF-Core")

        # 6. Verify GET /api/items exposes project_name and client_name
        get_items_res = self.client.get(
            "/api/items",
            headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"}
        )
        self.assertEqual(get_items_res.status_code, 200)
        all_it = get_items_res.json()
        f_it1 = next(i for i in all_it if i["id"] == item1["id"])
        self.assertEqual(f_it1["project_name"], "SIMF-Core")
        self.assertEqual(f_it1["client_name"], "SIMF-Core")

    @patch("app.sync.clickup_client.ClickUpClient.create_task")
    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_10_edge_cases_and_robustness(self, mock_cu_status, mock_cu_create):
        """Test edge cases: non-existent ibp_id, whitespace recommendations, default subprojects."""
        mock_cu_create.return_value = {"id": "cu_edge1", "url": "https://app.clickup.com/t/cu_edge1"}
        mock_cu_status.return_value = True

        # Non-existent IBP ID
        esc_res = self.client.post("/api/escalations", json={
            "title": "SIMF-ES-9999 Invalid IBP",
            "category": "Procesos",
            "ibp_id": 999999,
            "doc_url": "https://docs.google.com/document/d/edge1"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(esc_res.status_code, 200)
        self.assertIsNone(esc_res.json()["document"]["ibp_id"])
        esc_id = esc_res.json()["document"]["id"]

        # Default subprojects when none provided
        item_def_res = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Tarea sin proyecto explícito",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(item_def_res.status_code, 200)
        self.assertEqual(item_def_res.json()["item"]["project_name"], "SIMF")
        item_id = item_def_res.json()["item"]["id"]

        # Close with whitespace recommendations -> should not append empty string
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "   \n\t   ",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(close_res.status_code, 200)

        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNone(esc_db.recommendations)

        # Close again with real recommendations -> appends properly
        close_res2 = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Solución técnica aplicada correctamente.",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(close_res2.status_code, 200)

        db.refresh(esc_db)
        self.assertIsNotNone(esc_db.recommendations)
        self.assertIn("Solución técnica aplicada correctamente.", esc_db.recommendations)
        db.close()

    def test_11_ibp_string_persistence_and_official_catalog(self):
        """Test official 34 IBP catalog, string normalization, and direct database persistence on EscalationDocument."""
        # 1. Verify 34 official IBPs in inventory
        self.assertEqual(len(IBP_INVENTORY), 34)
        expected_codes = [
            "0000", "0001", "0102", "0104", "0105", "0108", "0114", "0115",
            "0128", "0134", "0137", "0138", "0145", "0146", "0151", "0152",
            "0156", "0157", "0163", "0166", "0168", "0169", "0171", "0172",
            "0173", "0174", "0175", "0177", "0178", "0191", "0601", "0882",
            "0883", "2017"
        ]
        actual_codes = [item["code"] for item in IBP_INVENTORY]
        self.assertEqual(actual_codes, expected_codes)

        # 2. Test normalization helper across all codes, aliases, accents and edge cases
        self.assertEqual(normalize_ibp("0134 - BANESCO"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("0134"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("134"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("1"), "0001 - BCV")
        self.assertEqual(normalize_ibp("BANESCO"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("banesco"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("0102"), "0102 - BDV")
        self.assertEqual(normalize_ibp("BDV"), "0102 - BDV")
        self.assertEqual(normalize_ibp("Banco de Venezuela"), "0102 - BDV")
        self.assertEqual(normalize_ibp("Venezuela"), "0102 - BDV")
        self.assertEqual(normalize_ibp("MERCANTIL"), "0105 - MERCANTIL")
        self.assertEqual(normalize_ibp("BBVA"), "0108 - PROVINCIAL")
        self.assertEqual(normalize_ibp("BBVA Provincial"), "0108 - PROVINCIAL")
        self.assertEqual(normalize_ibp("Caroni"), "0128 - CARONÍ")
        self.assertEqual(normalize_ibp("Caroní"), "0128 - CARONÍ")
        self.assertEqual(normalize_ibp("Agricola"), "0166 - AGRÍCOLA")
        self.assertEqual(normalize_ibp("Agrícola"), "0166 - AGRÍCOLA")
        self.assertEqual(normalize_ibp("100% Banco"), "0156 - 100%BANCO")
        self.assertEqual(normalize_ibp("100%BANCO"), "0156 - 100%BANCO")
        self.assertEqual(normalize_ibp("Bicentenario"), "0175 - BDT")
        self.assertEqual(normalize_ibp("BDT"), "0175 - BDT")
        self.assertEqual(normalize_ibp("0882"), "0882 - Suiche7B (Consorcio)")
        self.assertEqual(normalize_ibp("Suiche7B"), "0882 - Suiche7B (Consorcio)")
        self.assertEqual(normalize_ibp("Suiche 7B"), "0882 - Suiche7B (Consorcio)")
        self.assertEqual(normalize_ibp("Conexus"), "0883 - CONEXUS (Consorcio)")
        self.assertEqual(normalize_ibp("0883"), "0883 - CONEXUS (Consorcio)")
        self.assertEqual(normalize_ibp("Banco Central de Venezuela"), "0001 - BCV")
        self.assertIsNone(normalize_ibp(""))
        self.assertIsNone(normalize_ibp(None))

        # 3. Create Escalation with explicit IBP string
        res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Banesco SIMF-0134",
            "category": "PostgreSQL",
            "ibp": "0134 - BANESCO",
            "doc_url": "https://docs.google.com/document/d/banesco0134"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(res.status_code, 200)
        doc_data = res.json()["document"]
        self.assertEqual(doc_data["ibp"], "0134 - BANESCO")
        esc_id = doc_data["id"]

        # 4. Verify in DB directly: EscalationDocument.ibp column
        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNotNone(esc_db)
        self.assertEqual(esc_db.ibp, "0134 - BANESCO")
        db.close()

        # 5. Create Escalation with code only -> should normalize to label
        res_norm = self.client.post("/api/escalations", json={
            "title": "Escalamiento BDV SIMF-0102",
            "category": "CPU",
            "ibp": "0102",
            "doc_url": "https://docs.google.com/document/d/bdv0102"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(res_norm.status_code, 200)
        self.assertEqual(res_norm.json()["document"]["ibp"], "0102 - BDV")
        esc_bdv_id = res_norm.json()["document"]["id"]

        db = TestingSessionLocal()
        esc_bdv_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_bdv_id).first()
        self.assertEqual(esc_bdv_db.ibp, "0102 - BDV")
        db.close()

        # 6. Update Escalation changing IBP
        put_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp": "0108 - PROVINCIAL"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(put_res.status_code, 200)
        self.assertEqual(put_res.json()["document"]["ibp"], "0108 - PROVINCIAL")

        db = TestingSessionLocal()
        esc_updated = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertEqual(esc_updated.ibp, "0108 - PROVINCIAL")
        db.close()

        # 7. Unlink IBP via exact frontend modal payload: {"ibp": None, "ibp_id": None}
        unlink_null_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp": None,
            "ibp_id": None
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(unlink_null_res.status_code, 200)
        self.assertEqual(unlink_null_res.json()["document"]["ibp"], "")
        self.assertIsNone(unlink_null_res.json()["document"]["ibp_id"])

        db = TestingSessionLocal()
        esc_unlinked_null = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNone(esc_unlinked_null.ibp)
        self.assertIsNone(esc_unlinked_null.ibp_id)
        db.close()

        # 8. Re-link and then unlink via empty string
        relink_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp": "0134 - BANESCO"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(relink_res.status_code, 200)
        self.assertEqual(relink_res.json()["document"]["ibp"], "0134 - BANESCO")

        unlink_empty_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp": ""
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(unlink_empty_res.status_code, 200)
        self.assertEqual(unlink_empty_res.json()["document"]["ibp"], "")

        db = TestingSessionLocal()
        esc_unlinked = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNone(esc_unlinked.ibp)
        self.assertIsNone(esc_unlinked.ibp_id)
        db.close()

        # 9. Verify GET /api/escalations contains ibp field in all records
        get_res = self.client.get(
            "/api/escalations",
            headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"}
        )
        self.assertEqual(get_res.status_code, 200)
        escalations = get_res.json()
        bdv_item = next(e for e in escalations if e["id"] == esc_bdv_id)
        self.assertEqual(bdv_item["ibp"], "0102 - BDV")

        # 10. Seed official catalog into test DB and verify GET /api/ibps
        db = TestingSessionLocal()
        db.query(IBP).delete()
        for item in IBP_INVENTORY:
            db.add(IBP(name=item["label"], code=item["code"], is_active=1))
        db.commit()
        db.close()

        get_ibps_res = self.client.get(
            "/api/ibps",
            headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"}
        )
        self.assertEqual(get_ibps_res.status_code, 200)
        ibps_list = get_ibps_res.json()
        self.assertEqual(len(ibps_list), 34)
        self.assertEqual([x["code"] for x in ibps_list], expected_codes)

    def test_12_recommendations_sync_when_linking_existing_item(self):
        """Test that when an item already has recommendations and is linked to an escalation via PATCH,
        its recommendations are automatically appended to the escalation document."""
        db = TestingSessionLocal()
        esc = EscalationDocument(title="Escalamiento Sincronizar", category="PostgreSQL", doc_url="https://docs.google.com/sync1")
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu_sync1",
            title="Actividad con recomendación previa",
            status="curso",
            recommendations="Recomendación técnica documentada con antelación.",
            escalation_id=None,
            assignee_name="Adonis Lozada"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Link to escalation via PATCH without resending recommendations
        patch_res = self.client.patch(f"/api/items/{item_id}", json={
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(patch_res.status_code, 200)

        # Verify escalation document received the recommendations
        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNotNone(esc_db.recommendations)
        self.assertIn("Recomendación técnica documentada con antelación.", esc_db.recommendations)
        self.assertIn("ClickUp #cu_sync1", esc_db.recommendations)
        db.close()

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_13_close_item_with_existing_recommendations_without_resending(self, mock_cu):
        """Test that closing an item that already has recommendations appends them to the escalation
        even if the status update request does not explicitly pass recommendations in the body."""
        mock_cu.return_value = True

        db = TestingSessionLocal()
        esc = EscalationDocument(title="Escalamiento Cierre Directo", category="Servicios", doc_url="https://docs.google.com/close1")
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu_close1",
            title="Actividad para cerrar directo",
            status="curso",
            recommendations="Causa raíz identificada y solventada en BD.",
            escalation_id=esc_id,
            assignee_name="Adonis Lozada"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Close without passing recommendations in the body
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(close_res.status_code, 200)

        # Verify escalation document received the recommendations
        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNotNone(esc_db.recommendations)
        self.assertIn("Causa raíz identificada y solventada en BD.", esc_db.recommendations)
        db.close()

    def test_14_delete_escalation_unlinks_items(self):
        """Test that deleting an escalation safely unlinks any items that referenced it."""
        db = TestingSessionLocal()
        esc = EscalationDocument(title="Escalamiento a Eliminar", category="CPU", doc_url="https://docs.google.com/del1")
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu_del1",
            title="Actividad vinculada a escalamiento transitorio",
            status="curso",
            escalation_id=esc_id,
            assignee_name="Adonis Lozada"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Delete escalation
        del_res = self.client.delete(f"/api/escalations/{esc_id}", headers={"X-User-Role": "Coordinador"})
        self.assertEqual(del_res.status_code, 200)

        # Check item escalation_id is None
        db = TestingSessionLocal()
        item_db = db.query(Item).filter(Item.id == item_id).first()
        self.assertIsNone(item_db.escalation_id)
        db.close()

    def test_15_frontend_category_and_ibp_consistency(self):
        """Verify that categories and IBP structures in frontend/index.html match backend specs."""
        expected_cats = ["CPU", "RAM", "Disco", "Antivirus", "PostgreSQL", "Servicios", "Infraestructura"]
        self.assertEqual(VALID_ESCALATION_CATEGORIES, expected_cats)

        # Check frontend/index.html content
        frontend_path = os.path.abspath("frontend/index.html")
        self.assertTrue(os.path.isfile(frontend_path))
        with open(frontend_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Categories list in JS
        self.assertIn("const ESCALATION_CATEGORIES = ['CPU', 'RAM', 'Disco', 'Antivirus', 'PostgreSQL', 'Servicios', 'Infraestructura'];", html)

        # Category filter dropdown options
        for cat in VALID_ESCALATION_CATEGORIES:
            self.assertIn(f'<option value="{cat}">{cat}</option>', html)

        # 34 IBPs in IBP_INVENTORY
        self.assertEqual(len(IBP_INVENTORY), 34)

    def test_16_custom_ibp_creation_and_escalation_linkage(self):
        """Test creating a custom IBP and linking an escalation to it."""
        res_create = self.client.post("/api/ibps", json={
            "name": "Banco Central de Integración",
            "code": "BCI",
            "description": "Entidad bancaria internacional"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(res_create.status_code, 200)
        custom_ibp = res_create.json()["ibp"]
        self.assertEqual(custom_ibp["code"], "BCI")

        # Create escalation with this custom IBP ID
        esc_res = self.client.post("/api/escalations", json={
            "title": "SIMF-ES-9901 Caso Especial BCI",
            "category": "Infraestructura",
            "ibp_id": custom_ibp["id"],
            "doc_url": "https://docs.google.com/document/d/bci9901"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(esc_res.status_code, 200)
        doc = esc_res.json()["document"]
        self.assertEqual(doc["ibp_id"], custom_ibp["id"])
        self.assertEqual(doc["ibp_name"], "Banco Central de Integración")
        self.assertEqual(doc["category"], "Infraestructura")

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    @patch("app.sync.glpi_client.GLPIClient.update_ticket_status")
    def test_17_clickup_and_glpi_support_espera_status(self, mock_glpi_status, mock_cu_status):
        """Test that both ClickUp tasks and GLPI tickets can be set to 'espera'."""
        mock_glpi_status.return_value = True
        mock_cu_status.return_value = True

        db = TestingSessionLocal()
        cu_item = Item(
            source="ClickUp",
            external_id="cu_espera_ok",
            title="Tarea ClickUp con espera",
            status="curso",
            assignee_name="Adonis Lozada"
        )
        glpi_item = Item(
            source="GLPI",
            external_id="glpi_ok_espera",
            title="Ticket GLPI con espera",
            status="curso",
            assignee_name="Adonis Lozada"
        )
        db.add_all([cu_item, glpi_item])
        db.commit()
        cu_id = cu_item.id
        glpi_id = glpi_item.id
        db.close()

        # ClickUp task can be set to 'espera' -> 200 OK
        res_cu = self.client.post(f"/api/items/{cu_id}/status", json={
            "status": "espera"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(res_cu.status_code, 200)
        self.assertEqual(res_cu.json()["item"]["status"], "espera")
        self.assertEqual(res_cu.json()["item"]["status_origin"], "en espera")
        mock_cu_status.assert_called_with("cu_espera_ok", "espera")

        # GLPI ticket can be set to 'espera' -> 200 OK
        res_glpi = self.client.post(f"/api/items/{glpi_id}/status", json={
            "status": "espera"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(res_glpi.status_code, 200)
        self.assertEqual(res_glpi.json()["item"]["status"], "espera")

        # ClickUpClient.normalize_task maps waiting/en espera to 'espera'
        from app.sync.clickup_client import ClickUpClient
        cu_client = ClickUpClient()
        norm_cu1 = cu_client.normalize_task({
            "id": "cu_raw_wait1",
            "name": "Tarea con estado en espera",
            "status": {"status": "en espera"},
            "priority": {"priority": "normal"}
        })
        self.assertEqual(norm_cu1["status"], "espera")

        norm_cu2 = cu_client.normalize_task({
            "id": "cu_raw_wait2",
            "name": "Tarea con estado waiting",
            "status": {"status": "waiting"},
            "priority": {"priority": "normal"}
        })
        self.assertEqual(norm_cu2["status"], "espera")

    def test_18_glpi_entity_resolution(self):
        """Test GLPIClient entity mapping for SYCOM and Microfilms Center."""
        from app.sync.glpi_client import GLPIClient
        glpi = GLPIClient()

        # Default entity ID 1 -> SYCOM
        norm_sycom = glpi.normalize_ticket({
            "id": 236,
            "name": "Revisión servidor",
            "status": 2,
            "priority": 3,
            "entities_id": 1
        })
        self.assertEqual(norm_sycom["client_name"], "SYCOM")
        self.assertEqual(norm_sycom["project_name"], "SYCOM")

        # Default entity ID 2 -> Microfilms Center
        norm_mc = glpi.normalize_ticket({
            "id": 237,
            "name": "Escaneo lote documental",
            "status": 2,
            "priority": 3,
            "entities_id": 2
        })
        self.assertEqual(norm_mc["client_name"], "Microfilms Center")
        self.assertEqual(norm_mc["project_name"], "Microfilms Center")

        # Hierarchical string from GLPI -> cleaned entity name
        norm_hier = glpi.normalize_ticket({
            "id": 238,
            "name": "Soporte scanner",
            "status": 4,
            "priority": 4,
            "entities_id": "Root entity > SYCOM > Microfilms Center"
        })
        self.assertEqual(norm_hier["client_name"], "Microfilms Center")

        # Dynamic entities_map override
        norm_custom = glpi.normalize_ticket(
            {"id": 239, "name": "Ticket custom", "status": 1, "priority": 2, "entities_id": 5},
            entities_map={5: "Microfilms Center"}
        )
        self.assertEqual(norm_custom["client_name"], "Microfilms Center")

        # Reverse resolution for ticket creation
        self.assertEqual(glpi.resolve_entity_id("Sycom", {1: "SYCOM", 2: "Microfilms Center"}), 1)
        self.assertEqual(glpi.resolve_entity_id("MicroFilms Center", {1: "SYCOM", 2: "Microfilms Center"}), 2)

        # Verify sync_clickup preserves SIMF / SGLPAR subproject instead of overwriting with Gestión Sycom
        from app.sync.synchronizer import sync_clickup
        db = TestingSessionLocal()
        cu_simf = Item(
            source="ClickUp",
            external_id="cu_simf_sync_1",
            title="Tarea SIMF",
            client_name="SGLPAR",
            project_name="SGLPAR",
            status="pendiente",
            assignee_name="Adonis Lozada"
        )
        db.add(cu_simf)
        db.commit()
        with patch("app.sync.clickup_client.ClickUpClient.fetch_tasks", return_value=[{
            "id": "cu_simf_sync_1",
            "name": "Tarea SIMF",
            "status": {"status": "in progress"},
            "priority": {"priority": "high"},
            "assignees": [{"username": "Adonis Lozada"}]
        }]):
            sync_clickup(db)
        db.refresh(cu_simf)
        self.assertEqual(cu_simf.client_name, "SGLPAR")
        self.assertEqual(cu_simf.project_name, "SGLPAR")
        self.assertEqual(cu_simf.status, "curso")
        db.close()

    def test_19_escalation_chronological_inheritance_and_duration(self):
        """Test that EscalationDocument stores received_at & escalated_at, linked Items inherit them,
        and pure chronological metrics (duration_human, time_attention_label) are computed without SLA."""
        from datetime import datetime, timedelta
        now_utc = datetime.utcnow()
        rec_dt = (now_utc - timedelta(hours=26)).replace(microsecond=0)
        esc_dt = (now_utc - timedelta(hours=2)).replace(microsecond=0)

        esc_res = self.client.post("/api/escalations", json={
            "title": "SIMF-ES-7700 Chronological Duration Test",
            "category": "PostgreSQL",
            "ibp": "0105 - Banco Mercantil",
            "doc_url": "https://docs.google.com/document/d/dualchrono7700",
            "received_at": rec_dt.isoformat() + "Z",
            "escalated_at": esc_dt.isoformat() + "Z",
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(esc_res.status_code, 200)
        esc_doc = esc_res.json()["document"]
        esc_id = esc_doc["id"]
        self.assertTrue(esc_doc["received_at"].startswith(rec_dt.isoformat()))
        self.assertTrue(esc_doc["escalated_at"].startswith(esc_dt.isoformat()))

        # Create an activity with priority 'Alta' and link it via PATCH
        db = TestingSessionLocal()
        item = Item(
            source="ClickUp",
            external_id="cu_dual_chrono_1",
            title="Optimización índice PostgreSQL",
            priority="Alta",
            status="curso",
            assignee_name="Adonis Lozada"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        patch_res = self.client.patch(f"/api/items/{item_id}", json={
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(patch_res.status_code, 200)
        patched_item = patch_res.json()["item"]

        # Inherited timestamps from escalation
        self.assertTrue(patched_item["received_at"].startswith(rec_dt.isoformat()))
        self.assertTrue(patched_item["escalated_at"].startswith(esc_dt.isoformat()))

        # Pure chronological metrics
        self.assertIn("duration_human", patched_item)
        self.assertIn("time_attention_label", patched_item)
        self.assertTrue(patched_item["time_attention_label"].startswith("Lleva activa") or "activa" in patched_item["time_attention_label"])

        # Updating escalation dates propagates to linked items
        new_rec_dt = (now_utc - timedelta(hours=3)).replace(microsecond=0)
        new_esc_dt = (now_utc - timedelta(hours=1)).replace(microsecond=0)
        put_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "received_at": new_rec_dt.isoformat() + "Z",
            "escalated_at": new_esc_dt.isoformat() + "Z",
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(put_res.status_code, 200)

        items_res = self.client.get(
            "/api/items",
            headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"}
        )
        self.assertEqual(items_res.status_code, 200)
        updated_item = next(x for x in items_res.json() if x["id"] == item_id)
        self.assertTrue(updated_item["received_at"].startswith(new_rec_dt.isoformat()))
        self.assertTrue(updated_item["escalated_at"].startswith(new_esc_dt.isoformat()))

    @patch("app.sync.clickup_client.ClickUpClient.assign_task")
    def test_20_specialist_visibility_url_encoded_and_sync_preserves_assignees(self, mock_cu_assign):
        """Test that URL-encoded X-User-Name headers (e.g. Yoxaide%20Jim%C3%A9nez, Juan%20Fern%C3%A1ndez)
        correctly return assigned items to specialists, and sync_clickup preserves Agenda assignees
        even if ClickUp returns partial or empty assignees."""
        mock_cu_assign.return_value = False  # Simulate ClickUp Space permission restriction

        db = TestingSessionLocal()
        t_yox = Item(
            source="ClickUp",
            external_id="cu_yox_1",
            title="Revisión de backups Yoxaide",
            priority="Alta",
            status="pendiente",
            assignee_name="Sin asignar"
        )
        t_jl = Item(
            source="ClickUp",
            external_id="cu_jl_1",
            title="Despliegue cluster Juan y Luis",
            priority="Media",
            status="pendiente",
            assignee_name="Sin asignar"
        )
        db.add_all([t_yox, t_jl])
        db.commit()
        yox_id = t_yox.id
        jl_id = t_jl.id
        db.close()

        # 1. Coordinator assigns t_yox to Yoxaide
        res_assign_yox = self.client.post(f"/api/items/{yox_id}/assign", json={
            "specialist_keys": ["yoxaide"]
        }, headers={"X-User-Role": "coordinador"})
        self.assertEqual(res_assign_yox.status_code, 200)
        self.assertEqual(res_assign_yox.json()["item"]["assignee_name"], "Yoxaide Jiménez")

        # 2. Yoxaide logs in and fetches /api/items with URL-encoded header (as frontend sends)
        res_yox_items = self.client.get("/api/items", headers={
            "X-User-Role": "especialista",
            "X-User-Name": "Yoxaide%20Jim%C3%A9nez"
        })
        self.assertEqual(res_yox_items.status_code, 200)
        yox_ids = [x["id"] for x in res_yox_items.json()]
        self.assertIn(yox_id, yox_ids)

        # 3. Coordinator assigns t_jl to Juan and Luis
        res_assign_jl = self.client.post(f"/api/items/{jl_id}/assign", json={
            "specialist_keys": ["juan", "luis"]
        }, headers={"X-User-Role": "coordinador"})
        self.assertEqual(res_assign_jl.status_code, 200)
        self.assertEqual(res_assign_jl.json()["item"]["assignee_name"], "Juan Fernández, Luis Acosta")

        # 4. Run sync_clickup where ClickUp still reports Adonis or empty assignees
        from app.sync.synchronizer import sync_clickup
        db = TestingSessionLocal()
        with patch("app.sync.clickup_client.ClickUpClient.fetch_tasks", return_value=[
            {
                "id": "cu_yox_1",
                "name": "Revisión de backups Yoxaide",
                "status": {"status": "in progress"},
                "priority": {"priority": "high"},
                "assignees": []
            },
            {
                "id": "cu_jl_1",
                "name": "Despliegue cluster Juan y Luis",
                "status": {"status": "in progress"},
                "priority": {"priority": "normal"},
                "assignees": [{"username": "Adonis Lozada"}]
            }
        ]):
            sync_clickup(db)
        db.close()

        # 5. Verify Juan, Luis, and Yoxaide were NOT erased by sync_clickup
        res_juan = self.client.get("/api/items", headers={
            "X-User-Role": "especialista",
            "X-User-Name": "Juan%20Fern%C3%A1ndez"
        })
        self.assertEqual(res_juan.status_code, 200)
        juan_item = next((x for x in res_juan.json() if x["id"] == jl_id), None)
        self.assertIsNotNone(juan_item)
        self.assertEqual(juan_item["assignee_name"], "Juan Fernández, Luis Acosta")

        res_luis = self.client.get("/api/items", headers={
            "X-User-Role": "especialista",
            "X-User-Name": "Luis%20Acosta"
        })
        self.assertEqual(res_luis.status_code, 200)
        luis_item = next((x for x in res_luis.json() if x["id"] == jl_id), None)
        self.assertIsNotNone(luis_item)
        self.assertEqual(luis_item["assignee_name"], "Juan Fernández, Luis Acosta")

    def test_21_reports_filter_by_ibp_and_category_and_pdf_charts(self):
        """Test filtering reports by IBP and Category and verify PDF print-view contains KPIs and SVG charts."""
        # Create an escalation for Banesco (0134) + PostgreSQL
        esc_res = self.client.post("/api/escalations", json={
            "title": "SIMF-ES-7701 Saturación de conexiones PostgreSQL en 0134 - BANESCO",
            "category": "PostgreSQL",
            "ibp": "0134 - BANESCO",
            "doc_url": "https://docs.google.com/document/d/banesco7701",
            "recommendations": "Incrementar max_connections y ajustar PgBouncer."
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(esc_res.status_code, 200)
        esc_id = esc_res.json()["document"]["id"]

        db = TestingSessionLocal()
        item_pg = Item(
            source="ClickUp",
            external_id="cu_pg_7701",
            title="Tuning PostgreSQL 0134 Banesco",
            priority="Crítica",
            status="cerrado",
            assignee_name="Juan Fernández",
            escalation_id=esc_id
        )
        db.add(item_pg)
        db.commit()
        db.close()

        # 1. Query /api/reports/data filtered by ibp=0134 and category=PostgreSQL
        rep_res = self.client.get(
            "/api/reports/data?ibp=0134&category=PostgreSQL",
            headers={"X-User-Role": "Coordinador"}
        )
        self.assertEqual(rep_res.status_code, 200)
        rep_data = rep_res.json()
        self.assertGreaterEqual(rep_data["kpis"]["total_items"], 1)
        self.assertGreaterEqual(rep_data["kpis"]["total_escalamientos"], 1)
        self.assertTrue(any("0134" in r["ibp"] for r in rep_data["por_ibp"]))
        self.assertTrue(any(r["category"] == "PostgreSQL" and r["total"] >= 1 for r in rep_data["por_categoria"]))

        # 2. Query /api/reports/print-view filtered by ibp and category
        pdf_res = self.client.get(
            "/api/reports/print-view?ibp=0134%20-%20BANESCO&category=PostgreSQL",
            headers={"X-User-Role": "Coordinador"}
        )
        self.assertEqual(pdf_res.status_code, 200)
        html = pdf_res.text
        self.assertIn("IBP: 0134 - BANESCO", html)
        self.assertIn("Categoría: PostgreSQL", html)
        self.assertIn("Gráfico por Categoría Técnica", html)
        self.assertIn("Gráfico por IBP / Banco Participante", html)
        self.assertIn("<svg viewBox=\"0 0 130 130\"", html)
        self.assertIn("Incrementar max_connections y ajustar PgBouncer.", html)

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_22_clickup_espera_status_and_chronological_serialization(self, mock_update_status):
        """Test that ClickUp tasks can transition to 'espera' without 400 error,
        ClickUpClient receives 'espera', and serialized item has clean chronological fields without SLA."""
        mock_update_status.return_value = True

        db = TestingSessionLocal()
        item = Item(
            source="ClickUp",
            external_id="cu_test_espera_99",
            title="Tarea para validar transición a espera",
            priority="Media",
            status="curso",
            status_origin="in progress",
            assignee_name="Adonis Lozada"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Update status to 'espera'
        res = self.client.post(
            f"/api/items/{item_id}/status",
            json={"status": "espera"},
            headers={"X-User-Role": "coordinador"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(mock_update_status.called)
        args, kwargs = mock_update_status.call_args
        self.assertEqual(args[0], "cu_test_espera_99")
        self.assertEqual(args[1], "espera")

        # Verify DB state
        db = TestingSessionLocal()
        refreshed = db.query(Item).filter(Item.id == item_id).first()
        self.assertEqual(refreshed.status, "espera")
        self.assertEqual(refreshed.status_origin, "en espera")
        self.assertIsNone(refreshed.closed_at)
        db.close()

        # Verify serialized item returned by GET /api/items/{item_id}
        res_get = self.client.get(
            f"/api/items/{item_id}",
            headers={"X-User-Role": "coordinador", "X-User-Name": "Adonis Lozada"}
        )
        self.assertEqual(res_get.status_code, 200)
        data = res_get.json()
        self.assertEqual(data["status"], "espera")
        self.assertEqual(data["status_origin"], "en espera")
        self.assertIn("duration_human", data)
        self.assertIn("time_attention_label", data)
        self.assertNotIn("time_sla_label", data)
        self.assertNotIn("sla_percent", data)

    def test_23_kpis_and_reports_data_no_sla(self):
        """Test that /api/kpis and /api/reports/data return operational metrics without SLA traces."""
        # 1. Check /api/kpis
        res_kpis = self.client.get(
            "/api/kpis",
            headers={"X-User-Role": "coordinador", "X-User-Name": "Adonis Lozada"}
        )
        self.assertEqual(res_kpis.status_code, 200)
        kpis = res_kpis.json()
        self.assertIn("total_actividades", kpis)
        self.assertIn("pendientes", kpis)
        self.assertIn("en_curso", kpis)
        self.assertIn("en_espera", kpis)
        self.assertIn("cerrados", kpis)
        self.assertNotIn("cumplimiento_general", kpis)
        self.assertNotIn("cumplimiento_equipo", kpis)
        self.assertNotIn("fuera_sla", kpis)
        self.assertNotIn("dentro_sla", kpis)
        self.assertNotIn("sla_percent", kpis)

        # 2. Check /api/reports/data
        res_rep = self.client.get("/api/reports/data", headers={"X-User-Role": "coordinador"})
        self.assertEqual(res_rep.status_code, 200)
        rep = res_rep.json()
        rep_kpis = rep.get("kpis", {})
        self.assertIn("total_items", rep_kpis)
        self.assertIn("total_cerrados", rep_kpis)
        self.assertIn("total_en_curso", rep_kpis)
        self.assertIn("total_pendientes", rep_kpis)
        self.assertIn("total_en_espera", rep_kpis)
        self.assertIn("avg_resolution_str", rep_kpis)
        self.assertNotIn("cumplimiento_general", rep_kpis)
        self.assertNotIn("cumplimiento_equipo", rep_kpis)
        self.assertNotIn("fuera_sla", rep_kpis)
        self.assertNotIn("dentro_sla", rep_kpis)

    @patch("app.sync.clickup_client.ClickUpClient.create_task")
    def test_24_specialist_can_create_items_and_analyst_blocked(self, mock_cu):
        """Test that Specialists can create activities via POST /api/items, but Analysts are blocked."""
        mock_cu.return_value = {"id": "cu_spec_1", "url": "https://app.clickup.com/t/cu_spec_1"}

        # 1. Specialist creates activity -> 200 OK
        spec_res = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Actividad creada por Especialista",
            "priority": "Alta",
            "project_name": "SIMF"
        }, headers={"X-User-Role": "Especialista", "X-User-Name": "Juan Fernandez"})
        self.assertEqual(spec_res.status_code, 200)
        self.assertEqual(spec_res.json()["item"]["external_id"], "cu_spec_1")

        # 2. Analyst tries to create activity -> 403 Forbidden
        analyst_res = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Intento de creación por Analista"
        }, headers={"X-User-Role": "Analista"})
        self.assertEqual(analyst_res.status_code, 403)

    def test_user_profile_and_password_management(self):
        """Verifica la consulta y actualización de datos personales, foto y cambio de clave."""
        db = TestingSessionLocal()
        try:
            u = db.query(User).filter(User.username == "j.fernandez").first()
            if not u:
                u = User(
                    full_name="Juan Fernandez",
                    username="j.fernandez",
                    email="j.fernandez@sycom.com.ve",
                    telegram_username="@Juuan8",
                    role="especialista",
                    password_hash=hash_password("sycom2026"),
                    is_active=1
                )
                db.add(u)
                db.commit()
        finally:
            db.close()

        # 1. Consulta del perfil actual
        res_me = self.client.get(
            "/api/users/me",
            headers={"X-User-Role": "especialista", "X-User-Name": "Juan Fernandez"}
        )
        self.assertEqual(res_me.status_code, 200)
        u_data = res_me.json()
        self.assertIn("username", u_data)
        self.assertIn("role", u_data)
        self.assertIn("avatar_data", u_data)
        self.assertIn("phone", u_data)

        # 2. Actualización de datos personales y foto
        new_avatar = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        update_res = self.client.put("/api/users/me", json={
            "phone": "+58 412 9998877",
            "job_title": "Especialista en Infraestructura & BD",
            "bio": "Administrador de sistemas y bases de datos",
            "avatar_data": new_avatar
        }, headers={"X-User-Role": "especialista", "X-User-Name": "Juan Fernandez"})
        self.assertEqual(update_res.status_code, 200)
        up_data = update_res.json()["user"]
        self.assertEqual(up_data["phone"], "+58 412 9998877")
        self.assertEqual(up_data["job_title"], "Especialista en Infraestructura & BD")
        self.assertEqual(up_data["avatar_data"], new_avatar)

        # 3. Cambio de contraseña
        pw_res = self.client.post("/api/auth/change-password", json={
            "username": "j.fernandez",
            "old_password": "sycom2026",
            "new_password": "newpassword2026"
        })
        self.assertEqual(pw_res.status_code, 200)

        # 4. Revertir contraseña para no afectar otros tests
        rev_res = self.client.post("/api/auth/change-password", json={
            "username": "j.fernandez",
            "old_password": "newpassword2026",
            "new_password": "sycom2026"
        })
        self.assertEqual(rev_res.status_code, 200)

    @patch("app.sync.clickup_client.ClickUpClient.delete_task")
    @patch("app.sync.clickup_client.ClickUpClient.create_task")
    def test_25_specialist_can_delete_own_activity_and_blocked_on_others(self, mock_cu_create, mock_cu_delete):
        """Test that Specialists can delete items they created, but are blocked from deleting others."""
        mock_cu_create.return_value = {"id": "cu_del_1", "url": "https://app.clickup.com/t/cu_del_1"}
        mock_cu_delete.return_value = True

        # 1. Specialist Juan creates activity
        create_res = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Actividad de Juan para eliminar",
            "priority": "Media",
            "project_name": "SIMF"
        }, headers={"X-User-Role": "Especialista", "X-User-Name": "Juan Fernandez"})
        self.assertEqual(create_res.status_code, 200)
        item_id = create_res.json()["item"]["id"]
        self.assertEqual(create_res.json()["item"]["created_by"], "Juan Fernandez")

        # 2. Specialist Yoxaide tries to delete Juan's activity -> 403 Forbidden
        del_by_yox = self.client.delete(
            f"/api/items/{item_id}",
            headers={"X-User-Role": "Especialista", "X-User-Name": "Yoxaide Jimenez"}
        )
        self.assertEqual(del_by_yox.status_code, 403)
        self.assertIn("solo puedes eliminar actividades creadas por ti", del_by_yox.json()["detail"].lower())

        # 3. Analyst tries to delete Juan's activity -> 403 Forbidden
        del_by_analyst = self.client.delete(
            f"/api/items/{item_id}",
            headers={"X-User-Role": "Analista", "X-User-Name": "Luis Acosta"}
        )
        self.assertEqual(del_by_analyst.status_code, 403)

        # 4. Specialist Juan deletes his own activity -> 200 OK
        del_by_juan = self.client.delete(
            f"/api/items/{item_id}",
            headers={"X-User-Role": "Especialista", "X-User-Name": "Juan Fernandez"}
        )
        self.assertEqual(del_by_juan.status_code, 200)
        self.assertEqual(del_by_juan.json()["status"], "OK")

        # Verify it's gone from database
        db = TestingSessionLocal()
        it_db = db.query(Item).filter(Item.id == item_id).first()
        self.assertIsNone(it_db)
        db.close()

        # 5. Coordinator can delete any activity
        mock_cu_create.return_value = {"id": "cu_del_2", "url": "https://app.clickup.com/t/cu_del_2"}
        c_res = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Actividad para borrar por coordinador",
            "priority": "Baja",
            "project_name": "SIMF"
        }, headers={"X-User-Role": "Especialista", "X-User-Name": "Juan Fernandez"})
        c_item_id = c_res.json()["item"]["id"]

        del_by_coord = self.client.delete(
            f"/api/items/{c_item_id}",
            headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"}
        )
        self.assertEqual(del_by_coord.status_code, 200)

    def test_user_profile_endpoints(self):
        """Verifica que /api/users/me y cambio de clave funcionen con tolerancia a acentos y username."""
        db = TestingSessionLocal()
        try:
            # Asegurar usuario Juan Fernandez con acento en BD
            db.query(User).filter(User.username == "j.fernandez").delete()
            juan = User(
                full_name="Juan Fernández",
                username="j.fernandez",
                email="j.fernandez@sycom.com.ve",
                role="especialista",
                password_hash=hash_password("clave123"),
                phone="+58 412 1112233",
                job_title="Especialista Linux",
                bio="Administrador de sistemas"
            )
            db.add(juan)
            db.commit()
        finally:
            db.close()

        # 1. GET /api/users/me con header sin acento y username query
        res = self.client.get(
            "/api/users/me?username=j.fernandez",
            headers={"X-User-Name": "Juan Fernandez", "X-User-Role": "especialista"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["username"], "j.fernandez")
        self.assertEqual(data["full_name"], "Juan Fernández")

        # 2. PUT /api/users/me para actualizar bio y foto
        put_res = self.client.put(
            "/api/users/me?username=j.fernandez",
            headers={"X-User-Name": "Juan Fernandez", "X-User-Role": "especialista"},
            json={
                "full_name": "Juan Fernández Modificado",
                "phone": "+58 414 9998877",
                "bio": "Especialista Cloud & DB",
                "avatar_data": "data:image/jpeg;base64,samplephoto"
            }
        )
        self.assertEqual(put_res.status_code, 200)
        updated = put_res.json()["user"]
        self.assertEqual(updated["phone"], "+58 414 9998877")
        self.assertEqual(updated["avatar_data"], "data:image/jpeg;base64,samplephoto")

        # 3. POST /api/auth/change-password con validaciones de seguridad (6-15 caracteres y caracter especial)
        # 3a. Fallo si longitud menor a 6
        too_short_res = self.client.post("/api/auth/change-password", json={
            "username": "j.fernandez",
            "old_password": "clave123",
            "new_password": "ab$1"
        })
        self.assertEqual(too_short_res.status_code, 400)
        self.assertIn("al menos 6", too_short_res.json()["detail"])

        # 3b. Fallo si longitud mayor a 15
        too_long_res = self.client.post("/api/auth/change-password", json={
            "username": "j.fernandez",
            "old_password": "clave123",
            "new_password": "Password123456$Extra"
        })
        self.assertEqual(too_long_res.status_code, 400)
        self.assertIn("15 caracteres", too_long_res.json()["detail"])

        # 3c. Fallo si no tiene caracter especial ($&.*)
        no_special_res = self.client.post("/api/auth/change-password", json={
            "username": "j.fernandez",
            "old_password": "clave123",
            "new_password": "ClaveSegura123"
        })
        self.assertEqual(no_special_res.status_code, 400)
        self.assertIn("caracter especial", no_special_res.json()["detail"])

        # 3d. Éxito si cumple los estándares (6-15 caracteres con $, &, ., *)
        pw_res = self.client.post("/api/auth/change-password", json={
            "username": "j.fernandez",
            "old_password": "clave123",
            "new_password": "Nueva.Clave$1"
        })
        self.assertEqual(pw_res.status_code, 200)
        self.assertEqual(pw_res.json()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
