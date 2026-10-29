"""Small LLM client over the OpenAI-compatible chat endpoints of Groq and Gemini (stdlib only).

Keys and addresses come only from GROQ_API_KEY, GROQ_BASE_URL, GEMINI_API_KEY, GEMINI_BASE_URL.
Groq is tried first, Gemini second. Rate limits and server errors are retried with backoff;
if both providers fail, the caller falls back to a template, so a run never fails because of the LLM.
"""
import json
import os
import time
import urllib.error
import urllib.request

RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    pass


def _providers() -> list[dict]:
    out = []
    if os.environ.get("GROQ_API_KEY") and os.environ.get("GROQ_BASE_URL"):
        out.append({"name": "groq", "key": os.environ["GROQ_API_KEY"],
                    "url": os.environ["GROQ_BASE_URL"].rstrip("/") + "/openai/v1/chat/completions",
                    "model": os.environ.get("GROQ_MODEL") or "llama-3.3-70b-versatile"})
    if os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_BASE_URL"):
        out.append({"name": "gemini", "key": os.environ["GEMINI_API_KEY"],
                    "url": os.environ["GEMINI_BASE_URL"].rstrip("/") + "/v1beta/openai/chat/completions",
                    "model": os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"})
    return out


def _post(p: dict, messages: list[dict], timeout: float, max_tokens: int) -> str:
    body = json.dumps({"model": p["model"], "messages": messages, "temperature": 0.1, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(p["url"], data=body, method="POST", headers={
        "Authorization": f"Bearer {p['key']}", "Content-Type": "application/json", "User-Agent": "uc4-support-inbox/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content")
    if not text or not text.strip():
        raise LLMError("empty reply")
    return text.strip()


def chat(messages: list[dict], trace=None, retries: int = 3, timeout: float = 30, max_tokens: int = 700) -> tuple[str, str]:
    """Return (text, "provider/model"). Raises LLMError when every provider fails."""
    providers = _providers()
    if not providers:
        raise LLMError("no LLM key configured")
    errors = []
    for p in providers:
        for attempt in range(retries):
            try:
                start = time.perf_counter()
                text = _post(p, messages, timeout, max_tokens)
                if trace:
                    trace.log("llm", p["name"], model=p["model"], attempt=attempt + 1,
                              ms=round(1000 * (time.perf_counter() - start)), chars=len(text))
                return text, f"{p['name']}/{p['model']}"
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}"
                retry = e.code in RETRY_STATUS
                wait = float(e.headers.get("retry-after") or 2 ** attempt) if retry else 0
            except (urllib.error.URLError, TimeoutError, LLMError, ValueError, KeyError) as e:
                err, retry, wait = f"{type(e).__name__}: {e}", True, 2 ** attempt
            errors.append(f"{p['name']}: {err}")
            if trace:
                trace.log("llm_error", p["name"], model=p["model"], attempt=attempt + 1, error=err)
            if not retry:
                break
            time.sleep(min(wait, 10))
    raise LLMError("; ".join(errors))
