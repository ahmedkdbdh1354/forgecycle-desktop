"""Small, dependency-free adapters for hosted language model APIs."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib import error, parse, request


class ProviderError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


DEFAULT_URL = {
    "openai": "https://api.openai.com/v1",
    "kimi": "https://api.moonshot.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1",
}


def detect_provider(key: str, base_url: str = "") -> str:
    """Never send an ambiguous secret to several vendors to guess its origin."""
    host = parse.urlparse(base_url).hostname or ""
    if base_url:
        if host in ("api.openai.com",):
            return "openai"
        if host in ("api.moonshot.ai", "api.kimi.ai"):
            return "kimi"
        if host == "generativelanguage.googleapis.com":
            return "gemini"
        if host == "api.anthropic.com":
            return "anthropic"
        return "compatible"
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith("sk-proj-") or key.startswith("sk-svcacct-"):
        return "openai"
    return "unknown"


def resolve(config: dict) -> tuple[str, str, str]:
    key = str(config.get("api_key", "")).strip()
    kind = str(config.get("provider", "auto"))
    base = str(config.get("base_url", "")).strip().rstrip("/")
    if kind == "auto":
        kind = detect_provider(key, base)
    if kind == "unknown":
        raise ProviderError("المفتاح وحده لا يحدد المزوّد بأمان. اختر المزوّد من الإعدادات.")
    if kind not in (*DEFAULT_URL, "compatible", "demo"):
        raise ProviderError("مزوّد غير معروف.")
    if kind != "demo" and not key:
        raise ProviderError("أضف مفتاح API في الإعدادات أولًا.")
    if kind == "compatible" and not base:
        raise ProviderError("أدخل عنوان API المتوافق مع OpenAI.")
    if base:
        parts = parse.urlparse(base)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ProviderError("عنوان API غير صالح.")
        if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ProviderError("الاتصال البعيد يجب أن يستخدم HTTPS.")
    return kind, base or DEFAULT_URL.get(kind, ""), key


def _fetch(url: str, headers: dict, payload: dict | None = None, timeout: int = 90) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read(4_000_000)
        return json.loads(raw)
    except error.HTTPError as exc:
        body = exc.read(1400).decode("utf-8", "replace")
        # Do not include the URL or credentials in any visible exception.
        if exc.code in (401, 403):
            raise ProviderError(f"المفتاح مرفوض أو ليست لديه صلاحية ({exc.code}).") from exc
        if exc.code == 429:
            raise ProviderError("وصلت إلى حد الطلبات أو الرصيد (429).") from exc
        try:
            message = json.loads(body).get("error", {}).get("message", body)
        except (ValueError, AttributeError):
            message = body
        raise ProviderError(f"خطأ المزوّد {exc.code}: {str(message)[:250]}") from exc
    except (error.URLError, TimeoutError) as exc:
        raise ProviderError("تعذر الاتصال بالمزوّد؛ تحقق من الإنترنت والعنوان.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderError("استجابة API ليست JSON صالحًا.") from exc


def list_models(config: dict) -> list[str]:
    kind, base, key = resolve(config)
    if kind == "demo":
        return ["demo"]
    if kind == "gemini":
        result = _fetch(base + "/models?pageSize=1000", {"x-goog-api-key": key})
        return sorted(x["name"].removeprefix("models/") for x in result.get("models", [])
                      if "generateContent" in x.get("supportedGenerationMethods", []))
    if kind == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        result = _fetch(base + "/models?limit=100", headers)
        return sorted(x["id"] for x in result.get("data", []) if isinstance(x.get("id"), str))
    result = _fetch(base + "/models", {"Authorization": "Bearer " + key})
    return sorted(x["id"] for x in result.get("data", []) if isinstance(x.get("id"), str))


def _openai_text(result: dict) -> str:
    if isinstance(result.get("output_text"), str) and result["output_text"]:
        return result["output_text"]
    chunks = []
    for item in result.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    chunks.append(part.get("text", ""))
    return "\n".join(chunks)


def complete(config: dict, system: str, prompt: str, *, cancel=None) -> Reply:
    kind, base, key = resolve(config)
    model = str(config.get("model", "")).strip()
    if kind == "demo":
        return demo_reply(system, prompt)
    if not model:
        raise ProviderError("اختر نموذجًا لهذا الدور في الإعدادات.")
    if kind == "gemini":
        url = base + "/models/" + parse.quote(model.removeprefix("models/"), safe="-") + ":generateContent"
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        payload = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 12000}}
    elif kind == "anthropic":
        url = base + "/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        payload = {"model": model, "max_tokens": 12000, "system": system, "messages": [{"role": "user", "content": prompt}]}
    elif kind == "openai":
        url = base + "/responses"
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        payload = {"model": model, "instructions": system, "input": prompt, "max_output_tokens": 12000}
    else:
        url = base + "/chat/completions"
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "max_tokens": 12000}
        if kind == "kimi":
            payload.pop("max_tokens")
            payload["max_completion_tokens"] = 12000
            payload["reasoning_effort"] = config.get("reasoning_effort", "high")
    for attempt in range(3):
        if cancel is not None and cancel.is_set():
            raise ProviderError("تم إيقاف التنفيذ.")
        try:
            result = _fetch(url, headers, payload, timeout=180)
            break
        except ProviderError as exc:
            if ("429" not in str(exc) and " 5" not in str(exc)) or attempt == 2:
                raise
            if cancel is not None and cancel.wait(2 ** attempt * 2):
                raise ProviderError("تم إيقاف التنفيذ.") from exc
            if cancel is None:
                time.sleep(2 ** attempt * 2)
    if kind == "gemini":
        parts = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        txt = "\n".join(p.get("text", "") for p in parts if p.get("text"))
        usage = result.get("usageMetadata", {})
        in_tok, out_tok = usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    elif kind == "anthropic":
        txt = "\n".join(x.get("text", "") for x in result.get("content", []) if x.get("type") == "text")
        usage = result.get("usage", {})
        in_tok, out_tok = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    elif kind == "openai":
        txt = _openai_text(result)
        usage = result.get("usage", {})
        in_tok, out_tok = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    else:
        choice = result.get("choices", [{}])[0]
        txt = choice.get("message", {}).get("content", "")
        if isinstance(txt, list):
            txt = "\n".join(x.get("text", "") for x in txt if isinstance(x, dict))
        usage = result.get("usage", {})
        in_tok, out_tok = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    if not isinstance(txt, str) or not txt.strip():
        raise ProviderError("النموذج أرجع ردًا فارغًا؛ قد يكون حدّ المخرجات صغيرًا لهذا الطلب.")
    return Reply(txt, int(in_tok or 0), int(out_tok or 0))


def parse_object(text: str) -> dict:
    clean = text.strip()
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE)
    try:
        value = json.loads(clean)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        pos = clean.find("{")
        if pos == -1:
            raise ProviderError("النموذج لم يرجع JSON صالحًا؛ أعد المحاولة.")
        try:
            value, _ = decoder.raw_decode(clean[pos:])
        except json.JSONDecodeError as exc:
            raise ProviderError("النموذج لم يرجع JSON صالحًا؛ أعد المحاولة.") from exc
    if not isinstance(value, dict):
        raise ProviderError("صيغة رد النموذج غير صحيحة.")
    return value


def demo_reply(system: str, prompt: str) -> Reply:
    """Offline example exercises every stage without suggesting it is real AI."""
    if "PLANNER" in system:
        data = ({"questions": ["شنو وظيفة التطبيق الأساسية؟", "من راح يستخدمه؟", "شنو معيار نجاحه؟"], "brief": "", "acceptance": [], "ready": False}
                if "ANSWERS:" not in prompt else {"questions": [], "brief": "أنشئ برنامج Python بسيط يرحب بالمستخدم. " + prompt[:180], "acceptance": ["يشتغل ملف main.py", "يحتوي على README.md"], "ready": True})
    elif "BUILDER" in system:
        data = {"summary": "مثال محلي توضيحي", "files": [{"path": "main.py", "content": "def greet(name):\n    return f'Hello, {name}!'\n\nif __name__ == '__main__':\n    print(greet('World'))\n"}, {"path": "README.md", "content": "# Demo project\n\nRun `python main.py`.\n"}], "complete": True}
    else:
        data = {"approved": True, "issues": [], "summary": "ملفات المثال سليمة. هذه مراجعة تجريبية فقط."}
    return Reply(json.dumps(data, ensure_ascii=False))
