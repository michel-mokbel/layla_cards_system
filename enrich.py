"""
enrich.py — optional dish auto-fill helpers.

Goal:
- Given an English dish name, propose Arabic translation + macros + dietary flags.
- Keep results editable in the UI before persisting to CSV.

This module is designed to be "best effort":
- If AI env vars are not configured, it returns a minimal stub so the user can fill manually.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import ssl
from typing import Any, Dict, Optional
from urllib import request
from urllib.error import HTTPError, URLError


@dataclass(frozen=True)
class EnrichedDish:
    name_en: str
    name_ar: str
    calories_kcal: float
    carbs_g: float
    protein_g: float
    fat_g: float
    gluten: str         # "gluten" | "gluten_free"
    protein_type: str   # "veg" | "meat"
    dairy: str          # "dairy" | "dairy_free"
    source: str         # "openrouter" | "openai" | "manual"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is not None and str(value).strip():
        return str(value).strip()

    secret_value = _secret(name)
    if secret_value is not None and str(secret_value).strip():
        return str(secret_value).strip()

    return default


def _secret(name: str) -> Optional[str]:
    """
    Streamlit reads `.streamlit/secrets.toml` into `st.secrets`.
    These values are NOT automatically exported into `os.environ`,
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

    # 1) Exact key (e.g. OPENROUTER_API_KEY="...")
    try:
        if name in secrets:
            return str(secrets[name])
    except Exception:
        pass

    # 2) Section-based keys:
    #    [openrouter]
    #    api_key="..."
    #    model="..."
    # or [openai] with api_key/model, etc.
    prefix_map = {
        "OPENROUTER_": "openrouter",
        "OPENAI_": "openai",
    }
    section: Optional[str] = None
    suffix: Optional[str] = None
    for pfx, sec in prefix_map.items():
        if name.startswith(pfx):
            section = sec
            suffix = name[len(pfx) :]
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


def openrouter_configured() -> bool:
    return bool(_env("OPENROUTER_API_KEY")) and bool(_env("OPENROUTER_MODEL"))


def openai_configured() -> bool:
    return bool(_env("OPENAI_API_KEY")) and bool(_env("OPENAI_MODEL"))


def _openai_base_url() -> str:
    # Allow OpenAI-compatible endpoints too.
    return _env("OPENAI_BASE_URL", "https://api.openai.com")  # type: ignore[return-value]


def _openai_model() -> Optional[str]:
    # Avoid hardcoding a model name that might not exist; let the user set it.
    return _env("OPENAI_MODEL")


def _openrouter_base_url() -> str:
    return _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")  # type: ignore[return-value]


def _openrouter_model() -> Optional[str]:
    return _env("OPENROUTER_MODEL")


def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers=headers, method="POST")

    # Prefer certifi's CA bundle when available. This fixes common
    # macOS/Python trust-store issues with HTTPS APIs.
    ssl_context = None
    try:
        import certifi  # type: ignore

        ssl_context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ssl_context = ssl.create_default_context()

    try:
        with request.urlopen(req, timeout=45, context=ssl_context) as resp:
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


def _coerce_flag(value: str, allowed: set[str], default: str) -> str:
    v = (value or "").strip().lower()
    return v if v in allowed else default


def _parse_json_object(text: str) -> Dict[str, Any]:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Best-effort salvage if the model returned extra text.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in AI response.")
    snippet = text[start : end + 1]
    obj = json.loads(snippet)
    if not isinstance(obj, dict):
        raise ValueError("AI response JSON was not an object.")
    return obj


def enrich_dish_name(name_en: str, *, require_ai: bool = False) -> EnrichedDish:
    """
    Returns a best-effort proposed dish record.

    Provider priority:
    1) OpenRouter, if OPENROUTER_API_KEY + OPENROUTER_MODEL are set
    2) OpenAI, if OPENAI_API_KEY + OPENAI_MODEL are set
    Otherwise returns a manual stub (Arabic empty; macros 0; flags set to defaults).
    """
    cleaned = (name_en or "").strip()
    if not cleaned:
        raise ValueError("Dish name (EN) is required.")

    if not openrouter_configured() and not openai_configured():
        if require_ai:
            raise RuntimeError(
                "AI is not configured. Set OPENROUTER_API_KEY + OPENROUTER_MODEL (recommended), "
                "or OPENAI_API_KEY + OPENAI_MODEL."
            )
        return EnrichedDish(
            name_en=cleaned,
            name_ar="",
            calories_kcal=0.0,
            carbs_g=0.0,
            protein_g=0.0,
            fat_g=0.0,
            gluten="gluten_free",
            protein_type="veg",
            dairy="dairy_free",
            source="manual",
        )

    # Use a strict JSON-only contract to keep parsing robust.
    instruction = (
        "You are a food assistant for a restaurant admin tool.\n"
        "Given a dish name in English, return a single JSON object with exactly these keys:\n"
        "name_ar (string, Arabic translation), calories_kcal (number), carbs_g (number), protein_g (number), fat_g (number),\n"
        "gluten (\"gluten\" or \"gluten_free\"), protein_type (\"veg\" or \"meat\"), dairy (\"dairy\" or \"dairy_free\").\n"
        "Assume one standard serving unless the name implies otherwise.\n"
        "If uncertain, make a reasonable estimate and prefer gluten_free / veg / dairy_free when ambiguous.\n"
        "Return JSON only, no markdown, no extra text."
    )

    text_out: Optional[str] = None

    if openrouter_configured():
        base_url = _openrouter_base_url().rstrip("/")
        api_key = _env("OPENROUTER_API_KEY")
        model = _openrouter_model()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        site_url = _env("OPENROUTER_SITE_URL")
        app_name = _env("OPENROUTER_APP_NAME")
        if site_url:
            headers["HTTP-Referer"] = site_url
        if app_name:
            headers["X-Title"] = app_name

        chat_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": cleaned},
            ],
            "response_format": {"type": "json_object"},
        }
        chat = _post_json(f"{base_url}/chat/completions", payload=chat_payload, headers=headers)
        text_out = (
            (((chat.get("choices") or [None])[0] or {}).get("message") or {}).get("content")
            if isinstance(chat, dict)
            else None
        )
        source = "openrouter"
    else:
        base_url = _openai_base_url().rstrip("/")
        api_key = _env("OPENAI_API_KEY")
        model = _openai_model()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            payload = {
                "model": model,
                "input": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": cleaned},
                ],
                "text": {"format": {"type": "json_object"}},
            }
            result = _post_json(f"{base_url}/v1/responses", payload=payload, headers=headers)

            for item in result.get("output", []) or []:
                for c in item.get("content", []) or []:
                    if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                        text_out = c["text"]
                        break
                if text_out:
                    break
        except HTTPError:
            chat_payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": cleaned},
                ],
                "response_format": {"type": "json_object"},
            }
            chat = _post_json(f"{base_url}/v1/chat/completions", payload=chat_payload, headers=headers)
            text_out = (
                (((chat.get("choices") or [None])[0] or {}).get("message") or {}).get("content")
                if isinstance(chat, dict)
                else None
            )
        source = "openai"

    if not text_out or not isinstance(text_out, str):
        raise RuntimeError("AI response missing JSON text output.")

    obj = _parse_json_object(text_out)

    return EnrichedDish(
        name_en=cleaned,
        name_ar=str(obj.get("name_ar", "")).strip(),
        calories_kcal=float(obj.get("calories_kcal", 0.0) or 0.0),
        carbs_g=float(obj.get("carbs_g", 0.0) or 0.0),
        protein_g=float(obj.get("protein_g", 0.0) or 0.0),
        fat_g=float(obj.get("fat_g", 0.0) or 0.0),
        gluten=_coerce_flag(str(obj.get("gluten", "")), {"gluten", "gluten_free"}, "gluten_free"),
        protein_type=_coerce_flag(str(obj.get("protein_type", "")), {"veg", "meat"}, "veg"),
        dairy=_coerce_flag(str(obj.get("dairy", "")), {"dairy", "dairy_free"}, "dairy_free"),
        source=source,
    )
