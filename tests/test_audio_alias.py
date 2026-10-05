import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.services.project_matching import match_project


class AudioAliasTests(unittest.TestCase):
    def test_cima_resolves_to_sima_without_rewriting_transcript(self):
        data = json.loads((Path(__file__).resolve().parents[1] / "app/seed_data.json").read_text(encoding="utf-8"))
        projects = [SimpleNamespace(id=uuid4(), name=p["name"], slug=p["slug"],
                    aliases=[SimpleNamespace(alias=a) for a in p["aliases"]]) for p in data["projects"]]
        text = "Enviar el estatus de los costos del proyecto de CIMA mañana"
        match = match_project(text, projects)
        self.assertEqual(match.name, "SIMA")
        self.assertIn("CIMA", text)
        self.assertIsNone(match_project("Revisar encima de la mesa", projects))
        other = next(p for p in projects if p.name != "SIMA")
        self.assertIsNone(match_project("CIMA y " + other.name, projects))


if __name__ == "__main__":
    unittest.main()
