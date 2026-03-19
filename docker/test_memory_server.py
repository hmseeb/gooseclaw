"""Tests for mem0 MCP memory server tools (MEM-01 through MEM-05).

Mocks mem0.Memory to test tool functions in isolation.
No LLM or vector store calls needed.
"""

import json
import unittest
from unittest.mock import MagicMock, patch


def _make_mock_memory():
    """Create a mock Memory object with all required methods."""
    mock = MagicMock()
    mock.add = MagicMock()
    mock.search = MagicMock()
    mock.delete = MagicMock()
    mock.get_all = MagicMock()
    mock.history = MagicMock()
    mock.get = MagicMock()
    return mock


# We can't import memory.server directly because mem0 and mcp aren't
# installed locally. Mock both modules before importing.
import sys
import os

# Mock mcp module so memory.server can import FastMCP
_mock_mcp = MagicMock()
_mock_fastmcp_class = MagicMock()
# Make FastMCP("mem0-memory") return a mock that has a .tool() decorator
_mock_fastmcp_instance = MagicMock()
_mock_fastmcp_instance.tool.return_value = lambda fn: fn  # passthrough decorator
_mock_fastmcp_class.return_value = _mock_fastmcp_instance
_mock_mcp.server.fastmcp.FastMCP = _mock_fastmcp_class
sys.modules["mcp"] = _mock_mcp
sys.modules["mcp.server"] = _mock_mcp.server
sys.modules["mcp.server.fastmcp"] = _mock_mcp.server.fastmcp

# Mock mem0 module so memory.server can import Memory
_mock_mem0 = MagicMock()
_mock_memory_instance = _make_mock_memory()
_mock_mem0.Memory.from_config.return_value = _mock_memory_instance
sys.modules["mem0"] = _mock_mem0

# Add parent dir to path so mem0_config can be found
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Now import the server module
from memory.server import (
    memory_add,
    memory_search,
    memory_delete,
    memory_list,
    memory_history,
    memory_get,
)
import memory.server as srv


class _MemoryTestBase(unittest.TestCase):
    """Base class that patches the module-level memory object."""

    def setUp(self):
        self.mock_memory = _make_mock_memory()
        self._orig_memory = srv.memory
        srv.memory = self.mock_memory

    def tearDown(self):
        srv.memory = self._orig_memory


class TestMemoryAdd(_MemoryTestBase):
    """MEM-01: memory_add stores a memory and returns result."""

    def test_add_returns_result(self):
        self.mock_memory.add.return_value = {
            "results": [{"id": "abc123", "memory": "user likes Python"}]
        }
        result = memory_add("I like Python")
        parsed = json.loads(result)
        self.assertIn("results", parsed)
        self.assertEqual(parsed["results"][0]["id"], "abc123")
        self.mock_memory.add.assert_called_once()

    def test_add_error_handling(self):
        self.mock_memory.add.side_effect = Exception("LLM timeout")
        result = memory_add("test content")
        self.assertIn("Failed to store memory:", result)
        self.assertIn("LLM timeout", result)


class TestMemorySearch(_MemoryTestBase):
    """MEM-02: memory_search returns semantically relevant memories."""

    def test_search_returns_formatted(self):
        self.mock_memory.search.return_value = {
            "results": [
                {"id": "x1", "memory": "user prefers dark theme", "score": 0.95},
                {"id": "x2", "memory": "user uses VSCode", "score": 0.82},
            ]
        }
        result = memory_search("what theme does user like")
        self.assertIn("0.95", result)
        self.assertIn("dark theme", result)
        self.assertIn("x1", result)

    def test_search_empty(self):
        self.mock_memory.search.return_value = {"results": []}
        result = memory_search("nonexistent query")
        self.assertEqual(result, "No matching memories found.")

    def test_search_limit_clamped(self):
        self.mock_memory.search.return_value = {"results": []}
        memory_search("test", limit=100)
        _, kwargs = self.mock_memory.search.call_args
        self.assertEqual(kwargs["limit"], 20)

    def test_search_limit_minimum(self):
        self.mock_memory.search.return_value = {"results": []}
        memory_search("test", limit=-5)
        _, kwargs = self.mock_memory.search.call_args
        self.assertEqual(kwargs["limit"], 1)

    def test_search_handles_list_format(self):
        """Handle case where search returns a list instead of dict."""
        self.mock_memory.search.return_value = [
            {"id": "y1", "memory": "test fact", "score": 0.9}
        ]
        result = memory_search("test")
        self.assertIn("test fact", result)


class TestMemoryDelete(_MemoryTestBase):
    """MEM-03: memory_delete removes a memory by ID."""

    def test_delete_success(self):
        self.mock_memory.delete.return_value = None
        result = memory_delete("mem-abc")
        self.assertIn("Deleted memory:", result)
        self.assertIn("mem-abc", result)
        self.mock_memory.delete.assert_called_once_with(memory_id="mem-abc")

    def test_delete_error(self):
        self.mock_memory.delete.side_effect = Exception("not found")
        result = memory_delete("bad-id")
        self.assertIn("Failed to delete:", result)
        self.assertIn("not found", result)


class TestMemoryList(_MemoryTestBase):
    """MEM-04: memory_list returns all stored memories."""

    def test_list_returns_formatted(self):
        self.mock_memory.get_all.return_value = {
            "results": [
                {"id": "m1", "memory": "user is a developer"},
                {"id": "m2", "memory": "user likes coffee"},
            ]
        }
        result = memory_list()
        self.assertIn("developer", result)
        self.assertIn("coffee", result)
        self.assertIn("m1", result)

    def test_list_empty(self):
        self.mock_memory.get_all.return_value = {"results": []}
        result = memory_list()
        self.assertEqual(result, "No memories stored yet.")

    def test_list_respects_limit(self):
        self.mock_memory.get_all.return_value = {
            "results": [{"id": f"m{i}", "memory": f"fact {i}"} for i in range(20)]
        }
        result = memory_list(limit=3)
        lines = [l for l in result.split("\n") if l.startswith("-")]
        self.assertEqual(len(lines), 3)

    def test_list_handles_list_format(self):
        """Handle case where get_all returns a list instead of dict."""
        self.mock_memory.get_all.return_value = [
            {"id": "z1", "memory": "list format fact"}
        ]
        result = memory_list()
        self.assertIn("list format fact", result)


class TestMemoryHistory(_MemoryTestBase):
    """MEM-05: memory_history returns the audit trail."""

    def test_history_returns_events(self):
        self.mock_memory.history.return_value = [
            {
                "event": "ADD",
                "old_memory": "",
                "new_memory": "user likes Python",
                "created_at": "2026-03-20",
            },
            {
                "event": "UPDATE",
                "old_memory": "user likes Python",
                "new_memory": "user loves Python and Go",
                "created_at": "2026-03-21",
            },
        ]
        result = memory_history("mem-abc")
        self.assertIn("[ADD]", result)
        self.assertIn("[UPDATE]", result)
        self.assertIn("Python", result)

    def test_history_empty(self):
        self.mock_memory.history.return_value = []
        result = memory_history("nonexistent")
        self.assertEqual(result, "No history found for this memory.")


class TestMemoryGet(_MemoryTestBase):
    """memory_get retrieves a specific memory by ID."""

    def test_get_returns_formatted(self):
        self.mock_memory.get.return_value = {
            "id": "abc123",
            "memory": "user prefers dark mode",
            "created_at": "2026-03-20",
            "updated_at": "2026-03-21",
        }
        result = memory_get("abc123")
        self.assertIn("dark mode", result)
        self.assertIn("abc123", result)
        self.assertIn("2026-03-20", result)

    def test_get_not_found(self):
        self.mock_memory.get.return_value = None
        result = memory_get("nonexistent")
        self.assertEqual(result, "Memory not found.")


if __name__ == "__main__":
    unittest.main()
