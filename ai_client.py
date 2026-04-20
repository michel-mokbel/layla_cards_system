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
from urllib.parse import quote


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
        "OPENAI_": "openai",
        "GEMINI_": "gemini",
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


def openai_configured() -> bool:
    return bool(env("OPENAI_API_KEY")) and bool(env("OPENAI_MODEL"))


def gemini_configured() -> bool:
    return bool(gemini_api_key()) and bool(env("GEMINI_MODEL"))


def ai_configured() -> bool:
    return gemini_configured() or openai_configured()


def openai_base_url() -> str:
    return env("OPENAI_BASE_URL", "https://api.openai.com") or "https://api.openai.com"


def gemini_base_url() -> str:
    return (
        env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
        or "https://generativelanguage.googleapis.com/v1beta"
    )


def openai_model() -> Optional[str]:
    return env("OPENAI_MODEL")


def gemini_api_key() -> Optional[str]:
    return env("GEMINI_API_KEY") or env("GOOGLE_API_KEY")


def gemini_model() -> Optional[str]:
    return env("GEMINI_MODEL")


def _gemini_model_path(model: str) -> str:
    cleaned = str(model or "").strip()
    if not cleaned:
        raise RuntimeError("GEMINI_MODEL is required for Gemini generation.")
    if "/" in cleaned:
        return cleaned
    return f"models/{cleaned}"


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


def _request_openai_completion(
    system_instruction: str,
    user_content: str,
    *,
    model_override: Optional[str] = None,
) -> AICompletion:
    base_url = openai_base_url().rstrip("/")
    api_key = env("OPENAI_API_KEY")
    model = model_override or openai_model() or ""
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


def _request_gemini_completion(
    system_instruction: str,
    user_content: str,
    *,
    model_override: Optional[str] = None,
) -> AICompletion:
    base_url = gemini_base_url().rstrip("/")
    api_key = gemini_api_key()
    model = model_override or gemini_model() or ""
    model_path = _gemini_model_path(model)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": str(api_key or ""),
    }
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_content}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }
    response = _post_json(
        f"{base_url}/{quote(model_path, safe='/')}:generateContent",
        payload=payload,
        headers=headers,
    )
    prompt_feedback = response.get("promptFeedback") if isinstance(response, dict) else None
    candidates = response.get("candidates") if isinstance(response, dict) else None
    if not candidates:
        block_reason = ""
        if isinstance(prompt_feedback, dict):
            block_reason = str(prompt_feedback.get("blockReason") or "").strip()
        if block_reason:
            raise RuntimeError(f"Gemini blocked the prompt: {block_reason}")
        raise RuntimeError("AI response missing candidates.")

    text_parts: list[str] = []
    first_candidate = candidates[0] if isinstance(candidates, list) else None
    content = first_candidate.get("content") if isinstance(first_candidate, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    text = "".join(text_parts).strip()
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("AI response missing JSON text output.")
    return AICompletion(text=text, provider="gemini", model=model)


def request_json_completion(
    system_instruction: str,
    user_content: str,
    *,
    preferred_provider: Optional[str] = None,
    model_override: Optional[str] = None,
) -> AICompletion:
    preferred = str(preferred_provider or "").strip().lower()

    if preferred == "openai" and openai_configured():
        return _request_openai_completion(system_instruction, user_content, model_override=model_override)
    if preferred == "gemini" and gemini_configured():
        return _request_gemini_completion(system_instruction, user_content, model_override=model_override)

    if gemini_configured():
        return _request_gemini_completion(system_instruction, user_content, model_override=model_override)

    if not openai_configured():
        raise RuntimeError(
            "AI is not configured. Set GEMINI_API_KEY + GEMINI_MODEL, or OPENAI_API_KEY + OPENAI_MODEL."
        )
    return _request_openai_completion(system_instruction, user_content, model_override=model_override)
