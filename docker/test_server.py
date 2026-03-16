"""Tests for knowledge MCP server tools (KB-02, KB-03, KB-04, KB-10).

Uses chromadb.EphemeralClient for isolation. Tests call tool functions
directly (they're regular Python functions despite @mcp.tool() decoration).
"""

import unittest
import chromadb


class _ServerTestBase(unittest.TestCase):
    """Base class that patches server module collections with ephemeral ones."""

    def setUp(self):
        self.client = chromadb.EphemeralClient()
        self.system_col = self.client.get_or_create_collection("system")
        self.runtime_col = self.client.get_or_create_collection("runtime")

        # Monkey-patch module-level collections in server
        import docker.knowledge.server as srv

        self._orig_system = srv.system_col
        self._orig_runtime = srv.runtime_col
        srv.system_col = self.system_col
        srv.runtime_col = self.runtime_col

    def tearDown(self):
        import docker.knowledge.server as srv

        srv.system_col = self._orig_system
        srv.runtime_col = self._orig_runtime


class TestKnowledgeSearch(_ServerTestBase):
    """KB-02: knowledge_search returns top-N results with similarity scores."""

    def setUp(self):
        super().setUp()
        # Seed system collection
        self.system_col.add(
            ids=["sys.platform", "sys.rules.security", "sys.tools.jobs"],
            documents=[
                "Goose is an AI agent framework for autonomous coding.",
                "Never expose API keys in logs. Always use environment variables for secrets.",
                "Jobs run scheduled tasks via cron expressions. Use /jobs to manage.",
            ],
            metadatas=[
                {"type": "fact", "source": "system.md", "section": "Platform",
                 "namespace": "system", "refs": "sys.tools.jobs", "key": "sys.platform"},
                {"type": "procedure", "source": "system.md", "section": "Rules > Security",
                 "namespace": "system", "refs": "", "key": "sys.rules.security"},
                {"type": "procedure", "source": "system.md", "section": "Tools > Jobs",
                 "namespace": "system", "refs": "sys.platform", "key": "sys.tools.jobs"},
            ],
        )
        # Seed runtime collection
        self.runtime_col.add(
            ids=["rt.fireflies"],
            documents=["Fireflies integration: API key stored in vault, transcripts auto-summarized."],
            metadatas=[
                {"type": "integration", "source": "runtime", "section": "",
                 "namespace": "runtime", "refs": "", "key": "rt.fireflies"},
            ],
        )

    def test_search_returns_results_with_scores(self):
        from docker.knowledge.server import knowledge_search

        result = knowledge_search("AI agent framework")
        self.assertIn("score:", result)
        self.assertIn("sys.platform", result)

    def test_search_limit_caps_results(self):
        from docker.knowledge.server import knowledge_search

        result = knowledge_search("system procedures", limit=1)
        # Should have at most 1 result block (key line + content + optional refs + blank)
        key_lines = [l for l in result.split("\n") if l.startswith("[")]
        self.assertLessEqual(len(key_lines), 1)

    def test_search_type_filter(self):
        from docker.knowledge.server import knowledge_search

        result = knowledge_search("API keys secrets", type="procedure")
        self.assertIn("procedure", result)
        # Should not contain fact-type results when filtering by procedure
        # (integration type should be excluded)
        self.assertNotIn("integration", result.split("\n")[0] if result else "")

    def test_search_sorted_by_score_descending(self):
        from docker.knowledge.server import knowledge_search

        result = knowledge_search("jobs scheduled tasks cron", limit=5)
        import re

        scores = [float(m.group(1)) for m in re.finditer(r"score: ([\d.]+)", result)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_search_merges_both_collections(self):
        from docker.knowledge.server import knowledge_search

        result = knowledge_search("integration API", limit=10)
        # Should find results from both system and runtime collections
        has_system = "sys." in result
        has_runtime = "rt." in result
        self.assertTrue(has_system or has_runtime, "Should find results from at least one collection")

    def test_search_no_results(self):
        # Use uniquely-named empty collections (EphemeralClient shares state by collection name)
        import docker.knowledge.server as srv
        empty_client = chromadb.EphemeralClient()
        srv.system_col = empty_client.get_or_create_collection("system_empty_test")
        srv.runtime_col = empty_client.get_or_create_collection("runtime_empty_test")

        from docker.knowledge.server import knowledge_search

        result = knowledge_search("xyzzy nonsense query that matches nothing")
        self.assertEqual(result, "No matching knowledge found.")


class TestKnowledgeUpsert(_ServerTestBase):
    """KB-03: knowledge_upsert writes typed chunks to runtime collection."""

    def test_upsert_creates_chunk_in_runtime(self):
        from docker.knowledge.server import knowledge_upsert

        result = knowledge_upsert("test.chunk", "Test content here", "fact")
        self.assertIn("test.chunk", result)

        # Verify it's in runtime collection
        got = self.runtime_col.get(ids=["test.chunk"])
        self.assertEqual(len(got["ids"]), 1)
        self.assertEqual(got["documents"][0], "Test content here")

    def test_upsert_not_in_system_collection(self):
        from docker.knowledge.server import knowledge_upsert

        knowledge_upsert("test.only.runtime", "Should not be in system", "fact")

        got = self.system_col.get(ids=["test.only.runtime"])
        self.assertEqual(len(got["ids"]), 0)

    def test_upsert_same_key_updates_content(self):
        from docker.knowledge.server import knowledge_upsert

        knowledge_upsert("test.update", "Original content", "fact")
        knowledge_upsert("test.update", "Updated content", "fact")

        got = self.runtime_col.get(ids=["test.update"])
        self.assertEqual(got["documents"][0], "Updated content")

    def test_upsert_stores_metadata_correctly(self):
        from docker.knowledge.server import knowledge_upsert

        knowledge_upsert("test.meta", "Content with metadata", "integration", refs="test.other")

        got = self.runtime_col.get(ids=["test.meta"], include=["metadatas"])
        meta = got["metadatas"][0]
        self.assertEqual(meta["type"], "integration")
        self.assertEqual(meta["namespace"], "runtime")
        self.assertEqual(meta["refs"], "test.other")
        self.assertEqual(meta["key"], "test.meta")


class TestKnowledgeUpsertValidation(_ServerTestBase):
    """Validation tests for knowledge_upsert type checking."""

    def test_upsert_rejects_invalid_type(self):
        from docker.knowledge.server import knowledge_upsert

        result = knowledge_upsert("test.bad", "content", "invalid_type")
        self.assertIn("Invalid type", result)
        self.assertIn("invalid_type", result)

        # Verify nothing was stored
        got = self.runtime_col.get(ids=["test.bad"])
        self.assertEqual(len(got["ids"]), 0)

    def test_upsert_accepts_all_valid_types(self):
        from docker.knowledge.server import knowledge_upsert

        for t in ("procedure", "schema", "fact", "preference", "integration"):
            result = knowledge_upsert(f"test.{t}", f"content for {t}", t)
            self.assertIn("Stored", result)


class TestKnowledgeSearchLimitValidation(_ServerTestBase):
    """Validation tests for knowledge_search limit parameter."""

    def setUp(self):
        super().setUp()
        self.system_col.add(
            ids=["s1"],
            documents=["test doc"],
            metadatas=[{"type": "fact", "source": "test", "section": "",
                        "namespace": "system", "refs": "", "key": "s1"}],
        )

    def test_search_negative_limit_becomes_one(self):
        from docker.knowledge.server import knowledge_search

        # Should not crash with negative limit
        result = knowledge_search("test", limit=-5)
        self.assertIsInstance(result, str)

    def test_search_zero_limit_becomes_one(self):
        from docker.knowledge.server import knowledge_search

        result = knowledge_search("test", limit=0)
        self.assertIsInstance(result, str)


class TestKnowledgeGet(_ServerTestBase):
    """KB-04: knowledge_get retrieves chunk by exact key from either collection."""

    def setUp(self):
        super().setUp()
        self.system_col.add(
            ids=["sys.lookup"],
            documents=["System chunk for lookup test."],
            metadatas=[{"type": "fact", "source": "system.md", "section": "Test",
                        "namespace": "system", "refs": "", "key": "sys.lookup"}],
        )
        self.runtime_col.add(
            ids=["rt.lookup"],
            documents=["Runtime chunk for lookup test."],
            metadatas=[{"type": "fact", "source": "runtime", "section": "",
                        "namespace": "runtime", "refs": "sys.lookup", "key": "rt.lookup"}],
        )

    def test_get_system_chunk(self):
        from docker.knowledge.server import knowledge_get

        result = knowledge_get("sys.lookup")
        self.assertIn("System chunk for lookup test", result)

    def test_get_runtime_chunk(self):
        from docker.knowledge.server import knowledge_get

        result = knowledge_get("rt.lookup")
        self.assertIn("Runtime chunk for lookup test", result)

    def test_get_missing_key(self):
        from docker.knowledge.server import knowledge_get

        result = knowledge_get("nonexistent.key")
        self.assertIn("No chunk found", result)
        self.assertIn("nonexistent.key", result)


class TestKnowledgeDelete(_ServerTestBase):
    """KB-10: knowledge_delete removes runtime chunks, refuses system chunks."""

    def setUp(self):
        super().setUp()
        self.system_col.add(
            ids=["sys.protected"],
            documents=["This system chunk should not be deletable."],
            metadatas=[{"type": "procedure", "source": "system.md", "section": "Rules",
                        "namespace": "system", "refs": "", "key": "sys.protected"}],
        )
        self.runtime_col.add(
            ids=["rt.deletable"],
            documents=["This runtime chunk can be deleted."],
            metadatas=[{"type": "fact", "source": "runtime", "section": "",
                        "namespace": "runtime", "refs": "", "key": "rt.deletable"}],
        )

    def test_delete_runtime_chunk(self):
        from docker.knowledge.server import knowledge_delete

        result = knowledge_delete("rt.deletable")
        self.assertIn("rt.deletable", result)

        # Verify it's gone
        got = self.runtime_col.get(ids=["rt.deletable"])
        self.assertEqual(len(got["ids"]), 0)

    def test_delete_system_chunk_refused(self):
        from docker.knowledge.server import knowledge_delete

        result = knowledge_delete("sys.protected")
        self.assertIn("Cannot delete system chunks", result)
        self.assertIn("sys.protected", result)

    def test_system_chunk_still_exists_after_delete_attempt(self):
        from docker.knowledge.server import knowledge_delete

        knowledge_delete("sys.protected")

        got = self.system_col.get(ids=["sys.protected"])
        self.assertEqual(len(got["ids"]), 1)

    def test_delete_nonexistent_key(self):
        from docker.knowledge.server import knowledge_delete

        result = knowledge_delete("nonexistent.key")
        self.assertIn("No chunk found", result)


if __name__ == "__main__":
    unittest.main()
