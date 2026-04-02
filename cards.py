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


@dataclass(frozen=True)
class GreetingLabel:
    name: str


GREETING_LABEL_STYLE_CLEAN = "clean_brand_pastel"
GREETING_LABEL_STYLE_PLAYFUL = "playful_graphic_heavy"
GREETING_LABEL_STYLES = (
    GREETING_LABEL_STYLE_CLEAN,
    GREETING_LABEL_STYLE_PLAYFUL,
)


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


def parse_greeting_label_names(raw_text: str) -> List[GreetingLabel]:
    labels: List[GreetingLabel] = []
    for raw_line in (raw_text or "").splitlines():
        normalized = " ".join(raw_line.split()).strip()
        if normalized:
            labels.append(GreetingLabel(name=normalized))
    return labels


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
    layout_variant: str = "standard"
    cols: int = 2
    rows: int = 3
    grid_x_mm: float = 7.2
    grid_y_mm: float = 26.0
    grid_gap_x_mm: float = 0.0
    grid_gap_y_mm: float = 0.0
    auto_center_grid: bool = False
    card_w_mm: float = 98.8
    card_h_mm: float = 79.2
    draw_grid_lines: bool = True
    draw_logo: bool = False
    show_macros: bool = True
    logo_x_offset_mm: float = 34.4
    logo_y_offset_mm: float = 53.2
    logo_w_mm: float = 30.0
    logo_h_mm: float = 18.0
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


def _fit_text_block(
    text: str,
    *,
    wrap_fn,
    font_name: str,
    max_font_size: float,
    min_font_size: float,
    max_width: float,
    max_height: float,
) -> Tuple[List[str], float]:
    font_size = max_font_size
    best_lines = [""]

    while font_size >= min_font_size:
        lines = wrap_fn(text, font_name, font_size, max_width)
        if not lines:
            lines = [""]

        line_count = len([line for line in lines if line])
        if line_count == 0:
            return [""], font_size

        line_height = font_size * 1.15
        total_height = line_count * line_height
        widths_fit = all(
            pdfmetrics.stringWidth(line, font_name, font_size) <= max_width
            for line in lines
            if line
        )
        if widths_fit and total_height <= max_height:
            return lines, font_size

        best_lines = lines
        font_size -= 0.4

    return best_lines, min_font_size


def _draw_text_lines_centered(
    c: canvas.Canvas,
    lines: List[str],
    font_name: str,
    font_size: float,
    center_x: float,
    center_y: float,
) -> None:
    visible_lines = [line for line in lines if line]
    if not visible_lines:
        return

    line_height = font_size * 1.15
    total_height = len(visible_lines) * line_height
    first_baseline = center_y + (total_height / 2.0) - line_height

    c.setFont(font_name, font_size)
    for idx, line in enumerate(visible_lines):
        y = first_baseline - (idx * line_height)
        text_w = pdfmetrics.stringWidth(line, font_name, font_size)
        c.drawString(center_x - (text_w / 2.0), y, line)


def _draw_standard_card(
    c: canvas.Canvas,
    dish: Dish,
    *,
    x0: float,
    y0: float,
    card_w: float,
    card_h: float,
    assets: AssetPaths,
    layout: LayoutConfig,
    should_draw_logo: bool,
    latin_font_regular: str,
    latin_font_bold: str,
    arabic_font_bold: str,
) -> None:
    logo_x_offset = layout.logo_x_offset_mm * mm
    logo_y_offset = layout.logo_y_offset_mm * mm
    logo_w = layout.logo_w_mm * mm
    logo_h = layout.logo_h_mm * mm

    icon_y_offset = layout.icon_y_offset_mm * mm
    icon_size = layout.icon_size_mm * mm
    icon_gap = layout.icon_gap_mm * mm

    macro_x_offset = layout.macro_x_offset_mm * mm
    macro_y_top_offset = layout.macro_y_top_mm * mm
    macro_line_gap = layout.macro_line_gap_mm * mm

    dish_x_offset = layout.dish_x_offset_mm * mm
    dish_en_y = layout.dish_en_y_mm * mm
    dish_ar_y = dish_en_y - (layout.dish_ar_gap_mm * mm)
    dish_box_width = layout.dish_box_width_mm * mm

    c.setFillColor(colors.black)

    if should_draw_logo:
        try:
            c.drawImage(
                ImageReader(str(assets.logo)),
                x0 + logo_x_offset,
                y0 + logo_y_offset,
                width=logo_w,
                height=logo_h,
                mask="auto",
                preserveAspectRatio=True,
            )
        except Exception:
            pass

    center_x = x0 + dish_x_offset + (dish_box_width / 2.0)
    _draw_centered_text(
        c=c,
        text=dish.name_en,
        font_name=latin_font_bold,
        font_size=layout.dish_en_size,
        center_x=center_x,
        y=y0 + dish_en_y,
    )

    ar_text = _try_arabic_shape(dish.name_ar) if dish.name_ar else ""
    _draw_centered_text(
        c=c,
        text=ar_text,
        font_name=arabic_font_bold,
        font_size=layout.dish_ar_size,
        center_x=center_x,
        y=y0 + dish_ar_y,
    )

    icons = _icon_triplet(dish, assets)
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
            c.rect(ix, iy, icon_size, icon_size, stroke=1, fill=0)
        ix += icon_size + icon_gap

    if layout.show_macros:
        c.setFont(latin_font_regular, layout.macro_size)

        def macro_line(label: str, value: str, n: int):
            c.drawString(
                x0 + macro_x_offset,
                y0 + macro_y_top_offset - n * macro_line_gap,
                f"{label}: {value}",
            )

        macro_line("Calories", f"{_fmt(dish.calories_kcal)} kcal", 0)
        macro_line("Carbohydrates", f"{_fmt(dish.carbs_g)} g", 1)
        macro_line("Protein", f"{_fmt(dish.protein_g)} g", 2)
        macro_line("Fat", f"{_fmt(dish.fat_g)} g", 3)


def _draw_compact_55x90_card(
    c: canvas.Canvas,
    dish: Dish,
    *,
    x0: float,
    y0: float,
    card_w: float,
    card_h: float,
    assets: AssetPaths,
    layout: LayoutConfig,
    should_draw_logo: bool,
    latin_font_bold: str,
    arabic_font_bold: str,
) -> None:
    inner_pad_x = 3.0 * mm
    inner_pad_y = 3.5 * mm
    lane_gap = 2.0 * mm
    logo_lane_w = max(layout.dish_x_offset_mm * mm, (layout.logo_w_mm * mm) + (1.0 * mm))
    icon_size = layout.icon_size_mm * mm
    icon_gap = layout.icon_gap_mm * mm
    icon_row_gap = 4.6 * mm

    logo_w = min(layout.logo_w_mm * mm, max(0.0, logo_lane_w - (1.0 * mm)))
    logo_h = layout.logo_h_mm * mm
    logo_x = x0 + inner_pad_x + ((logo_lane_w - logo_w) / 2.0)
    logo_y = y0 + ((card_h - logo_h) / 2.0)

    if should_draw_logo:
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

    content_x = x0 + inner_pad_x + logo_lane_w + lane_gap
    content_w = card_w - (2.0 * inner_pad_x) - logo_lane_w - lane_gap
    content_center_x = content_x + (content_w / 2.0)
    content_h = card_h - (2.0 * inner_pad_y)
    content_center_y = y0 + inner_pad_y + (content_h / 2.0)

    en_lines, en_size = _fit_text_block(
        dish.name_en,
        wrap_fn=_wrap_text_two_lines,
        font_name=latin_font_bold,
        max_font_size=layout.dish_en_size,
        min_font_size=9.0,
        max_width=content_w,
        max_height=content_h * 0.34,
    )
    ar_lines, ar_size = _fit_text_block(
        dish.name_ar or "",
        wrap_fn=_wrap_arabic_two_lines,
        font_name=arabic_font_bold,
        max_font_size=layout.dish_ar_size,
        min_font_size=8.0,
        max_width=content_w,
        max_height=content_h * 0.24,
    )

    en_visible = [line for line in en_lines if line]
    ar_visible = [line for line in ar_lines if line]
    en_height = len(en_visible) * en_size * 1.15
    ar_height = len(ar_visible) * ar_size * 1.15
    text_gap = 1.2 * mm if en_visible and ar_visible else 0.0
    total_text_height = en_height + ar_height + text_gap
    stack_height = total_text_height + icon_row_gap + icon_size
    stack_top = content_center_y + (stack_height / 2.0)

    c.setFillColor(colors.black)
    if en_visible:
        en_center_y = stack_top - (en_height / 2.0)
        _draw_text_lines_centered(
            c,
            en_visible,
            latin_font_bold,
            en_size,
            content_center_x,
            en_center_y,
        )

    if ar_visible:
        ar_center_y = stack_top - en_height - text_gap - (ar_height / 2.0)
        _draw_text_lines_centered(
            c,
            ar_visible,
            arabic_font_bold,
            ar_size,
            content_center_x,
            ar_center_y,
        )

    icons = _icon_triplet(dish, assets)
    icon_group_w = (len(icons) * icon_size) + (max(0, len(icons) - 1) * icon_gap)
    ix = content_center_x - (icon_group_w / 2.0)
    text_bottom = stack_top - total_text_height
    iy = text_bottom - icon_row_gap - icon_size
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
            c.rect(ix, iy, icon_size, icon_size, stroke=1, fill=0)
        ix += icon_size + icon_gap


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
    card_w = layout.card_w_mm * mm
    card_h = layout.card_h_mm * mm
    grid_gap_x = layout.grid_gap_x_mm * mm
    grid_gap_y = layout.grid_gap_y_mm * mm
    grid_w = (card_w * cols) + (grid_gap_x * max(0, cols - 1))
    grid_h = (card_h * rows) + (grid_gap_y * max(0, rows - 1))
    if layout.auto_center_grid:
        grid_x = (page_w - grid_w) / 2.0
        grid_y = (page_h - grid_h) / 2.0
    else:
        grid_x = layout.grid_x_mm * mm
        grid_y = layout.grid_y_mm * mm

    # Styling
    border_color = colors.Color(0.85, 0.85, 0.85)
    border_width = 0.6
    draw_grid_lines = layout.draw_grid_lines and (assets.template_page is None)

    dish_list = list(dishes)
    idx = 0
    layout_data = _load_layout_json(debug_overlay.docx_layout_json) if debug_overlay else None
    is_compact_55x90 = layout.layout_variant == "compact_55x90"

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
            if grid_gap_x == 0 and grid_gap_y == 0:
                c.rect(grid_x, grid_y, grid_w, grid_h, stroke=1, fill=0)
                for col in range(1, cols):
                    x = grid_x + col * card_w
                    c.line(x, grid_y, x, grid_y + grid_h)
                for row in range(1, rows):
                    y = grid_y + row * card_h
                    c.line(grid_x, y, grid_x + grid_w, y)
            else:
                for r in range(rows):
                    for col in range(cols):
                        x = grid_x + col * (card_w + grid_gap_x)
                        y = grid_y + (rows - 1 - r) * (card_h + grid_gap_y)
                        c.rect(x, y, card_w, card_h, stroke=1, fill=0)

        # Draw up to 6 cards
        for r in range(rows):
            for col in range(cols):
                if idx >= len(dish_list):
                    break
                d = dish_list[idx]
                idx += 1

                # Card origin (bottom-left)
                x0 = grid_x + col * (card_w + grid_gap_x)
                y0 = grid_y + (rows - 1 - r) * (card_h + grid_gap_y)

                should_draw_logo = (draw_logo or layout.draw_logo) and (not assets.template_page)
                if is_compact_55x90:
                    _draw_compact_55x90_card(
                        c,
                        d,
                        x0=x0,
                        y0=y0,
                        card_w=card_w,
                        card_h=card_h,
                        assets=assets,
                        layout=layout,
                        should_draw_logo=should_draw_logo,
                        latin_font_bold=latin_font_bold,
                        arabic_font_bold=arabic_font_bold,
                    )
                else:
                    _draw_standard_card(
                        c,
                        d,
                        x0=x0,
                        y0=y0,
                        card_w=card_w,
                        card_h=card_h,
                        assets=assets,
                        layout=layout,
                        should_draw_logo=should_draw_logo,
                        latin_font_regular=latin_font_regular,
                        latin_font_bold=latin_font_bold,
                        arabic_font_bold=arabic_font_bold,
                    )

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


def _wrap_arabic_two_lines(text: str, font_name: str, font_size: float, max_width: float) -> List[str]:
    """
    Wrap Arabic text into up to two lines using unshaped text for token order,
    then shape each output line for rendering.
    """
    raw = (text or "").strip()
    if not raw:
        return [""]

    words = raw.split()
    if not words:
        return [_try_arabic_shape(raw)]

    lines_raw: List[str] = []
    current: List[str] = []
    for w in words:
        candidate_raw = " ".join(current + [w]) if current else w
        candidate_shaped = _try_arabic_shape(candidate_raw)
        if pdfmetrics.stringWidth(candidate_shaped, font_name, font_size) <= max_width:
            current.append(w)
            continue

        if current:
            lines_raw.append(" ".join(current))
            current = [w]
        else:
            lines_raw.append(w)
            current = []

        if len(lines_raw) == 2:
            break

    if len(lines_raw) < 2 and current:
        lines_raw.append(" ".join(current))

    if len(lines_raw) > 2:
        lines_raw = lines_raw[:2]

    if len(lines_raw) == 2:
        second_raw = lines_raw[1]
        second_shaped = _try_arabic_shape(second_raw)
        while second_raw and pdfmetrics.stringWidth(second_shaped, font_name, font_size) > max_width:
            second_raw = second_raw[:-1].rstrip()
            second_shaped = _try_arabic_shape(second_raw)
        lines_raw[1] = second_raw

    return [_try_arabic_shape(line) for line in lines_raw]


def _wrap_text_lines(text: str, font_name: str, font_size: float, max_width: float, max_lines: int) -> List[str]:
    raw = (text or "").strip()
    if not raw:
        return [""]

    words = raw.split()
    if not words:
        return [raw]

    lines: List[str] = []
    current: List[str] = []
    idx = 0

    while idx < len(words):
        word = words[idx]
        candidate = " ".join(current + [word]) if current else word
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
            current.append(word)
            idx += 1
            continue

        if current:
            lines.append(" ".join(current))
            current = []
        else:
            lines.append(word)
            idx += 1

        if len(lines) == max_lines:
            break

    if len(lines) < max_lines and current:
        lines.append(" ".join(current))

    if idx < len(words) and lines:
        overflow = " ".join(words[idx:])
        last_line = f"{lines[-1]} {overflow}".strip()
        while last_line and pdfmetrics.stringWidth(last_line, font_name, font_size) > max_width:
            last_line = last_line[:-1].rstrip()
        lines[-1] = last_line

    return lines[:max_lines]


_GREETING_LABEL_COLS = 2
_GREETING_LABEL_ROWS = 5
_GREETING_LABEL_COL_TWIP = 5934
_GREETING_LABEL_ROW_TWIPS = (3456, 3081, 3339, 3518, 3230)
_GREETING_MM_PER_TWIP = 25.4 / 1440.0
_GREETING_LABEL_CELL_W_MM = _GREETING_LABEL_COL_TWIP * _GREETING_MM_PER_TWIP
_GREETING_LABEL_ROW_HEIGHTS_MM = tuple(v * _GREETING_MM_PER_TWIP for v in _GREETING_LABEL_ROW_TWIPS)
_GREETING_LABEL_TEXT = "Happy Easter"


def _draw_egg(c: canvas.Canvas, *, x: float, y: float, w: float, h: float, fill: colors.Color, stripe: Optional[colors.Color] = None) -> None:
    c.saveState()
    c.setFillColor(fill)
    c.setStrokeColor(fill)
    c.ellipse(x, y, x + w, y + h, stroke=1, fill=1)
    if stripe is not None:
        c.setFillColor(stripe)
        c.setStrokeColor(stripe)
        c.setLineWidth(1.1)
        c.line(x + (0.18 * w), y + (0.58 * h), x + (0.82 * w), y + (0.58 * h))
        c.line(x + (0.25 * w), y + (0.43 * h), x + (0.75 * w), y + (0.43 * h))
    c.restoreState()


def _draw_flower(c: canvas.Canvas, *, center_x: float, center_y: float, petal_r: float, petal_fill: colors.Color, core_fill: colors.Color) -> None:
    c.saveState()
    c.setFillColor(petal_fill)
    c.setStrokeColor(petal_fill)
    offsets = (
        (-petal_r, 0.0),
        (petal_r, 0.0),
        (0.0, -petal_r),
        (0.0, petal_r),
    )
    for dx, dy in offsets:
        c.circle(center_x + dx, center_y + dy, petal_r * 0.78, stroke=0, fill=1)
    c.setFillColor(core_fill)
    c.setStrokeColor(core_fill)
    c.circle(center_x, center_y, petal_r * 0.72, stroke=0, fill=1)
    c.restoreState()


def _draw_greeting_logo(
    c: canvas.Canvas,
    *,
    assets: AssetPaths,
    x: float,
    y: float,
    w: float,
    h: float,
) -> None:
    logo_path = assets.logo
    transparent_logo = assets.logo.with_name("logo-no-bg.png")
    if transparent_logo.exists():
        logo_path = transparent_logo

    if not logo_path.exists():
        return
    try:
        c.drawImage(
            ImageReader(str(logo_path)),
            x,
            y,
            width=w,
            height=h,
            mask="auto",
            preserveAspectRatio=True,
        )
    except Exception:
        return


def _draw_greeting_label_card(
    c: canvas.Canvas,
    label: GreetingLabel,
    *,
    x0: float,
    y0: float,
    card_w: float,
    card_h: float,
    assets: AssetPaths,
    style: str,
    latin_font_regular: str,
    latin_font_bold: str,
) -> None:
    soft_green = colors.Color(0.92, 0.97, 0.92)
    mint = colors.Color(0.86, 0.95, 0.89)
    blush = colors.Color(0.98, 0.91, 0.92)
    butter = colors.Color(1.0, 0.95, 0.77)
    apricot = colors.Color(0.99, 0.82, 0.65)
    peach = colors.Color(0.98, 0.72, 0.58)
    lilac = colors.Color(0.88, 0.83, 0.98)
    sky = colors.Color(0.82, 0.92, 0.99)
    ink = colors.Color(0.23, 0.19, 0.18)
    warm_brown = colors.Color(0.47, 0.33, 0.24)
    white = colors.Color(1.0, 1.0, 1.0)
    content_pad_x = 7.0 * mm
    inner_x = x0
    inner_y = y0
    inner_w = card_w
    inner_h = card_h

    if style == GREETING_LABEL_STYLE_PLAYFUL:
        c.setFillColor(colors.Color(1.0, 0.98, 0.94))
        c.rect(inner_x, inner_y, inner_w, inner_h, stroke=0, fill=1)

        _draw_egg(c, x=inner_x + (6.0 * mm), y=inner_y + inner_h - (19.5 * mm), w=9.0 * mm, h=12.0 * mm, fill=butter, stripe=peach)
        _draw_egg(c, x=inner_x + inner_w - (15.0 * mm), y=inner_y + inner_h - (20.2 * mm), w=9.4 * mm, h=12.6 * mm, fill=lilac, stripe=white)
        _draw_egg(c, x=inner_x + inner_w - (17.0 * mm), y=inner_y + (4.0 * mm), w=10.8 * mm, h=14.2 * mm, fill=apricot, stripe=white)
        _draw_egg(c, x=inner_x + (5.0 * mm), y=inner_y + (3.2 * mm), w=10.8 * mm, h=14.2 * mm, fill=mint, stripe=white)
        _draw_flower(c, center_x=inner_x + (20.0 * mm), center_y=inner_y + (8.2 * mm), petal_r=1.7 * mm, petal_fill=blush, core_fill=butter)
        _draw_flower(c, center_x=inner_x + inner_w - (20.0 * mm), center_y=inner_y + (9.2 * mm), petal_r=1.7 * mm, petal_fill=soft_green, core_fill=butter)
        logo_w = 28.0 * mm
        logo_h = 20.0 * mm
        logo_y = y0 + card_h - (24.0 * mm)
    else:
        c.setFillColor(colors.Color(0.97, 0.94, 0.88))
        c.rect(inner_x, inner_y, inner_w, inner_h, stroke=0, fill=1)

        _draw_egg(c, x=inner_x + (6.5 * mm), y=inner_y + (4.2 * mm), w=8.8 * mm, h=11.8 * mm, fill=blush, stripe=white)
        _draw_egg(c, x=inner_x + inner_w - (15.2 * mm), y=inner_y + (4.0 * mm), w=8.8 * mm, h=11.8 * mm, fill=butter, stripe=white)
        _draw_flower(c, center_x=inner_x + (14.0 * mm), center_y=inner_y + inner_h - (7.5 * mm), petal_r=1.5 * mm, petal_fill=soft_green, core_fill=butter)
        _draw_flower(c, center_x=inner_x + inner_w - (14.0 * mm), center_y=inner_y + inner_h - (7.5 * mm), petal_r=1.5 * mm, petal_fill=blush, core_fill=butter)
        logo_w = 26.0 * mm
        logo_h = 18.5 * mm
        logo_y = y0 + card_h - (22.0 * mm)

    logo_x = x0 + (card_w - logo_w) / 2.0
    _draw_greeting_logo(c, assets=assets, x=logo_x, y=logo_y, w=logo_w, h=logo_h)

    greeting_font = 13.6 if style == GREETING_LABEL_STYLE_CLEAN else 14.6
    greeting_y = logo_y - (6.8 * mm)
    c.setFillColor(warm_brown)
    _draw_centered_text(
        c=c,
        text=_GREETING_LABEL_TEXT,
        font_name=latin_font_bold,
        font_size=greeting_font,
        center_x=x0 + (card_w / 2.0),
        y=greeting_y,
    )

    signature_y = y0 + (4.8 * mm)
    divider_y = signature_y + (2.8 * mm)
    name_box_top = greeting_y - (3.8 * mm)
    name_box_bottom = divider_y + (4.5 * mm)
    name_box_h = max(12.0 * mm, name_box_top - name_box_bottom)
    name_center_y = name_box_bottom + (name_box_h / 2.0)
    name_max_width = card_w - (2.0 * content_pad_x)
    name_lines, name_font_size = _fit_text_block(
        label.name,
        wrap_fn=lambda text, font_name, font_size, max_width: _wrap_text_lines(text, font_name, font_size, max_width, 3),
        font_name=latin_font_bold,
            max_font_size=17.6 if style == GREETING_LABEL_STYLE_CLEAN else 18.6,
        min_font_size=10.0,
        max_width=name_max_width,
        max_height=name_box_h,
    )
    c.setFillColor(ink)
    _draw_text_lines_centered(
        c=c,
        lines=name_lines,
        font_name=latin_font_bold,
        font_size=name_font_size,
        center_x=x0 + (card_w / 2.0),
        center_y=name_center_y,
    )

    c.setStrokeColor(colors.Color(0.87, 0.80, 0.67))
    c.setLineWidth(0.7)
    c.line(x0 + (19.0 * mm), divider_y, x0 + card_w - (19.0 * mm), divider_y)
    c.setFillColor(warm_brown)
    _draw_centered_text(
        c=c,
        text="With love, Layla Kitchen",
        font_name=latin_font_regular,
        font_size=10.2 if style == GREETING_LABEL_STYLE_CLEAN else 8.7,
        center_x=x0 + (card_w / 2.0),
        y=signature_y,
    )


def generate_greeting_labels_pdf(
    labels: Iterable[GreetingLabel],
    out_pdf_path: str | Path,
    assets: AssetPaths,
    *,
    style: str = GREETING_LABEL_STYLE_CLEAN,
    title: str = "Layla Easter Greeting Labels",
) -> Path:
    if style not in GREETING_LABEL_STYLES:
        raise ValueError(f"Unsupported greeting label style: {style}")

    out_pdf_path = Path(out_pdf_path)
    out_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    label_list = list(labels)
    page_w, page_h = A4
    c = canvas.Canvas(str(out_pdf_path), pagesize=A4)
    c.setTitle(title)

    latin_font_regular, latin_font_bold, _arabic_font_regular, _arabic_font_bold = _register_fonts(assets)

    card_w = _GREETING_LABEL_CELL_W_MM * mm
    row_heights = [row_h_mm * mm for row_h_mm in _GREETING_LABEL_ROW_HEIGHTS_MM]
    grid_w = _GREETING_LABEL_COLS * card_w
    grid_h = sum(row_heights)
    grid_x = (page_w - grid_w) / 2.0
    grid_y = (page_h - grid_h) / 2.0
    per_page = _GREETING_LABEL_COLS * _GREETING_LABEL_ROWS

    if not label_list:
        label_list = [GreetingLabel(name="")]

    for page_start in range(0, len(label_list), per_page):
        page_labels = label_list[page_start:page_start + per_page]
        c.setFillColor(colors.Color(1.0, 1.0, 1.0))
        c.rect(0, 0, page_w, page_h, stroke=0, fill=1)

        top_cursor = page_h - grid_y
        label_idx = 0
        for row_h in row_heights:
            top_cursor -= row_h
            for col in range(_GREETING_LABEL_COLS):
                if label_idx >= len(page_labels):
                    break
                x0 = grid_x + (col * card_w)
                _draw_greeting_label_card(
                    c,
                    page_labels[label_idx],
                    x0=x0,
                    y0=top_cursor,
                    card_w=card_w,
                    card_h=row_h,
                    assets=assets,
                    style=style,
                    latin_font_regular=latin_font_regular,
                    latin_font_bold=latin_font_bold,
                )
                label_idx += 1
            if label_idx >= len(page_labels):
                break

        if page_start + per_page < len(label_list):
            c.showPage()

    c.save()
    return out_pdf_path


def generate_buffet_menu_pdf(
    dishes: Iterable[Dish],
    out_pdf_path: str | Path,
    assets: AssetPaths,
    title: str = "Buffet Menu",
    subtitle: str = "Nutriments and Macronutrients",
    menu_date: Optional[str] = None,
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

        if menu_date:
            c.setFont(latin_font_bold, 10.0)
            c.setFillColor(blue_deep)
            c.drawRightString(
                header_x + header_w - (4.0 * mm),
                header_y + 12.5 * mm,
                f"Date: {menu_date}",
            )

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

            ar_text = dish.name_ar or ""
            ar_font = 10.3
            ar_lines = _wrap_arabic_two_lines(ar_text, arabic_font_bold, ar_font, ar_lane_w)
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
