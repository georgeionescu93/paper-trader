"""OpenAI-compatible chat completions, for servers with no Ollama.

The desktop build talks to a local Ollama. A hosted server usually has none, so
this module lets the very same analysis run against any OpenAI-compatible
endpoint (OpenAI, DeepSeek, Groq, Together, a self-hosted vLLM...) by setting
three environment variables:

    LLM_BASE_URL   https://api.deepseek.com/v1
    LLM_API_KEY    sk-...
    LLM_MODEL      deepseek-chat

It is deliberately opt-in: with no LLM_BASE_URL nothing changes and the app
keeps using Ollama exactly as before. Only the prompt/response transport lives
here - the prompt itself is still built by the desktop module, so the model sees
byte-identical instructions whichever backend answers.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_S = 300


def config():
    """The configured endpoint, or empty strings when it is not set up."""
    return {
        "base": (os.environ.get("LLM_BASE_URL") or "").strip().rstrip("/"),
        "key": (os.environ.get("LLM_API_KEY") or "").strip(),
        "model": (os.environ.get("LLM_MODEL") or "").strip(),
    }


def enabled():
    return bool(config()["base"])


def request_timeout():
    raw = (os.environ.get("LLM_TIMEOUT_S") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_TIMEOUT_S


def _headers(cfg):
    headers = {"Content-Type": "application/json"}
    if cfg["key"]:
        headers["Authorization"] = f"Bearer {cfg['key']}"
    return headers


def chat(prompt, model=None, temperature=0.2):
    """Sends one user message and returns the assistant's text.

    Raises RuntimeError with the endpoint's own message when it refuses, so the
    dashboard can show the real reason instead of a generic failure.
    """
    cfg = config()
    if not cfg["base"]:
        raise RuntimeError("LLM_BASE_URL is not configured")
    payload = {
        "model": (model or cfg["model"] or "gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "stream": False,
    }
    request = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(cfg), method="POST")
    try:
        with urllib.request.urlopen(request,
                                    timeout=request_timeout()) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{exc.code} from {cfg['base']}: {detail}") from exc
    except Exception as exc:                            # noqa: BLE001
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc

    try:
        data = json.loads(body)
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Unexpected answer from {cfg['base']}: "
                           f"{body[:200]}") from exc


def list_models():
    """The endpoint's model ids, falling back to the configured one."""
    cfg = config()
    if not cfg["base"]:
        return []
    try:
        request = urllib.request.Request(cfg["base"] + "/models",
                                         headers=_headers(cfg))
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        names = [str(m.get("id")) for m in data.get("data", [])
                 if m.get("id")]
        if names:
            return sorted(names)
    except Exception:                                   # noqa: BLE001
        pass
    return [cfg["model"]] if cfg["model"] else []
