"""
enrich.py - optional single-dish auto-fill helpers.

Goal:
- Given an English dish name, propose Arabic translation + macros + dietary flags.
- Keep results editable in the UI before persisting to CSV.

This module is designed to be "best effort":
- If AI env vars are not configured, it returns a minimal stub so the user can fill manually.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_client import (
    openai_configured,
    openrouter_configured,
    parse_json_object,
    request_json_completion,
)


@dataclass(frozen=True)
class EnrichedDish:
    name_en: str
    name_ar: str
    calories_kcal: float
    carbs_g: float
    protein_g: float
    fat_g: float
    gluten: str
    protein_type: str
    dairy: str
    source: str


def _coerce_flag(value: str, allowed: set[str], default: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in allowed else default


def enrich_dish_name(name_en: str, *, require_ai: bool = False) -> EnrichedDish:
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

    instruction = (
        "You are a food assistant for a restaurant admin tool.\n"
        "Given a dish name in English, return a single JSON object with exactly these keys:\n"
        "name_ar (string, Arabic translation), calories_kcal (number), carbs_g (number), protein_g (number), fat_g (number),\n"
        "gluten (\"gluten\" or \"gluten_free\"), protein_type (\"veg\" or \"meat\"), dairy (\"dairy\" or \"dairy_free\").\n"
        "Assume one standard serving unless the name implies otherwise.\n"
        "If uncertain, make a reasonable estimate and prefer gluten_free / veg / dairy_free when ambiguous.\n"
        "Return JSON only, no markdown, no extra text."
    )

    completion = request_json_completion(instruction, cleaned)
    obj = parse_json_object(completion.text)

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
        source=completion.provider,
    )
