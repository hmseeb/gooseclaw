"""Tests for mem0 config builder (CFG-01, CFG-02, CFG-03, CFG-04).

Verifies build_mem0_config() returns correct config dicts for all
provider types, with chromadb vector store, huggingface embedder,
and cheap extraction models.
"""

import json
import os
import tempfile
import unittest


class TestConfigDefaults(unittest.TestCase):
    """CFG-01 / CFG-04: default config structure with no setup.json."""

    def setUp(self):
        """Point CONFIG_DIR at an empty temp dir (no setup.json)."""
        self._tmpdir = tempfile.mkdtemp()
        import mem0_config
        self._orig_setup_file = mem0_config.SETUP_FILE
        mem0_config.SETUP_FILE = os.path.join(self._tmpdir, "setup.json")

    def tearDown(self):
        import mem0_config
        mem0_config.SETUP_FILE = self._orig_setup_file
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_default_config_structure(self):
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertIn("vector_store", config)
        self.assertIn("embedder", config)
        self.assertIn("llm", config)
        self.assertIn("version", config)

    def test_vector_store_is_chromadb(self):
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertEqual(config["vector_store"]["provider"], "chromadb")
        self.assertEqual(
            config["vector_store"]["config"]["collection_name"],
            "mem0_memories",
        )

    def test_embedder_is_huggingface(self):
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertEqual(config["embedder"]["provider"], "huggingface")
        self.assertIn("MiniLM", config["embedder"]["config"]["model"])

    def test_version_is_v1_1(self):
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertEqual(config["version"], "v1.1")


class TestConfigProvider(unittest.TestCase):
    """CFG-02: reads provider from setup.json and maps correctly."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        import mem0_config
        self._orig_setup_file = mem0_config.SETUP_FILE
        self._setup_path = os.path.join(self._tmpdir, "setup.json")
        mem0_config.SETUP_FILE = self._setup_path

    def tearDown(self):
        import mem0_config
        mem0_config.SETUP_FILE = self._orig_setup_file
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_setup(self, provider_type):
        with open(self._setup_path, "w") as f:
            json.dump({"provider_type": provider_type}, f)

    def test_reads_anthropic_from_setup(self):
        self._write_setup("anthropic")
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertEqual(config["llm"]["provider"], "anthropic")

    def test_reads_openai_from_setup(self):
        self._write_setup("openai")
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertEqual(config["llm"]["provider"], "openai")

    def test_reads_openrouter_from_setup(self):
        self._write_setup("openrouter")
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertEqual(config["llm"]["provider"], "litellm")

    def test_fallback_without_setup(self):
        # No setup.json written
        import mem0_config
        mem0_config.SETUP_FILE = os.path.join(self._tmpdir, "nonexistent.json")
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertEqual(config["llm"]["provider"], "anthropic")


class TestConfigCheapModel(unittest.TestCase):
    """CFG-03: routes extraction to cheap model, not user's main model."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        import mem0_config
        self._orig_setup_file = mem0_config.SETUP_FILE
        self._setup_path = os.path.join(self._tmpdir, "setup.json")
        mem0_config.SETUP_FILE = self._setup_path

    def tearDown(self):
        import mem0_config
        mem0_config.SETUP_FILE = self._orig_setup_file
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_setup(self, provider_type):
        with open(self._setup_path, "w") as f:
            json.dump({"provider_type": provider_type}, f)

    def test_anthropic_cheap_model(self):
        self._write_setup("anthropic")
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        model = config["llm"]["config"]["model"]
        self.assertEqual(model, "claude-haiku-4-20250414")
        self.assertNotIn("opus", model)
        self.assertNotIn("sonnet", model)

    def test_openai_cheap_model(self):
        self._write_setup("openai")
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertEqual(config["llm"]["config"]["model"], "gpt-4.1-nano")

    def test_google_cheap_model(self):
        self._write_setup("google")
        from mem0_config import build_mem0_config
        config = build_mem0_config()
        self.assertEqual(config["llm"]["config"]["model"], "gemini-2.0-flash")

    def test_all_providers_have_cheap_model(self):
        from mem0_config import CHEAP_MODELS
        expected_providers = [
            "anthropic", "openai", "google", "groq", "ollama",
            "deepseek", "together", "litellm", "openrouter", "azure-openai",
        ]
        for p in expected_providers:
            self.assertIn(p, CHEAP_MODELS, f"Missing cheap model for {p}")
        self.assertEqual(len(CHEAP_MODELS), 10)


if __name__ == "__main__":
    unittest.main()
