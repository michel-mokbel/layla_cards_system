"""
app.py — Streamlit UI for managing dishes and generating Layla cards PDF.

Run:
  pip install streamlit pandas reportlab pillow arabic-reshaper python-bidi
  streamlit run app.py
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import pandas as pd
import streamlit as st

from cards import (
    AssetPaths,
    Dish,
    LayoutConfig,
    default_layout_dict,
    generate_cards_pdf,
    load_dishes_csv,
    load_layout_config,
)
from enrich import enrich_dish_name, openai_configured, openrouter_configured


BASE_DIR = Path(__file__).resolve().parent
DATA_CSV = BASE_DIR / "data" / "dishes.csv"
LAYOUT_JSON = BASE_DIR / "data" / "layout_config.json"
LAYOUT_NO_MACROS_JSON = BASE_DIR / "data" / "layout_no_macros.json"
ASSETS_DIR = BASE_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
FONTS_DIR = ASSETS_DIR / "fonts"
TEMPLATE_PAGE = ASSETS_DIR / "template_page.png"

OUT_DIR = BASE_DIR / "out"
OUT_DIR.mkdir(exist_ok=True)

st.set_page_config(page_title="Layla Cards Generator", layout="wide")
st.title("Layla Cards Generator")

# Load dish DB
if not DATA_CSV.exists():
    st.error(f"Missing data file: {DATA_CSV}")
    st.stop()

df = pd.read_csv(DATA_CSV)
db = load_dishes_csv(DATA_CSV)

# Ensure required columns exist (keeps CSV forward-compatible)
required_cols = [
    "name_en",
    "name_ar",
    "calories_kcal",
    "carbs_g",
    "protein_g",
    "fat_g",
    "gluten",
    "protein_type",
    "dairy",
]
missing = [c for c in required_cols if c not in df.columns]
if missing:
    for c in missing:
        df[c] = ""
    df = df[required_cols]
    df.to_csv(DATA_CSV, index=False)
    st.warning(f"CSV was missing columns {missing}. Added them and reloaded.")
    df = pd.read_csv(DATA_CSV)
    db = load_dishes_csv(DATA_CSV)


def _pick_arabic_font() -> Path | None:
    if not FONTS_DIR.exists():
        return None
    ttf = sorted(FONTS_DIR.glob("*Regular*.ttf")) or sorted(FONTS_DIR.glob("*.ttf"))
    return ttf[0] if ttf else None


def _pick_arabic_bold_font() -> Path | None:
    if not FONTS_DIR.exists():
        return None
    ttf = sorted(FONTS_DIR.glob("*Bold*.ttf"))
    return ttf[0] if ttf else None


def _load_layout_dict(layout_path: Path) -> dict:
    base = default_layout_dict()
    if not layout_path.exists():
        return base
    try:
        user_data = json.loads(layout_path.read_text(encoding="utf-8"))
    except Exception:
        return base
    if not isinstance(user_data, dict):
        return base
    return {**base, **{k: v for k, v in user_data.items() if k in base}}


# Assets
default_arabic_font = _pick_arabic_font()
default_arabic_bold_font = _pick_arabic_bold_font()
assets = AssetPaths(
    logo=ASSETS_DIR / "logo.png",
    icon_gluten=ICONS_DIR / "gluten.png",
    icon_gluten_free=ICONS_DIR / "gluten_free.png",
    icon_veg=ICONS_DIR / "veg.png",
    icon_meat=ICONS_DIR / "meat.png",
    icon_dairy=ICONS_DIR / "dairy.png",
    icon_dairy_free=ICONS_DIR / "dairy_free.png",
    template_page=TEMPLATE_PAGE if TEMPLATE_PAGE.exists() else None,
    # Auto-pick first TTF in assets/fonts/ if present (fixes Arabic "boxes" issue)
    font_arabic=default_arabic_font,
    font_arabic_bold=default_arabic_bold_font,
)

tab1, tab2, tab3, tab4 = st.tabs(
    ["Generate PDF", "Dish Database", "Add Dish (Auto-fill)", "Layout Tuner"]
)

with tab1:
    st.subheader("1) Choose dishes (up to 6 per page)")
    names = sorted(df["name_en"].tolist())
    selected = st.multiselect("Dishes", names, default=[])

    colA, colB = st.columns([1, 1])
    with colA:
        st.caption("Preview (selected rows)")
        if selected:
            st.dataframe(df[df["name_en"].isin(selected)], use_container_width=True, hide_index=True)
        else:
            st.info("Select dishes to generate.")

    with colB:
        st.subheader("2) Export")
        layout_mode = st.selectbox(
            "Layout",
            options=["Full (with macros)", "Names + Nutriments (no macros)"],
            index=0,
        )
        no_macros_mode = "no macros" in layout_mode.lower()
        active_layout_path = LAYOUT_NO_MACROS_JSON if no_macros_mode else LAYOUT_JSON
        layout_cfg = load_layout_config(active_layout_path)
        if no_macros_mode:
            layout_cfg = replace(layout_cfg, show_macros=False)
        filename = st.text_input("Output filename", value="layla_cards.pdf")
        if st.button("Generate PDF", type="primary", disabled=(len(selected) == 0)):
            dishes = [db[n.lower()] for n in selected if n.lower() in db]
            out_path = OUT_DIR / filename
            generate_cards_pdf(dishes=dishes, out_pdf_path=out_path, assets=assets, layout_config=layout_cfg)
            pdf_bytes = out_path.read_bytes()
            st.session_state["generated_pdf_bytes"] = pdf_bytes
            st.session_state["generated_pdf_name"] = out_path.name
            st.success("PDF generated.")

        pdf_bytes = st.session_state.get("generated_pdf_bytes")
        pdf_name = st.session_state.get("generated_pdf_name", "layla_cards.pdf")
        if pdf_bytes:
            st.download_button(
                "Download PDF",
                data=pdf_bytes,
                file_name=pdf_name,
                mime="application/pdf",
                use_container_width=True,
            )

with tab2:
    st.subheader("Dish Database (CSV-backed)")
    st.caption("Edit here, then click Save. This keeps everything deterministic vs calling a nutrition API.")
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True)

    if st.button("Save", type="primary"):
        edited.to_csv(DATA_CSV, index=False)
        st.success("Saved. Reloading…")
        st.rerun()

with tab3:
    st.subheader("Add a dish")
    st.caption(
        "Type the dish name in English, then auto-fill (optional) and review/edit before saving to the CSV."
    )

    if default_arabic_font is None:
        st.info("Arabic font not found. Add a .ttf to assets/fonts/ to avoid Arabic text showing as boxes.")

    if openrouter_configured():
        st.caption("Auto-fill is enabled via OpenRouter (`OPENROUTER_API_KEY` + `OPENROUTER_MODEL`).")
    elif openai_configured():
        st.caption("Auto-fill is enabled via OpenAI (`OPENAI_API_KEY` + `OPENAI_MODEL`).")
    else:
        st.caption(
            "Auto-fill is not configured. Set `OPENROUTER_API_KEY` + `OPENROUTER_MODEL` (recommended)."
        )

    with st.form("add_dish_form", clear_on_submit=False):
        name_en = st.text_input("Dish name (EN)", value="", placeholder="e.g. Chicken Shawarma Wrap")
        col1, col2, col3 = st.columns([1, 1, 1])
        with col1:
            do_autofill = st.checkbox(
                "Auto-fill (AI)",
                value=openrouter_configured() or openai_configured(),
                help="Requires OPENROUTER_API_KEY + OPENROUTER_MODEL (recommended) or OPENAI_API_KEY + OPENAI_MODEL.",
            )
        with col2:
            overwrite_pref = st.checkbox(
                "Overwrite if exists",
                value=bool(st.session_state.get("overwrite_if_exists", False)),
            )
        with col3:
            submit = st.form_submit_button("Fetch + Review", type="primary")

    if submit:
        st.session_state["overwrite_if_exists"] = bool(overwrite_pref)
        try:
            wait_message = "Fetching dish data from AI. This can take a few seconds..."
            with st.spinner(wait_message, show_time=True):
                enriched = enrich_dish_name(name_en, require_ai=do_autofill)
            st.session_state["candidate"] = enriched.__dict__
            st.success("Fetched. Review below, then Save.")
        except Exception as e:
            st.error(f"Auto-fill failed: {e}")

    candidate = st.session_state.get("candidate")
    if candidate:
        st.markdown("### Review / edit")
        overwrite_if_exists = st.checkbox(
            "Overwrite if exists (for Save)",
            value=bool(st.session_state.get("overwrite_if_exists", False)),
        )
        colA, colB = st.columns([1, 1])
        with colA:
            candidate["name_en"] = st.text_input("name_en", value=candidate.get("name_en", ""))
            candidate["name_ar"] = st.text_input("name_ar", value=candidate.get("name_ar", ""))
            candidate["gluten"] = st.selectbox(
                "gluten", options=["gluten_free", "gluten"], index=0 if candidate.get("gluten") != "gluten" else 1
            )
            candidate["protein_type"] = st.selectbox(
                "protein_type",
                options=["veg", "meat"],
                index=0 if candidate.get("protein_type") != "meat" else 1,
            )
            candidate["dairy"] = st.selectbox(
                "dairy", options=["dairy_free", "dairy"], index=0 if candidate.get("dairy") != "dairy" else 1
            )
        with colB:
            candidate["calories_kcal"] = st.number_input(
                "calories_kcal", value=float(candidate.get("calories_kcal") or 0.0), min_value=0.0, step=1.0
            )
            candidate["carbs_g"] = st.number_input(
                "carbs_g", value=float(candidate.get("carbs_g") or 0.0), min_value=0.0, step=0.5
            )
            candidate["protein_g"] = st.number_input(
                "protein_g", value=float(candidate.get("protein_g") or 0.0), min_value=0.0, step=0.5
            )
            candidate["fat_g"] = st.number_input(
                "fat_g", value=float(candidate.get("fat_g") or 0.0), min_value=0.0, step=0.5
            )
            st.caption(f"source: {candidate.get('source', 'manual')}")

        colS1, colS2 = st.columns([1, 2])
        with colS1:
            if st.button("Save to CSV", type="primary"):
                new_row = {k: candidate.get(k, "") for k in required_cols}
                if not str(new_row["name_en"]).strip():
                    st.error("name_en is required.")
                else:
                    name_key = str(new_row["name_en"]).strip().lower()
                    current = pd.read_csv(DATA_CSV)
                    if "name_en" not in current.columns:
                        st.error("CSV schema is invalid: missing name_en.")
                    else:
                        matches = current["name_en"].astype(str).str.strip().str.lower() == name_key
                        if matches.any():
                            if not overwrite_if_exists:
                                st.error("Dish already exists. Enable “Overwrite if exists” to update it.")
                            else:
                                current.loc[matches.idxmax(), required_cols] = [new_row[c] for c in required_cols]
                                current.to_csv(DATA_CSV, index=False)
                                st.success("Updated existing dish. Reloading…")
                                st.session_state.pop("candidate", None)
                                st.rerun()
                        else:
                            current = pd.concat([current, pd.DataFrame([new_row])], ignore_index=True)
                            current.to_csv(DATA_CSV, index=False)
                            st.success("Added dish. Reloading…")
                            st.session_state.pop("candidate", None)
                            st.rerun()
        with colS2:
            st.caption(
                "Tip: put an Arabic TTF in assets/fonts/ so the PDF renders Arabic correctly."
            )

with tab4:
    st.subheader("Layout Tuner (No code)")
    st.caption("Change values, save, then generate a PDF to test alignment.")

    profile = st.selectbox(
        "Profile",
        options=["Full (with macros)", "Names + Nutriments (no macros)"],
        index=0,
        key="layout_tuner_profile",
    )
    active_layout_path = LAYOUT_NO_MACROS_JSON if "no macros" in profile.lower() else LAYOUT_JSON
    layout = _load_layout_dict(active_layout_path)

    col1, col2 = st.columns(2)
    with col1:
        layout["grid_x_mm"] = st.number_input("grid_x_mm", value=float(layout["grid_x_mm"]), step=0.1)
        layout["grid_y_mm"] = st.number_input("grid_y_mm", value=float(layout["grid_y_mm"]), step=0.1)
        layout["card_w_mm"] = st.number_input("card_w_mm", value=float(layout["card_w_mm"]), step=0.1)
        layout["card_h_mm"] = st.number_input("card_h_mm", value=float(layout["card_h_mm"]), step=0.1)
        layout["dish_x_offset_mm"] = st.number_input(
            "dish_x_offset_mm", value=float(layout["dish_x_offset_mm"]), step=0.1
        )
        layout["dish_box_width_mm"] = st.number_input(
            "dish_box_width_mm", value=float(layout["dish_box_width_mm"]), step=0.1
        )
        layout["dish_en_y_mm"] = st.number_input("dish_en_y_mm", value=float(layout["dish_en_y_mm"]), step=0.1)
        layout["dish_ar_gap_mm"] = st.number_input("dish_ar_gap_mm", value=float(layout["dish_ar_gap_mm"]), step=0.1)
        layout["dish_en_size"] = st.number_input("dish_en_size", value=float(layout["dish_en_size"]), step=0.1)
        layout["dish_ar_size"] = st.number_input("dish_ar_size", value=float(layout["dish_ar_size"]), step=0.1)
        layout["show_macros"] = st.checkbox("show_macros", value=bool(layout.get("show_macros", True)))
    with col2:
        layout["icon_x_offset_mm"] = st.number_input("icon_x_offset_mm", value=float(layout["icon_x_offset_mm"]), step=0.1)
        layout["icon_y_offset_mm"] = st.number_input("icon_y_offset_mm", value=float(layout["icon_y_offset_mm"]), step=0.1)
        layout["icon_size_mm"] = st.number_input("icon_size_mm", value=float(layout["icon_size_mm"]), step=0.1)
        layout["icon_gap_mm"] = st.number_input("icon_gap_mm", value=float(layout["icon_gap_mm"]), step=0.1)
        layout["macro_x_offset_mm"] = st.number_input(
            "macro_x_offset_mm", value=float(layout["macro_x_offset_mm"]), step=0.1
        )
        layout["macro_y_top_mm"] = st.number_input("macro_y_top_mm", value=float(layout["macro_y_top_mm"]), step=0.1)
        layout["macro_line_gap_mm"] = st.number_input(
            "macro_line_gap_mm", value=float(layout["macro_line_gap_mm"]), step=0.1
        )
        layout["macro_size"] = st.number_input("macro_size", value=float(layout["macro_size"]), step=0.1)
        layout["draw_grid_lines"] = st.checkbox("draw_grid_lines", value=bool(layout["draw_grid_lines"]))

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Save Layout", type="primary"):
            active_layout_path.write_text(json.dumps(layout, indent=2), encoding="utf-8")
            st.success(f"Saved: {active_layout_path}")
            st.rerun()
    with c2:
        if st.button("Reset Layout"):
            defaults = default_layout_dict()
            if "no macros" in profile.lower():
                defaults.update(
                    {
                        "show_macros": False,
                        "dish_box_width_mm": 98.8,
                        "dish_en_y_mm": 49.0,
                        "dish_ar_gap_mm": 10.0,
                        "dish_en_size": 16.0,
                        "dish_ar_size": 15.0,
                        "icon_size_mm": 13.2,
                        "icon_gap_mm": 5.0,
                        "icon_y_offset_mm": 24.0,
                    }
                )
            active_layout_path.write_text(json.dumps(defaults, indent=2), encoding="utf-8")
            st.success("Layout reset to defaults.")
            st.rerun()
