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

from dataclasses import asdict, dataclass
import json
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
    template_page: Optional[Path] = None
    # Optional fonts (TTF)
    font_latin: Optional[Path] = None
    font_latin_bold: Optional[Path] = None
    font_arabic: Optional[Path] = None
    font_arabic_bold: Optional[Path] = None


@dataclass(frozen=True)
class DebugOverlayOptions:
    enabled: bool = False
    show_grid: bool = True
    show_docx_shapes: bool = False
    docx_layout_json: Optional[Path] = None
    reference_image: Optional[Path] = None
    limit_shapes: int = 250


@dataclass(frozen=True)
class LayoutConfig:
    cols: int = 2
    rows: int = 3
    grid_x_mm: float = 7.2
    grid_y_mm: float = 26.0
    card_w_mm: float = 98.8
    card_h_mm: float = 79.2
    draw_grid_lines: bool = True
    draw_logo: bool = False
    dish_x_offset_mm: float = 0.0
    dish_box_width_mm: float = 51.752
    dish_en_y_mm: float = 53.5
    dish_ar_gap_mm: float = 8.6
    dish_en_size: float = 14.0
    dish_ar_size: float = 13.0
    icon_x_offset_mm: float = 4.9
    icon_y_offset_mm: float = 27.6
    icon_size_mm: float = 11.938
    icon_gap_mm: float = 4.49
    macro_x_offset_mm: float = 49.6
    macro_y_top_mm: float = 48.0
    macro_line_gap_mm: float = 5.9
    macro_size: float = 10.5


def default_layout_dict() -> Dict[str, object]:
    return asdict(LayoutConfig())


def load_layout_config(path: Optional[Path]) -> LayoutConfig:
    if not path or (not path.exists()):
        return LayoutConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return LayoutConfig()
    if not isinstance(raw, dict):
        return LayoutConfig()

    base = asdict(LayoutConfig())
    merged = {**base, **{k: v for k, v in raw.items() if k in base}}
    try:
        return LayoutConfig(**merged)
    except Exception:
        return LayoutConfig()


def _register_fonts(assets: AssetPaths) -> Tuple[str, str, str, str]:
    """
    Register TTF fonts if provided.
    Returns (latin_regular, latin_bold, arabic_regular, arabic_bold).
    """
    latin_regular = "Times-Roman"
    latin_bold = "Times-Bold"
    arabic_regular = latin_regular
    arabic_bold = latin_bold

    if assets.font_latin and assets.font_latin.exists():
        latin_regular = "LaylaLatinRegular"
        pdfmetrics.registerFont(TTFont(latin_regular, str(assets.font_latin)))

    if assets.font_latin_bold and assets.font_latin_bold.exists():
        latin_bold = "LaylaLatinBold"
        pdfmetrics.registerFont(TTFont(latin_bold, str(assets.font_latin_bold)))
    elif assets.font_latin and assets.font_latin.exists():
        latin_bold = latin_regular

    if assets.font_arabic and assets.font_arabic.exists():
        arabic_regular = "LaylaArabicRegular"
        pdfmetrics.registerFont(TTFont(arabic_regular, str(assets.font_arabic)))

    if assets.font_arabic_bold and assets.font_arabic_bold.exists():
        arabic_bold = "LaylaArabicBold"
        pdfmetrics.registerFont(TTFont(arabic_bold, str(assets.font_arabic_bold)))
    elif assets.font_arabic and assets.font_arabic.exists():
        arabic_bold = arabic_regular

    return latin_regular, latin_bold, arabic_regular, arabic_bold


def _load_layout_json(path: Optional[Path]) -> Optional[dict]:
    if path is None:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _draw_docx_shape_overlay(c: canvas.Canvas, layout_data: dict, page_w: float, page_h: float, limit: int) -> None:
    parts = layout_data.get("parts", [])
    if not isinstance(parts, list):
        return
    doc_part = next((p for p in parts if p.get("part") == "word/document.xml"), None)
    if not doc_part:
        return
    drawings = doc_part.get("drawings", [])
    if not isinstance(drawings, list):
        return

    # Fall back to Word defaults (1 inch margins) if missing.
    margin_left_mm = 25.4
    margin_top_mm = 25.4
    sections = doc_part.get("sections", [])
    if isinstance(sections, list) and sections:
        margins = sections[0].get("margins_mm", {})
        if isinstance(margins, dict):
            margin_left_mm = float(margins.get("left") or margin_left_mm)
            margin_top_mm = float(margins.get("top") or margin_top_mm)

    c.saveState()
    c.setStrokeColor(colors.Color(0.20, 0.55, 0.95))
    c.setLineWidth(0.5)
    c.setDash(2, 2)
    c.setFont("Times-Roman", 6.5)

    drawn = 0
    for shape in drawings:
        if drawn >= limit:
            break
        if not isinstance(shape, dict):
            continue
        pos = shape.get("position", {})
        extent = shape.get("extent_mm", {})
        if not isinstance(pos, dict) or not isinstance(extent, dict):
            continue

        rel_h = pos.get("horizontal_relative_from")
        rel_v = pos.get("vertical_relative_from")
        x_off_mm = pos.get("horizontal_offset_mm")
        y_off_mm = pos.get("vertical_offset_mm")
        w_mm = extent.get("w")
        h_mm = extent.get("h")

        if None in (x_off_mm, y_off_mm, w_mm, h_mm):
            continue
        if rel_h not in {"margin", "page"}:
            continue
        if rel_v not in {"margin", "page", "paragraph"}:
            continue

        x_mm = float(x_off_mm) + (0.0 if rel_h == "page" else margin_left_mm)
        y_top_mm = float(y_off_mm) + (0.0 if rel_v == "page" else margin_top_mm)
        w_pt = float(w_mm) * mm
        h_pt = float(h_mm) * mm
        x_pt = x_mm * mm
        y_pt = page_h - ((y_top_mm * mm) + h_pt)

        c.rect(x_pt, y_pt, w_pt, h_pt, stroke=1, fill=0)
        name = ((shape.get("doc_pr") or {}).get("name") if isinstance(shape.get("doc_pr"), dict) else "") or ""
        if name:
            c.drawString(x_pt + 1.5, y_pt + h_pt + 1.5, str(name)[:40])
        drawn += 1

    c.restoreState()


def _draw_debug_overlay(
    c: canvas.Canvas,
    page_w: float,
    page_h: float,
    grid_x: float,
    grid_y: float,
    grid_w: float,
    grid_h: float,
    card_w: float,
    card_h: float,
    opts: Optional[DebugOverlayOptions],
    layout_data: Optional[dict],
) -> None:
    if not opts or not opts.enabled:
        return

    if opts.reference_image and opts.reference_image.exists():
        try:
            c.drawImage(
                ImageReader(str(opts.reference_image)),
                0,
                0,
                width=page_w,
                height=page_h,
                mask="auto",
                preserveAspectRatio=False,
            )
        except Exception:
            pass

    c.saveState()
    if opts.show_grid:
        c.setStrokeColor(colors.Color(0.93, 0.20, 0.20))
        c.setLineWidth(0.7)
        c.setDash(4, 2)
        c.rect(grid_x, grid_y, grid_w, grid_h, stroke=1, fill=0)
        c.line(grid_x + card_w, grid_y, grid_x + card_w, grid_y + grid_h)
        c.line(grid_x, grid_y + card_h, grid_x + grid_w, grid_y + card_h)
        c.line(grid_x, grid_y + 2 * card_h, grid_x + grid_w, grid_y + 2 * card_h)
        c.setFont("Times-Roman", 7)
        c.drawString(grid_x + 2, page_h - grid_y + 4, "debug:grid")
    c.restoreState()

    if opts.show_docx_shapes and layout_data:
        _draw_docx_shape_overlay(c, layout_data, page_w, page_h, opts.limit_shapes)


def _icon_triplet(d: Dish, assets: AssetPaths) -> List[Path]:
    # Fixed 3-icons layout to match your paper:
    # [gluten/gluten-free] [veg/meat] [dairy/dairy-free]
    gluten_icon = assets.icon_gluten_free if d.gluten == "gluten_free" else assets.icon_gluten
    protein_icon = assets.icon_veg if d.protein_type == "veg" else assets.icon_meat
    dairy_icon = assets.icon_dairy_free if d.dairy == "dairy_free" else assets.icon_dairy
    return [gluten_icon, protein_icon, dairy_icon]


def _draw_centered_text(
    c: canvas.Canvas,
    text: str,
    font_name: str,
    font_size: float,
    center_x: float,
    y: float,
) -> None:
    if not text:
        return
    c.setFont(font_name, font_size)
    text_w = pdfmetrics.stringWidth(text, font_name, font_size)
    x = center_x - (text_w / 2.0)
    c.drawString(x, y, text)


def generate_cards_pdf(
    dishes: Iterable[Dish],
    out_pdf_path: str | Path,
    assets: AssetPaths,
    title: str = "Layla Cards",
    draw_logo: bool = False,
    debug_overlay: Optional[DebugOverlayOptions] = None,
    layout_config: Optional[LayoutConfig] = None,
) -> Path:
    """
    Generate an A4 PDF, 2 columns x 3 rows per page (6 cards).
    """
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    page_w, page_h = A4
    c = canvas.Canvas(str(out_pdf_path), pagesize=A4)
    c.setTitle(title)

    latin_font_regular, latin_font_bold, arabic_font_regular, arabic_font_bold = _register_fonts(assets)

    layout = layout_config or LayoutConfig()

    cols, rows = layout.cols, layout.rows
    grid_x = layout.grid_x_mm * mm
    grid_y = layout.grid_y_mm * mm
    card_w = layout.card_w_mm * mm
    card_h = layout.card_h_mm * mm
    grid_w = card_w * cols
    grid_h = card_h * rows

    # Styling
    border_color = colors.Color(0.85, 0.85, 0.85)
    border_width = 0.6
    draw_grid_lines = layout.draw_grid_lines and (assets.template_page is None)

    # Content offsets within card
    pad_x = 0.0 * mm
    pad_top = 8 * mm
    logo_h = 18 * mm
    logo_w = 30 * mm

    icon_x_offset = layout.icon_x_offset_mm * mm
    icon_y_offset = layout.icon_y_offset_mm * mm
    icon_size = layout.icon_size_mm * mm
    icon_gap = layout.icon_gap_mm * mm

    # Macro column
    macro_x_offset = layout.macro_x_offset_mm * mm
    macro_y_top_offset = layout.macro_y_top_mm * mm
    macro_line_gap = layout.macro_line_gap_mm * mm

    # Dish text
    dish_x_offset = layout.dish_x_offset_mm * mm
    dish_en_y = layout.dish_en_y_mm * mm
    dish_ar_y = dish_en_y - (layout.dish_ar_gap_mm * mm)

    dish_en_size = layout.dish_en_size
    dish_ar_size = layout.dish_ar_size
    macro_size = layout.macro_size

    dish_list = list(dishes)
    idx = 0
    layout_data = _load_layout_json(debug_overlay.docx_layout_json) if debug_overlay else None

    while idx < len(dish_list):
        if assets.template_page and assets.template_page.exists():
            try:
                c.drawImage(
                    ImageReader(str(assets.template_page)),
                    0,
                    0,
                    width=page_w,
                    height=page_h,
                    mask="auto",
                    preserveAspectRatio=False,
                )
            except Exception:
                pass

        _draw_debug_overlay(
            c=c,
            page_w=page_w,
            page_h=page_h,
            grid_x=grid_x,
            grid_y=grid_y,
            grid_w=grid_w,
            grid_h=grid_h,
            card_w=card_w,
            card_h=card_h,
            opts=debug_overlay,
            layout_data=layout_data,
        )

        # Grid lines
        if draw_grid_lines:
            c.setStrokeColor(border_color)
            c.setLineWidth(border_width)
            # Outer border
            c.rect(grid_x, grid_y, grid_w, grid_h, stroke=1, fill=0)
            # Vertical divider
            c.line(grid_x + card_w, grid_y, grid_x + card_w, grid_y + grid_h)
            # Horizontal dividers (2 lines)
            c.line(grid_x, grid_y + card_h, grid_x + grid_w, grid_y + card_h)
            c.line(grid_x, grid_y + 2 * card_h, grid_x + grid_w, grid_y + 2 * card_h)

        # Draw up to 6 cards
        for r in range(rows):
            for col in range(cols):
                if idx >= len(dish_list):
                    break
                d = dish_list[idx]
                idx += 1

                # Card origin (bottom-left)
                x0 = grid_x + col * card_w
                y0 = grid_y + (rows - 1 - r) * card_h

                # Logo (top center)
                should_draw_logo = (draw_logo or layout.draw_logo) and (not assets.template_page)
                if should_draw_logo:
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
                center_x = x0 + dish_x_offset + (layout.dish_box_width_mm * mm / 2.0)
                _draw_centered_text(
                    c=c,
                    text=d.name_en,
                    font_name=latin_font_bold,
                    font_size=dish_en_size,
                    center_x=center_x,
                    y=y0 + dish_en_y,
                )

                # Dish name AR (under EN, left side)
                ar_text = _try_arabic_shape(d.name_ar) if d.name_ar else ""
                _draw_centered_text(
                    c=c,
                    text=ar_text,
                    font_name=arabic_font_bold,
                    font_size=dish_ar_size,
                    center_x=center_x,
                    y=y0 + dish_ar_y,
                )

                # Icons (left bottom-ish)
                icons = _icon_triplet(d, assets)
                icon_group_w = (len(icons) * icon_size) + (max(0, len(icons) - 1) * icon_gap)
                ix = center_x - (icon_group_w / 2.0)
                iy = y0 + icon_y_offset
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
                c.setFont(latin_font_regular, macro_size)

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
