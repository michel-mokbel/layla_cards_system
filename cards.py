"""
cards.py — Generate Layla-style nutrition cards into a printable PDF (A4, 2x3 grid).

What this supports:
- Dish EN + Arabic name
- Macros: calories / carbs / protein / fat
- 3 icons per card (gluten vs gluten-free, veg vs meat, dairy vs dairy-free)
- Logo inside each card
- Exports to PDF for printing

You can wire this into any UI (Streamlit, Flask, CLI, Google Sheets export, etc.)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import csv

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ---------- Optional Arabic shaping (recommended) ----------
def _try_arabic_shape(text: str) -> str:
    """
    ReportLab doesn't shape Arabic by default.
    If you install:
        pip install arabic-reshaper python-bidi
    and provide an Arabic-capable TTF font, Arabic will render correctly.
    """
    try:
        import arabic_reshaper  # type: ignore
        from bidi.algorithm import get_display  # type: ignore
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


# ---------- Data model ----------
@dataclass(frozen=True)
class Dish:
    name_en: str
    name_ar: str
    calories_kcal: float
    carbs_g: float
    protein_g: float
    fat_g: float
    gluten: str         # "gluten" | "gluten_free"
    protein_type: str   # "veg" | "meat"
    dairy: str          # "dairy" | "dairy_free"


def load_dishes_csv(csv_path: str | Path) -> Dict[str, Dish]:
    """
    CSV columns expected:
      name_en,name_ar,calories_kcal,carbs_g,protein_g,fat_g,gluten,protein_type,dairy
    Keys are lowercased English names.
    """
    csv_path = Path(csv_path)
    dishes: Dict[str, Dish] = {}

    def to_float(value: object, default: float = 0.0) -> float:
        try:
            s = str(value).strip()
            if s == "":
                return default
            return float(s)
        except Exception:
            return default

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name_en = (row.get("name_en") or "").strip()
            if not name_en:
                continue
            d = Dish(
                name_en=name_en,
                name_ar=row.get("name_ar", "").strip(),
                calories_kcal=to_float(row.get("calories_kcal", 0.0)),
                carbs_g=to_float(row.get("carbs_g", 0.0)),
                protein_g=to_float(row.get("protein_g", 0.0)),
                fat_g=to_float(row.get("fat_g", 0.0)),
                gluten=row.get("gluten", "gluten_free").strip(),
                protein_type=row.get("protein_type", "veg").strip(),
                dairy=row.get("dairy", "dairy_free").strip(),
            )
            dishes[d.name_en.lower()] = d
    return dishes


# ---------- Rendering ----------
@dataclass(frozen=True)
class AssetPaths:
    logo: Path
    icon_gluten: Path
    icon_gluten_free: Path
    icon_veg: Path
    icon_meat: Path
    icon_dairy: Path
    icon_dairy_free: Path
    # Optional fonts (TTF)
    font_latin: Optional[Path] = None
    font_arabic: Optional[Path] = None


def _register_fonts(assets: AssetPaths) -> Tuple[str, str]:
    """
    Register TTF fonts if provided.
    Returns (latin_font_name, arabic_font_name).
    """
    latin_name = "Helvetica"
    arabic_name = "Helvetica"

    if assets.font_latin and assets.font_latin.exists():
        latin_name = "LaylaLatin"
        pdfmetrics.registerFont(TTFont(latin_name, str(assets.font_latin)))

    if assets.font_arabic and assets.font_arabic.exists():
        arabic_name = "LaylaArabic"
        pdfmetrics.registerFont(TTFont(arabic_name, str(assets.font_arabic)))

    return latin_name, arabic_name


def _icon_triplet(d: Dish, assets: AssetPaths) -> List[Path]:
    # Fixed 3-icons layout to match your paper:
    # [gluten/gluten-free] [veg/meat] [dairy/dairy-free]
    gluten_icon = assets.icon_gluten_free if d.gluten == "gluten_free" else assets.icon_gluten
    protein_icon = assets.icon_veg if d.protein_type == "veg" else assets.icon_meat
    dairy_icon = assets.icon_dairy_free if d.dairy == "dairy_free" else assets.icon_dairy
    return [gluten_icon, protein_icon, dairy_icon]


def generate_cards_pdf(
    dishes: Iterable[Dish],
    out_pdf_path: str | Path,
    assets: AssetPaths,
    title: str = "Layla Cards",
) -> Path:
    """
    Generate an A4 PDF, 2 columns x 3 rows per page (6 cards).
    """
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    page_w, page_h = A4
    c = canvas.Canvas(str(out_pdf_path), pagesize=A4)
    c.setTitle(title)

    latin_font, arabic_font = _register_fonts(assets)

    # Layout (tweakable)
    margin = 12 * mm
    grid_w = page_w - 2 * margin
    grid_h = page_h - 2 * margin

    cols, rows = 2, 3
    card_w = grid_w / cols
    card_h = grid_h / rows

    # Styling
    border_color = colors.Color(0.85, 0.85, 0.85)
    border_width = 0.6

    # Content offsets within card
    pad_x = 10 * mm
    pad_top = 8 * mm
    logo_h = 18 * mm
    logo_w = 30 * mm

    icon_size = 12 * mm
    icon_gap = 4 * mm

    # Macro column
    macro_x_offset = card_w * 0.60
    macro_y_top_offset = card_h * 0.60
    macro_line_gap = 4.5 * mm

    # Dish text
    dish_en_y = card_h * 0.58
    dish_ar_y = dish_en_y - 7.5 * mm

    dish_en_size = 12
    dish_ar_size = 11
    macro_size = 9.5

    dish_list = list(dishes)
    idx = 0

    while idx < len(dish_list):
        # Grid lines
        c.setStrokeColor(border_color)
        c.setLineWidth(border_width)
        # Outer border
        c.rect(margin, margin, grid_w, grid_h, stroke=1, fill=0)
        # Vertical divider
        c.line(margin + card_w, margin, margin + card_w, margin + grid_h)
        # Horizontal dividers (2 lines)
        c.line(margin, margin + card_h, margin + grid_w, margin + card_h)
        c.line(margin, margin + 2 * card_h, margin + grid_w, margin + 2 * card_h)

        # Draw up to 6 cards
        for r in range(rows):
            for col in range(cols):
                if idx >= len(dish_list):
                    break
                d = dish_list[idx]
                idx += 1

                # Card origin (bottom-left)
                x0 = margin + col * card_w
                y0 = margin + (rows - 1 - r) * card_h

                # Logo (top center)
                try:
                    c.drawImage(
                        ImageReader(str(assets.logo)),
                        x0 + (card_w - logo_w) / 2,
                        y0 + card_h - pad_top - logo_h,
                        width=logo_w,
                        height=logo_h,
                        mask="auto",
                        preserveAspectRatio=True,
                        anchor="c",
                    )
                except Exception:
                    pass

                # Dish name EN (bold)
                c.setFillColor(colors.black)
                c.setFont(latin_font, dish_en_size)
                c.drawString(x0 + pad_x, y0 + dish_en_y, d.name_en)

                # Dish name AR (under EN, left side)
                c.setFont(arabic_font, dish_ar_size)
                ar_text = _try_arabic_shape(d.name_ar) if d.name_ar else ""
                c.drawString(x0 + pad_x, y0 + dish_ar_y, ar_text)

                # Icons (left bottom-ish)
                icons = _icon_triplet(d, assets)
                ix = x0 + pad_x
                iy = y0 + 14 * mm
                for p in icons:
                    try:
                        c.drawImage(
                            ImageReader(str(p)),
                            ix,
                            iy,
                            width=icon_size,
                            height=icon_size,
                            mask="auto",
                            preserveAspectRatio=True,
                        )
                    except Exception:
                        # If icon missing, draw placeholder square
                        c.rect(ix, iy, icon_size, icon_size, stroke=1, fill=0)
                    ix += icon_size + icon_gap

                # Macros (right side list)
                c.setFont(latin_font, macro_size)

                def macro_line(label: str, value: str, n: int):
                    c.drawString(
                        x0 + macro_x_offset,
                        y0 + macro_y_top_offset - n * macro_line_gap,
                        f"{label}: {value}",
                    )

                macro_line("Calories", f"{_fmt(d.calories_kcal)} kcal", 0)
                macro_line("Carbohydrates", f"{_fmt(d.carbs_g)} g", 1)
                macro_line("Protein", f"{_fmt(d.protein_g)} g", 2)
                macro_line("Fat", f"{_fmt(d.fat_g)} g", 3)

            if idx >= len(dish_list):
                break

        c.showPage()

    c.save()
    return out_pdf_path


def _fmt(x: float) -> str:
    # Pretty formatting: 3.0 -> "3", 3.5 -> "3.5"
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.1f}"
