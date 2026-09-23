"""Persistent local workflow with an explicit approval gate and bounded review loop."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from .providers import ProviderError, complete, list_models, parse_object, resolve


CONFIG_DIR = Path.home() / ".config" / "forgecycle"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
RUNS_DIR = CONFIG_DIR / "runs"
ROLES = ("planner", "builder", "reviewer")
SKIP_PARTS = {".git", ".forgecycle", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
SECRET_NAMES = {".env", ".env.local", "credentials.json", "settings.json", "id_rsa", "id_ed25519", ".npmrc", ".pypirc", ".git-credentials", "secrets.json"}


def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def safe_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("اسم ملف غير صالح.")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or any(p in SKIP_PARTS for p in relative.parts):
        raise ValueError("المسار خارج المشروع أو محجوز.")
    if relative.name.lower() in SECRET_NAMES or relative.name.startswith(".env"):
        raise ValueError("لا يُسمح للنموذج بتعديل ملفات الأسرار.")
    destination = (root / relative).resolve()
    if not destination.is_relative_to(root.resolve()) or (root / relative).is_symlink():
        raise ValueError("المسار يتجاوز مجلد المشروع.")
    return destination


def snapshot(root: Path) -> str:
    entries, length = [], 0
    if not root.exists():
        return "(empty project)"
    for file in sorted(root.rglob("*")):
        rel = file.relative_to(root)
        if any(p in SKIP_PARTS for p in rel.parts) or file.name.lower() in SECRET_NAMES or file.name.startswith(".env"):
            continue
        if not file.is_file() or file.is_symlink() or file.stat().st_size > 35000:
            continue
        try:
            content = file.read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            continue
        line = f"\n### {rel}\n{content[:22000]}\n"
        if length + len(line) > 110_000:
            break
        entries.append(line)
        length += len(line)
    return "".join(entries) or "(empty project)"


def write_files(root: Path, files: list, round_number: int) -> list[str]:
    if not isinstance(files, list) or not files or len(files) > 32:
        raise ValueError("البنّاء يجب أن يرسل من 1 إلى 32 ملفًا في الجولة.")
    pending = []
    total = 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise ValueError("صيغة الملفات المرسلة غير صحيحة.")
        target = safe_path(root, item.get("path", ""))
        data = item["content"].encode("utf-8")
        total += len(data)
        if len(data) > 170_000 or total > 450_000:
            raise ValueError("حجم الملفات في الجولة كبير؛ قسّم العمل إلى خطوات أصغر.")
        pending.append((target, data))
    if len({p for p, _ in pending}) != len(pending):
        raise ValueError("البنّاء أرسل مسارات ملفات مكررة.")
    backups = root / ".forgecycle" / "history" / f"round-{round_number}"
    # Validate everything before making any changes; preserve previous versions.
    for target, data in pending:
        relative = target.relative_to(root)
        if target.exists():
            old = backups / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(target.read_bytes())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return [str(path.relative_to(root)) for path, _ in pending]


def verify_project(root: Path, optional_command: str = "") -> str:
    commands = []
    if any(root.rglob("*.py")):
        commands.append([sys.executable, "-m", "compileall", "-q", str(root)])
    package = root / "package.json"
    if package.exists() and (root / "node_modules").exists():
        try:
            scripts = json.loads(package.read_text()).get("scripts", {})
            for name in ("test", "build"):
                if name in scripts:
                    commands.append(["npm", "run", name, "--", "--run"] if name == "test" else ["npm", "run", name])
        except (OSError, ValueError, AttributeError):
            pass
    if optional_command.strip():
        args = shlex.split(optional_command)
        if not args or Path(args[0]).name not in {"python", "python3", "pytest", "npm", "npx", "cargo", "go", "make"}:
            raise ValueError("أمر الفحص يجب أن يبدأ بـ python أو pytest أو npm أو npx أو cargo أو go أو make.")
        commands.append(args)
    if not commands:
        return "لم يُضبط فحص آلي لهذا النوع من المشروع."
    reports = []
    for args in commands:
        label = " ".join(args[:4])
        try:
            result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=90, shell=False)
            reports.append(f"$ {label}\nexit={result.returncode}\n{(result.stdout + result.stderr)[-7000:]}")
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            reports.append(f"$ {label}\nerror={type(exc).__name__}")
    return "\n\n".join(reports)[:14000]


PLANNER = """You are the PLANNER. Speak Arabic, ask clear questions, and do not start implementation.
Return ONLY a JSON object: {"questions":["..."],"brief":"...","acceptance":["..."],"ready":false}.
On the FIRST interaction ask 3 to 6 useful, non-redundant questions, ready=false.
After answers, ask follow-up questions only for genuine blockers. When sufficiently clear, set ready=true, give a detailed brief and measurable acceptance criteria. Never claim certainty about unstated details."""
BUILDER = """You are the BUILDER. Create or repair a real project from the approved brief.
Return ONLY JSON {"summary":"...","files":[{"path":"relative/path","content":"full UTF-8 contents"}],"complete":true,"next_steps":"..."}.
Write complete files, never markdown fences or pseudo code. Include README with install and launch instructions. Never write .env, credentials or .git files. Repair issues and failing checks. Keep the change within 32 files and 450KB of UTF-8 per round. The coordinator will apply your files and run checks."""
REVIEWER = """You are the REVIEWER. Critically compare implementation with the approved brief and acceptance criteria.
Return ONLY JSON {"approved":false,"issues":[{"severity":"critical|major|minor","file":"path","problem":"...","fix":"..."}],"summary":"..."}.
Check actual files and check results. Approve only if criteria are satisfied and checks pass. Never pretend to have tested something that is not shown. Prioritize concrete actionable problems."""


class Workflow:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.settings = {"roles": {r: {"provider": "auto", "api_key": "", "base_url": "", "model": ""} for r in ROLES}}
        if SETTINGS_FILE.exists():
            try:
                loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                self.settings["roles"].update(loaded.get("roles", {}))
            except (OSError, ValueError):
                pass
        self.state = self._initial_state()
        self.worker = None

    @staticmethod
    def _initial_state():
        return {"phase": "idle", "prompt": "", "answers": [], "questions": [], "brief": "", "acceptance": [], "round": 0, "events": [], "workspace": "", "files": [], "checks": "", "usage": {"input": 0, "output": 0, "calls": 0, "kimi_estimate_usd": 0.0}, "error": ""}

    def public(self):
        with self.lock:
            configs = {}
            for role in ROLES:
                cfg = dict(self.settings["roles"].get(role, {}))
                cfg["has_key"] = bool(cfg.pop("api_key", ""))
                configs[role] = cfg
            return {"state": json.loads(json.dumps(self.state)), "settings": configs}

    def save_settings(self, data: dict):
        role = data.get("role")
        if role not in ROLES:
            raise ValueError("الدور غير معروف.")
        kind = str(data.get("provider", "auto"))
        if kind not in {"auto", "openai", "kimi", "gemini", "anthropic", "compatible", "demo"}:
            raise ValueError("المزوّد غير معروف.")
        selected = list(ROLES) if data.get("apply_all") else [role]
        with self.lock:
            for target in selected:
                old = self.settings["roles"].get(target, {})
                update = {"provider": kind, "base_url": str(data.get("base_url", "")).strip(), "model": str(data.get("model", "")).strip(), "reasoning_effort": str(data.get("reasoning_effort", "high"))}
                update["api_key"] = str(data["api_key"]).strip() if data.get("api_key") else old.get("api_key", "")
                if data.get("clear_key"):
                    update["api_key"] = ""
                self.settings["roles"][target] = update
            _atomic_json(SETTINGS_FILE, self.settings)
        return self.public()["settings"]

    def models(self, role: str, draft: dict | None = None):
        if role not in ROLES:
            raise ValueError("الدور غير معروف.")
        with self.lock:
            config = dict(self.settings["roles"].get(role, {}))
        if draft:
            for field in ("provider", "base_url"):
                if field in draft:
                    config[field] = draft[field]
            if draft.get("api_key"):
                config["api_key"] = draft["api_key"]
        kind, _, _ = resolve(config)
        return {"provider": kind, "models": list_models(config)}

    def _emit(self, role: str, message: str):
        with self.lock:
            self.state["events"].append({"time": time.strftime("%H:%M:%S"), "role": role, "text": message[:1500]})
            self.state["events"] = self.state["events"][-100:]
            self._persist()

    def _persist(self):
        if self.state.get("id"):
            _atomic_json(RUNS_DIR / f"{self.state['id']}.json", self.state)

    def _call(self, role, system, prompt):
        with self.lock:
            if self.state["usage"]["calls"] >= self.state.get("max_calls", 60):
                raise RuntimeError("وصل التنفيذ إلى حد طلبات API المحدد في الإعدادات.")
            config = dict(self.settings["roles"].get(role, {}))
        reply = complete(config, system, prompt, cancel=self.cancel_event)
        with self.lock:
            usage = self.state["usage"]
            usage["calls"] += 1
            usage["input"] += reply.input_tokens
            usage["output"] += reply.output_tokens
            if resolve(config)[0] == "kimi":
                usage["kimi_estimate_usd"] = round(usage["kimi_estimate_usd"] + reply.input_tokens * 3 / 1e6 + reply.output_tokens * 15 / 1e6, 4)
            self._persist()
        return parse_object(reply.text)

    def _launch(self, fn, *args):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("عملية أخرى قيد التنفيذ.")
            self.cancel_event.clear()
            self.worker = threading.Thread(target=self._guard, args=(fn, args), daemon=True)
            self.worker.start()

    def _guard(self, fn, args):
        try:
            fn(*args)
        except Exception as exc:
            with self.lock:
                self.state["phase"] = "stopped" if self.cancel_event.is_set() else "error"
                self.state["error"] = str(exc)[:500]
                self._persist()
            self._emit("system", "توقف التنفيذ: " + str(exc)[:400])

    def start(self, prompt: str, max_rounds=4, max_calls=60):
        if not prompt.strip() or len(prompt) > 10000:
            raise ValueError("اكتب وصفًا للمشروع أقل من 10 آلاف حرف.")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("عملية أخرى قيد التنفيذ.")
            self.state = self._initial_state()
            self.state.update({"id": str(int(time.time() * 1000)), "phase": "planning", "prompt": prompt.strip(), "max_rounds": max(1, min(int(max_rounds), 8)), "max_calls": max(4, min(int(max_calls), 200))})
            self._persist()
        self._launch(self._plan)

    def answer(self, answers: list[str]):
        with self.lock:
            if self.state["phase"] != "questions":
                raise ValueError("ماكو أسئلة بانتظار الإجابة.")
            questions = list(self.state["questions"])
            if not isinstance(answers, list) or len(answers) != len(questions) or any(not str(x).strip() for x in answers):
                raise ValueError("أجب على جميع الأسئلة أولًا.")
            self.state["answers"].extend({"question": q, "answer": str(a)[:3000]} for q, a in zip(questions, answers))
            self.state["phase"] = "planning"
            self._persist()
        self._launch(self._plan)

    def _plan(self):
        with self.lock:
            answers = list(self.state["answers"])
            original = self.state["prompt"]
        self._emit("planner", "أحلّل الفكرة وأجهّز الأسئلة..." if not answers else "أراجع إجاباتك وأجهّز المواصفات...")
        prompt = "USER REQUEST:\n" + original + ("\nANSWERS:\n" + json.dumps(answers, ensure_ascii=False) if answers else "")
        result = self._call("planner", PLANNER, prompt)
        questions = result.get("questions", [])
        if not isinstance(questions, list):
            questions = []
        questions = [str(x)[:400] for x in questions[:6] if str(x).strip()]
        with self.lock:
            if self.cancel_event.is_set():
                self.state["phase"] = "stopped"
            elif result.get("ready") and answers and str(result.get("brief", "")).strip():
                self.state["brief"] = str(result["brief"])[:12000]
                self.state["acceptance"] = [str(x)[:500] for x in result.get("acceptance", [])[:15]]
                self.state["phase"] = "approval"
            elif questions:
                self.state["questions"] = questions
                self.state["phase"] = "questions"
            else:
                raise ProviderError("المنسّق لم يعطِ مواصفات أو أسئلة واضحة؛ أعد صياغة الطلب.")
            self._persist()
        self._emit("planner", "بانتظار موافقتك على الخطة." if self.state["phase"] == "approval" else "بانتظار إجاباتك على الأسئلة.")

    def approve(self, brief: str, acceptance: list[str], workspace: str, test_command: str = ""):
        with self.lock:
            if self.state["phase"] != "approval":
                raise ValueError("لازم يكتمل التخطيط أولًا.")
            if len(brief.strip()) < 20 or len(brief) > 14000:
                raise ValueError("ملخص المتطلبات غير صالح.")
            root = Path(workspace).expanduser().resolve() if workspace.strip() else (Path.home() / "ForgeCycle" / "projects" / re.sub(r"[^a-z0-9-]", "-", self.state["prompt"].lower()[:30]).strip("-")).resolve()
            if root == Path.home() or root == Path("/") or root == CONFIG_DIR or root.is_relative_to(CONFIG_DIR):
                raise ValueError("اختر مجلد مشروع مخصصًا.")
            self.state.update({"brief": brief.strip(), "acceptance": [str(x)[:500] for x in acceptance[:15] if str(x).strip()], "workspace": str(root), "test_command": str(test_command)[:240], "phase": "building"})
            self._persist()
        self._launch(self._build)

    def _build(self):
        with self.lock:
            root = Path(self.state["workspace"])
            rounds = self.state["max_rounds"]
            brief = self.state["brief"]
            acceptance = list(self.state["acceptance"])
            command = self.state.get("test_command", "")
        root.mkdir(parents=True, exist_ok=True)
        feedback = ""
        for n in range(1, rounds + 1):
            if self.cancel_event.is_set():
                raise RuntimeError("تم إيقاف التنفيذ.")
            with self.lock:
                self.state.update({"phase": "building", "round": n})
                self._persist()
            self._emit("builder", f"جولة {n}/{rounds}: أبني أو أصلح الملفات.")
            files = []
            finished = False
            next_steps = ""
            for step in range(1, 4):
                if self.cancel_event.is_set():
                    raise RuntimeError("تم إيقاف التنفيذ.")
                body = f"APPROVED BRIEF:\n{brief}\nACCEPTANCE:\n{json.dumps(acceptance, ensure_ascii=False)}\nCURRENT FILES:\n{snapshot(root)}\nPREVIOUS REVIEW/CHECKS:\n{feedback}\nSTEP {step}/3; REMAINING WORK:\n{next_steps}"
                creation = self._call("builder", BUILDER, body)
                written = write_files(root, creation.get("files", []), n * 10 + step)
                files.extend(written)
                self._emit("builder", str(creation.get("summary", "تم تحديث الملفات.")) + " | " + ", ".join(written[:10]))
                finished = creation.get("complete") is True
                next_steps = str(creation.get("next_steps", ""))[:1000]
                if finished:
                    break
            with self.lock:
                self.state["files"] = files
                self.state["phase"] = "reviewing"
                self._persist()
            checks = verify_project(root, command)
            with self.lock:
                self.state["checks"] = checks
                self._persist()
            self._emit("system", "نتائج الفحص: " + checks[-1100:])
            if self.cancel_event.is_set():
                raise RuntimeError("تم إيقاف التنفيذ.")
            self._emit("reviewer", "أراجع الملفات مقابل المعايير ونتائج الفحص...")
            review = self._call("reviewer", REVIEWER, f"BRIEF:\n{brief}\nACCEPTANCE:\n{json.dumps(acceptance, ensure_ascii=False)}\nFILES:\n{snapshot(root)}\nCHECK RESULTS:\n{checks}")
            issues = review.get("issues", [])
            if not isinstance(issues, list):
                issues = []
            check_failed = "exit=" in checks and any("exit=" + str(i) in checks for i in range(1, 20))
            self._emit("reviewer", str(review.get("summary", "")) + (" | مشاكل: " + json.dumps(issues[:8], ensure_ascii=False) if issues else ""))
            if review.get("approved") is True and not check_failed and finished:
                with self.lock:
                    self.state["phase"] = "complete"
                    self._persist()
                self._emit("system", "اكتمل المشروع بعد مراجعة الوكيل والفحوص المتاحة.")
                return
            feedback = json.dumps({"issues": issues[:15], "checks": checks[-7000:], "unfinished": not finished, "next_steps": next_steps}, ensure_ascii=False)
        with self.lock:
            self.state["phase"] = "needs_attention"
            self._persist()
        self._emit("system", "وصلنا للحد المحدد من الجولات؛ راجع التقرير والملفات قبل الاستعمال.")

    def cancel(self):
        self.cancel_event.set()
        self._emit("system", "طُلب الإيقاف. سينتهي الطلب الحالي أولًا.")
