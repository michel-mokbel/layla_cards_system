from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from cards import (
    AssetPaths,
    DeliveryNoteRow,
    Dish,
    GREETING_LABEL_STYLE_CLEAN,
    GREETING_LABEL_STYLE_PLAYFUL,
    generate_cards_pdf,
    generate_delivery_note_pdf,
    generate_greeting_labels_pdf,
    parse_greeting_label_names,
)


BASE_DIR = Path(__file__).resolve().parents[1]
ASSETS_DIR = BASE_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"


def _pdf_page_count(pdf_bytes: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", pdf_bytes))


def _assets() -> AssetPaths:
    return AssetPaths(
        logo=ASSETS_DIR / "logo.png",
        icon_gluten=ICONS_DIR / "gluten.png",
        icon_gluten_free=ICONS_DIR / "gluten_free.png",
        icon_veg=ICONS_DIR / "veg.png",
        icon_meat=ICONS_DIR / "meat.png",
        icon_dairy=ICONS_DIR / "dairy.png",
        icon_dairy_free=ICONS_DIR / "dairy_free.png",
        template_page=None,
    )


class GreetingLabelTests(unittest.TestCase):
    def test_parse_greeting_label_names_normalizes_input(self) -> None:
        parsed = parse_greeting_label_names("  MOI  \n\n  HAMAD   HOSPITAL \n OPERATORS  TEAM ")
        self.assertEqual([item.name for item in parsed], ["MOI", "HAMAD HOSPITAL", "OPERATORS TEAM"])

    def test_generate_greeting_labels_pdf_paginates_by_ten(self) -> None:
        assets = _assets()
        with tempfile.TemporaryDirectory() as tmp_dir:
            for label_count, expected_pages in ((1, 1), (10, 1), (11, 2), (23, 3)):
                with self.subTest(label_count=label_count):
                    labels = parse_greeting_label_names("\n".join(f"Client {idx}" for idx in range(1, label_count + 1)))
                    out_path = Path(tmp_dir) / f"labels_{label_count}.pdf"
                    generate_greeting_labels_pdf(
                        labels=labels,
                        out_pdf_path=out_path,
                        assets=assets,
                        style=GREETING_LABEL_STYLE_CLEAN,
                    )
                    pdf_bytes = out_path.read_bytes()
                    self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
                    self.assertGreater(len(pdf_bytes), 1000)
                    self.assertEqual(_pdf_page_count(pdf_bytes), expected_pages)

    def test_generate_greeting_labels_pdf_supports_both_styles(self) -> None:
        assets = _assets()
        labels = parse_greeting_label_names("MOI\nOPERATORS\nMARSHALLS")
        with tempfile.TemporaryDirectory() as tmp_dir:
            clean_path = Path(tmp_dir) / "clean.pdf"
            playful_path = Path(tmp_dir) / "playful.pdf"

            generate_greeting_labels_pdf(
                labels=labels,
                out_pdf_path=clean_path,
                assets=assets,
                style=GREETING_LABEL_STYLE_CLEAN,
            )
            generate_greeting_labels_pdf(
                labels=labels,
                out_pdf_path=playful_path,
                assets=assets,
                style=GREETING_LABEL_STYLE_PLAYFUL,
            )

            clean_bytes = clean_path.read_bytes()
            playful_bytes = playful_path.read_bytes()
            self.assertGreater(len(clean_bytes), 1000)
            self.assertGreater(len(playful_bytes), 1000)
            self.assertNotEqual(clean_bytes, playful_bytes)

    def test_generate_greeting_labels_pdf_handles_long_names(self) -> None:
        assets = _assets()
        labels = parse_greeting_label_names("Serge Mokbel\nNada Abdul Karim")
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "long_names.pdf"
            generate_greeting_labels_pdf(
                labels=labels,
                out_pdf_path=out_path,
                assets=assets,
                style=GREETING_LABEL_STYLE_CLEAN,
            )
            pdf_bytes = out_path.read_bytes()
            self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
            self.assertGreater(len(pdf_bytes), 1000)

    def test_existing_dish_card_generation_still_works(self) -> None:
        assets = _assets()
        dishes = [
            Dish(
                name_en="Chicken Shawarma",
                name_ar="شاورما دجاج",
                calories_kcal=420,
                carbs_g=28,
                protein_g=26,
                fat_g=18,
                gluten="gluten",
                protein_type="meat",
                dairy="dairy_free",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "cards.pdf"
            generate_cards_pdf(
                dishes=dishes,
                out_pdf_path=out_path,
                assets=assets,
            )
            pdf_bytes = out_path.read_bytes()
            self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
            self.assertGreater(len(pdf_bytes), 1000)

    def test_generate_delivery_note_pdf_supports_reference_layout(self) -> None:
        assets = _assets()
        rows = [
            DeliveryNoteRow(sr_no="1", food_type="Strawberry Juice", unit="Bot"),
            DeliveryNoteRow(sr_no="2", food_type="Orange Juice", unit="Bot"),
            DeliveryNoteRow(sr_no="3", food_type="Chicken Sandwich", unit="Pc"),
            DeliveryNoteRow(sr_no="4", food_type="Sweet Potato / Cheese", unit="Pc"),
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "delivery_note.pdf"
            generate_delivery_note_pdf(
                rows=rows,
                out_pdf_path=out_path,
                assets=assets,
                client_name="Microsoft",
                location="Al Fardan Tower - Lusail",
                reference="KL/EFS-MS/026",
                revision="00",
                issue_date="26/02/2026",
                issue_no="00",
            )
            pdf_bytes = out_path.read_bytes()
            self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
            self.assertGreater(len(pdf_bytes), 1000)
            self.assertEqual(_pdf_page_count(pdf_bytes), 1)


if __name__ == "__main__":
    unittest.main()
