import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HttpFlowTests(unittest.TestCase):
    def test_arch_launcher_local_api_and_full_demo_flow(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home}
            proc = subprocess.Popen(["bash", str(ROOT / "run.sh"), "--no-browser"], cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = proc.stdout.readline().strip()
                self.assertIn("ForgeCycle ready: http://127.0.0.1:", line)
                base = line.split("ForgeCycle ready: ", 1)[1]

                def get(route):
                    with urllib.request.urlopen(base + route, timeout=3) as response:
                        return json.load(response)

                token = get("/api/state")["csrf"]
                self.assertIn("ForgeCycle", urllib.request.urlopen(base).read().decode())
                self.assertIn("model-row", urllib.request.urlopen(base + "/style.css").read().decode())

                def post(route, data, origin=base):
                    request = urllib.request.Request(base + "/api/" + route, data=json.dumps(data).encode(),
                                                     headers={"Content-Type": "application/json", "Origin": origin, "X-ForgeCycle-Token": token})
                    with urllib.request.urlopen(request, timeout=3) as response:
                        return json.load(response)

                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post("settings", {"role": "planner", "provider": "demo"}, origin="http://evil.invalid")
                self.assertEqual(rejected.exception.code, 403)
                post("settings", {"role": "planner", "provider": "demo", "model": "demo", "apply_all": True})
                post("start", {"prompt": "أريد برنامج ترحيب في Python"})

                def until(phases):
                    for _ in range(100):
                        state = get("/api/state")["state"]
                        if state["phase"] in phases:
                            return state
                        time.sleep(.03)
                    self.fail("Timed out: " + repr(state))

                self.assertEqual(until({"questions", "error"})["phase"], "questions")
                post("answer", {"answers": ["برنامج صغير", "مستخدم واحد", "يطبع تحية"]})
                state = until({"approval", "error"})
                self.assertEqual(state["phase"], "approval")
                workspace = str(Path(home) / "created-project")
                self.assertFalse(Path(workspace).exists())
                post("approve", {"brief": state["brief"], "acceptance": state["acceptance"], "workspace": workspace})
                final = until({"complete", "error", "needs_attention"})
                self.assertEqual(final["phase"], "complete", final.get("error"))
                self.assertTrue((Path(workspace) / "main.py").exists())
                self.assertTrue(get("/api/history")["history"])
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=3)
