"""LLM access for the WRITE path only (cognify, curator). Never called at recall time.

Primary: SAINT (OpenAI-compatible, on the workstation — powers off nightly).
Fallback: direct Anthropic (official SDK) when SAINT is unreachable and a key is present.
`chat_json` asks for a JSON object and validates it; on failure returns None (callers
queue+retry — never write junk).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

from astoria.config import settings

log = logging.getLogger("astoria.llm")


@dataclass
class LLMResult:
    text: str
    model: str
    route: str          # "saint" | "anthropic"
    latency_s: float


class LLMUnavailable(RuntimeError):
    pass


def _saint_chat(messages: list[dict], *, model: str, max_tokens: int, temperature: float) -> LLMResult:
    s = settings()
    t0 = time.time()
    with httpx.Client(timeout=s.llm_timeout_s) as client:
        r = client.post(
            f"{s.llm_url.rstrip('/')}/chat/completions",
            json={"model": model, "messages": messages, "max_tokens": max_tokens,
                  "temperature": temperature, "stream": False},
        )
        r.raise_for_status()
        data = r.json()
    text = data["choices"][0]["message"]["content"] or ""
    return LLMResult(text=text, model=data.get("model", model), route="saint", latency_s=time.time() - t0)


def _anthropic_chat(messages: list[dict], *, model: str, max_tokens: int, temperature: float) -> LLMResult:
    s = settings()
    if not s.anthropic_api_key:
        raise LLMUnavailable("no ANTHROPIC_API_KEY for fallback")
    try:
        import anthropic  # official SDK; optional dependency at runtime
    except ImportError as e:  # pragma: no cover
        raise LLMUnavailable("anthropic SDK not installed") from e
    t0 = time.time()
    system = "\n".join(m["content"] for m in messages if m["role"] == "system") or None
    convo = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    client = anthropic.Anthropic(api_key=s.anthropic_api_key, timeout=s.llm_timeout_s)
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": convo, "temperature": temperature}
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    text = "".join(getattr(b, "text", "") for b in resp.content)
    return LLMResult(text=text, model=model, route="anthropic", latency_s=time.time() - t0)


def chat(messages: list[dict], *, model: str | None = None, max_tokens: int = 1500,
         temperature: float = 0.0) -> LLMResult:
    """SAINT first; on connection failure fall back to direct Anthropic. Raises LLMUnavailable."""
    s = settings()
    errors = []
    try:
        return _saint_chat(messages, model=model or s.llm_model, max_tokens=max_tokens, temperature=temperature)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPStatusError) as e:
        errors.append(f"saint: {e}")
        log.warning("SAINT unavailable (%s); trying direct Anthropic", e)
    except Exception as e:  # noqa: BLE001
        errors.append(f"saint: {e}")
        log.warning("SAINT error (%s); trying direct Anthropic", e)
    try:
        return _anthropic_chat(messages, model=s.llm_fallback_model, max_tokens=max_tokens, temperature=temperature)
    except Exception as e:  # noqa: BLE001
        errors.append(f"anthropic: {e}")
    raise LLMUnavailable("; ".join(errors))


_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.S)


def chat_json(messages: list[dict], *, model: str | None = None, max_tokens: int = 1500) -> dict | list | None:
    """Ask for strict JSON; parse robustly (strip fences, find first object). None if unparsable."""
    res = chat(messages, model=model, max_tokens=max_tokens, temperature=0.0)
    txt = res.text.strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S).strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        m = _JSON_RE.search(txt)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    log.warning("LLM returned non-JSON (%s via %s): %.200r", res.model, res.route, txt)
    return None


def llm_health() -> dict:
    s = settings()
    out = {"saint_url": s.llm_url, "model": s.llm_model, "fallback": bool(s.anthropic_api_key)}
    try:
        with httpx.Client(timeout=4) as client:
            r = client.get(f"{s.llm_url.rstrip('/')}/models")
            out["saint"] = "reachable" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:  # noqa: BLE001
        out["saint"] = f"unreachable ({type(e).__name__})"
    return out
