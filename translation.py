"""
translation.py - glossary-aware Arabic dish name translation.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
import re

from ai_client import (
    env,
    gemini_configured,
    openai_configured,
    parse_json_object,
    request_json_completion,
)


BASE_DIR = Path(__file__).resolve().parent
DISHES_CSV = BASE_DIR / "data" / "dishes.csv"

TOKEN_GLOSSARY = {
    "almond": "لوز",
    "baguette": "باغيت",
    "banana": "موز",
    "bar": "بار",
    "beetroot": "شمندر",
    "bites": "لقيمات",
    "brownies": "براونيز",
    "brownie": "براوني",
    "cake": "كيك",
    "carrot": "جزر",
    "cheese": "جبنة",
    "chia": "شيا",
    "chicken": "دجاج",
    "choco": "شوكولا",
    "chocolate": "شوكولا",
    "club": "كلوب",
    "cone": "كون",
    "cookies": "كوكيز",
    "cookie": "كوكي",
    "croissant": "كرواسون",
    "cup": "كوب",
    "dates": "تمر",
    "dry": "جاف",
    "egg": "بيض",
    "energy": "طاقة",
    "eclair": "إكلير",
    "fajita": "فاهيتا",
    "fatayer": "فطاير",
    "fresh": "طازجة",
    "fruit": "فواكه",
    "gluten": "غلوتين",
    "granola": "غرانولا",
    "greek": "يوناني",
    "halloumi": "حلوم",
    "hash": "هاش",
    "hommos": "حمص",
    "hummus": "حمص",
    "juice": "عصير",
    "labneh": "لبنة",
    "mango": "مانجو",
    "marble": "ماربل",
    "meat": "لحم",
    "mini": "ميني",
    "mousse": "موس",
    "muffins": "مافن",
    "mushroom": "فطر",
    "nuts": "مكسرات",
    "omelette": "أومليت",
    "orange": "برتقال",
    "oreo": "أوريو",
    "pasta": "باستا",
    "peanut": "فول سوداني",
    "philly": "فيلي",
    "pie": "فطيرة",
    "pizza": "بيتزا",
    "protein": "بروتين",
    "quinoa": "كينوا",
    "rolls": "رولز",
    "salad": "سلطة",
    "sandwich": "ساندويتش",
    "sandwiches": "ساندويتشات",
    "shawarma": "شاورما",
    "spinach": "سبانخ",
    "strawberry": "فراولة",
    "sticks": "أصابع",
    "tortilla": "تورتيلا",
    "tuna": "تونة",
    "turkey": "ديك رومي",
    "veggie": "خضار",
    "veg": "خضار",
    "walnut": "جوز",
    "wrap": "راب",
    "wraps": "رابات",
    "yogurt": "زبادي",
    "zaatar": "زعتر",
}


def translate_dish_name(name_en: str, *, proposed_name_ar: str = "", allow_ai: bool = True) -> str:
    cleaned = " ".join(str(name_en or "").split()).strip()
    if not cleaned:
        return ""
    if allow_ai:
        return _translate_dish_name_cached(cleaned, " ".join(str(proposed_name_ar or "").split()).strip())
    return _translate_dish_name_without_ai(cleaned, " ".join(str(proposed_name_ar or "").split()).strip())


@lru_cache(maxsize=256)
def _translate_dish_name_cached(name_en: str, proposed_name_ar: str) -> str:
    exact = _lookup_existing_translation(name_en)
    if exact:
        return exact

    preferred_provider = _translation_provider_preference()
    if preferred_provider and (gemini_configured() or openai_configured()):
        translated = _translate_with_ai(name_en, proposed_name_ar=proposed_name_ar, preferred_provider=preferred_provider)
        if translated:
            return translated

    return _translate_dish_name_without_ai(name_en, proposed_name_ar)


def _translate_dish_name_without_ai(name_en: str, proposed_name_ar: str) -> str:
    return _fallback_glossary_translation(name_en) or proposed_name_ar


def _translation_provider_preference() -> str:
    configured = str(env("AI_TRANSLATION_PROVIDER", "") or "").strip().lower()
    if configured in {"gemini", "openai"}:
        return configured
    if gemini_configured():
        return "gemini"
    if openai_configured():
        return "openai"
    return ""


def _translation_model_override(provider: str) -> str | None:
    if provider == "gemini":
        return env("GEMINI_TRANSLATION_MODEL") or env("GEMINI_MODEL")
    if provider == "openai":
        return env("OPENAI_TRANSLATION_MODEL") or env("OPENAI_MODEL")
    return None


def _translate_with_ai(name_en: str, *, proposed_name_ar: str = "", preferred_provider: str) -> str:
    examples = _example_pairs(limit=14)
    glossary = ", ".join(f"{key} -> {value}" for key, value in sorted(TOKEN_GLOSSARY.items())[:28])
    system_prompt = (
        "You are a professional culinary translator for Arabic catering menus.\n"
        "Translate English dish names into natural, menu-ready Arabic.\n"
        "Rules:\n"
        "- Return JSON only: {\"name_ar\": \"...\"}\n"
        "- Keep translations short and menu-friendly.\n"
        "- Use natural Arabic food terms, not word-for-word literal translations.\n"
        "- Transliterate brand names or foreign dish forms when needed.\n"
        "- Do not invent ingredients that are not in the English name.\n"
        "- Prefer consistency with the provided house glossary and examples."
    )
    user_prompt = (
        f"House glossary examples:\n{examples}\n\n"
        f"Token glossary:\n{glossary}\n\n"
        f"English dish name: {name_en}\n"
        f"Previous poor translation to improve (if any): {proposed_name_ar or '-'}"
    )
    completion = request_json_completion(
        system_prompt,
        user_prompt,
        preferred_provider=preferred_provider,
        model_override=_translation_model_override(preferred_provider),
    )
    obj = parse_json_object(completion.text)
    return " ".join(str(obj.get("name_ar", "")).split()).strip()


def _lookup_existing_translation(name_en: str) -> str:
    key = _normalize_english(name_en)
    if not key or not DISHES_CSV.exists():
        return ""

    with DISHES_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            current_name = _normalize_english(row.get("name_en", ""))
            if current_name == key:
                return " ".join(str(row.get("name_ar", "")).split()).strip()
    return ""


def _example_pairs(*, limit: int = 12) -> str:
    if not DISHES_CSV.exists():
        return ""

    examples: list[str] = []
    with DISHES_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name_en = " ".join(str(row.get("name_en", "")).split()).strip()
            name_ar = " ".join(str(row.get("name_ar", "")).split()).strip()
            if name_en and name_ar:
                examples.append(f"- {name_en} => {name_ar}")
            if len(examples) >= limit:
                break
    return "\n".join(examples)


def _fallback_glossary_translation(name_en: str) -> str:
    tokens = re.findall(r"[A-Za-z&]+", name_en)
    translated_tokens: list[str] = []
    for token in tokens:
        normalized = token.lower()
        if normalized == "&":
            translated_tokens.append("و")
            continue
        translated = TOKEN_GLOSSARY.get(normalized)
        if translated:
            translated_tokens.append(translated)
    return " ".join(translated_tokens).strip()


def _normalize_english(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())
