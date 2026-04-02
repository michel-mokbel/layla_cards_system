from __future__ import annotations

import unittest

from idea_center import IDEA_PRESETS


class IdeaCenterTests(unittest.TestCase):
    def test_presets_have_unique_ids_and_prompts(self) -> None:
        preset_ids = [preset.preset_id for preset in IDEA_PRESETS]
        self.assertEqual(len(preset_ids), len(set(preset_ids)))
        self.assertTrue(IDEA_PRESETS)
        for preset in IDEA_PRESETS:
            self.assertTrue(preset.title.strip())
            self.assertTrue(preset.summary.strip())
            self.assertTrue(preset.prompt.strip())
            self.assertGreaterEqual(preset.count, 1)
            self.assertLessEqual(preset.count, 10)


if __name__ == "__main__":
    unittest.main()
