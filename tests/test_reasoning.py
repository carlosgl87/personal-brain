import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.database import get_session
from app.main import app
from app.models import ReasoningRun
from app.schemas.reasoning import QueryPlan, SynthesizedAnswer
from app.services.embeddings import EmbeddingError
from app.services.memory import MemoryError
from app.services.queries import Scope
from app.services.reasoning import answer_reasoning, reasoning_question, retrieve, trace_context
from app.services.reasoning_llm import ReasoningError, call_json, plan_question, synthesize


def settings(**kwargs):
    with patch.dict("os.environ", {}, clear=True):
        return Settings(_env_file=None, **kwargs)


def context():
    return {"tasks": [], "decisions": [], "recent_sources": [], "chunks": [],
            "projects": [], "warnings": [], "scope": "global"}


def update(text, user_id=12345):
    return {"update_id": 100, "message": {"message_id": 10, "date": 1700000000,
        "from": {"id": user_id, "is_bot": False}, "chat": {"id": user_id, "type": "private"}, "text": text}}


class ReasoningTests(unittest.TestCase):
    def tearDown(self):
        app.dependency_overrides.clear()

    def test_plan_rejects_sql_excess_queries_invalid_scope_and_dates(self):
        invalid = ({"sql": "DROP TABLE sources"}, {"semantic_queries": ["q"] * 4},
                   {"semantic_queries": [" "]}, {"recent_sources_limit": 11},
                   {"recent_sources_limit": "5"}, {"scope_type": "project"},
                   {"scope_type": "global", "scope_value": "SIMA"},
                   {"include_tasks": "yes"}, {"date_from": "2026-10-01T00:00:00"},
                   {"date_from": "2026-10-02T00:00:00-05:00", "date_to": "2026-10-01T00:00:00-05:00"})
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                QueryPlan.model_validate(payload)

    def test_plan_accepts_timezone_dates_and_limits(self):
        plan = QueryPlan(semantic_queries=["inventory"], scope_type="company", scope_value="Catusita",
                         recent_sources_limit=10, date_from="2026-10-01T00:00:00-05:00")
        self.assertEqual(plan.date_from.utcoffset().total_seconds(), -18000)
        self.assertEqual(plan.scope_value, "Catusita")

    def test_question_detection_is_conservative_and_commands_support_bot_name(self):
        for text in ("/ask question", "/pregunta question", "/ask@mybot question", "\u00bfQuestion?", "Question?"):
            self.assertIsNotNone(reasoning_question(text))
        for text in ("Client likes the dashboard.", "/pendientes SIMA", "/completar id", "/nota Question?"):
            self.assertIsNone(reasoning_question(text))
        self.assertEqual(reasoning_question("/ask"), "")

    def test_planner_supplies_catalog_current_time_and_timezone(self):
        now = datetime.now(timezone.utc)
        with patch("app.services.reasoning_llm.call_json", return_value=QueryPlan()) as call:
            plan_question("question", {"projects": []}, now, settings())
        data = call.call_args.args[2]
        self.assertEqual(data["timezone"], "America/Lima")
        self.assertEqual(data["now"], now.isoformat())
        self.assertEqual(data["catalog"], {"projects": []})

    def test_claude_mock_structured_output_and_sanitized_failure(self):
        client = MagicMock()
        client.post.return_value.json.return_value = {"stop_reason": "end_turn",
            "content": [{"type": "text", "text": "{}"}]}
        config = settings(LLM_API_KEY="fake", LLM_MODEL="test")
        self.assertIsInstance(call_json(config, "rules", {}, QueryPlan, client), QueryPlan)
        self.assertNotIn("tools", client.post.call_args.kwargs["json"])
        client.post.side_effect = RuntimeError("private-provider-body")
        with self.assertRaises(ReasoningError) as caught:
            call_json(config, "rules", {}, QueryPlan, client)
        self.assertNotIn("private-provider-body", str(caught.exception))

    def test_provider_schema_is_supported_and_local_limits_still_apply(self):
        client = MagicMock()
        client.post.return_value.json.return_value = {"stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"recent_sources_limit": 11}'}]}
        with self.assertRaises(ReasoningError):
            call_json(settings(LLM_API_KEY="fake", LLM_MODEL="test"), "", {}, QueryPlan, client)
        wire = json.dumps(client.post.call_args.kwargs["json"]["output_config"])
        for keyword in ('"maximum"', '"maxItems"', '"maxLength"', '"minimum"'):
            self.assertNotIn(keyword, wire)
        self.assertIn('"additionalProperties": false', wire)

    def test_claude_truncation_is_rejected(self):
        client = MagicMock()
        client.post.return_value.json.return_value = {"stop_reason": "max_tokens",
            "content": [{"type": "text", "text": "{}"}]}
        with self.assertRaises(ReasoningError):
            call_json(settings(LLM_API_KEY="fake", LLM_MODEL="test"), "", {}, QueryPlan, client)

    def test_synthesis_adds_only_known_source_references(self):
        evidence = context()
        source_id = uuid4()
        evidence["tasks"] = [{"source_id": str(source_id)}]
        with patch("app.services.reasoning_llm.call_json", return_value=SynthesizedAnswer(
                text="Hecho: hay un compromiso registrado.", source_ids=[source_id])):
            answer = synthesize("question", evidence, settings())
        self.assertIn("[Fuente: " + str(source_id) + "]", answer)
        with patch("app.services.reasoning_llm.call_json", return_value=SynthesizedAnswer(
                text="Unknown.", source_ids=[uuid4()])):
            with self.assertRaises(ReasoningError):
                synthesize("question", evidence, settings())

    def test_synthesis_rejects_missing_and_fabricated_inline_references(self):
        evidence = context()
        source_id = uuid4()
        evidence["chunks"] = [{"source_id": str(source_id)}]
        for result in (SynthesizedAnswer(text="claim", source_ids=[]),
                       SynthesizedAnswer(text="[Fuente: " + str(uuid4()) + "]", source_ids=[source_id])):
            with patch("app.services.reasoning_llm.call_json", return_value=result):
                with self.assertRaises(ReasoningError):
                    synthesize("q", evidence, settings())

    def test_unknown_scope_stops_before_database_retrieval_or_synthesis(self):
        session = MagicMock()
        with patch("app.services.reasoning.plan_scope", return_value=Scope([], "", "Unknown project")):
            with self.assertRaises(MemoryError):
                retrieve(session, QueryPlan(scope_type="project", scope_value="missing"), settings())
        session.execute.assert_not_called()

    def test_semantic_retrieval_deduplicates_and_caps_chunks(self):
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        config = settings(OPENROUTER_API_KEY="fake", OPENROUTER_EMBEDDING_MODEL="test",
                          OPENROUTER_EMBEDDING_DIMENSIONS=3)
        items = [{"chunk_id": str(i), "source_id": str(uuid4()), "distance": i / 20,
                  "content": "evidence"} for i in range(10)]
        with patch("app.services.reasoning.semantic_search", return_value=items) as search:
            result = retrieve(session, QueryPlan(semantic_queries=["one", "two"],
                include_tasks=False, include_decisions=False, include_recent_sources=False), config)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(len(result["chunks"]), 8)
        self.assertEqual(result["chunks"][0]["distance"], 0)

    def test_embedding_failure_degrades_to_explicit_structured_coverage(self):
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        config = settings(OPENROUTER_API_KEY="fake", OPENROUTER_EMBEDDING_MODEL="test")
        with patch("app.services.reasoning.semantic_search", side_effect=EmbeddingError("safe")):
            result = retrieve(session, QueryPlan(semantic_queries=["q"], include_tasks=False,
                include_decisions=False, include_recent_sources=False), config)
        self.assertTrue(any("estructurada" in warning for warning in result["warnings"]))
        self.assertEqual(result["chunks"], [])

    def test_trace_stores_ids_and_hashes_without_full_text(self):
        evidence = context()
        evidence["chunks"] = [{"chunk_id": "chunk", "source_id": "source", "distance": 0.1,
                               "content": "private full text"}]
        evidence["recent_sources"] = [{"source_id": "source", "run_id": "run", "received_at": "date",
                                      "excerpt": {"text": "private full text", "truncated": False}}]
        trace = trace_context(evidence)
        self.assertNotIn("private full text", json.dumps(trace))
        self.assertEqual(trace["chunks"][0]["source_id"], "source")
        self.assertIn("content_sha256", trace["chunks"][0])

    def test_saved_reasoning_retry_does_not_repeat_paid_calls(self):
        session = MagicMock()
        session.scalar.side_effect = [SimpleNamespace(id=uuid4(), source_type="telegram_query"),
                                     SimpleNamespace(answer="cached answer")]
        with patch("app.services.reasoning.plan_question") as planner, patch(
                "app.services.reasoning.synthesize") as synthesis:
            result = answer_reasoning(session, "q", uuid4(), settings())
        self.assertEqual(result, "cached answer")
        planner.assert_not_called()
        synthesis.assert_not_called()
        session.add.assert_not_called()

    def test_reasoning_saves_plan_trace_answer_without_creating_tasks(self):
        session = MagicMock()
        source_id = uuid4()
        session.scalar.side_effect = [SimpleNamespace(id=source_id, source_type="telegram_query"), None]
        evidence = context()
        evidence["tasks"] = [{"source_id": str(uuid4()), "task_id": str(uuid4())}]
        with patch("app.services.reasoning.catalog_context", return_value={}), patch(
                "app.services.reasoning.plan_question", return_value=QueryPlan()), patch(
                "app.services.reasoning.retrieve", return_value=evidence), patch(
                "app.services.reasoning.synthesize", return_value="answer"):
            result = answer_reasoning(session, "question", source_id, settings(LLM_API_KEY="fake", LLM_MODEL="test"))
        self.assertEqual(result, "answer")
        self.assertEqual(session.add.call_count, 1)
        run = session.add.call_args.args[0]
        self.assertIsInstance(run, ReasoningRun)
        self.assertEqual(run.question_source_id, source_id)
        self.assertEqual(run.planner_version, "memory-planner-v1")

    def test_failed_claude_keeps_question_and_does_not_save_a_successful_run(self):
        session = MagicMock()
        session.scalar.side_effect = [SimpleNamespace(id=uuid4(), source_type="telegram_query"), None]
        with patch("app.services.reasoning.catalog_context", return_value={}), patch(
                "app.services.reasoning.plan_question", side_effect=ReasoningError("safe")):
            answer = answer_reasoning(session, "q", uuid4(), settings(LLM_API_KEY="fake", LLM_MODEL="test"))
        self.assertIn("conserva", answer)
        session.add.assert_not_called()

    def test_no_evidence_returns_insufficient_without_second_claude_call(self):
        session = MagicMock()
        session.scalar.side_effect = [SimpleNamespace(id=uuid4(), source_type="telegram_query"), None]
        with patch("app.services.reasoning.catalog_context", return_value={}), patch(
                "app.services.reasoning.plan_question", return_value=QueryPlan()), patch(
                "app.services.reasoning.retrieve", return_value=context()), patch(
                "app.services.reasoning.synthesize") as synthesis:
            answer = answer_reasoning(session, "q", uuid4(), settings(LLM_API_KEY="fake", LLM_MODEL="test"))
        self.assertIn("suficiente", answer)
        synthesis.assert_not_called()

    def setup_route(self):
        config = settings(TELEGRAM_BOT_TOKEN="fake-token", TELEGRAM_USER_ID="12345")
        app.dependency_overrides[get_settings] = lambda: config
        app.dependency_overrides[get_session] = lambda: MagicMock()
        return TestClient(app)

    def test_telegram_questions_ask_and_pregunta_are_saved_as_queries(self):
        client = self.setup_route()
        for text in ("/ask question", "/pregunta question", "\u00bfQu\u00e9 pasa?", "Question?"):
            with self.subTest(text=text), patch("app.api.routes.telegram.ingest_update",
                return_value={"status": "saved", "source_id": str(uuid4())}) as ingest, patch(
                "app.api.routes.telegram.answer_reasoning", return_value="answer") as reasoning, patch(
                "app.api.routes.telegram.answer_query") as sql:
                response = client.post("/telegram/updates", json=update(text),
                                       headers={"Authorization": "Bearer fake-token"})
                self.assertEqual(response.json()["answer"], "answer")
                self.assertTrue(ingest.call_args.kwargs["is_query"])
                reasoning.assert_called_once()
                sql.assert_not_called()

    def test_normal_note_and_nota_with_question_do_not_reason(self):
        client = self.setup_route()
        for text in ("Client is happy.", "/nota Question?", "Enviar informe pendiente", "que tengo pendiente de SIMA"):
            with patch("app.api.routes.telegram.ingest_update",
                return_value={"status": "saved", "source_id": str(uuid4())}) as ingest, patch(
                "app.api.routes.telegram.answer_reasoning") as reasoning:
                response = client.post("/telegram/updates", json=update(text),
                                       headers={"Authorization": "Bearer fake-token"})
                self.assertEqual(response.json()["status"], "saved")
                self.assertNotIn("is_query", ingest.call_args.kwargs)
                reasoning.assert_not_called()

    def test_unauthorized_question_never_calls_claude(self):
        client = self.setup_route()
        with patch("app.api.routes.telegram.answer_reasoning") as reasoning:
            result = client.post("/telegram/updates", json=update("/ask question", 999),
                                 headers={"Authorization": "Bearer fake-token"})
        self.assertEqual(result.json()["status"], "ignored")
        reasoning.assert_not_called()

    def test_deterministic_commands_do_not_call_reasoning(self):
        client = self.setup_route()
        for text in ("/pendientes SIMA", "/decisiones SIMA", "/resumen SIMA", "/estado SIMA", "/ayuda"):
            with patch("app.api.routes.telegram.ingest_update", return_value={
                "status": "saved", "source_id": str(uuid4())}), patch(
                "app.api.routes.telegram.answer_query", return_value="SQL answer"), patch(
                "app.api.routes.telegram.answer_reasoning") as reasoning:
                result = client.post("/telegram/updates", json=update(text),
                                     headers={"Authorization": "Bearer fake-token"})
                self.assertEqual(result.json()["answer"], "SQL answer")
                reasoning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
