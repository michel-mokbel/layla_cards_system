from __future__ import annotations

import tempfile
import unittest

from ai_client import AICompletion
from ai_recipe_studio import (
    GeneratedDishDraft,
    GenerationRequest,
    approve_drafts,
    dish_record_from_draft,
    evaluate_draft,
    generate_dish_drafts,
    load_drafts,
    save_draft_batch,
    validate_draft,
)


class FakeDocRef:
    def __init__(self, store: dict[str, dict], doc_id: str) -> None:
        self.store = store
        self.doc_id = doc_id

    def set(self, payload: dict) -> None:
        self.store[self.doc_id] = payload


class FakeSnapshot:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return self._payload


class FakeCollection:
    def __init__(self) -> None:
        self.store: dict[str, dict] = {}

    def document(self, doc_id: str) -> FakeDocRef:
        return FakeDocRef(self.store, doc_id)

    def stream(self) -> list[FakeSnapshot]:
        return [FakeSnapshot(payload) for payload in self.store.values()]


def _sample_draft() -> GeneratedDishDraft:
    return GeneratedDishDraft.from_dict(
        {
            "draft_id": "draft-1",
            "status": "review_ready",
            "created_at": "2026-04-01T10:00:00+00:00",
            "source_model": "test:model",
            "generation_prompt": "prompt",
            "request_brief": "Mediterranean protein breakfast",
            "request_constraints": {"protein_type": "meat", "gluten": None, "dairy": None},
            "dish": {
                "name_en": "Chicken Zaatar Pocket",
                "name_ar": "جيب دجاج زعتر",
                "calories_kcal": 420,
                "carbs_g": 28,
                "protein_g": 26,
                "fat_g": 18,
                "gluten": "gluten",
                "protein_type": "meat",
                "dairy": "dairy_free",
            },
            "recipe": {
                "yield_servings": 2,
                "ingredients": [
                    "200 g chicken breast",
                    "2 pita pockets",
                    "1 tbsp zaatar",
                    "1 tbsp olive oil",
                ],
                "steps": [
                    "Season and cook the chicken.",
                    "Warm the pita pockets.",
                    "Fill and serve.",
                ],
            },
            "validation": {"passed": True, "errors": [], "warnings": []},
            "evaluation": {
                "overall_score": 0.82,
                "plausibility_score": 0.85,
                "menu_usefulness_score": 0.8,
                "recipe_completeness_score": 0.8,
                "metadata_consistency_score": 0.83,
                "notes": [],
            },
            "attempts": 1,
            "repair_history": [],
        }
    )


class AIRecipeStudioTests(unittest.TestCase):
    def test_validate_draft_flags_and_numbers(self) -> None:
        draft = GeneratedDishDraft.from_dict(
            {
                **_sample_draft().to_dict(),
                "dish": {
                    "name_en": "  ",
                    "name_ar": "",
                    "calories_kcal": -10,
                    "carbs_g": 12,
                    "protein_g": 8,
                    "fat_g": 4,
                    "gluten": "unknown",
                    "protein_type": "veg",
                    "dairy": "dairy_free",
                },
                "recipe": {"yield_servings": 0, "ingredients": [], "steps": []},
            }
        )

        result = validate_draft(draft, reserved_names={"chicken zaatar pocket"})

        self.assertFalse(result.passed)
        self.assertTrue(any("Dish English name is required." in error for error in result.errors))
        self.assertTrue(any("gluten must be" in error for error in result.errors))
        self.assertTrue(any("yield_servings" in error for error in result.errors))

    def test_evaluate_draft_low_score(self) -> None:
        draft = GeneratedDishDraft.from_dict(
            {
                **_sample_draft().to_dict(),
                "dish": {
                    **_sample_draft().dish,
                    "name_en": "Snack Cup",
                    "name_ar": "كوب سناك",
                },
                "recipe": {
                    "yield_servings": 1,
                    "ingredients": ["1 cup mixed beans"],
                    "steps": ["Serve chilled."],
                },
                "request_brief": "luxury seafood brunch",
            }
        )

        score = evaluate_draft(draft, GenerationRequest(brief="luxury seafood brunch"))

        self.assertLess(score.overall_score, 0.7)
        self.assertTrue(score.notes)

    def test_generate_repairs_and_refines(self) -> None:
        responses = iter(
            [
                AICompletion(
                    text='{"candidates":[{"name_en":"Spiced Chicken Pocket","name_ar":"","calories_kcal":420,"carbs_g":28,"protein_g":26,"fat_g":18,"gluten":"gluten","protein_type":"meat","dairy":"dairy_free","yield_servings":2,"ingredients":["200 g chicken breast"],"steps":["Cook."]}]}',
                    provider="test",
                    model="stub",
                ),
                AICompletion(
                    text='{"name_en":"Spiced Chicken Pocket","name_ar":"جيب دجاج متبل","calories_kcal":420,"carbs_g":28,"protein_g":26,"fat_g":18,"gluten":"gluten","protein_type":"meat","dairy":"dairy_free","yield_servings":2,"ingredients":["200 g chicken breast"],"steps":["Cook."]}',
                    provider="test",
                    model="stub",
                ),
                AICompletion(
                    text='{"name_en":"Spiced Chicken Pocket","name_ar":"جيب دجاج متبل","calories_kcal":420,"carbs_g":28,"protein_g":26,"fat_g":18,"gluten":"gluten","protein_type":"meat","dairy":"dairy_free","yield_servings":2,"ingredients":["200 g chicken breast","2 pita pockets","1 tbsp zaatar","1 tbsp olive oil"],"steps":["Season the chicken.","Cook the chicken.","Warm the pita.","Assemble and serve."]}',
                    provider="test",
                    model="stub",
                ),
            ]
        )

        drafts = generate_dish_drafts(
            GenerationRequest(brief="Mediterranean protein breakfast", count=1),
            completion_fn=lambda *_args: next(responses),
        )

        self.assertEqual(len(drafts), 1)
        draft = drafts[0]
        self.assertTrue(draft.validation.passed)
        self.assertGreaterEqual(draft.evaluation.overall_score, 0.7)
        self.assertEqual(draft.attempts, 3)
        self.assertTrue(any("Repair attempt" in item for item in draft.repair_history))
        self.assertTrue(any("Refinement" in item for item in draft.repair_history))

    def test_generate_stops_after_failed_repair(self) -> None:
        responses = iter(
            [
                AICompletion(
                    text='{"candidates":[{"name_en":"","name_ar":"","calories_kcal":-1,"carbs_g":10,"protein_g":4,"fat_g":2,"gluten":"bad","protein_type":"veg","dairy":"dairy_free","yield_servings":0,"ingredients":[],"steps":[]}]}',
                    provider="test",
                    model="stub",
                ),
                AICompletion(
                    text='{"name_en":"","name_ar":"","calories_kcal":-1,"carbs_g":10,"protein_g":4,"fat_g":2,"gluten":"bad","protein_type":"veg","dairy":"dairy_free","yield_servings":0,"ingredients":[],"steps":[]}',
                    provider="test",
                    model="stub",
                ),
            ]
        )

        drafts = generate_dish_drafts(
            GenerationRequest(brief="Healthy salad ideas", count=1),
            completion_fn=lambda *_args: next(responses),
        )

        self.assertEqual(drafts[0].status, "needs_attention")
        self.assertEqual(drafts[0].attempts, 2)

    def test_json_storage_roundtrip_and_approval(self) -> None:
        draft = _sample_draft()
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = f"{tmp_dir}/drafts.json"
            save_draft_batch([draft], storage_path=path)
            loaded = load_drafts(storage_path=path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].dish["name_en"], draft.dish["name_en"])

            approved = approve_drafts([draft.draft_id], storage_path=path, promoted_names={draft.draft_id: "Approved Dish"})
            self.assertEqual(approved[0].status, "approved")
            self.assertEqual(approved[0].approved_dish_name, "Approved Dish")

    def test_firestore_storage_roundtrip(self) -> None:
        draft = _sample_draft()
        collection = FakeCollection()

        save_draft_batch([draft], firestore_collection=collection)
        loaded = load_drafts(firestore_collection=collection)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].draft_id, draft.draft_id)

    def test_dish_record_from_draft_matches_existing_schema(self) -> None:
        record = dish_record_from_draft(_sample_draft())

        self.assertEqual(
            set(record.keys()),
            {"name_en", "name_ar", "calories_kcal", "carbs_g", "protein_g", "fat_g", "gluten", "protein_type", "dairy"},
        )


if __name__ == "__main__":
    unittest.main()
