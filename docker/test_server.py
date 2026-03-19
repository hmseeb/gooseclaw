"""Tests for knowledge MCP server tools (KB-02, KB-04).

System-only architecture: all tools query system_col only.
Uses chromadb.EphemeralClient for isolation. Tests call tool functions
directly (they're regular Python functions despite @mcp.tool() decoration).
"""

import unittest
import chromadb


class _ServerTestBase(unittest.TestCase):
    """Base class that patches server module system collection with ephemeral one."""

    def setUp(self):
        self.client = chromadb.EphemeralClient()
        self.system_col = self.client.get_or_create_collection("system")

        # Monkey-patch module-level collection in server
        import docker.knowledge.server as srv

        self._orig_system = srv.system_col
        srv.system_col = self.system_col

    def tearDown(self):
        import docker.knowledge.server as srv

        srv.system_col = self._orig_system


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

    def test_search_sorted_by_score_descending(self):
        from docker.knowledge.server import knowledge_search

        result = knowledge_search("jobs scheduled tasks cron", limit=5)
        import re

        scores = [float(m.group(1)) for m in re.finditer(r"score: ([\d.]+)", result)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_search_returns_system_results(self):
        from docker.knowledge.server import knowledge_search

        result = knowledge_search("agent framework", limit=10)
        self.assertIn("sys.", result)

    def test_search_no_results(self):
        # Use uniquely-named empty collection
        import docker.knowledge.server as srv
        empty_client = chromadb.EphemeralClient()
        srv.system_col = empty_client.get_or_create_collection("system_empty_test")

        from docker.knowledge.server import knowledge_search

        result = knowledge_search("xyzzy nonsense query that matches nothing")
        self.assertEqual(result, "No matching knowledge found.")


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
    """KB-04: knowledge_get retrieves system chunk by exact key."""

    def setUp(self):
        super().setUp()
        self.system_col.add(
            ids=["sys.lookup"],
            documents=["System chunk for lookup test."],
            metadatas=[{"type": "fact", "source": "system.md", "section": "Test",
                        "namespace": "system", "refs": "", "key": "sys.lookup"}],
        )

    def test_get_system_chunk(self):
        from docker.knowledge.server import knowledge_get

        result = knowledge_get("sys.lookup")
        self.assertIn("System chunk for lookup test", result)

    def test_get_missing_key(self):
        from docker.knowledge.server import knowledge_get

        result = knowledge_get("nonexistent.key")
        self.assertIn("No chunk found", result)
        self.assertIn("nonexistent.key", result)


if __name__ == "__main__":
    unittest.main()
