"""
app.py — Streamlit UI for managing dishes and generating Layla cards PDF.

Run:
  pip install streamlit pandas reportlab pillow arabic-reshaper python-bidi
  streamlit run app.py
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import pandas as pd
import streamlit as st

from cards import (
    AssetPaths,
    Dish,
    LayoutConfig,
    default_layout_dict,
    generate_buffet_menu_pdf,
    generate_cards_pdf,
    load_layout_config,
)
from enrich import enrich_dish_name, openai_configured, openrouter_configured

try:
    import firebase_admin  # type: ignore
    from firebase_admin import credentials as firebase_credentials  # type: ignore
    from firebase_admin import firestore as firebase_firestore  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    firebase_admin = None
    firebase_credentials = None
    firebase_firestore = None

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    fitz = None


BASE_DIR = Path(__file__).resolve().parent
DATA_CSV = BASE_DIR / "data" / "dishes.csv"
LAYOUT_JSON = BASE_DIR / "data" / "layout_config.json"
LAYOUT_NO_MACROS_JSON = BASE_DIR / "data" / "layout_no_macros.json"
LAYOUT_55X90_JSON = BASE_DIR / "data" / "layout_55x90.json"
ASSETS_DIR = BASE_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
FONTS_DIR = ASSETS_DIR / "fonts"
TEMPLATE_PAGE = ASSETS_DIR / "template_page.png"

OUT_DIR = BASE_DIR / "out"
OUT_DIR.mkdir(exist_ok=True)

REQUIRED_COLS = [
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

_FIRESTORE_CLIENT = None
_FIRESTORE_INIT_ERROR: str | None = None
_FIRESTORE_INIT_DONE = False
_FIRESTORE_BOOTSTRAP_MSG: str | None = None
FIRESTORE_BATCH_LIMIT = 450


def _read_dishes_df(path: Path) -> pd.DataFrame:
    # Use utf-8-sig to normalize BOM-prefixed CSVs across environments.
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [str(c).replace("\ufeff", "").strip() for c in df.columns]
    return df


def _write_dishes_df(df: pd.DataFrame, path: Path) -> None:
    # Keep encoding stable so headers don't oscillate between BOM/non-BOM forms.
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _normalize_dishes_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in REQUIRED_COLS:
        if c not in out.columns:
            out[c] = ""
    out = out[REQUIRED_COLS]
    out["name_en"] = out["name_en"].astype(str).str.strip()
    out["name_ar"] = out["name_ar"].astype(str).str.strip()
    out["gluten"] = out["gluten"].astype(str).str.strip().replace("", "gluten_free")
    out["protein_type"] = out["protein_type"].astype(str).str.strip().replace("", "veg")
    out["dairy"] = out["dairy"].astype(str).str.strip().replace("", "dairy_free")

    for c in ("calories_kcal", "carbs_g", "protein_g", "fat_g"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    return out


def _to_float(value: object) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _to_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _dish_db_from_df(df: pd.DataFrame) -> dict[str, Dish]:
    db: dict[str, Dish] = {}
    for _, row in _normalize_dishes_df(df).iterrows():
        name_en = str(row.get("name_en", "")).strip()
        if not name_en:
            continue
        d = Dish(
            name_en=name_en,
            name_ar=str(row.get("name_ar", "")).strip(),
            calories_kcal=_to_float(row.get("calories_kcal", 0.0)),
            carbs_g=_to_float(row.get("carbs_g", 0.0)),
            protein_g=_to_float(row.get("protein_g", 0.0)),
            fat_g=_to_float(row.get("fat_g", 0.0)),
            gluten=str(row.get("gluten", "gluten_free")).strip() or "gluten_free",
            protein_type=str(row.get("protein_type", "veg")).strip() or "veg",
            dairy=str(row.get("dairy", "dairy_free")).strip() or "dairy_free",
        )
        db[name_en.lower()] = d
    return db


def _get_secret_value(key: str) -> object | None:
    if key in st.secrets:
        return st.secrets[key]
    if "firebase" in st.secrets:
        firebase_map = st.secrets["firebase"]
        if hasattr(firebase_map, "get"):
            value = firebase_map.get(key)
            if value is not None:
                return value
    return os.getenv(key)


def _detect_local_service_account_path() -> Path | None:
    candidates = sorted(BASE_DIR.glob("*firebase-adminsdk*.json")) + sorted(
        BASE_DIR.glob("*service-account*.json")
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _init_firestore_client() -> None:
    global _FIRESTORE_CLIENT, _FIRESTORE_INIT_ERROR, _FIRESTORE_INIT_DONE
    if _FIRESTORE_INIT_DONE:
        return
    _FIRESTORE_INIT_DONE = True

    if firebase_admin is None or firebase_credentials is None or firebase_firestore is None:
        _FIRESTORE_INIT_ERROR = "firebase-admin is not installed."
        return

    try:
        service_account_obj = _get_secret_value("FIREBASE_SERVICE_ACCOUNT_JSON")
        service_account_path = _get_secret_value("FIREBASE_SERVICE_ACCOUNT_PATH")
        project_id = _get_secret_value("FIREBASE_PROJECT_ID")
        local_service_account_path = _detect_local_service_account_path()

        cred_obj = None
        if service_account_obj:
            if isinstance(service_account_obj, str):
                service_account_obj = json.loads(service_account_obj)
            if isinstance(service_account_obj, dict):
                cred_obj = firebase_credentials.Certificate(dict(service_account_obj))
        elif service_account_path:
            cred_obj = firebase_credentials.Certificate(str(service_account_path))
        elif local_service_account_path:
            cred_obj = firebase_credentials.Certificate(str(local_service_account_path))
        elif os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            cred_obj = firebase_credentials.ApplicationDefault()

        if cred_obj is None:
            _FIRESTORE_INIT_ERROR = (
                "Firebase credentials are missing. Set FIREBASE_SERVICE_ACCOUNT_JSON or "
                "FIREBASE_SERVICE_ACCOUNT_PATH."
            )
            return

        if not firebase_admin._apps:  # type: ignore[attr-defined]
            options = {"projectId": str(project_id)} if project_id else None
            firebase_admin.initialize_app(cred_obj, options=options)  # type: ignore[arg-type]

        _FIRESTORE_CLIENT = firebase_firestore.client()
    except Exception as e:  # pragma: no cover - runtime credentials/env issues
        _FIRESTORE_INIT_ERROR = str(e)
        _FIRESTORE_CLIENT = None


def _firebase_collection_name() -> str:
    value = _get_secret_value("FIREBASE_DISHES_COLLECTION")
    if value:
        return str(value).strip() or "dishes"
    return "dishes"


def _record_from_row(row: pd.Series) -> dict[str, object]:
    return {
        "name_en": str(row.get("name_en", "")).strip(),
        "name_ar": str(row.get("name_ar", "")).strip(),
        "calories_kcal": _to_float(row.get("calories_kcal", 0.0)),
        "carbs_g": _to_float(row.get("carbs_g", 0.0)),
        "protein_g": _to_float(row.get("protein_g", 0.0)),
        "fat_g": _to_float(row.get("fat_g", 0.0)),
        "gluten": str(row.get("gluten", "gluten_free")).strip() or "gluten_free",
        "protein_type": str(row.get("protein_type", "veg")).strip() or "veg",
        "dairy": str(row.get("dairy", "dairy_free")).strip() or "dairy_free",
    }


def _doc_id_from_name(name_en: str) -> str:
    base = "".join(ch for ch in name_en.lower().strip() if ch.isalnum() or ch in {"-", "_", " "})
    base = "-".join(base.split())
    return base or "dish"


def _load_dishes_from_firestore() -> pd.DataFrame:
    coll = _FIRESTORE_CLIENT.collection(_firebase_collection_name())
    rows: list[dict[str, object]] = []
    for doc in coll.stream():
        data = doc.to_dict() or {}
        if isinstance(data, dict):
            rows.append(data)
    if not rows:
        return pd.DataFrame(columns=REQUIRED_COLS)
    out = _normalize_dishes_df(pd.DataFrame(rows))
    return out.sort_values("name_en", kind="stable").reset_index(drop=True)


def _load_firestore_docs() -> list[tuple[str, dict[str, object]]]:
    coll = _FIRESTORE_CLIENT.collection(_firebase_collection_name())
    docs: list[tuple[str, dict[str, object]]] = []
    for doc in coll.stream():
        data = doc.to_dict() or {}
        if isinstance(data, dict):
            docs.append((doc.id, data))
    return docs


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _build_firestore_records(
    df: pd.DataFrame,
    existing_docs: list[tuple[str, dict[str, object]]],
) -> dict[str, dict[str, object]]:
    normalized = _normalize_dishes_df(df)
    normalized = normalized[normalized["name_en"].astype(str).str.strip() != ""].copy()

    existing_by_name: dict[str, list[str]] = {}
    existing_ids = {doc_id for doc_id, _ in existing_docs}
    for doc_id, data in existing_docs:
        name_key = str(data.get("name_en", "")).strip().lower()
        if not name_key:
            continue
        existing_by_name.setdefault(name_key, []).append(doc_id)

    used_doc_ids: set[str] = set()
    records: dict[str, dict[str, object]] = {}

    for _, row in normalized.iterrows():
        record = _record_from_row(row)
        name_key = str(record["name_en"]).strip().lower()
        doc_id = ""

        for candidate in existing_by_name.get(name_key, []):
            if candidate not in used_doc_ids:
                doc_id = candidate
                break

        if not doc_id:
            base_id = _doc_id_from_name(str(record["name_en"]))
            doc_id = base_id
            suffix = 2
            while doc_id in used_doc_ids or doc_id in existing_ids:
                doc_id = f"{base_id}-{suffix}"
                suffix += 1

        used_doc_ids.add(doc_id)
        records[doc_id] = record
    return records


def _commit_firestore_sync(
    records: dict[str, dict[str, object]],
    *,
    delete_ids: set[str],
) -> None:
    coll = _FIRESTORE_CLIENT.collection(_firebase_collection_name())

    for batch_ids in _chunked(list(records.keys()), FIRESTORE_BATCH_LIMIT):
        batch = _FIRESTORE_CLIENT.batch()
        for doc_id in batch_ids:
            batch.set(coll.document(doc_id), records[doc_id])
        batch.commit()

    for batch_ids in _chunked(list(delete_ids), FIRESTORE_BATCH_LIMIT):
        batch = _FIRESTORE_CLIENT.batch()
        for doc_id in batch_ids:
            batch.delete(coll.document(doc_id))
        batch.commit()


def _save_dishes_to_firestore(df: pd.DataFrame, *, replace: bool = True) -> None:
    existing_docs = _load_firestore_docs()
    records = _build_firestore_records(df, existing_docs)
    existing_ids = {doc_id for doc_id, _ in existing_docs}
    delete_ids = existing_ids - set(records.keys()) if replace else set()
    _commit_firestore_sync(records, delete_ids=delete_ids)


def _upsert_dish_to_firestore(record: dict[str, object]) -> None:
    normalized = _normalize_dishes_df(pd.DataFrame([record]))
    if normalized.empty:
        raise ValueError("Cannot save an empty dish record to Firestore.")

    existing_docs = _load_firestore_docs()
    name_key = str(normalized.iloc[0].get("name_en", "")).strip().lower()
    matching_ids = {
        doc_id
        for doc_id, data in existing_docs
        if str(data.get("name_en", "")).strip().lower() == name_key
    }
    records = _build_firestore_records(normalized, existing_docs)
    _commit_firestore_sync(records, delete_ids=matching_ids - set(records.keys()))


def _auto_seed_enabled() -> bool:
    val = _get_secret_value("FIREBASE_AUTO_SEED_FROM_CSV")
    return _to_bool(val, default=True)


def _layout_tuner_visible() -> bool:
    val = _get_secret_value("SHOW_LAYOUT_TUNER")
    return _to_bool(val, default=True)


def _seed_firestore_from_csv_if_empty() -> bool:
    global _FIRESTORE_BOOTSTRAP_MSG
    if _FIRESTORE_CLIENT is None:
        return False
    if not _auto_seed_enabled():
        return False
    if not DATA_CSV.exists():
        return False

    csv_df = _normalize_dishes_df(_read_dishes_df(DATA_CSV))
    csv_df = csv_df[csv_df["name_en"].astype(str).str.strip() != ""]
    if csv_df.empty:
        return False

    _save_dishes_to_firestore(csv_df)
    _FIRESTORE_BOOTSTRAP_MSG = f"Bootstrapped Firestore with {len(csv_df)} dishes from local CSV."
    return True


def _load_dishes() -> tuple[pd.DataFrame, str]:
    _init_firestore_client()
    if _FIRESTORE_CLIENT is not None:
        firestore_df = _load_dishes_from_firestore()
        if firestore_df.empty:
            seeded = _seed_firestore_from_csv_if_empty()
            if seeded:
                firestore_df = _load_dishes_from_firestore()
        return firestore_df, "firebase"

    if not DATA_CSV.exists():
        return pd.DataFrame(columns=REQUIRED_COLS), "csv"
    return _normalize_dishes_df(_read_dishes_df(DATA_CSV)), "csv"


def _save_dishes(df: pd.DataFrame, backend: str) -> None:
    normalized = _normalize_dishes_df(df)
    if backend == "firebase" and _FIRESTORE_CLIENT is not None:
        _save_dishes_to_firestore(normalized, replace=True)
        return
    _write_dishes_df(normalized, DATA_CSV)


def _parse_batch_dish_names(raw_text: str, *, limit: int = 5) -> tuple[list[str], list[str]]:
    names: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()

    for line in str(raw_text or "").splitlines():
        cleaned = " ".join(str(line).strip().split())
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            duplicates.append(cleaned)
            continue
        seen.add(key)
        names.append(cleaned)

    if len(names) > limit:
        raise ValueError(f"You can fetch at most {limit} dishes at a time.")

    return names, duplicates


def _save_candidate_rows(
    records: list[dict[str, object]],
    *,
    overwrite_if_exists: bool,
    backend: str,
) -> tuple[int, int, list[str], list[str]]:
    current, _ = _load_dishes()
    if "name_en" not in current.columns:
        raise ValueError("Dish schema is invalid: missing name_en.")

    working = current.copy()
    added = 0
    updated = 0
    skipped: list[str] = []
    errors: list[str] = []

    for record in records:
        new_row = {k: record.get(k, "") for k in REQUIRED_COLS}
        normalized_row_df = _normalize_dishes_df(pd.DataFrame([new_row]))
        if normalized_row_df.empty:
            skipped.append("A dish row was skipped because it was empty.")
            continue
        normalized_row = _record_from_row(normalized_row_df.iloc[0])
        name_en = str(normalized_row.get("name_en", "")).strip()
        if not name_en:
            skipped.append("A dish row was skipped because `name_en` is empty.")
            continue

        name_key = name_en.lower()
        matches = working["name_en"].astype(str).str.strip().str.lower() == name_key

        try:
            if matches.any():
                if not overwrite_if_exists:
                    skipped.append(f"{name_en}: already exists")
                    continue
                if backend == "firebase" and _FIRESTORE_CLIENT is not None:
                    _upsert_dish_to_firestore(normalized_row)
                working.loc[matches.idxmax(), REQUIRED_COLS] = [normalized_row[c] for c in REQUIRED_COLS]
                updated += 1
            else:
                if backend == "firebase" and _FIRESTORE_CLIENT is not None:
                    _upsert_dish_to_firestore(normalized_row)
                working = pd.concat([working, pd.DataFrame([normalized_row])], ignore_index=True)
                updated_row = working.tail(1)
                working.loc[updated_row.index[0], REQUIRED_COLS] = [normalized_row[c] for c in REQUIRED_COLS]
                added += 1
        except Exception as e:
            errors.append(f"{name_en}: {e}")

    if backend != "firebase" or _FIRESTORE_CLIENT is None:
        if added or updated:
            _save_dishes(working, backend)

    return added, updated, skipped, errors


st.set_page_config(page_title="Layla Cards Generator", layout="wide")
st.title("Layla Cards Generator")

# Load dish DB (Firebase first, local CSV fallback)
df, dishes_backend = _load_dishes()
db = _dish_db_from_df(df)
if dishes_backend == "firebase":
    st.caption("Data backend: Firebase Firestore")
    if _FIRESTORE_BOOTSTRAP_MSG:
        st.info(_FIRESTORE_BOOTSTRAP_MSG)
else:
    st.warning("Data backend: local CSV fallback. Configure Firebase credentials for persistent cloud storage.")
    if _FIRESTORE_INIT_ERROR:
        st.caption(f"Firebase setup issue: {_FIRESTORE_INIT_ERROR}")


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


def _layout_config_from_dict(layout_dict: dict) -> LayoutConfig:
    base = default_layout_dict()
    merged = {**base, **{k: v for k, v in layout_dict.items() if k in base}}
    return LayoutConfig(**merged)


def _assets_for_layout(layout_cfg: LayoutConfig) -> AssetPaths:
    if layout_cfg.layout_variant == "compact_55x90":
        return replace(assets, template_page=None)
    return assets


def _select_preview_dishes(df: pd.DataFrame, layout_dict: dict) -> list[Dish]:
    normalized = _normalize_dishes_df(df).sort_values("name_en", kind="stable").reset_index(drop=True)
    preview_count = min(int(layout_dict.get("cols", 2)) * int(layout_dict.get("rows", 3)), 8)
    preview_rows = normalized.head(preview_count)
    dishes: list[Dish] = []
    for _, row in preview_rows.iterrows():
        dishes.append(
            Dish(
                name_en=str(row.get("name_en", "")).strip(),
                name_ar=str(row.get("name_ar", "")).strip(),
                calories_kcal=float(row.get("calories_kcal", 0) or 0),
                carbs_g=float(row.get("carbs_g", 0) or 0),
                protein_g=float(row.get("protein_g", 0) or 0),
                fat_g=float(row.get("fat_g", 0) or 0),
                gluten=str(row.get("gluten", "gluten_free")).strip() or "gluten_free",
                protein_type=str(row.get("protein_type", "veg")).strip() or "veg",
                dairy=str(row.get("dairy", "dairy_free")).strip() or "dairy_free",
            )
        )
    return dishes


@st.cache_data(show_spinner=False)
def _render_layout_preview(layout_json: str, dishes_json: str) -> bytes:
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed. Add `pymupdf` to the environment to enable live preview.")

    layout_cfg = _layout_config_from_dict(json.loads(layout_json))
    dish_payload = json.loads(dishes_json)
    dishes = [Dish(**item) for item in dish_payload]
    preview_assets = _assets_for_layout(layout_cfg)

    with tempfile.TemporaryDirectory() as tmp_dir:
        preview_pdf = Path(tmp_dir) / "layout_preview.pdf"
        generate_cards_pdf(
            dishes=dishes,
            out_pdf_path=preview_pdf,
            assets=preview_assets,
            layout_config=layout_cfg,
            title="Layout Preview",
        )
        doc = fitz.open(preview_pdf)  # type: ignore[operator]
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)  # type: ignore[union-attr]
        png_bytes = pix.tobytes("png")
        doc.close()
    return png_bytes


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

layout_tuner_visible = _layout_tuner_visible()
tab_labels = ["Generate Cards PDF", "Buffet A4 Menu", "Dish Database", "Add Dish (Auto-fill)"]
if layout_tuner_visible:
    tab_labels.append("Layout Tuner")
tabs = st.tabs(tab_labels)
tab1, tab2, tab3, tab4 = tabs[:4]
tab5 = tabs[4] if layout_tuner_visible else None


with tab1:
    st.subheader("1) Choose dishes")
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
            options=[
                "Full (with macros)",
                "Names + Nutriments (no macros)",
                "5.5 x 9.0 cm cards",
            ],
            index=0,
        )
        no_macros_mode = "no macros" in layout_mode.lower()
        is_55x90_mode = "5.5 x 9.0" in layout_mode.lower()
        if is_55x90_mode:
            active_layout_path = LAYOUT_55X90_JSON
        else:
            active_layout_path = LAYOUT_NO_MACROS_JSON if no_macros_mode else LAYOUT_JSON
        layout_cfg = load_layout_config(active_layout_path)
        if no_macros_mode or is_55x90_mode:
            layout_cfg = replace(layout_cfg, show_macros=False)
        profile_page_capacity = layout_cfg.cols * layout_cfg.rows
        st.caption(f"Page capacity for this layout: {profile_page_capacity} cards")
        filename = st.text_input("Output filename", value="layla_cards.pdf")
        if st.button("Generate PDF", type="primary", disabled=(len(selected) == 0)):
            dishes = [db[n.lower()] for n in selected if n.lower() in db]
            out_path = OUT_DIR / filename
            generate_cards_pdf(
                dishes=dishes,
                out_pdf_path=out_path,
                assets=_assets_for_layout(layout_cfg),
                layout_config=layout_cfg,
            )
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
    st.subheader("Buffet Table Menu")
    st.caption(
        "Create a professional buffet menu PDF with logo, dish name, nutriments, and macros (auto-paginates after 8 items/page)."
    )

    names = sorted(df["name_en"].tolist())
    selected_menu = st.multiselect("Menu dishes", names, default=[], key="buffet_menu_selected")

    colA, colB = st.columns([1, 1])
    with colA:
        menu_title = st.text_input("Menu title", value="Layla Buffet Menu", key="buffet_menu_title")
        menu_subtitle = st.text_input(
            "Menu subtitle",
            value="Nutriments and Macronutrients",
            key="buffet_menu_subtitle",
        )
        menu_date = st.date_input("Menu date", value=date.today(), key="buffet_menu_date")
    with colB:
        menu_filename = st.text_input("Output filename", value="layla_buffet_menu.pdf", key="buffet_menu_filename")
        if len(selected_menu) > 8:
            st.info("More than 8 dishes will continue on the next page.")

    if st.button("Generate Buffet Menu PDF", type="primary", disabled=(len(selected_menu) == 0), key="gen_buffet"):
        dishes = [db[n.strip().lower()] for n in selected_menu if n.strip().lower() in db]
        if not dishes:
            # Fallback from dataframe rows when lookup keys differ.
            for n in selected_menu:
                rows = df[df["name_en"].astype(str).str.strip() == str(n).strip()]
                if rows.empty:
                    continue
                row = rows.iloc[0]
                dishes.append(
                    Dish(
                        name_en=str(row.get("name_en", "")).strip(),
                        name_ar=str(row.get("name_ar", "")).strip(),
                        calories_kcal=float(row.get("calories_kcal", 0) or 0),
                        carbs_g=float(row.get("carbs_g", 0) or 0),
                        protein_g=float(row.get("protein_g", 0) or 0),
                        fat_g=float(row.get("fat_g", 0) or 0),
                        gluten=str(row.get("gluten", "gluten_free")).strip() or "gluten_free",
                        protein_type=str(row.get("protein_type", "veg")).strip() or "veg",
                        dairy=str(row.get("dairy", "dairy_free")).strip() or "dairy_free",
                    )
                )

        if not dishes:
            st.error("Could not resolve selected dishes from the database. Please reload the page and try again.")
        else:
            out_path = OUT_DIR / menu_filename
            generate_buffet_menu_pdf(
                dishes=dishes,
                out_pdf_path=out_path,
                assets=assets,
                title=menu_title.strip() or "Layla Buffet Menu",
                subtitle=menu_subtitle.strip() or "Nutriments and Macronutrients",
                menu_date=menu_date.strftime("%d %b %Y"),
            )
            st.session_state["generated_buffet_pdf_bytes"] = out_path.read_bytes()
            st.session_state["generated_buffet_pdf_name"] = out_path.name
            st.success("Buffet menu PDF generated.")

    buffet_bytes = st.session_state.get("generated_buffet_pdf_bytes")
    buffet_name = st.session_state.get("generated_buffet_pdf_name", "layla_buffet_menu.pdf")
    if buffet_bytes:
        st.download_button(
            "Download Buffet Menu PDF",
            data=buffet_bytes,
            file_name=buffet_name,
            mime="application/pdf",
            use_container_width=True,
            key="download_buffet_pdf",
        )

with tab3:
    st.subheader(f"Dish Database ({'Firebase-backed' if dishes_backend == 'firebase' else 'CSV-backed'})")
    st.caption("Edit here, then click Save.")
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True)

    if st.button("Save", type="primary"):
        _save_dishes(edited, dishes_backend)
        st.success("Saved. Reloading…")
        st.rerun()

with tab4:
    st.subheader("Add a dish")
    st.caption(
        "Enter up to 5 dish names in English, then auto-fill (optional) and review/edit before saving."
    )

    flash_messages = st.session_state.pop("add_dish_batch_flash", None)
    if isinstance(flash_messages, dict):
        if flash_messages.get("success"):
            st.success(str(flash_messages["success"]))
        if flash_messages.get("warning"):
            st.warning(str(flash_messages["warning"]))
        if flash_messages.get("error"):
            st.error(str(flash_messages["error"]))

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
        batch_names_input = st.text_area(
            "Dish names (EN)",
            value=str(st.session_state.get("batch_dish_names", "")),
            placeholder="One dish per line\nBanana Cake\nAlmond Chia Energy Bites\nChicken Shawarma Wrap",
            height=140,
        )
        st.caption("Enter 1 to 5 dishes, one per line.")
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
        st.session_state["batch_dish_names"] = batch_names_input
        try:
            names_to_fetch, duplicate_names = _parse_batch_dish_names(batch_names_input, limit=5)
            if not names_to_fetch:
                raise ValueError("Enter at least one dish name.")

            progress = st.progress(0.0, text="Preparing batch fetch...")
            fetched_candidates: list[dict[str, object]] = []
            fetch_errors: list[str] = []
            total = len(names_to_fetch)
            for idx, dish_name in enumerate(names_to_fetch, start=1):
                progress.progress((idx - 1) / total, text=f"Fetching {idx}/{total}: {dish_name}")
                try:
                    enriched = enrich_dish_name(dish_name, require_ai=do_autofill)
                    fetched_candidates.append(enriched.__dict__)
                except Exception as e:
                    fetch_errors.append(f"{dish_name}: {e}")
                progress.progress(idx / total, text=f"Finished {idx}/{total}: {dish_name}")
            progress.empty()

            if fetched_candidates:
                st.session_state["candidates"] = fetched_candidates
                st.session_state["candidate_editor_version"] = int(
                    st.session_state.get("candidate_editor_version", 0)
                ) + 1
                st.success(f"Fetched {len(fetched_candidates)} dish(es). Review below, then Save.")
            if duplicate_names:
                st.info(f"Ignored duplicate names in the batch: {', '.join(duplicate_names)}")
            if fetch_errors:
                st.error("Some dishes could not be fetched:\n" + "\n".join(fetch_errors))
            if not fetched_candidates and not fetch_errors:
                st.warning("No dishes were fetched.")
        except Exception as e:
            st.error(f"Auto-fill failed: {e}")

    candidates = st.session_state.get("candidates")
    if candidates:
        st.markdown("### Review / edit")
        overwrite_if_exists = st.checkbox(
            "Overwrite if exists (for Save)",
            value=bool(st.session_state.get("overwrite_if_exists", False)),
        )
        candidate_df = pd.DataFrame(candidates)
        for col in REQUIRED_COLS:
            if col not in candidate_df.columns:
                candidate_df[col] = ""
        if "source" not in candidate_df.columns:
            candidate_df["source"] = "manual"
        candidate_df = candidate_df[REQUIRED_COLS + ["source"]]

        edited_candidates = st.data_editor(
            candidate_df,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            key=f"candidate_editor_{int(st.session_state.get('candidate_editor_version', 0))}",
            column_config={
                "gluten": st.column_config.SelectboxColumn("gluten", options=["gluten_free", "gluten"]),
                "protein_type": st.column_config.SelectboxColumn("protein_type", options=["veg", "meat"]),
                "dairy": st.column_config.SelectboxColumn("dairy", options=["dairy_free", "dairy"]),
                "calories_kcal": st.column_config.NumberColumn("calories_kcal", min_value=0.0, step=1.0),
                "carbs_g": st.column_config.NumberColumn("carbs_g", min_value=0.0, step=0.5),
                "protein_g": st.column_config.NumberColumn("protein_g", min_value=0.0, step=0.5),
                "fat_g": st.column_config.NumberColumn("fat_g", min_value=0.0, step=0.5),
                "source": st.column_config.TextColumn("source", help="Auto-fill provider used for this row."),
            },
            disabled=["source"],
        )
        st.session_state["candidates"] = edited_candidates.to_dict(orient="records")

        colS1, colS2, colS3 = st.columns([1, 1, 2])
        with colS1:
            if st.button("Save Dishes", type="primary"):
                try:
                    records_to_save = edited_candidates.to_dict(orient="records")
                    added, updated, skipped, errors = _save_candidate_rows(
                        records_to_save,
                        overwrite_if_exists=bool(overwrite_if_exists),
                        backend=dishes_backend,
                    )

                    if errors:
                        st.error("Some dishes could not be saved:\n" + "\n".join(errors))
                    elif added or updated:
                        summary_parts = []
                        if added:
                            summary_parts.append(f"added {added}")
                        if updated:
                            summary_parts.append(f"updated {updated}")
                        warning_text = "\n".join(skipped) if skipped else ""
                        st.session_state["add_dish_batch_flash"] = {
                            "success": f"Saved batch: {', '.join(summary_parts)}.",
                            "warning": warning_text or None,
                        }
                        st.session_state.pop("candidates", None)
                        st.rerun()
                    elif skipped:
                        st.warning("No dishes were saved:\n" + "\n".join(skipped))
                    else:
                        st.warning("No dishes were saved.")
                except Exception as e:
                    st.error(f"Save failed: {e}")
        with colS2:
            if st.button("Clear Batch"):
                st.session_state.pop("candidates", None)
                st.rerun()
        with colS3:
            st.caption(
                "Tip: put an Arabic TTF in assets/fonts/ so the PDF renders Arabic correctly."
            )

if layout_tuner_visible and tab5 is not None:
    with tab5:
        st.subheader("Layout Tuner (No code)")
        st.caption("Change values, save, then generate a PDF to test alignment.")

        profile = st.selectbox(
            "Profile",
            options=["Full (with macros)", "Names + Nutriments (no macros)", "5.5 x 9.0 cm cards"],
            index=0,
            key="layout_tuner_profile",
        )
        if "5.5 x 9.0" in profile.lower():
            active_layout_path = LAYOUT_55X90_JSON
        elif "no macros" in profile.lower():
            active_layout_path = LAYOUT_NO_MACROS_JSON
        else:
            active_layout_path = LAYOUT_JSON
        layout = _load_layout_dict(active_layout_path)
        preview_dishes = _select_preview_dishes(df, layout)
        preview_dish_names = [dish.name_en for dish in preview_dishes]

        col1, col2 = st.columns(2)
        with col1:
            layout["grid_x_mm"] = st.number_input("grid_x_mm", value=float(layout["grid_x_mm"]), step=0.1)
            layout["grid_y_mm"] = st.number_input("grid_y_mm", value=float(layout["grid_y_mm"]), step=0.1)
            layout["grid_gap_x_mm"] = st.number_input("grid_gap_x_mm", value=float(layout.get("grid_gap_x_mm", 0.0)), step=0.1)
            layout["grid_gap_y_mm"] = st.number_input("grid_gap_y_mm", value=float(layout.get("grid_gap_y_mm", 0.0)), step=0.1)
            layout["auto_center_grid"] = st.checkbox("auto_center_grid", value=bool(layout.get("auto_center_grid", False)))
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
            layout["draw_grid_lines"] = st.checkbox("draw_grid_lines", value=bool(layout["draw_grid_lines"]))
            layout["draw_logo"] = st.checkbox("draw_logo", value=bool(layout.get("draw_logo", False)))
            layout["logo_x_offset_mm"] = st.number_input("logo_x_offset_mm", value=float(layout.get("logo_x_offset_mm", 34.4)), step=0.1)
            layout["logo_y_offset_mm"] = st.number_input("logo_y_offset_mm", value=float(layout.get("logo_y_offset_mm", 53.2)), step=0.1)
            layout["logo_w_mm"] = st.number_input("logo_w_mm", value=float(layout.get("logo_w_mm", 30.0)), step=0.1)
            layout["logo_h_mm"] = st.number_input("logo_h_mm", value=float(layout.get("logo_h_mm", 18.0)), step=0.1)
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

        c1, c2 = st.columns(2)
        with c1:
            if st.button("Save Layout", type="primary"):
                active_layout_path.write_text(json.dumps(layout, indent=2), encoding="utf-8")
                st.success(f"Saved: {active_layout_path}")
                st.rerun()
        with c2:
            if st.button("Reset Layout"):
                defaults = default_layout_dict()
                if "5.5 x 9.0" in profile.lower():
                    defaults.update(
                        {
                            "layout_variant": "compact_55x90",
                            "cols": 2,
                            "rows": 4,
                            "grid_gap_x_mm": 5.0,
                            "grid_gap_y_mm": 5.0,
                            "auto_center_grid": True,
                            "card_w_mm": 90.0,
                            "card_h_mm": 55.0,
                            "draw_logo": True,
                            "show_macros": False,
                            "logo_w_mm": 26.0,
                            "logo_h_mm": 26.0,
                            "dish_x_offset_mm": 27.0,
                            "dish_box_width_mm": 61.0,
                            "dish_en_size": 15.5,
                            "dish_ar_size": 13.5,
                            "icon_size_mm": 12.5,
                            "icon_gap_mm": 3.2,
                        }
                    )
                elif "no macros" in profile.lower():
                    defaults.update(
                        {
                            "show_macros": False,
                            "dish_box_width_mm": 98.8,
                            "dish_en_y_mm": 45.0,
                            "dish_ar_gap_mm": 8.6,
                            "dish_en_size": 16.0,
                            "dish_ar_size": 15.0,
                            "icon_size_mm": 13.2,
                            "icon_gap_mm": 5.0,
                            "icon_y_offset_mm": 20.0,
                        }
                    )
                active_layout_path.write_text(json.dumps(defaults, indent=2), encoding="utf-8")
                st.success("Layout reset to defaults.")
                st.rerun()

        st.markdown("### Live Preview")
        st.caption(f"Sample dishes: {', '.join(preview_dish_names) if preview_dish_names else 'No dishes available'}")
        preview_col1, preview_col2 = st.columns([1, 3])
        with preview_col1:
            update_preview = st.button("Update Preview", key="layout_tuner_update_preview")
        with preview_col2:
            st.caption("Preview shows the first rendered A4 page using the current in-memory tuner values.")

        preview_state_key = f"layout_preview::{active_layout_path.name}"
        if update_preview:
            try:
                preview_bytes = _render_layout_preview(
                    json.dumps(layout, sort_keys=True),
                    json.dumps([dish.__dict__ for dish in preview_dishes], ensure_ascii=False, sort_keys=True),
                )
                st.session_state[preview_state_key] = preview_bytes
                st.session_state[f"{preview_state_key}::names"] = preview_dish_names
            except Exception as e:
                st.error(f"Preview generation failed: {e}")

        preview_bytes = st.session_state.get(preview_state_key)
        if preview_bytes:
            st.image(preview_bytes, caption="Layout preview: rendered first page", use_container_width=True)
        else:
            st.info("Click `Update Preview` to render the current layout settings.")
