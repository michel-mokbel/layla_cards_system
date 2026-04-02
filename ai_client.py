"""
ai_client.py - shared AI provider helpers for JSON-oriented generation flows.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import ssl
from typing import Any, Optional
from urllib import request
from urllib.error import HTTPError, URLError


@dataclass(frozen=True)
class AICompletion:
    text: str
    provider: str
    model: str


def _secret(name: str) -> Optional[str]:
    """
    Streamlit reads `.streamlit/secrets.toml` into `st.secrets`.
    These values are not automatically exported into `os.environ`,
    so we explicitly read them here.
    """
    try:
        import streamlit as st  # type: ignore
    except Exception:
        return None

    try:
        secrets = st.secrets  # type: ignore[attr-defined]
    except Exception:
        return None

    try:
        if name in secrets:
            return str(secrets[name])
    except Exception:
        pass

    prefix_map = {
        "OPENROUTER_": "openrouter",
        "OPENAI_": "openai",
    }
    section: Optional[str] = None
    suffix: Optional[str] = None
    for prefix, mapped_section in prefix_map.items():
        if name.startswith(prefix):
            section = mapped_section
            suffix = name[len(prefix) :]
            break

    if not section or not suffix:
        return None

    key_map = {
        "API_KEY": "api_key",
        "MODEL": "model",
        "BASE_URL": "base_url",
        "SITE_URL": "site_url",
        "APP_NAME": "app_name",
    }
    section_key = key_map.get(suffix)
    if not section_key:
        return None

    try:
        if section in secrets and section_key in secrets[section]:
            return str(secrets[section][section_key])
    except Exception:
        return None

    return None


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is not None and str(value).strip():
        return str(value).strip()

    secret_value = _secret(name)
    if secret_value is not None and str(secret_value).strip():
        return str(secret_value).strip()

    return default


def openrouter_configured() -> bool:
    return bool(env("OPENROUTER_API_KEY")) and bool(env("OPENROUTER_MODEL"))


def openai_configured() -> bool:
    return bool(env("OPENAI_API_KEY")) and bool(env("OPENAI_MODEL"))


def ai_configured() -> bool:
    return openrouter_configured() or openai_configured()


def openai_base_url() -> str:
    return env("OPENAI_BASE_URL", "https://api.openai.com") or "https://api.openai.com"


def openrouter_base_url() -> str:
    return env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1") or "https://openrouter.ai/api/v1"


def openai_model() -> Optional[str]:
    return env("OPENAI_MODEL")


def openrouter_model() -> Optional[str]:
    return env("OPENROUTER_MODEL")


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in AI response.")

    snippet = text[start : end + 1]
    obj = json.loads(snippet)
    if not isinstance(obj, dict):
        raise ValueError("AI response JSON was not an object.")
    return obj


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")

    try:
        import certifi  # type: ignore

        ssl_context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ssl_context = ssl.create_default_context()

    try:
        with request.urlopen(req, timeout=60, context=ssl_context) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8")
        except Exception:
            error_body = ""

        details = ""
        try:
            parsed = json.loads(error_body) if error_body else {}
            if isinstance(parsed, dict):
                if isinstance(parsed.get("error"), dict):
                    details = str(
                        parsed["error"].get("message")
                        or parsed["error"].get("code")
                        or ""
                    ).strip()
                elif parsed.get("message"):
                    details = str(parsed.get("message")).strip()
        except Exception:
            details = ""

        if exc.code == 402:
            msg = (
                "OpenRouter request failed with 402 Payment Required. "
                "Your account likely has no credits, billing is disabled, or the selected model requires paid access."
            )
            if details:
                msg = f"{msg} Provider message: {details}"
            raise RuntimeError(msg) from exc

        if details:
            raise RuntimeError(f"API request failed ({exc.code}): {details}") from exc
        raise RuntimeError(f"API request failed ({exc.code}): {exc.reason}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise RuntimeError(
                "TLS certificate verification failed. Install/update certifi (`pip install -U certifi`) "
                "and ensure your Python trust store is configured."
            ) from exc
        raise


def request_json_completion(system_instruction: str, user_content: str) -> AICompletion:
    if openrouter_configured():
        base_url = openrouter_base_url().rstrip("/")
        api_key = env("OPENROUTER_API_KEY")
        model = openrouter_model() or ""
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        site_url = env("OPENROUTER_SITE_URL")
        app_name = env("OPENROUTER_APP_NAME")
        if site_url:
            headers["HTTP-Referer"] = site_url
        if app_name:
            headers["X-Title"] = app_name

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
        }
        response = _post_json(f"{base_url}/chat/completions", payload=payload, headers=headers)
        text = (
            (((response.get("choices") or [None])[0] or {}).get("message") or {}).get("content")
            if isinstance(response, dict)
            else None
        )
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("AI response missing JSON text output.")
        return AICompletion(text=text, provider="openrouter", model=model)

    if not openai_configured():
        raise RuntimeError(
            "AI is not configured. Set OPENROUTER_API_KEY + OPENROUTER_MODEL (recommended), "
            "or OPENAI_API_KEY + OPENAI_MODEL."
        )

    base_url = openai_base_url().rstrip("/")
    api_key = env("OPENAI_API_KEY")
    model = openai_model() or ""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        payload = {
            "model": model,
            "input": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "text": {"format": {"type": "json_object"}},
        }
        result = _post_json(f"{base_url}/v1/responses", payload=payload, headers=headers)
        text = None
        for item in result.get("output", []) or []:
            for content in item.get("content", []) or []:
                if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    text = content["text"]
                    break
            if text:
                break
        if not text:
            raise RuntimeError("AI response missing JSON text output.")
        return AICompletion(text=text, provider="openai", model=model)
    except RuntimeError:
        raise
    except Exception:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
        }
        result = _post_json(f"{base_url}/v1/chat/completions", payload=payload, headers=headers)
        text = (
            (((result.get("choices") or [None])[0] or {}).get("message") or {}).get("content")
            if isinstance(result, dict)
            else None
        )
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("AI response missing JSON text output.")
        return AICompletion(text=text, provider="openai", model=model)
