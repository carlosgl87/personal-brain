import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4
from sqlalchemy.dialects import postgresql
from pydantic import ValidationError
from app.config import Settings
from app.schemas.reasoning import QueryPlan
from app.services.memory import semantic_statement
from app.services.reasoning import retrieve, trace_context
from app.services.retrieval_budget import bound_context, diverse_chunks, effective_limits


def settings(**values):
    with patch.dict("os.environ", {}, clear=True):
        return Settings(_env_file=None, **({"PROJECT_MEMORY_ENABLED": False} | values))


class AdaptiveTests(unittest.TestCase):
    def test_depth_limits_are_application_owned(self):
        for depth, expected in (("focused", (6, 5, 10, 10)), ("normal", (12, 10, 20, 20)),
                                ("broad", (18, 15, 30, 30))):
            limits = effective_limits(QueryPlan(retrieval_depth=depth))
            self.assertEqual(tuple(limits.values()), expected)
        self.assertEqual(effective_limits(QueryPlan(retrieval_depth="focused", recent_sources_limit=15))["recent_sources"], 5)
        for bad in ({"retrieval_depth": "unlimited"}, {"max_chunks": 500}):
            with self.assertRaises(ValidationError):
                QueryPlan.model_validate(bad)

    def test_depth_does_not_change_project_scope(self):
        plan = QueryPlan(scope_type="project", scope_value="SIMA", retrieval_depth="broad")
        self.assertEqual(plan.scope_type, "project")

    def test_focused_normal_broad_never_exceed_chunk_limits(self):
        for depth, expected in (("focused", 6), ("normal", 12), ("broad", 18)):
            session = MagicMock()
            session.scalars.return_value.all.return_value = []
            items = [{"chunk_id": str(i), "source_id": str(uuid4()), "project_id": str(i % 4),
                      "distance": i / 100, "content": "evidence"} for i in range(40)]
            plan = QueryPlan(scope_type="project", scope_value="SIMA", retrieval_depth=depth,
                semantic_queries=["q"], include_tasks=False, include_decisions=False, include_recent_sources=False)
            with patch("app.services.reasoning.plan_scope", return_value=SimpleNamespace(
                    project_ids=[uuid4()], label="SIMA", error=None)), patch(
                    "app.services.reasoning.semantic_search", return_value=items) as search:
                result = retrieve(session, plan, settings(OPENROUTER_API_KEY="fake", OPENROUTER_EMBEDDING_MODEL="test"))
            self.assertEqual(len(result["chunks"]), expected)
            self.assertEqual(search.call_args.kwargs["limit"], expected)
            self.assertFalse(search.call_args.kwargs["diversify"])
            self.assertEqual(result["retrieval"]["counts"]["chunks"], expected)

    def test_global_broad_requests_diverse_candidates_including_null(self):
        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        items = [{"chunk_id": str(i), "source_id": str(uuid4()), "project_id": "dominant",
                  "distance": i / 100, "content": "evidence"} for i in range(30)]
        items += [{"chunk_id": "other", "source_id": str(uuid4()), "project_id": "other", "distance": 0.8, "content": "other"},
                  {"chunk_id": "unassigned", "source_id": str(uuid4()), "project_id": None, "distance": 0.9, "content": "unassigned"}]
        with patch("app.services.reasoning.semantic_search", return_value=items) as search:
            result = retrieve(session, QueryPlan(retrieval_depth="broad", semantic_queries=["q"],
                include_tasks=False, include_decisions=False, include_recent_sources=False),
                settings(OPENROUTER_API_KEY="fake", OPENROUTER_EMBEDDING_MODEL="test"))
        self.assertTrue(search.call_args.kwargs["diversify"])
        self.assertEqual(search.call_args.kwargs["limit"], 54)
        self.assertEqual({item["project_id"] for item in result["chunks"]}, {"dominant", "other", None})

    def test_diverse_candidate_sql_partitions_before_global_limit(self):
        config = settings(OPENROUTER_EMBEDDING_MODEL="test", OPENROUTER_EMBEDDING_DIMENSIONS=3)
        sql = str(semantic_statement([1, 0, 0], config, diversify=True).compile(dialect=postgresql.dialect()))
        self.assertIn("row_number() OVER (PARTITION BY sources.primary_project_id", sql)
        self.assertIn("project_rank <=", sql)
        self.assertIn("chunk_embeddings.model =", sql)
        self.assertIn("NOT (EXISTS", sql)

    def test_budget_keeps_structure_before_semantics_and_recents_without_mutating_original(self):
        original = {"scope": "SIMA", "warnings": [], "projects": [],
                    "tasks": [{"source_id": "t", "title": "urgent"}], "decisions": [],
                    "chunks": [{"chunk_id": str(i), "source_id": "s", "content": "x" * 2500} for i in range(10)],
                    "recent_sources": [{"source_id": "r", "excerpt": {"text": "x" * 2500, "truncated": False}}]}
        result = bound_context(original, QueryPlan(), effective_limits(QueryPlan()), 6000)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 6000)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(len(result["recent_sources"]), 0)
        self.assertTrue(result["retrieval"]["budget_reduced"])
        self.assertTrue(any("Cobertura parcial" in warning for warning in result["warnings"]))
        self.assertEqual(original["chunks"][0]["content"], "x" * 2500)

    def test_large_item_is_skipped_but_smaller_lower_priority_evidence_can_fit(self):
        evidence = {"scope": "global", "warnings": [], "projects": [], "tasks": [], "decisions": [],
                    "chunks": [{"content": "x" * 10000}, {"content": "useful short evidence"}],
                    "recent_sources": []}
        result = bound_context(evidence, QueryPlan(), effective_limits(QueryPlan()), 5000)
        self.assertEqual(result["chunks"], [{"content": "useful short evidence"}])

    def test_trace_keeps_budget_limits_counts_and_hashes(self):
        evidence = {"scope": "global", "warnings": [], "projects": [], "tasks": [], "decisions": [],
                    "chunks": [{"source_id": "source", "chunk_id": "chunk", "distance": 0.1, "content": "original"}],
                    "recent_sources": []}
        bounded = bound_context(evidence, QueryPlan(retrieval_depth="focused"),
                                effective_limits(QueryPlan(retrieval_depth="focused")), 10000)
        trace = trace_context(bounded)
        self.assertEqual(trace["retrieval"]["retrieval_depth"], "focused")
        self.assertEqual(trace["retrieval"]["counts"]["chunks"], 1)
        self.assertNotIn("original", json.dumps(trace))

    def test_trace_does_not_copy_structured_text_bodies(self):
        evidence = {"scope": "global", "warnings": [], "projects": [],
                    "tasks": [{"task_id": "task", "source_id": "source", "title": "private title",
                               "description": {"text": "private body", "truncated": False}}],
                    "decisions": [{"decision_id": "decision", "source_id": "source",
                                   "text": {"text": "private decision", "truncated": True}}],
                    "chunks": [], "recent_sources": []}
        trace = trace_context(evidence)
        self.assertNotIn("private", json.dumps(trace))
        self.assertEqual(trace["tasks"][0]["task_id"], "task")
        self.assertIn("text_sha256", trace["decisions"][0])
        self.assertTrue(trace["decisions"][0]["text_truncated"])

    def test_structured_sql_limits_follow_depth(self):
        for depth, count in (("focused", 10), ("normal", 20), ("broad", 30)):
            session = MagicMock()
            session.scalars.return_value.all.return_value = []
            session.execute.return_value.all.return_value = []
            retrieve(session, QueryPlan(retrieval_depth=depth, include_recent_sources=False), settings())
            for call in session.execute.call_args_list:
                compiled = call.args[0].compile(dialect=postgresql.dialect())
                self.assertIn(count + 1, compiled.params.values())


if __name__ == "__main__":
    unittest.main()
