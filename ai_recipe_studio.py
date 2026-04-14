"""
ai_recipe_studio.py - structured AI draft generation for dishes and recipes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Optional
from uuid import uuid4

from ai_client import AICompletion, ai_configured, parse_json_object, request_json_completion
from translation import translate_dish_name


DEFAULT_COUNT = 5
MAX_COUNT = 10
VALID_GLUTEN = {"gluten", "gluten_free"}
VALID_PROTEIN = {"veg", "meat"}
VALID_DAIRY = {"dairy", "dairy_free"}
SOFT_MAX_CALORIES = 1200.0
SOFT_MAX_MACRO_GRAMS = 150.0

MEAT_KEYWORDS = {
    "beef", "chicken", "turkey", "lamb", "meat", "steak", "tuna", "salmon", "fish", "shrimp",
    "prawn", "bacon", "ham",
}
DAIRY_KEYWORDS = {
    "milk", "butter", "cream", "cheese", "yogurt", "labneh", "halloumi", "mozzarella", "feta",
    "parmesan", "ghee",
}
GLUTEN_KEYWORDS = {
    "flour", "bread", "pasta", "croissant", "baguette", "wheat", "barley", "semolina", "cake",
    "cookie", "cracker", "bun", "tortilla", "wrap", "breadcrumbs", "soy sauce",
}


CompletionFn = Callable[[str, str], AICompletion]


@dataclass(frozen=True)
class GenerationRequest:
    brief: str
    count: int = DEFAULT_COUNT
    protein_type: Optional[str] = None
    gluten: Optional[str] = None
    dairy: Optional[str] = None

    def normalized(self) -> "GenerationRequest":
        brief = " ".join(str(self.brief or "").split()).strip()
        if not brief:
            raise ValueError("Generation brief is required.")

        count = int(self.count or DEFAULT_COUNT)
        if count < 1:
            count = 1
        if count > MAX_COUNT:
            count = MAX_COUNT

        protein_type = _normalize_optional_flag(self.protein_type, VALID_PROTEIN)
        gluten = _normalize_optional_flag(self.gluten, VALID_GLUTEN)
        dairy = _normalize_optional_flag(self.dairy, VALID_DAIRY)
        return GenerationRequest(
            brief=brief,
            count=count,
            protein_type=protein_type,
            gluten=gluten,
            dairy=dairy,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ValidationResult":
        return cls(
            passed=bool(payload.get("passed", False)),
            errors=[str(item) for item in payload.get("errors", []) or []],
            warnings=[str(item) for item in payload.get("warnings", []) or []],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class EvaluationResult:
    overall_score: float
    plausibility_score: float
    menu_usefulness_score: float
    recipe_completeness_score: float
    metadata_consistency_score: float
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvaluationResult":
        return cls(
            overall_score=float(payload.get("overall_score", 0.0) or 0.0),
            plausibility_score=float(payload.get("plausibility_score", 0.0) or 0.0),
            menu_usefulness_score=float(payload.get("menu_usefulness_score", 0.0) or 0.0),
            recipe_completeness_score=float(payload.get("recipe_completeness_score", 0.0) or 0.0),
            metadata_consistency_score=float(payload.get("metadata_consistency_score", 0.0) or 0.0),
            notes=[str(item) for item in payload.get("notes", []) or []],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_score": self.overall_score,
            "plausibility_score": self.plausibility_score,
            "menu_usefulness_score": self.menu_usefulness_score,
            "recipe_completeness_score": self.recipe_completeness_score,
            "metadata_consistency_score": self.metadata_consistency_score,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class GeneratedDishDraft:
    draft_id: str
    status: str
    created_at: str
    source_model: str
    generation_prompt: str
    request_brief: str
    request_constraints: dict[str, Optional[str]]
    dish: dict[str, Any]
    recipe: dict[str, Any]
    validation: ValidationResult
    evaluation: EvaluationResult
    attempts: int
    repair_history: list[str] = field(default_factory=list)
    approved_dish_name: str = ""
    approved_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "status": self.status,
            "created_at": self.created_at,
            "source_model": self.source_model,
            "generation_prompt": self.generation_prompt,
            "request_brief": self.request_brief,
            "request_constraints": dict(self.request_constraints),
            "dish": dict(self.dish),
            "recipe": {
                "yield_servings": int(self.recipe.get("yield_servings", 0) or 0),
                "ingredients": [str(item) for item in self.recipe.get("ingredients", []) or []],
                "steps": [str(item) for item in self.recipe.get("steps", []) or []],
            },
            "validation": self.validation.to_dict(),
            "evaluation": self.evaluation.to_dict(),
            "attempts": int(self.attempts or 0),
            "repair_history": list(self.repair_history),
            "approved_dish_name": self.approved_dish_name,
            "approved_at": self.approved_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "GeneratedDishDraft":
        return cls(
            draft_id=str(payload.get("draft_id") or uuid4().hex),
            status=str(payload.get("status") or "review_ready"),
            created_at=str(payload.get("created_at") or _now_iso()),
            source_model=str(payload.get("source_model") or ""),
            generation_prompt=str(payload.get("generation_prompt") or ""),
            request_brief=str(payload.get("request_brief") or ""),
            request_constraints=dict(payload.get("request_constraints") or {}),
            dish=_normalize_dish_payload(payload.get("dish") or {}),
            recipe=_normalize_recipe_payload(payload.get("recipe") or {}),
            validation=ValidationResult.from_dict(dict(payload.get("validation") or {})),
            evaluation=EvaluationResult.from_dict(dict(payload.get("evaluation") or {})),
            attempts=int(payload.get("attempts") or 0),
            repair_history=[str(item) for item in payload.get("repair_history", []) or []],
            approved_dish_name=str(payload.get("approved_dish_name") or ""),
            approved_at=str(payload.get("approved_at") or ""),
        )


def generate_dish_drafts(
    request: GenerationRequest,
    *,
    existing_dish_names: Optional[Iterable[str]] = None,
    completion_fn: Optional[CompletionFn] = None,
) -> list[GeneratedDishDraft]:
    normalized_request = request.normalized()
    if completion_fn is None and not ai_configured():
        raise RuntimeError(
            "AI is not configured. Set GEMINI_API_KEY + GEMINI_MODEL, or OPENAI_API_KEY + OPENAI_MODEL."
        )

    completion = completion_fn or request_json_completion
    system_prompt = _generation_system_prompt()
    user_prompt = _generation_user_prompt(normalized_request, existing_dish_names or [])
    prompt_snapshot = f"{system_prompt}\n\nUSER:\n{user_prompt}"

    batch_completion = completion(system_prompt, user_prompt)
    raw_obj = _parse_candidate_batch(batch_completion.text, completion)

    raw_candidates = raw_obj.get("candidates")
    if not isinstance(raw_candidates, list):
        raise RuntimeError("AI response JSON must contain a top-level `candidates` array.")

    drafts: list[GeneratedDishDraft] = []
    reserved_names = {str(name).strip().lower() for name in existing_dish_names or [] if str(name).strip()}
    for raw_candidate in raw_candidates[: normalized_request.count]:
        draft = _draft_from_candidate(
            raw_candidate,
            source_model=f"{batch_completion.provider}:{batch_completion.model}",
            prompt_snapshot=prompt_snapshot,
            request=normalized_request,
        )
        draft = _repair_until_valid(
            draft,
            request=normalized_request,
            reserved_names=reserved_names,
            completion_fn=completion,
        )
        draft = _evaluate_and_refine_draft(
            draft,
            request=normalized_request,
            reserved_names=reserved_names,
            completion_fn=completion,
        )
        draft = _apply_dedicated_translation(draft, allow_ai=(completion_fn is None))
        if draft.validation.passed:
            reserved_names.add(str(draft.dish.get("name_en", "")).strip().lower())
        drafts.append(draft)
    return drafts


def validate_draft(
    draft: GeneratedDishDraft,
    *,
    reserved_names: Optional[Iterable[str]] = None,
) -> ValidationResult:
    dish = _normalize_dish_payload(draft.dish)
    recipe = _normalize_recipe_payload(draft.recipe)
    errors: list[str] = []
    warnings: list[str] = []

    name_en = str(dish.get("name_en", "")).strip()
    name_ar = str(dish.get("name_ar", "")).strip()
    if not name_en:
        errors.append("Dish English name is required.")
    if not name_ar:
        errors.append("Dish Arabic name is required.")

    if reserved_names is not None and name_en:
        normalized_name = name_en.lower()
        if normalized_name in {str(item).strip().lower() for item in reserved_names if str(item).strip()}:
            errors.append(f"Dish name `{name_en}` already exists in this review batch or dish database.")

    for field_name in ("calories_kcal", "carbs_g", "protein_g", "fat_g"):
        value = float(dish.get(field_name, 0.0) or 0.0)
        if value < 0:
            errors.append(f"{field_name} cannot be negative.")
        elif field_name == "calories_kcal" and value > 2000:
            errors.append("Calories are implausibly high for a single serving.")
        elif field_name != "calories_kcal" and value > 300:
            errors.append(f"{field_name} is implausibly high for a single serving.")
        elif field_name == "calories_kcal" and value > SOFT_MAX_CALORIES:
            warnings.append("Calories are unusually high for a single serving.")
        elif field_name != "calories_kcal" and value > SOFT_MAX_MACRO_GRAMS:
            warnings.append(f"{field_name} is unusually high for a single serving.")

    if dish.get("gluten") not in VALID_GLUTEN:
        errors.append("gluten must be `gluten` or `gluten_free`.")
    if dish.get("protein_type") not in VALID_PROTEIN:
        errors.append("protein_type must be `veg` or `meat`.")
    if dish.get("dairy") not in VALID_DAIRY:
        errors.append("dairy must be `dairy` or `dairy_free`.")

    yield_servings = int(recipe.get("yield_servings", 0) or 0)
    if yield_servings < 1:
        errors.append("Recipe yield_servings must be at least 1.")

    ingredients = [item for item in recipe.get("ingredients", []) or [] if str(item).strip()]
    steps = [item for item in recipe.get("steps", []) or [] if str(item).strip()]
    if not ingredients:
        errors.append("Recipe must include at least one ingredient.")
    if not steps:
        errors.append("Recipe must include at least one step.")

    calories = float(dish.get("calories_kcal", 0.0) or 0.0)
    carbs = float(dish.get("carbs_g", 0.0) or 0.0)
    protein = float(dish.get("protein_g", 0.0) or 0.0)
    fat = float(dish.get("fat_g", 0.0) or 0.0)
    estimated_kcal = (carbs * 4.0) + (protein * 4.0) + (fat * 9.0)
    if calories > 0:
        delta_ratio = abs(estimated_kcal - calories) / max(calories, 1.0)
        if delta_ratio > 0.55:
            warnings.append("Calories do not align closely with the reported macros.")

    ingredient_blob = " ".join(ingredients).lower()
    if dish.get("protein_type") == "veg" and _contains_keywords(ingredient_blob, MEAT_KEYWORDS):
        warnings.append("Ingredients suggest meat, but protein_type is set to veg.")
    if dish.get("protein_type") == "meat" and not _contains_keywords(ingredient_blob, MEAT_KEYWORDS):
        warnings.append("protein_type is meat, but no obvious meat ingredient was found.")
    if dish.get("dairy") == "dairy_free" and _contains_keywords(ingredient_blob, DAIRY_KEYWORDS):
        warnings.append("Ingredients suggest dairy, but dairy is set to dairy_free.")
    if dish.get("gluten") == "gluten_free" and _contains_keywords(ingredient_blob, GLUTEN_KEYWORDS):
        warnings.append("Ingredients suggest gluten, but gluten is set to gluten_free.")

    return ValidationResult(passed=(len(errors) == 0), errors=errors, warnings=warnings)


def evaluate_draft(draft: GeneratedDishDraft, request: GenerationRequest) -> EvaluationResult:
    validation = validate_draft(draft)
    dish = draft.dish
    recipe = draft.recipe
    notes: list[str] = []

    plausibility = 1.0
    if validation.errors:
        plausibility -= min(0.75, 0.25 * len(validation.errors))
        notes.extend(validation.errors[:3])
    if validation.warnings:
        plausibility -= min(0.25, 0.08 * len(validation.warnings))
        notes.extend(validation.warnings[:2])

    recipe_completeness = 0.2
    ingredient_count = len(recipe.get("ingredients", []) or [])
    step_count = len(recipe.get("steps", []) or [])
    if ingredient_count >= 4:
        recipe_completeness += 0.4
    elif ingredient_count >= 2:
        recipe_completeness += 0.2
    elif ingredient_count > 0:
        recipe_completeness += 0.05
        notes.append("Add more ingredient detail to make the recipe kitchen-ready.")
    if step_count >= 4:
        recipe_completeness += 0.4
    elif step_count >= 2:
        recipe_completeness += 0.2
    elif step_count > 0:
        recipe_completeness += 0.05
        notes.append("Add clearer cooking steps to improve execution.")
    if int(recipe.get("yield_servings", 0) or 0) < 1:
        recipe_completeness = min(recipe_completeness, 0.25)

    metadata_consistency = 1.0 - min(0.6, 0.15 * len(validation.warnings))
    if draft.dish.get("name_en", "").strip().lower() == draft.dish.get("name_ar", "").strip().lower():
        metadata_consistency -= 0.2
        notes.append("Arabic and English names look too similar. Improve localization.")

    menu_usefulness = 0.3
    brief_terms = _meaningful_terms(request.brief)
    candidate_text = " ".join(
        [
            str(dish.get("name_en", "")),
            " ".join(recipe.get("ingredients", []) or []),
        ]
    ).lower()
    overlap = len([term for term in brief_terms if term in candidate_text])
    if brief_terms:
        menu_usefulness += min(0.3, 0.06 * overlap)
        if overlap == 0:
            notes.append("Candidate does not strongly reflect the generation brief.")
    if len(str(dish.get("name_en", "")).split()) >= 2:
        menu_usefulness += 0.1
    if any(filter_value for filter_value in draft.request_constraints.values()):
        menu_usefulness += 0.05

    plausibility = _clamp_score(plausibility)
    menu_usefulness = _clamp_score(menu_usefulness)
    recipe_completeness = _clamp_score(recipe_completeness)
    metadata_consistency = _clamp_score(metadata_consistency)
    overall = _clamp_score(
        (plausibility + menu_usefulness + recipe_completeness + metadata_consistency) / 4.0
    )

    return EvaluationResult(
        overall_score=overall,
        plausibility_score=plausibility,
        menu_usefulness_score=menu_usefulness,
        recipe_completeness_score=recipe_completeness,
        metadata_consistency_score=metadata_consistency,
        notes=_dedupe_notes(notes),
    )


def repair_draft(
    raw_or_draft: Any,
    errors: list[str],
    *,
    request: GenerationRequest,
    completion_fn: Optional[CompletionFn] = None,
) -> GeneratedDishDraft:
    completion = completion_fn or request_json_completion
    system_prompt = _repair_system_prompt()
    user_prompt = _repair_user_prompt(raw_or_draft, errors, request)
    fixed = completion(system_prompt, user_prompt)
    obj = parse_json_object(fixed.text)
    return _draft_from_candidate(
        obj,
        source_model=f"{fixed.provider}:{fixed.model}",
        prompt_snapshot=f"{system_prompt}\n\nUSER:\n{user_prompt}",
        request=request.normalized(),
    )


def load_drafts(
    *,
    storage_path: Optional[Path | str] = None,
    firestore_collection: Any = None,
) -> list[GeneratedDishDraft]:
    if firestore_collection is not None:
        rows: list[dict[str, Any]] = []
        for doc in firestore_collection.stream():
            payload = doc.to_dict() or {}
            if isinstance(payload, dict):
                rows.append(payload)
        return sorted(
            [GeneratedDishDraft.from_dict(item) for item in rows],
            key=lambda draft: draft.created_at,
            reverse=True,
        )

    if storage_path is None:
        return []

    path = Path(storage_path)
    if not path.exists():
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    return sorted(
        [GeneratedDishDraft.from_dict(item) for item in raw if isinstance(item, dict)],
        key=lambda draft: draft.created_at,
        reverse=True,
    )


def save_draft_batch(
    drafts: list[GeneratedDishDraft],
    *,
    storage_path: Optional[Path | str] = None,
    firestore_collection: Any = None,
) -> None:
    if firestore_collection is not None:
        for draft in drafts:
            firestore_collection.document(draft.draft_id).set(draft.to_dict())
        return

    if storage_path is None:
        raise ValueError("storage_path is required when Firestore is not configured.")

    path = Path(storage_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [draft.to_dict() for draft in sorted(drafts, key=lambda draft: draft.created_at, reverse=True)]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def approve_drafts(
    draft_ids: Iterable[str],
    *,
    storage_path: Optional[Path | str] = None,
    firestore_collection: Any = None,
    promoted_names: Optional[dict[str, str]] = None,
) -> list[GeneratedDishDraft]:
    target_ids = {str(item) for item in draft_ids}
    drafts = load_drafts(storage_path=storage_path, firestore_collection=firestore_collection)
    updated: list[GeneratedDishDraft] = []
    for draft in drafts:
        if draft.draft_id in target_ids:
            updated.append(
                _replace_draft(
                    draft,
                    status="approved",
                    approved_dish_name=(promoted_names or {}).get(draft.draft_id, draft.dish.get("name_en", "")),
                    approved_at=_now_iso(),
                )
            )
        else:
            updated.append(draft)
    save_draft_batch(updated, storage_path=storage_path, firestore_collection=firestore_collection)
    return updated


def reject_drafts(
    draft_ids: Iterable[str],
    *,
    storage_path: Optional[Path | str] = None,
    firestore_collection: Any = None,
    status: str = "rejected",
) -> list[GeneratedDishDraft]:
    target_ids = {str(item) for item in draft_ids}
    drafts = load_drafts(storage_path=storage_path, firestore_collection=firestore_collection)
    updated: list[GeneratedDishDraft] = []
    for draft in drafts:
        if draft.draft_id in target_ids:
            updated.append(_replace_draft(draft, status=status))
        else:
            updated.append(draft)
    save_draft_batch(updated, storage_path=storage_path, firestore_collection=firestore_collection)
    return updated


def dish_record_from_draft(draft: GeneratedDishDraft) -> dict[str, Any]:
    return _normalize_dish_payload(draft.dish)


def request_from_draft(draft: GeneratedDishDraft) -> GenerationRequest:
    return GenerationRequest(
        brief=draft.request_brief,
        count=1,
        protein_type=draft.request_constraints.get("protein_type"),
        gluten=draft.request_constraints.get("gluten"),
        dairy=draft.request_constraints.get("dairy"),
    ).normalized()


def _draft_from_candidate(
    raw_candidate: Any,
    *,
    source_model: str,
    prompt_snapshot: str,
    request: GenerationRequest,
) -> GeneratedDishDraft:
    candidate = dict(raw_candidate) if isinstance(raw_candidate, dict) else {}
    recipe = {
        "yield_servings": candidate.get("yield_servings", candidate.get("recipe_yield", 1)),
        "ingredients": candidate.get("ingredients", []),
        "steps": candidate.get("steps", []),
    }
    draft = GeneratedDishDraft(
        draft_id=uuid4().hex,
        status="review_ready",
        created_at=_now_iso(),
        source_model=source_model,
        generation_prompt=prompt_snapshot,
        request_brief=request.brief,
        request_constraints={
            "protein_type": request.protein_type,
            "gluten": request.gluten,
            "dairy": request.dairy,
        },
        dish=_normalize_dish_payload(candidate),
        recipe=_normalize_recipe_payload(recipe),
        validation=ValidationResult(passed=False),
        evaluation=EvaluationResult(0.0, 0.0, 0.0, 0.0, 0.0, []),
        attempts=1,
        repair_history=[],
    )
    validation = validate_draft(draft)
    evaluation = evaluate_draft(draft, request)
    return _replace_draft(draft, validation=validation, evaluation=evaluation)


def _repair_until_valid(
    draft: GeneratedDishDraft,
    *,
    request: GenerationRequest,
    reserved_names: set[str],
    completion_fn: CompletionFn,
) -> GeneratedDishDraft:
    current = draft
    validation = validate_draft(current, reserved_names=reserved_names)
    current = _replace_draft(current, validation=validation)
    while not validation.passed and current.attempts < 2:
        repaired = repair_draft(
            current.to_dict(),
            validation.errors,
            request=request,
            completion_fn=completion_fn,
        )
        validation = validate_draft(repaired, reserved_names=reserved_names)
        repair_history = list(current.repair_history) + [f"Repair attempt {current.attempts + 1}: {'; '.join(validation.errors or ['passed'])}"]
        current = _replace_draft(
            repaired,
            draft_id=current.draft_id,
            created_at=current.created_at,
            attempts=current.attempts + 1,
            repair_history=repair_history,
            validation=validation,
        )
    final_status = "review_ready" if validation.passed else "needs_attention"
    return _replace_draft(current, status=final_status, validation=validation)


def _evaluate_and_refine_draft(
    draft: GeneratedDishDraft,
    *,
    request: GenerationRequest,
    reserved_names: set[str],
    completion_fn: CompletionFn,
) -> GeneratedDishDraft:
    current = draft
    if not current.validation.passed:
        return current
    evaluation = evaluate_draft(current, request)
    current = _replace_draft(current, evaluation=evaluation)
    if evaluation.overall_score >= 0.7:
        return current

    refined = _refine_draft(current, evaluation.notes, request=request, completion_fn=completion_fn)
    validation = validate_draft(refined, reserved_names=reserved_names)
    evaluation = evaluate_draft(_replace_draft(refined, validation=validation), request)
    repair_history = list(current.repair_history) + [f"Refinement: {'; '.join(evaluation.notes or ['rescored'])}"]
    refined_status = "review_ready" if validation.passed else "needs_attention"
    return _replace_draft(
        refined,
        draft_id=current.draft_id,
        created_at=current.created_at,
        attempts=current.attempts + 1,
        repair_history=repair_history,
        status=refined_status,
        validation=validation,
        evaluation=evaluation,
    )


def _refine_draft(
    draft: GeneratedDishDraft,
    notes: list[str],
    *,
    request: GenerationRequest,
    completion_fn: CompletionFn,
) -> GeneratedDishDraft:
    system_prompt = _refine_system_prompt()
    user_prompt = _refine_user_prompt(draft, notes, request)
    completion = completion_fn(system_prompt, user_prompt)
    obj = parse_json_object(completion.text)
    return _draft_from_candidate(
        obj,
        source_model=f"{completion.provider}:{completion.model}",
        prompt_snapshot=f"{system_prompt}\n\nUSER:\n{user_prompt}",
        request=request,
    )


def _parse_candidate_batch(text: str, completion_fn: CompletionFn) -> dict[str, Any]:
    raw_text = text
    errors = ""
    for attempt in range(1, 3):
        try:
            parsed = parse_json_object(raw_text)
            candidates = parsed.get("candidates")
            if isinstance(candidates, list):
                return parsed
            raise ValueError("Top-level JSON object must contain a `candidates` array.")
        except Exception as exc:
            errors = str(exc)
            if attempt >= 2:
                break
            repair = completion_fn(
                _repair_system_prompt(),
                "Fix this JSON object so it contains only valid JSON with a top-level `candidates` array.\n"
                f"Error: {errors}\n"
                f"Broken JSON:\n{raw_text}",
            )
            raw_text = repair.text
    raise RuntimeError(f"Could not parse generated candidate batch. Last error: {errors}")


def _generation_system_prompt() -> str:
    return (
        "You generate restaurant-ready dish concepts for a menu card system.\n"
        "Return only JSON with this exact top-level shape: {\"candidates\": [...]}.\n"
        "Each candidate must contain exactly these keys: "
        "name_en, name_ar, calories_kcal, carbs_g, protein_g, fat_g, gluten, protein_type, dairy, "
        "yield_servings, ingredients, steps.\n"
        "ingredients must be an array of concise ingredient lines with amounts.\n"
        "steps must be an array of concise preparation steps.\n"
        "gluten must be `gluten` or `gluten_free`.\n"
        "protein_type must be `veg` or `meat`.\n"
        "dairy must be `dairy` or `dairy_free`.\n"
        "Use plausible single-serving nutrition values.\n"
        "Do not wrap the JSON in markdown."
    )


def _generation_user_prompt(request: GenerationRequest, existing_dish_names: Iterable[str]) -> str:
    existing = sorted({str(name).strip() for name in existing_dish_names if str(name).strip()})
    constraints = []
    if request.protein_type:
        constraints.append(f"protein_type must be {request.protein_type}")
    if request.gluten:
        constraints.append(f"gluten must be {request.gluten}")
    if request.dairy:
        constraints.append(f"dairy must be {request.dairy}")
    constraint_text = "; ".join(constraints) if constraints else "No hard dietary filters."

    return (
        f"Generate {request.count} distinct dish candidates for this brief: {request.brief}\n"
        f"Constraints: {constraint_text}\n"
        f"Avoid these existing dish names: {existing[:50]}\n"
        "Every candidate must be distinct, menu-friendly, and suitable for human review before saving."
    )


def _repair_system_prompt() -> str:
    return (
        "You are repairing a JSON response for a recipe-generation pipeline.\n"
        "Return corrected JSON only. Do not include markdown, commentary, or extra keys."
    )


def _repair_user_prompt(raw_or_draft: Any, errors: list[str], request: GenerationRequest) -> str:
    raw_json = json.dumps(raw_or_draft, ensure_ascii=False, indent=2)
    error_text = "\n".join(f"- {item}" for item in errors)
    return (
        f"Fix this candidate for the brief: {request.brief}\n"
        f"Validation errors:\n{error_text}\n"
        "Return a single corrected candidate JSON object with keys: "
        "name_en, name_ar, calories_kcal, carbs_g, protein_g, fat_g, gluten, protein_type, dairy, "
        "yield_servings, ingredients, steps.\n"
        f"Candidate:\n{raw_json}"
    )


def _refine_system_prompt() -> str:
    return (
        "You refine dish draft JSON for a restaurant content pipeline.\n"
        "Return a single improved candidate JSON object only, with no markdown."
    )


def _refine_user_prompt(draft: GeneratedDishDraft, notes: list[str], request: GenerationRequest) -> str:
    note_text = "\n".join(f"- {item}" for item in notes) or "- Improve clarity and consistency."
    return (
        f"Improve this candidate for the brief: {request.brief}\n"
        f"Refinement notes:\n{note_text}\n"
        "Keep the same JSON keys: name_en, name_ar, calories_kcal, carbs_g, protein_g, fat_g, "
        "gluten, protein_type, dairy, yield_servings, ingredients, steps.\n"
        f"Candidate:\n{json.dumps(draft.to_dict(), ensure_ascii=False, indent=2)}"
    )


def _normalize_dish_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name_en": " ".join(str(payload.get("name_en", "")).split()).strip(),
        "name_ar": " ".join(str(payload.get("name_ar", "")).split()).strip(),
        "calories_kcal": _to_float(payload.get("calories_kcal", 0.0)),
        "carbs_g": _to_float(payload.get("carbs_g", 0.0)),
        "protein_g": _to_float(payload.get("protein_g", 0.0)),
        "fat_g": _to_float(payload.get("fat_g", 0.0)),
        "gluten": _normalize_enum_payload(payload.get("gluten"), "gluten_free"),
        "protein_type": _normalize_enum_payload(payload.get("protein_type"), "veg"),
        "dairy": _normalize_enum_payload(payload.get("dairy"), "dairy_free"),
    }


def _apply_dedicated_translation(draft: GeneratedDishDraft, *, allow_ai: bool) -> GeneratedDishDraft:
    name_en = str(draft.dish.get("name_en", "")).strip()
    if not name_en:
        return draft
    translated_name = translate_dish_name(
        name_en,
        proposed_name_ar=str(draft.dish.get("name_ar", "")).strip(),
        allow_ai=allow_ai,
    )
    if not translated_name:
        return draft
    payload = draft.to_dict()
    dish_payload = dict(payload.get("dish") or {})
    dish_payload["name_ar"] = translated_name
    payload["dish"] = dish_payload
    updated = GeneratedDishDraft.from_dict(payload)
    validation = validate_draft(updated)
    evaluation = evaluate_draft(updated, request_from_draft(updated))
    return _replace_draft(updated, validation=validation, evaluation=evaluation)


def _normalize_recipe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ingredients = payload.get("ingredients", []) or []
    steps = payload.get("steps", []) or []
    return {
        "yield_servings": max(0, int(_to_float(payload.get("yield_servings", 0)))),
        "ingredients": [" ".join(str(item).split()).strip() for item in ingredients if str(item).strip()],
        "steps": [" ".join(str(item).split()).strip() for item in steps if str(item).strip()],
    }


def _normalize_optional_flag(value: Any, allowed: set[str]) -> Optional[str]:
    if value is None or str(value).strip() == "" or str(value).strip().lower() == "any":
        return None
    normalized = str(value).strip().lower()
    return normalized if normalized in allowed else None


def _normalize_enum_payload(value: Any, default: str) -> str:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower()


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _contains_keywords(text: str, keywords: set[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _meaningful_terms(text: str) -> list[str]:
    candidates = re.findall(r"[a-zA-Z]{4,}", text.lower())
    stop_words = {"with", "from", "that", "this", "into", "your", "about", "menu", "dish", "recipe"}
    return [item for item in candidates if item not in stop_words]


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, round(value, 3)))


def _dedupe_notes(notes: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for note in notes:
        cleaned = " ".join(str(note).split()).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        output.append(cleaned)
    return output


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _replace_draft(draft: GeneratedDishDraft, **changes: Any) -> GeneratedDishDraft:
    payload = draft.to_dict()
    for key, value in changes.items():
        if isinstance(value, ValidationResult):
            payload[key] = value.to_dict()
        elif isinstance(value, EvaluationResult):
            payload[key] = value.to_dict()
        else:
            payload[key] = value
    return GeneratedDishDraft.from_dict(payload)
