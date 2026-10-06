
import json
import io

from alembic import command
from alembic.config import Config
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers

from app.config import Settings
from app.database import get_session
from app.main import app
from app.models import Base, Project
from app.seed import ensure, seed
from app.services.normalization import normalize


class PhaseOneTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    def test_configuration_priority_and_empty_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None, DATABASE_URL="postgres://internal/db",
                                DATABASE_PUBLIC_URL="postgresql://public/db")
            self.assertEqual(settings.database_url.host, "internal")
            fallback = Settings(_env_file=None, DATABASE_URL="",
                                DATABASE_PUBLIC_URL="postgresql://public/db")
            self.assertEqual(fallback.database_url.host, "public")
            self.assertEqual(fallback.database_url.drivername, "postgresql+psycopg")
            with self.assertRaisesRegex(ValueError, "Configura"):
                Settings(_env_file=None).database_url

    def test_invalid_configuration_never_echoes_input(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError) as caught:
                Settings(_env_file=None, DATABASE_URL="invalid-test-secret").database_url
            self.assertNotIn("invalid-test-secret", str(caught.exception))

    def test_models_and_normalization(self):
        configure_mappers()
        self.assertTrue({"areas", "categories", "companies", "projects", "project_aliases", "sources", "tasks", "decisions"}.issubset(Base.metadata.tables))
        self.assertEqual(normalize("  Automatización de Trámites "), "automatizacion de tramites")
        self.assertEqual(normalize("ALMA"), normalize("Alma"))
        self.assertEqual(normalize("E-commerce"), normalize("E commerce"))

    def test_seed_exact_inventory(self):
        data = json.loads(Path("app/seed_data.json").read_text(encoding="utf-8"))
        self.assertEqual(data["areas"], ["Laureate", "Consultora"])
        self.assertEqual(len(data["projects"]), 34)
        self.assertEqual(sum(len(p["aliases"]) for p in data["projects"]), 114)
        self.assertEqual(sum(p["area"] == "Laureate" for p in data["projects"]), 27)
        self.assertEqual(sum(p.get("company") == "Catusita" for p in data["projects"]), 3)
        keys = [(p["area"], p["slug"]) for p in data["projects"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_seed_repeated_and_new_alias_preserves_existing(self):
        rows = {}
        def fake_ensure(session, model, keys, values):
            key = (model.__tablename__, tuple(keys.items()))
            return rows.setdefault(key, SimpleNamespace(id=uuid4(), **keys, **values))
        with patch("app.seed.ensure", side_effect=fake_ensure):
            seed(MagicMock())
            count = len(rows)
            original_ids = {key: row.id for key, row in rows.items()}
            seed(MagicMock())
            self.assertEqual({key: row.id for key, row in rows.items()}, original_ids)
            self.assertEqual(sum(key[0] == "projects" for key in rows), 34)
            self.assertEqual(sum(key[0] == "project_aliases" for key in rows), 114)
            self.assertEqual(len(rows), count)
            self.assertEqual(count, 2 + 4 + 4 + 34 + 114)
            data = json.loads(Path("app/seed_data.json").read_text(encoding="utf-8"))
            data["projects"][0]["aliases"].append("Nuevo alias de prueba")
            with patch("app.seed.Path.read_text", return_value=json.dumps(data)):
                seed(MagicMock())
            self.assertEqual(len(rows), count + 1)

    def test_migration_compiles_offline_without_real_settings(self):
        buffer = io.StringIO()
        config = Config("alembic.ini", output_buffer=buffer)
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None, DATABASE_URL="postgresql://offline/db")
            with patch("app.config.get_settings", return_value=settings):
                command.upgrade(config, "head", sql=True)
        sql = buffer.getvalue()
        for table in Base.metadata.tables:
            self.assertIn("CREATE TABLE " + table, sql)
        self.assertIn("CREATE TRIGGER preserve_original_source", sql)
        self.assertIn("NEW.raw_content IS DISTINCT FROM OLD.raw_content", sql)
        self.assertNotIn("DROP TABLE", sql)
        self.assertNotIn("TRUNCATE", sql)

    def test_seed_uses_postgresql_conflict_protection(self):
        session = MagicMock()
        ensure(session, Project, {"area_id": uuid4(), "slug": "example"}, {"name": "Example"})
        statement = session.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("ON CONFLICT (area_id, slug) DO NOTHING", sql)

    def test_health_and_database_failures_are_safe(self):
        client = TestClient(app)
        self.assertEqual(client.get("/health").json(), {"status": "ok"})
        with patch("app.api.routes.health.get_engine") as engine:
            engine.return_value.connect.return_value.__enter__.return_value.execute.return_value.scalars.return_value.all.return_value = ["0010_documents_jobs"]
            self.assertEqual(client.get("/health/db").status_code, 200)
            engine.side_effect = RuntimeError("test-secret")
            response = client.get("/health/db")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("test-secret", response.text)

    def test_projects_filters_and_detail(self):
        from app.api.routes.telegram import require_telegram_access
        app.dependency_overrides[require_telegram_access] = lambda: 123
        session = MagicMock()
        app.dependency_overrides[get_session] = lambda: session
        client = TestClient(app)
        session.scalars.return_value.all.return_value = []
        response = client.get("/projects?area=Laureate&category=Student%20Ecosystem&company=Catusita&status=active")
        self.assertEqual(response.json(), [])
        query = session.scalars.call_args.args[0]
        sql = str(query.compile(dialect=postgresql.dialect()))
        for table in ("areas", "categories", "companies"):
            self.assertIn("JOIN " + table, sql)
        self.assertIn("projects.status =", sql)
        session.scalar.return_value = None
        self.assertEqual(client.get("/projects/" + str(uuid4())).status_code, 404)
        self.assertEqual(client.get("/projects/invalid").status_code, 422)
        now = datetime.now(timezone.utc)
        catalog = SimpleNamespace(id=uuid4(), name="Consultora", slug="consultora")
        session.scalar.return_value = SimpleNamespace(
            id=uuid4(), name="SIMA", slug="sima", description=None, status="active",
            created_at=now, updated_at=now, archived_at=None,
            area=catalog, category=None, company=SimpleNamespace(id=uuid4(), name="Estrategia", slug="estrategia"),
            aliases=[SimpleNamespace(id=uuid4(), alias="SIMA3", normalized_alias="sima3")],
        )
        response = client.get("/projects/" + str(session.scalar.return_value.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["aliases"][0]["alias"], "SIMA3")


if __name__ == "__main__":
    unittest.main()
