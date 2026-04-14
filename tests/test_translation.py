from __future__ import annotations

import unittest

from translation import _fallback_glossary_translation, _lookup_existing_translation, translate_dish_name


class TranslationTests(unittest.TestCase):
    def test_existing_translation_memory_is_used(self) -> None:
        self.assertEqual(_lookup_existing_translation("Banana Cake"), "ﻛﯿﻚ اﻟﻤﻮز")

    def test_glossary_fallback_builds_basic_arabic_name(self) -> None:
        translated = _fallback_glossary_translation("Chicken Salad Wrap")
        self.assertIn("دجاج", translated)
        self.assertIn("سلطة", translated)
        self.assertIn("راب", translated)

    def test_translate_uses_exact_match_without_ai(self) -> None:
        self.assertEqual(translate_dish_name("Chicken Wrap"), "راب دﺟﺎج")


if __name__ == "__main__":
    unittest.main()
