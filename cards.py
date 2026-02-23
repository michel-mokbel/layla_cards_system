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
import math
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

    # `utf-8-sig` handles BOM-prefixed CSV headers (e.g. "\ufeffname_en").
    with csv_path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name_en = (row.get("name_en") or row.get("\ufeffname_en") or "").strip()
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
    show_macros: bool = True
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
                if layout.show_macros:
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


def _draw_header_badge(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    text: str,
    latin_font_regular: str,
) -> None:
    badge_h = 6.2 * mm
    badge_pad_x = 2.1 * mm
    text_size = 7.3
    min_text_size = 6.0

    c.saveState()
    c.setFillColor(colors.Color(1.0, 1.0, 1.0))
    c.roundRect(x, y, w, badge_h, 2.0 * mm, stroke=0, fill=1)

    while text_size > min_text_size and pdfmetrics.stringWidth(text, latin_font_regular, text_size) > (w - 2.0 * badge_pad_x):
        text_size -= 0.2

    c.setFillColor(colors.Color(0.06, 0.20, 0.50))
    c.setFont(latin_font_regular, text_size)
    c.drawString(x + badge_pad_x, y + 1.8 * mm, text)
    c.restoreState()


def _draw_macro_chip(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    value: str,
    latin_font_regular: str,
    latin_font_bold: str,
    accent: colors.Color,
) -> None:
    c.saveState()
    c.setFillColor(colors.Color(1.0, 1.0, 1.0))
    c.roundRect(x, y, w, h, 1.8 * mm, stroke=0, fill=1)
    c.setFillColor(accent)
    c.rect(x, y, 1.1 * mm, h, stroke=0, fill=1)

    c.setFillColor(colors.Color(0.12, 0.23, 0.55))
    c.setFont(latin_font_regular, 6.9)
    c.drawString(x + 2.0 * mm, y + h - (3.0 * mm), label)

    c.setFillColor(colors.Color(0.06, 0.20, 0.50))
    c.setFont(latin_font_bold, 8.6)
    c.drawString(x + 2.0 * mm, y + 1.7 * mm, value)
    c.restoreState()


def _nutriment_entries(dish: Dish) -> List[str]:
    entries: List[str] = []
    if dish.gluten == "gluten_free":
        entries.append("Gluten Free")
    else:
        entries.append("Contains Gluten")

    if dish.protein_type == "veg":
        entries.append("Vegetarian")
    else:
        entries.append("Contains Meat")

    if dish.dairy == "dairy_free":
        entries.append("Dairy Free")
    else:
        entries.append("Contains Dairy")
    return entries


def _wrap_text_two_lines(text: str, font_name: str, font_size: float, max_width: float) -> List[str]:
    """
    Wrap text into up to two lines that fit `max_width`.
    """
    raw = (text or "").strip()
    if not raw:
        return [""]

    words = raw.split()
    if not words:
        return [raw]

    lines: List[str] = []
    current: List[str] = []
    for w in words:
        candidate = " ".join(current + [w]) if current else w
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
            current.append(w)
            continue

        if current:
            lines.append(" ".join(current))
            current = [w]
        else:
            lines.append(w)
            current = []

        if len(lines) == 2:
            break

    if len(lines) < 2 and current:
        lines.append(" ".join(current))

    if len(lines) > 2:
        lines = lines[:2]

    if len(lines) == 2:
        # hard trim second line if still too wide
        second = lines[1]
        while second and pdfmetrics.stringWidth(second, font_name, font_size) > max_width:
            second = second[:-1].rstrip()
        lines[1] = second

    return lines


def generate_buffet_menu_pdf(
    dishes: Iterable[Dish],
    out_pdf_path: str | Path,
    assets: AssetPaths,
    title: str = "Buffet Menu",
    subtitle: str = "Nutriments and Macronutrients",
) -> Path:
    """
    Generate a single A4 buffet-menu sheet with logo, dish sections, nutriments and macros.
    """
    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    dish_list = list(dishes)
    page_w, page_h = A4
    c = canvas.Canvas(str(out_pdf_path), pagesize=A4)
    c.setTitle(title)

    latin_font_regular, latin_font_bold, arabic_font_regular, arabic_font_bold = _register_fonts(assets)

    margin_x = 13.5 * mm
    margin_top = 11.0 * mm
    margin_bottom = 11.5 * mm
    header_h = 37.0 * mm
    content_gap = 4.2 * mm

    frame_x = margin_x
    frame_y = margin_bottom
    frame_w = page_w - (margin_x * 2.0)
    frame_h = page_h - margin_top - margin_bottom

    # Palette: white, blue, gold
    blue_deep = colors.Color(0.06, 0.20, 0.50)
    blue_mid = colors.Color(0.14, 0.34, 0.68)
    gold = colors.Color(0.84, 0.67, 0.27)
    gold_soft = colors.Color(0.95, 0.88, 0.70)

    header_y = frame_y + frame_h - header_h - (5.0 * mm)
    header_x = frame_x + (5.0 * mm)
    header_w = frame_w - (10.0 * mm)
    logo_w = 28.0 * mm
    logo_h = 28.0 * mm
    body_x = header_x
    body_top = header_y - content_gap
    body_w = header_w
    body_h = body_top - (frame_y + 5.5 * mm)

    per_page = 8
    if not dish_list:
        page_chunks: List[List[Dish]] = [[]]
    else:
        page_chunks = [dish_list[i:i + per_page] for i in range(0, len(dish_list), per_page)]

    total_pages = len(page_chunks)

    for page_idx, page_dishes in enumerate(page_chunks):
        # Branded background and border frame
        c.setFillColor(colors.Color(1.0, 1.0, 1.0))
        c.rect(0, 0, page_w, page_h, stroke=0, fill=1)

        c.setFillColor(colors.Color(1.0, 1.0, 1.0))
        c.roundRect(frame_x, frame_y, frame_w, frame_h, 5.0 * mm, stroke=0, fill=1)
        c.setStrokeColor(gold)
        c.setLineWidth(1.15)
        c.roundRect(frame_x, frame_y, frame_w, frame_h, 5.0 * mm, stroke=1, fill=0)

        # Header area (white background, gold accent line)
        c.setFillColor(colors.Color(1.0, 1.0, 1.0))
        c.roundRect(header_x, header_y, header_w, header_h, 3.5 * mm, stroke=0, fill=1)
        c.setFillColor(gold)
        c.rect(header_x, header_y, header_w, 1.4 * mm, stroke=0, fill=1)

        logo_x = header_x + 4.2 * mm
        logo_y = header_y + (header_h - logo_h) / 2.0
        if assets.logo and assets.logo.exists():
            try:
                c.drawImage(
                    ImageReader(str(assets.logo)),
                    logo_x,
                    logo_y,
                    width=logo_w,
                    height=logo_h,
                    mask="auto",
                    preserveAspectRatio=True,
                )
            except Exception:
                pass

        text_x = logo_x + logo_w + (5.0 * mm)
        c.setFillColor(blue_deep)
        c.setFont(latin_font_bold, 24.0)
        c.drawString(text_x, header_y + header_h - (11.2 * mm), title)
        c.setFont(latin_font_regular, 10.8)
        c.setFillColor(blue_mid)
        c.drawString(text_x, header_y + 8.8 * mm, subtitle)

        if total_pages > 1:
            c.setFont(latin_font_regular, 8.8)
            c.setFillColor(blue_mid)
            c.drawRightString(
                header_x + header_w - (4.0 * mm),
                header_y + header_h - (5.0 * mm),
                f"Page {page_idx + 1} of {total_pages}",
            )

        item_count = max(1, len(page_dishes))
        cols = 1 if item_count <= 4 else 2
        rows = max(1, math.ceil(item_count / cols))
        col_gap = 4.0 * mm
        row_gap = 4.0 * mm
        card_w = (body_w - ((cols - 1) * col_gap)) / cols
        card_h = (body_h - ((rows - 1) * row_gap)) / rows

        for i, dish in enumerate(page_dishes):
            row = i // cols
            col = i % cols
            card_x = body_x + col * (card_w + col_gap)
            card_y = body_top - ((row + 1) * card_h) - (row * row_gap)

            fill = colors.Color(1.0, 1.0, 1.0) if (i % 2 == 0) else colors.Color(0.985, 0.992, 1.0)
            c.setFillColor(fill)
            c.setStrokeColor(gold)
            c.setLineWidth(0.7)
            c.roundRect(card_x, card_y, card_w, card_h, 2.8 * mm, stroke=1, fill=1)

            # Dedicated non-overlapping lanes for names: EN on left, AR on right.
            left_pad = 3.4 * mm
            right_pad = 3.4 * mm
            top_text_y = card_y + card_h - 5.0 * mm
            en_lane_w = (card_w * 0.60) - left_pad
            ar_lane_w = (card_w * 0.34) - right_pad

            en_font = 12.5
            en_lines = _wrap_text_two_lines(dish.name_en, latin_font_bold, en_font, en_lane_w)
            en_line_gap = 4.8 * mm
            c.setFillColor(blue_deep)
            c.setFont(latin_font_bold, en_font)
            c.drawString(card_x + left_pad, top_text_y - 1.8 * mm, en_lines[0] if en_lines else "")
            if len(en_lines) > 1 and en_lines[1]:
                c.drawString(card_x + left_pad, top_text_y - 1.8 * mm - en_line_gap, en_lines[1])

            ar_text = _try_arabic_shape(dish.name_ar) if dish.name_ar else ""
            ar_font = 10.3
            ar_lines = _wrap_text_two_lines(ar_text, arabic_font_bold, ar_font, ar_lane_w)
            ar_line_gap = 4.4 * mm
            c.setFillColor(blue_mid)
            c.setFont(arabic_font_bold, ar_font)
            c.drawRightString(card_x + card_w - right_pad, top_text_y - 1.8 * mm, ar_lines[0] if ar_lines else "")
            if len(ar_lines) > 1 and ar_lines[1]:
                c.drawRightString(card_x + card_w - right_pad, top_text_y - 1.8 * mm - ar_line_gap, ar_lines[1])

            c.setStrokeColor(gold_soft)
            c.setLineWidth(0.5)
            divider_y = card_y + card_h - 14.2 * mm
            c.line(card_x + 3.0 * mm, divider_y, card_x + card_w - 3.0 * mm, divider_y)

            nutriments = _nutriment_entries(dish)
            badge_y = divider_y - 8.2 * mm
            badge_x = card_x + 3.0 * mm
            badge_gap = 1.6 * mm
            badge_w = (card_w - 6.0 * mm - (2.0 * badge_gap)) / 3.0
            for idx_badge, label in enumerate(nutriments[:3]):
                _draw_header_badge(
                    c=c,
                    x=badge_x + idx_badge * (badge_w + badge_gap),
                    y=badge_y,
                    w=badge_w,
                    text=label,
                    latin_font_regular=latin_font_regular,
                )

            section_sep_y = badge_y - 2.6 * mm
            c.setStrokeColor(gold_soft)
            c.setLineWidth(0.45)
            c.line(card_x + 3.0 * mm, section_sep_y, card_x + card_w - 3.0 * mm, section_sep_y)

            macro_title_y = badge_y - 12.0 * mm

            chip_h = 8.5 * mm
            chip_gap_x = 2.0 * mm
            chip_gap_y = 1.9 * mm
            chip_w = (card_w - 6.0 * mm - chip_gap_x) / 2.0
            chip_x = card_x + 3.0 * mm
            chip_y = macro_title_y - 5.2 * mm

            macro_items = [
                ("Calories", f"{_fmt(dish.calories_kcal)} kcal", gold),
                ("Carbs", f"{_fmt(dish.carbs_g)} g", blue_mid),
                ("Protein", f"{_fmt(dish.protein_g)} g", gold),
                ("Fat", f"{_fmt(dish.fat_g)} g", blue_mid),
            ]

            for j, (label, value, accent) in enumerate(macro_items):
                cx = chip_x + (j % 2) * (chip_w + chip_gap_x)
                cy = chip_y - (j // 2) * (chip_h + chip_gap_y)
                _draw_macro_chip(
                    c=c,
                    x=cx,
                    y=cy,
                    w=chip_w,
                    h=chip_h,
                    label=label,
                    value=value,
                    latin_font_regular=latin_font_regular,
                    latin_font_bold=latin_font_bold,
                    accent=accent,
                )

        if page_idx < total_pages - 1:
            c.showPage()

    c.save()
    return out_pdf_path
