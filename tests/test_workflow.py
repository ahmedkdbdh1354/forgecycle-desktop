import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from forgecycle import engine
from forgecycle.providers import ProviderError, detect_provider, parse_object


def wait_for(flow, phases, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        phase = flow.public()["state"]["phase"]
        if phase in phases:
            return phase
        time.sleep(.03)
    raise AssertionError("Workflow did not reach phase " + repr(phases) + ": " + str(flow.public()["state"]))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name) / "config"
        for target, value in (("CONFIG_DIR", base), ("SETTINGS_FILE", base / "settings.json"), ("RUNS_DIR", base / "runs")):
            ctx = patch.object(engine, target, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        self.flow = engine.Workflow()
        self.flow.save_settings({"role": "planner", "provider": "demo", "model": "demo", "apply_all": True})

    def test_planning_needs_answers_and_explicit_approval_before_writing(self):
        project = Path(self.temp.name) / "project"
        self.flow.start("برنامج ترحيب بلغة بايثون")
        self.assertEqual(wait_for(self.flow, {"questions", "error"}), "questions")
        self.assertFalse(project.exists())
        with self.assertRaises(ValueError):
            self.flow.approve("تفاصيل متفق عليها طويلة بما يكفي", [], str(project))
        self.flow.answer(["يرحب", "شخص واحد", "يتنفذ بدون أخطاء"])
        self.assertEqual(wait_for(self.flow, {"approval", "error"}), "approval")
        self.assertFalse(project.exists())
        state = self.flow.public()["state"]
        self.flow.approve(state["brief"], state["acceptance"], str(project))
        self.assertEqual(wait_for(self.flow, {"complete", "error", "needs_attention"}), "complete")
        self.assertTrue((project / "main.py").exists())
        self.assertIn("exit=0", self.flow.public()["state"]["checks"])
        self.assertEqual(self.flow.public()["state"]["round"], 1)

    def test_settings_never_expose_key_and_file_permissions(self):
        self.flow.save_settings({"role": "builder", "provider": "kimi", "api_key": "test-secret", "model": "kimi-k3"})
        self.assertTrue(self.flow.public()["settings"]["builder"]["has_key"])
        self.assertNotIn("test-secret", str(self.flow.public()))
        self.assertEqual(engine.SETTINGS_FILE.stat().st_mode & 0o777, 0o600)

    def test_paths_and_secret_files_are_blocked(self):
        root = Path(self.temp.name) / "work"
        root.mkdir()
        for name in ("../outside.py", "/tmp/outside.py", ".env", ".git/config", "sub/../../escape"):
            with self.assertRaises(ValueError, msg=name):
                engine.safe_path(root, name)
        (root / "link").symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(ValueError):
            engine.safe_path(root, "link/file.py")


class ProviderTests(unittest.TestCase):
    def test_ambiguous_key_never_sent_to_an_unrelated_host(self):
        self.assertEqual(detect_provider("sk-ambiguous"), "unknown")
        self.assertEqual(detect_provider("sk-ant-test"), "anthropic")
        self.assertEqual(detect_provider("AIza-demo"), "gemini")
        self.assertEqual(detect_provider("sk-anything", "https://api.moonshot.ai/v1"), "kimi")

    def test_json_fenced_and_plain(self):
        self.assertEqual(parse_object('```json\n{"ready":true}\n```')["ready"], True)
        with self.assertRaises(ProviderError):
            parse_object("this is not JSON")


if __name__ == "__main__":
    unittest.main()
