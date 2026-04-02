"""
idea_center.py - curated prompt presets for AI Recipe Studio.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IdeaPreset:
    preset_id: str
    title: str
    summary: str
    audience: str
    prompt: str
    tags: list[str] = field(default_factory=list)
    count: int = 5
    protein_type: str | None = None
    gluten: str | None = None
    dairy: str | None = None


IDEA_PRESETS: list[IdeaPreset] = [
    IdeaPreset(
        preset_id="premium-breakfast",
        title="Premium Breakfast Board",
        summary="Elegant high-protein breakfast dishes for premium buffet service.",
        audience="Corporate events",
        prompt=(
            "Generate 5 elegant Mediterranean breakfast buffet dishes for a premium corporate event. "
            "Focus on grab-and-go items that look refined on a buffet table, feel modern and healthy, "
            "and fit morning service. Prioritize high-protein options, balanced macros, and clear "
            "ingredient lists. Avoid duplicates of common sandwiches and basic salads."
        ),
        tags=["Breakfast", "Premium", "Corporate", "High Protein"],
        count=5,
    ),
    IdeaPreset(
        preset_id="wellness-vegetarian",
        title="Wellness Retreat",
        summary="Colorful vegetarian dishes with a light, modern catering feel.",
        audience="Retreats and wellness programs",
        prompt=(
            "Generate 5 Levantine-inspired vegetarian lunch dishes for a wellness retreat buffet. "
            "Make them colorful, modern, easy to portion, and suitable for premium catering. "
            "Prefer gluten-free where possible, keep the flavors bright and fresh, and make sure "
            "the nutrition estimates feel realistic for a healthy menu."
        ),
        tags=["Vegetarian", "Wellness", "Lunch", "Levantine"],
        count=5,
        protein_type="veg",
    ),
    IdeaPreset(
        preset_id="arabic-fusion-canapes",
        title="Arabic Fusion Canapes",
        summary="Compact upscale bites for evening networking and cocktail-style service.",
        audience="Networking events",
        prompt=(
            "Generate 5 upscale Arabic-fusion canape-style dishes for an evening networking event. "
            "Focus on compact portions, premium presentation, bold flavor, and realistic single-serving "
            "nutriment estimates. Keep the recipes practical for catering production and avoid dishes "
            "that become messy on standing buffets."
        ),
        tags=["Fusion", "Canapes", "Evening", "Luxury"],
        count=5,
    ),
    IdeaPreset(
        preset_id="kids-healthy-snacks",
        title="Healthy Kids Picks",
        summary="Approachable snack ideas that stay fun while improving nutrition quality.",
        audience="School and family events",
        prompt=(
            "Generate 5 kids-friendly healthy snack dishes for a school event menu. "
            "Keep flavors approachable, ingredients simple, and recipes practical for batch preparation. "
            "Make the dishes visually appealing for children while still giving sensible nutrition estimates "
            "and clear ingredient amounts."
        ),
        tags=["Kids", "Snacks", "Healthy", "Simple"],
        count=5,
    ),
    IdeaPreset(
        preset_id="grab-go-protein",
        title="Grab-and-Go Protein",
        summary="Modern portable dishes for busy office and training-day service.",
        audience="Office catering",
        prompt=(
            "Generate 6 modern grab-and-go savory dishes for an office catering menu. "
            "Each dish should feel premium but practical, deliver strong protein, stay easy to portion, "
            "and work well in buffet or boxed service. Avoid heavy fried dishes and repetitive bread-based items."
        ),
        tags=["Portable", "Savory", "Office", "Protein"],
        count=6,
    ),
    IdeaPreset(
        preset_id="gluten-free-garden",
        title="Gluten-Free Garden",
        summary="Fresh premium buffet ideas with gluten-free positioning built in.",
        audience="Diet-aware catering",
        prompt=(
            "Generate 5 premium gluten-free buffet dishes inspired by Mediterranean and Levantine flavors. "
            "Keep them vibrant, modern, and suitable for a stylish catering display. "
            "Use clear ingredient lists, practical steps, and realistic nutriment estimates for one serving."
        ),
        tags=["Gluten Free", "Mediterranean", "Buffet", "Fresh"],
        count=5,
        gluten="gluten_free",
    ),
    IdeaPreset(
        preset_id="dairy-free-brunch",
        title="Dairy-Free Brunch",
        summary="Soft luxury brunch concepts without relying on cheese or cream.",
        audience="Brunch service",
        prompt=(
            "Generate 5 dairy-free brunch dishes for a premium buffet menu. "
            "Aim for a warm, stylish brunch feeling with realistic single-serving nutrition values, "
            "clear step-by-step preparation, and ingredients that work in real catering kitchens."
        ),
        tags=["Brunch", "Dairy Free", "Premium", "Buffet"],
        count=5,
        dairy="dairy_free",
    ),
    IdeaPreset(
        preset_id="ramadan-hospitality",
        title="Ramadan Hospitality",
        summary="Refined Arabic-inspired dishes suitable for generous evening buffet service.",
        audience="Seasonal hospitality",
        prompt=(
            "Generate 5 refined Arabic-inspired buffet dishes for a Ramadan hospitality setting. "
            "Balance tradition with modern presentation, keep the dishes practical for catering scale, "
            "and include realistic macros and dietary flags for each serving."
        ),
        tags=["Seasonal", "Arabic", "Hospitality", "Buffet"],
        count=5,
    ),
]
