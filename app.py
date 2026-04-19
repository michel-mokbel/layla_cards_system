"""
app.py — Streamlit UI for managing dishes and generating Layla cards PDF.

Run:
  pip install streamlit pandas reportlab pillow arabic-reshaper python-bidi
  streamlit run app.py
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
import json
import math
import os
from pathlib import Path
import tempfile
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from ai_recipe_studio import (
    GenerationRequest,
    GeneratedDishDraft,
    approve_drafts,
    dish_record_from_draft,
    evaluate_draft,
    generate_dish_drafts,
    load_drafts,
    reject_drafts,
    request_from_draft,
    save_draft_batch,
    validate_draft,
)
from idea_center import IDEA_PRESETS, IdeaPreset
from firebase_auth_service import (
    FirebaseAuthSession,
    auth_session_expiring,
    firebase_auth_error_message,
    refresh_id_token,
    sign_in_with_email_password,
)
from cards import (
    AssetPaths,
    Dish,
    GREETING_LABEL_STYLE_CLEAN,
    GREETING_LABEL_STYLE_PLAYFUL,
    LayoutConfig,
    default_layout_dict,
    generate_buffet_menu_pdf,
    generate_cards_pdf,
    generate_delivery_note_pdf,
    generate_greeting_labels_pdf,
    load_layout_config,
    parse_greeting_label_names,
)
from enrich import enrich_dish_name, gemini_configured, openai_configured

try:
    import firebase_admin  # type: ignore
    from firebase_admin import auth as firebase_admin_auth  # type: ignore
    from firebase_admin import credentials as firebase_credentials  # type: ignore
    from firebase_admin import firestore as firebase_firestore  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    firebase_admin = None
    firebase_admin_auth = None
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
DRAFTS_JSON = BASE_DIR / "data" / "generated_dish_drafts.json"
EASTER_GREETING_RENDER_VERSION = 2

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
AUTH_SESSION_KEY = "firebase_auth_session"
AUTH_COOKIE_NAME = "layla_auth_session"
AUTH_COOKIE_SET_KEY = "_auth_cookie_to_set"
AUTH_COOKIE_CLEAR_KEY = "_auth_cookie_to_clear"
AUTH_COOKIE_STORAGE_KEY = "layla_auth_session_storage"


@dataclass(frozen=True)
class DeliveryNoteRow:
    sr_no: str
    food_type: str
    unit: str = ""
    quantity: str = ""
    dispatch_temp: str = ""
    delivery_temp: str = ""
    remarks: str = ""


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


def _firebase_web_api_key() -> str:
    value = _get_secret_value("FIREBASE_WEB_API_KEY")
    return str(value).strip() if value is not None else ""


def _firebase_auth_required() -> bool:
    value = _get_secret_value("FIREBASE_AUTH_REQUIRED")
    return _to_bool(value, default=True)


def _firebase_auth_configured() -> bool:
    return bool(_firebase_web_api_key()) and firebase_admin_auth is not None


def _auth_cookie_name() -> str:
    value = _get_secret_value("FIREBASE_AUTH_COOKIE_NAME")
    return str(value).strip() if value else AUTH_COOKIE_NAME


def _auth_session_days() -> int:
    value = _get_secret_value("FIREBASE_AUTH_SESSION_DAYS")
    try:
        days = int(str(value).strip()) if value is not None else 7
    except Exception:
        days = 7
    return min(max(days, 1), 14)


def _save_auth_session(session: FirebaseAuthSession) -> None:
    st.session_state[AUTH_SESSION_KEY] = session.to_dict()


def _load_auth_session() -> FirebaseAuthSession | None:
    payload = st.session_state.get(AUTH_SESSION_KEY)
    if not isinstance(payload, dict):
        return None
    session = FirebaseAuthSession.from_dict(payload)
    if not session.uid or not session.email or not session.id_token:
        return None
    return session


def _clear_auth_session() -> None:
    st.session_state.pop(AUTH_SESSION_KEY, None)


def _load_auth_cookie_value() -> str:
    try:
        cookies = st.context.cookies
    except Exception:
        return ""
    value = cookies.get(_auth_cookie_name(), "")
    return str(value).strip() if value else ""


def _queue_set_auth_cookie(cookie_value: str) -> None:
    if cookie_value:
        st.session_state[AUTH_COOKIE_SET_KEY] = cookie_value
    st.session_state.pop(AUTH_COOKIE_CLEAR_KEY, None)


def _queue_clear_auth_cookie() -> None:
    st.session_state.pop(AUTH_COOKIE_SET_KEY, None)
    st.session_state[AUTH_COOKIE_CLEAR_KEY] = True


def _auth_cookie_bridge_html(*, cookie_name: str, secure_attr: str, pending_value: str = "", clear_cookie: bool = False) -> str:
    encoded_cookie_name = json.dumps(cookie_name)
    encoded_storage_key = json.dumps(AUTH_COOKIE_STORAGE_KEY)
    encoded_pending_value = json.dumps(pending_value)
    return f"""
    <script>
    const cookieName = {encoded_cookie_name};
    const storageKey = {encoded_storage_key};
    const pendingValue = {encoded_pending_value};
    const shouldClear = {str(clear_cookie).lower()};
    const cookieSuffix = "; path=/; SameSite=Lax; {secure_attr}";

    const rootDoc = (() => {{
      try {{
        if (window.top && window.top.document) return window.top.document;
      }} catch (e) {{}}
      try {{
        if (window.parent && window.parent.document) return window.parent.document;
      }} catch (e) {{}}
      return document;
    }})();

    function setCookie(value, maxAge) {{
      rootDoc.cookie = cookieName + "=" + encodeURIComponent(value) + "; max-age=" + maxAge + cookieSuffix;
    }}

    function clearCookie() {{
      rootDoc.cookie = cookieName + "=; max-age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT" + cookieSuffix;
    }}

    try {{
      if (shouldClear) {{
        window.localStorage.removeItem(storageKey);
        clearCookie();
        window.top.location.reload();
      }} else if (pendingValue) {{
        window.localStorage.setItem(storageKey, pendingValue);
        setCookie(pendingValue, {_auth_session_days() * 24 * 60 * 60});
        window.top.location.reload();
      }} else {{
        const restored = window.localStorage.getItem(storageKey);
        if (restored) {{
          setCookie(restored, {_auth_session_days() * 24 * 60 * 60});
          window.top.location.reload();
        }}
      }}
    }} catch (e) {{
      console.error("auth cookie bridge failed", e);
    }}
    </script>
    """


def _render_auth_cookie_bridge() -> None:
    cookie_name = _auth_cookie_name()
    cookie_value = _load_auth_cookie_value()
    pending_value = str(st.session_state.get(AUTH_COOKIE_SET_KEY, "") or "")
    pending_clear = bool(st.session_state.get(AUTH_COOKIE_CLEAR_KEY, False))
    secure_attr = "Secure;" if str(getattr(st.context, "url", "") or "").startswith("https://") else ""

    if pending_value:
        if cookie_value == pending_value:
            st.session_state.pop(AUTH_COOKIE_SET_KEY, None)
            return
        components.html(
            _auth_cookie_bridge_html(
                cookie_name=cookie_name,
                secure_attr=secure_attr,
                pending_value=pending_value,
            ),
            height=0,
        )
        st.stop()

    if pending_clear:
        components.html(
            _auth_cookie_bridge_html(
                cookie_name=cookie_name,
                secure_attr=secure_attr,
                clear_cookie=True,
            ),
            height=0,
        )
        st.stop()

    if not cookie_value:
        components.html(
            _auth_cookie_bridge_html(
                cookie_name=cookie_name,
                secure_attr=secure_attr,
            ),
            height=0,
        )


def _verify_auth_cookie_session() -> dict[str, object] | None:
    cookie_value = _load_auth_cookie_value()
    if not cookie_value or firebase_admin_auth is None:
        return None
    try:
        return firebase_admin_auth.verify_session_cookie(cookie_value, check_revoked=True)
    except Exception:
        _queue_clear_auth_cookie()
        return None


def _verify_or_refresh_auth_session() -> dict[str, object] | None:
    session = _load_auth_session()
    if session is None:
        return _verify_auth_cookie_session()
    if firebase_admin_auth is None:
        _clear_auth_session()
        return None

    if auth_session_expiring(session):
        try:
            refreshed = refresh_id_token(
                _firebase_web_api_key(),
                session.refresh_token,
                email=session.email,
            )
            if not refreshed.email:
                refreshed = FirebaseAuthSession(
                    uid=refreshed.uid,
                    email=session.email,
                    id_token=refreshed.id_token,
                    refresh_token=refreshed.refresh_token,
                    expires_at=refreshed.expires_at,
                )
            session = refreshed
            _save_auth_session(session)
        except Exception:
            _clear_auth_session()
            _queue_clear_auth_cookie()
            return None

    try:
        decoded = firebase_admin_auth.verify_id_token(session.id_token)
        if not session.email and decoded.get("email"):
            _save_auth_session(
                FirebaseAuthSession(
                    uid=str(decoded.get("uid", session.uid)),
                    email=str(decoded.get("email", "")).strip(),
                    id_token=session.id_token,
                    refresh_token=session.refresh_token,
                    expires_at=session.expires_at,
                )
            )
        return decoded
    except Exception:
        try:
            refreshed = refresh_id_token(
                _firebase_web_api_key(),
                session.refresh_token,
                email=session.email,
            )
            if not refreshed.email:
                refreshed = FirebaseAuthSession(
                    uid=refreshed.uid,
                    email=session.email,
                    id_token=refreshed.id_token,
                    refresh_token=refreshed.refresh_token,
                    expires_at=refreshed.expires_at,
                )
            _save_auth_session(refreshed)
            return firebase_admin_auth.verify_id_token(refreshed.id_token)
        except Exception:
            _clear_auth_session()
            _queue_clear_auth_cookie()
            return None


def _render_login_gate() -> dict[str, object] | None:
    if not _firebase_auth_required():
        return {"uid": "dev-bypass"}

    if not _firebase_auth_configured():
        st.error(
            "Firebase Authentication is required, but it is not configured. "
            "Set `FIREBASE_WEB_API_KEY` and valid Firebase admin credentials."
        )
        st.stop()

    decoded = _verify_or_refresh_auth_session()
    if decoded is not None:
        session = _load_auth_session()
        with st.sidebar:
            st.markdown("**Session**")
            st.caption(f"Signed in as {session.email if session else decoded.get('email', 'user')}")
            if st.button("Sign Out", use_container_width=True):
                _clear_auth_session()
                _queue_clear_auth_cookie()
                st.rerun()
        return decoded

    st.subheader("Sign In")
    st.caption("This admin workspace is protected by Firebase Authentication.")

    with st.form("firebase_sign_in_form", clear_on_submit=False):
        email = st.text_input("Email", autocomplete="email")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)

    if submitted:
        try:
            session = sign_in_with_email_password(_firebase_web_api_key(), email, password)
            session_cookie = firebase_admin_auth.create_session_cookie(  # type: ignore[union-attr]
                session.id_token,
                expires_in=timedelta(days=_auth_session_days()),
            )
            _save_auth_session(session)
            _queue_set_auth_cookie(session_cookie)
            decoded = _verify_or_refresh_auth_session()
            if decoded is None:
                raise RuntimeError("Firebase sign-in succeeded but token verification failed.")
            st.rerun()
        except Exception as exc:
            st.error(firebase_auth_error_message(exc))

    st.stop()


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
) -> tuple[int, int, list[str], list[str], list[str]]:
    current, _ = _load_dishes()
    if "name_en" not in current.columns:
        raise ValueError("Dish schema is invalid: missing name_en.")

    working = current.copy()
    added = 0
    updated = 0
    skipped: list[str] = []
    errors: list[str] = []
    saved_names: list[str] = []

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
                saved_names.append(name_en)
            else:
                if backend == "firebase" and _FIRESTORE_CLIENT is not None:
                    _upsert_dish_to_firestore(normalized_row)
                working = pd.concat([working, pd.DataFrame([normalized_row])], ignore_index=True)
                updated_row = working.tail(1)
                working.loc[updated_row.index[0], REQUIRED_COLS] = [normalized_row[c] for c in REQUIRED_COLS]
                added += 1
                saved_names.append(name_en)
        except Exception as e:
            errors.append(f"{name_en}: {e}")

    if backend != "firebase" or _FIRESTORE_CLIENT is None:
        if added or updated:
            _save_dishes(working, backend)

    return added, updated, skipped, errors, saved_names


def _resolve_selected_dishes(selected_names: list[str], df: pd.DataFrame, db: dict[str, Dish]) -> list[Dish]:
    dishes = [db[n.strip().lower()] for n in selected_names if n.strip().lower() in db]
    if dishes:
        return dishes

    resolved: list[Dish] = []
    for name in selected_names:
        rows = df[df["name_en"].astype(str).str.strip() == str(name).strip()]
        if rows.empty:
            continue
        row = rows.iloc[0]
        resolved.append(
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
    return resolved


def _default_delivery_unit(dish_name: str) -> str:
    normalized = str(dish_name or "").strip().lower()
    if any(token in normalized for token in ("juice", "water", "tea", "coffee", "smoothie")):
        return "Bot"
    return "Pc"


def _delivery_note_rows_df_from_dishes(dishes: list[Dish]) -> pd.DataFrame:
    rows: list[dict[str, str]] = []
    for idx, dish in enumerate(dishes, start=1):
        rows.append(
            {
                "sr_no": str(idx),
                "food_type": dish.name_en,
                "unit": _default_delivery_unit(dish.name_en),
                "quantity": "",
                "dispatch_temp": "",
                "delivery_temp": "",
                "remarks": "",
            }
        )
    while len(rows) < max(10, len(dishes)):
        rows.append(
            {
                "sr_no": str(len(rows) + 1),
                "food_type": "",
                "unit": "",
                "quantity": "",
                "dispatch_temp": "",
                "delivery_temp": "",
                "remarks": "",
            }
        )
    return pd.DataFrame(rows)


def _delivery_note_rows_from_df(edited_df: pd.DataFrame) -> list[DeliveryNoteRow]:
    rows: list[DeliveryNoteRow] = []
    for _, row in edited_df.iterrows():
        values = {
            "sr_no": str(row.get("sr_no", "")).strip(),
            "food_type": str(row.get("food_type", "")).strip(),
            "unit": str(row.get("unit", "")).strip(),
            "quantity": str(row.get("quantity", "")).strip(),
            "dispatch_temp": str(row.get("dispatch_temp", "")).strip(),
            "delivery_temp": str(row.get("delivery_temp", "")).strip(),
            "remarks": str(row.get("remarks", "")).strip(),
        }
        rows.append(DeliveryNoteRow(**values))
    return rows


def _firebase_drafts_collection_name() -> str:
    value = _get_secret_value("FIREBASE_DRAFTS_COLLECTION")
    if value:
        return str(value).strip() or "generated_dish_drafts"
    return "generated_dish_drafts"


def _draft_storage_kwargs() -> dict[str, object]:
    if _FIRESTORE_CLIENT is not None:
        return {"firestore_collection": _FIRESTORE_CLIENT.collection(_firebase_drafts_collection_name())}
    return {"storage_path": DRAFTS_JSON}


def _load_recipe_drafts() -> list[GeneratedDishDraft]:
    return load_drafts(**_draft_storage_kwargs())


def _save_recipe_drafts(drafts: list[GeneratedDishDraft]) -> None:
    save_draft_batch(drafts, **_draft_storage_kwargs())


def _replace_draft_in_list(
    drafts: list[GeneratedDishDraft],
    updated_drafts: list[GeneratedDishDraft],
) -> list[GeneratedDishDraft]:
    updated_by_id = {draft.draft_id: draft for draft in updated_drafts}
    merged = [updated_by_id.get(draft.draft_id, draft) for draft in drafts]
    for draft in updated_drafts:
        if draft.draft_id not in {item.draft_id for item in drafts}:
            merged.append(draft)
    return sorted(merged, key=lambda draft: draft.created_at, reverse=True)


def _draft_from_payload(payload: dict[str, object]) -> GeneratedDishDraft:
    return GeneratedDishDraft.from_dict(payload)


def _coerce_optional_filter(value: str) -> str | None:
    cleaned = str(value or "").strip().lower()
    return None if cleaned in {"", "any"} else cleaned


def _label_for_filter(value: str | None) -> str:
    return "Any" if not value else value


def _apply_idea_preset_to_state(preset: IdeaPreset) -> None:
    st.session_state["ai_recipe_brief"] = preset.prompt
    st.session_state["ai_recipe_count"] = int(preset.count)
    st.session_state["ai_recipe_protein_filter"] = _label_for_filter(preset.protein_type)
    st.session_state["ai_recipe_gluten_filter"] = _label_for_filter(preset.gluten)
    st.session_state["ai_recipe_dairy_filter"] = _label_for_filter(preset.dairy)


def _generate_recipe_drafts_for_request(
    generation_request: GenerationRequest,
    existing_drafts: list[GeneratedDishDraft],
    existing_dish_names: list[str],
) -> list[GeneratedDishDraft]:
    existing_names = list(existing_dish_names) + [
        str(draft.dish.get("name_en", "")).strip()
        for draft in existing_drafts
        if draft.status in {"review_ready", "needs_attention", "approved"}
    ]
    new_drafts = generate_dish_drafts(generation_request, existing_dish_names=existing_names)
    merged = _replace_draft_in_list(existing_drafts, new_drafts)
    _save_recipe_drafts(merged)
    return new_drafts


def _review_status_for_draft(draft: GeneratedDishDraft) -> str:
    if draft.status in {"approved", "rejected", "superseded"}:
        return draft.status
    return "review_ready" if draft.validation.passed else "needs_attention"


def _sync_editor_rows_to_drafts(
    drafts: list[GeneratedDishDraft],
    edited_df: pd.DataFrame,
    existing_names: list[str],
) -> list[GeneratedDishDraft]:
    if edited_df.empty:
        return drafts

    rows_by_id = {
        str(row.get("draft_id", "")).strip(): row
        for _, row in edited_df.iterrows()
        if str(row.get("draft_id", "")).strip()
    }
    active_statuses = {"review_ready", "needs_attention"}
    reserved_names = {str(name).strip().lower() for name in existing_names if str(name).strip()}
    updated: list[GeneratedDishDraft] = []

    for draft in sorted(drafts, key=lambda item: item.created_at):
        if draft.draft_id not in rows_by_id or draft.status not in active_statuses:
            updated.append(draft)
            continue

        row = rows_by_id[draft.draft_id]
        payload = draft.to_dict()
        payload["dish"] = {
            "name_en": str(row.get("name_en", "")).strip(),
            "name_ar": str(row.get("name_ar", "")).strip(),
            "calories_kcal": float(row.get("calories_kcal", 0) or 0),
            "carbs_g": float(row.get("carbs_g", 0) or 0),
            "protein_g": float(row.get("protein_g", 0) or 0),
            "fat_g": float(row.get("fat_g", 0) or 0),
            "gluten": str(row.get("gluten", "gluten_free")).strip() or "gluten_free",
            "protein_type": str(row.get("protein_type", "veg")).strip() or "veg",
            "dairy": str(row.get("dairy", "dairy_free")).strip() or "dairy_free",
        }
        candidate = _draft_from_payload(payload)
        validation = validate_draft(candidate, reserved_names=reserved_names)
        evaluation = evaluate_draft(candidate, request_from_draft(candidate))
        updated_payload = candidate.to_dict()
        updated_payload["validation"] = validation.to_dict()
        updated_payload["evaluation"] = evaluation.to_dict()
        updated_payload["status"] = "review_ready" if validation.passed else "needs_attention"
        updated_draft = _draft_from_payload(updated_payload)
        if validation.passed:
            reserved_names.add(str(updated_draft.dish.get("name_en", "")).strip().lower())
        updated.append(updated_draft)

    return sorted(updated, key=lambda draft: draft.created_at, reverse=True)


st.set_page_config(page_title="Layla Cards Generator", layout="wide")
st.title("Layla Cards Generator")
_render_auth_cookie_bridge()
_init_firestore_client()
_AUTH_CONTEXT = _render_login_gate()

# Load dish DB (Firebase first, local CSV fallback)
df, dishes_backend = _load_dishes()
db = _dish_db_from_df(df)
if dishes_backend == "firebase":
    # st.caption("Data backend: Firebase Firestore")
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
workspace_options = [
    "Generate Cards PDF",
    "Easter Greeting Labels",
    "Buffet A4 Menu",
    "Dish Database",
    "Add Dish (Auto-fill)",
    "Idea Center",
    "AI Recipe Studio",
]
if layout_tuner_visible:
    workspace_options.append("Layout Tuner")
default_workspace = (
    st.session_state.get("active_workspace")
    if st.session_state.get("active_workspace") in workspace_options
    else workspace_options[0]
)
active_workspace = st.segmented_control(
    "Workspace",
    options=workspace_options,
    selection_mode="single",
    default=default_workspace,
    key="workspace_selector",
)
if not active_workspace:
    active_workspace = default_workspace
st.session_state["active_workspace"] = active_workspace


if active_workspace == "Generate Cards PDF":
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

if active_workspace == "Easter Greeting Labels":
    st.subheader("Easter Greeting Labels")
    st.caption("Paste client names one per line. The app will place one name per card on a Word-sized 2 × 5 A4 label sheet.")

    colA, colB = st.columns([1.1, 0.9])
    with colA:
        raw_client_names = st.text_area(
            "Client names",
            value=st.session_state.get("easter_greeting_names", ""),
            height=320,
            placeholder="MOI\nOPERATORS\nMARSHALLS\nLEKHWIYA",
            key="easter_greeting_names",
        )
        greeting_labels = parse_greeting_label_names(raw_client_names)
        if greeting_labels:
            preview_names = ", ".join(label.name for label in greeting_labels[:6])
            if len(greeting_labels) > 6:
                preview_names += ", ..."
            st.caption(f"{len(greeting_labels)} names parsed. Preview: {preview_names}")
        else:
            st.info("Paste at least one client name to generate labels.")

    with colB:
        style_label = st.selectbox(
            "Style",
            options=["Clean Brand Pastel", "Playful Graphic-Heavy"],
            index=0,
            key="easter_greeting_style",
        )
        style_value = (
            GREETING_LABEL_STYLE_CLEAN
            if style_label == "Clean Brand Pastel"
            else GREETING_LABEL_STYLE_PLAYFUL
        )
        current_request_signature = (
            EASTER_GREETING_RENDER_VERSION,
            raw_client_names,
            style_value,
        )
        if st.session_state.get("easter_greeting_request_signature") != current_request_signature:
            st.session_state.pop("easter_greeting_pdf_bytes", None)
            st.session_state.pop("easter_greeting_pdf_name", None)
            st.session_state["easter_greeting_request_signature"] = current_request_signature
        per_page = 10
        page_count = max(1, math.ceil(len(greeting_labels) / per_page)) if greeting_labels else 0
        st.caption(f"Sheet capacity: {per_page} labels per page")
        if greeting_labels:
            st.caption(f"Estimated pages: {page_count}")

        greeting_filename = st.text_input(
            "Output filename",
            value="layla_easter_labels.pdf",
            key="easter_greeting_filename",
        )
        if style_value == GREETING_LABEL_STYLE_CLEAN:
            st.caption("Centered brand layout with pastel eggs, floral accents, and a restrained Easter look.")
        else:
            st.caption("More decorative Easter layout with bolder color blocks and graphic accents.")

        if st.button("Generate Easter PDF", type="primary", disabled=(len(greeting_labels) == 0), key="easter_greeting_generate"):
            out_path = OUT_DIR / greeting_filename
            generate_greeting_labels_pdf(
                labels=greeting_labels,
                out_pdf_path=out_path,
                assets=assets,
                style=style_value,
            )
            st.session_state["easter_greeting_pdf_bytes"] = out_path.read_bytes()
            st.session_state["easter_greeting_pdf_name"] = out_path.name
            st.session_state["easter_greeting_request_signature"] = current_request_signature
            st.success("Easter greeting PDF generated.")

        easter_pdf_bytes = st.session_state.get("easter_greeting_pdf_bytes")
        easter_pdf_name = st.session_state.get("easter_greeting_pdf_name", "layla_easter_labels.pdf")
        if easter_pdf_bytes:
            st.download_button(
                "Download Easter PDF",
                data=easter_pdf_bytes,
                file_name=easter_pdf_name,
                mime="application/pdf",
                use_container_width=True,
                key="easter_greeting_download",
            )

if active_workspace == "Buffet A4 Menu":
    st.subheader("Buffet Table Menu")
    st.caption(
        "Create the A4 buffet menu and a matching delivery note from the same dish selection."
    )

    names = sorted(df["name_en"].tolist())
    selected_menu = st.multiselect("Menu dishes", names, default=[], key="buffet_menu_selected")
    selected_menu_signature = tuple(selected_menu)
    if st.session_state.get("delivery_note_selected_signature") != selected_menu_signature:
        resolved_preview_dishes = _resolve_selected_dishes(selected_menu, df, db)
        st.session_state["delivery_note_rows"] = _delivery_note_rows_df_from_dishes(resolved_preview_dishes)
        st.session_state["delivery_note_selected_signature"] = selected_menu_signature

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

    st.markdown("### Delivery Note")
    dn_col1, dn_col2 = st.columns([1, 1])
    with dn_col1:
        delivery_client = st.text_input("Client", value="Microsoft", key="delivery_note_client")
        delivery_location = st.text_input(
            "Location",
            value="Al Fardan Tower - Lusail",
            key="delivery_note_location",
        )
        delivery_reference = st.text_input("Reference", value="KL/EFS-MS/026", key="delivery_note_reference")
    with dn_col2:
        delivery_revision = st.text_input("Rev", value="00", key="delivery_note_revision")
        delivery_issue_date = st.date_input("Date of Issue", value=menu_date, key="delivery_note_issue_date")
        delivery_issue_no = st.text_input("Issue No", value="00", key="delivery_note_issue_no")

    delivery_filename = st.text_input(
        "Delivery note filename",
        value="layla_delivery_note.pdf",
        key="delivery_note_filename",
    )
    delivery_rows_df = st.session_state.get("delivery_note_rows")
    if not isinstance(delivery_rows_df, pd.DataFrame):
        delivery_rows_df = _delivery_note_rows_df_from_dishes([])
        st.session_state["delivery_note_rows"] = delivery_rows_df

    edited_delivery_rows = st.data_editor(
        delivery_rows_df,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        key="delivery_note_editor",
        column_config={
            "sr_no": st.column_config.TextColumn("Sr.#", width="small"),
            "food_type": st.column_config.TextColumn("Food type", width="large"),
            "unit": st.column_config.TextColumn("Unit", width="small"),
            "quantity": st.column_config.TextColumn("Quantity", width="small"),
            "dispatch_temp": st.column_config.TextColumn("Dispatch temp", width="medium"),
            "delivery_temp": st.column_config.TextColumn("Delivery temp", width="medium"),
            "remarks": st.column_config.TextColumn("Remarks", width="medium"),
        },
        disabled=["sr_no"],
    )
    st.session_state["delivery_note_rows"] = edited_delivery_rows

    if st.button("Generate Buffet Menu + Delivery Note", type="primary", disabled=(len(selected_menu) == 0), key="gen_buffet"):
        dishes = _resolve_selected_dishes(selected_menu, df, db)

        if not dishes:
            st.error("Could not resolve selected dishes from the database. Please reload the page and try again.")
        else:
            out_path = OUT_DIR / menu_filename
            delivery_out_path = OUT_DIR / delivery_filename
            generate_buffet_menu_pdf(
                dishes=dishes,
                out_pdf_path=out_path,
                assets=assets,
                title=menu_title.strip() or "Layla Buffet Menu",
                subtitle=menu_subtitle.strip() or "Nutriments and Macronutrients",
                menu_date=menu_date.strftime("%d %b %Y"),
            )
            generate_delivery_note_pdf(
                rows=_delivery_note_rows_from_df(edited_delivery_rows),
                out_pdf_path=delivery_out_path,
                assets=assets,
                client_name=delivery_client.strip() or "Microsoft",
                location=delivery_location.strip(),
                reference=delivery_reference.strip(),
                revision=delivery_revision.strip(),
                issue_date=delivery_issue_date.strftime("%d/%m/%Y"),
                issue_no=delivery_issue_no.strip(),
            )
            st.session_state["generated_buffet_pdf_bytes"] = out_path.read_bytes()
            st.session_state["generated_buffet_pdf_name"] = out_path.name
            st.session_state["generated_delivery_note_pdf_bytes"] = delivery_out_path.read_bytes()
            st.session_state["generated_delivery_note_pdf_name"] = delivery_out_path.name
            st.success("Buffet menu and delivery note PDFs generated.")

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
    delivery_note_bytes = st.session_state.get("generated_delivery_note_pdf_bytes")
    delivery_note_name = st.session_state.get("generated_delivery_note_pdf_name", "layla_delivery_note.pdf")
    if delivery_note_bytes:
        st.download_button(
            "Download Delivery Note PDF",
            data=delivery_note_bytes,
            file_name=delivery_note_name,
            mime="application/pdf",
            use_container_width=True,
            key="download_delivery_note_pdf",
        )

if active_workspace == "Dish Database":
    st.subheader(f"Dish Database ({'Firebase-backed' if dishes_backend == 'firebase' else 'CSV-backed'})")
    st.caption("Edit here, then click Save.")
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True)

    if st.button("Save", type="primary"):
        _save_dishes(edited, dishes_backend)
        st.success("Saved. Reloading…")
        st.rerun()

if active_workspace == "Add Dish (Auto-fill)":
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

    if gemini_configured():
        st.caption("Auto-fill is enabled via Gemini (`GEMINI_API_KEY` + `GEMINI_MODEL`).")
    elif openai_configured():
        st.caption("Auto-fill is enabled via OpenAI (`OPENAI_API_KEY` + `OPENAI_MODEL`).")
    else:
        st.caption(
            "Auto-fill is not configured. Set `GEMINI_API_KEY` + `GEMINI_MODEL`, or `OPENAI_API_KEY` + `OPENAI_MODEL`."
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
                value=gemini_configured() or openai_configured(),
                help="Requires Gemini or OpenAI credentials.",
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
                    added, updated, skipped, errors, _ = _save_candidate_rows(
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

st.markdown(
    """
    <style>
    .idea-card {
        border: 1px solid rgba(49, 51, 63, 0.18);
        border-radius: 18px;
        padding: 1rem 1rem 0.9rem;
        margin-bottom: 1rem;
        background:
            linear-gradient(180deg, rgba(250, 246, 239, 0.95), rgba(255, 255, 255, 0.98));
        box-shadow: 0 10px 30px rgba(15, 23, 42, 0.06);
        height: 320px;
        display: flex;
        flex-direction: column;
        overflow: hidden;
    }
    .idea-kicker {
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #a15c38;
        margin-bottom: 0.45rem;
    }
    .idea-title {
        font-size: 1.15rem;
        font-weight: 700;
        color: #2c211b;
        margin-bottom: 0.45rem;
        line-height: 1.25;
        min-height: 2.9rem;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .idea-summary {
        font-size: 0.92rem;
        color: #4f4037;
        margin-bottom: 0.85rem;
        line-height: 1.45;
        min-height: 4rem;
        display: -webkit-box;
        -webkit-line-clamp: 3;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .idea-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem;
        margin-bottom: 0.8rem;
    }
    .idea-tag {
        display: inline-block;
        border-radius: 999px;
        padding: 0.2rem 0.55rem;
        background: rgba(161, 92, 56, 0.12);
        color: #7b452b;
        font-size: 0.74rem;
        font-weight: 600;
    }
    .idea-prompt {
        font-size: 0.85rem;
        color: #5d5148;
        line-height: 1.45;
        display: -webkit-box;
        -webkit-line-clamp: 6;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    .idea-card-body {
        display: flex;
        flex-direction: column;
        height: 100%;
    }
    .idea-card-spacer {
        flex: 1 1 auto;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if active_workspace == "Idea Center":
    st.subheader("Idea Center")
    st.caption(
        "Curated prompt boards for common catering themes. Click a board to open AI Recipe Studio with the prompt pre-filled and generation started automatically."
    )

    st.markdown(
        "Browse the boards here, then continue in `AI Recipe Studio` to edit the prompt, review the generated drafts, and approve the best options."
    )

    board_columns = st.columns(3)
    for index, preset in enumerate(IDEA_PRESETS):
        column = board_columns[index % 3]
        with column:
            tags_html = "".join(f"<span class='idea-tag'>{tag}</span>" for tag in preset.tags)
            st.markdown(
                f"""
                <div class="idea-card">
                    <div class="idea-card-body">
                        <div class="idea-kicker">{preset.audience}</div>
                        <div class="idea-title">{preset.title}</div>
                        <div class="idea-summary">{preset.summary}</div>
                        <div class="idea-tags">{tags_html}</div>
                        <div class="idea-card-spacer"></div>
                        <div class="idea-prompt">{preset.prompt}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button("Generate Now", key=f"idea_center_generate_{preset.preset_id}", use_container_width=True):
                _apply_idea_preset_to_state(preset)
                st.session_state["ai_recipe_autorun_request"] = {
                    "title": preset.title,
                    "brief": preset.prompt,
                    "count": int(preset.count),
                    "protein_type": preset.protein_type,
                    "gluten": preset.gluten,
                    "dairy": preset.dairy,
                }
                st.session_state["workspace_selector"] = "AI Recipe Studio"
                st.session_state["active_workspace"] = "AI Recipe Studio"
                st.rerun()

if active_workspace == "AI Recipe Studio":
    st.subheader("AI Recipe Studio")
    st.caption(
        "Generate dish ideas, recipe drafts, and review-ready nutriment metadata with validation and scoring."
    )

    ai_drafts = _load_recipe_drafts()
  

    if gemini_configured():
        st.caption("Generation is enabled via Gemini.")
    elif openai_configured():
        st.caption("Generation is enabled via OpenAI.")
    else:
        st.warning("AI generation is not configured. Set Gemini or OpenAI credentials.")

    pending_autorun = st.session_state.get("ai_recipe_autorun_request")
    if isinstance(pending_autorun, dict):
        try:
            generation_request = GenerationRequest(
                brief=str(pending_autorun.get("brief", "")),
                count=int(pending_autorun.get("count", 5) or 5),
                protein_type=pending_autorun.get("protein_type"),
                gluten=pending_autorun.get("gluten"),
                dairy=pending_autorun.get("dairy"),
            )
            with st.spinner(f"Generating drafts for {pending_autorun.get('title', 'Idea Center preset')}..."):
                new_drafts = _generate_recipe_drafts_for_request(
                    generation_request,
                    ai_drafts,
                    list(df["name_en"].astype(str).tolist()),
                )
            st.session_state.pop("ai_recipe_autorun_request", None)
            st.success(f"Generated {len(new_drafts)} draft(s) from {pending_autorun.get('title', 'Idea Center')}.")
            st.rerun()
        except Exception as e:
            st.session_state.pop("ai_recipe_autorun_request", None)
            st.error(f"Draft generation failed: {e}")

    with st.form("ai_recipe_studio_form"):
        brief = st.text_area(
            "Generation brief",
            value=str(st.session_state.get("ai_recipe_brief", "")),
            placeholder="Mediterranean high-protein breakfast buffet with elegant grab-and-go options",
            height=120,
        )
        col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
        with col1:
            candidate_count = st.number_input(
                "Candidate count",
                min_value=1,
                max_value=10,
                value=int(st.session_state.get("ai_recipe_count", 5)),
                step=1,
            )
        with col2:
            protein_options = ["Any", "veg", "meat"]
            protein_default = str(st.session_state.get("ai_recipe_protein_filter", "Any"))
            protein_filter = st.selectbox(
                "protein_type",
                options=protein_options,
                index=protein_options.index(protein_default) if protein_default in protein_options else 0,
            )
        with col3:
            gluten_options = ["Any", "gluten_free", "gluten"]
            gluten_default = str(st.session_state.get("ai_recipe_gluten_filter", "Any"))
            gluten_filter = st.selectbox(
                "gluten",
                options=gluten_options,
                index=gluten_options.index(gluten_default) if gluten_default in gluten_options else 0,
            )
        with col4:
            dairy_options = ["Any", "dairy_free", "dairy"]
            dairy_default = str(st.session_state.get("ai_recipe_dairy_filter", "Any"))
            dairy_filter = st.selectbox(
                "dairy",
                options=dairy_options,
                index=dairy_options.index(dairy_default) if dairy_default in dairy_options else 0,
            )
        generate_drafts_submit = st.form_submit_button("Generate Drafts", type="primary")

    if generate_drafts_submit:
        st.session_state["ai_recipe_brief"] = brief
        st.session_state["ai_recipe_count"] = int(candidate_count)
        st.session_state["ai_recipe_protein_filter"] = protein_filter
        st.session_state["ai_recipe_gluten_filter"] = gluten_filter
        st.session_state["ai_recipe_dairy_filter"] = dairy_filter
        try:
            generation_request = GenerationRequest(
                brief=brief,
                count=int(candidate_count),
                protein_type=_coerce_optional_filter(protein_filter),
                gluten=_coerce_optional_filter(gluten_filter),
                dairy=_coerce_optional_filter(dairy_filter),
            )
            with st.spinner("Generating AI recipe drafts..."):
                new_drafts = _generate_recipe_drafts_for_request(
                    generation_request,
                    ai_drafts,
                    list(df["name_en"].astype(str).tolist()),
                )
            st.success(f"Generated {len(new_drafts)} draft(s).")
            st.rerun()
        except Exception as e:
            st.error(f"Draft generation failed: {e}")

    st.markdown("### Review Queue")
    editable_drafts = [draft for draft in ai_drafts if draft.status not in {"approved", "rejected", "superseded"}]
    archived_drafts = [draft for draft in ai_drafts if draft.status in {"approved", "rejected", "superseded"}]
    status_counts = {}
    for draft in ai_drafts:
        status_counts[draft.status] = status_counts.get(draft.status, 0) + 1
    if status_counts:
        st.caption(
            ", ".join(f"{status}: {count}" for status, count in sorted(status_counts.items()))
        )

    overwrite_generated = st.checkbox("Overwrite existing dishes on approve", value=False, key="ai_recipe_overwrite")

    if editable_drafts:
        review_rows: list[dict[str, object]] = []
        for draft in editable_drafts:
            review_rows.append(
                {
                    "select": False,
                    "draft_id": draft.draft_id,
                    "status": _review_status_for_draft(draft),
                    "score": float(draft.evaluation.overall_score),
                    "attempts": int(draft.attempts),
                    "name_en": draft.dish.get("name_en", ""),
                    "name_ar": draft.dish.get("name_ar", ""),
                    "calories_kcal": float(draft.dish.get("calories_kcal", 0) or 0),
                    "carbs_g": float(draft.dish.get("carbs_g", 0) or 0),
                    "protein_g": float(draft.dish.get("protein_g", 0) or 0),
                    "fat_g": float(draft.dish.get("fat_g", 0) or 0),
                    "gluten": draft.dish.get("gluten", "gluten_free"),
                    "protein_type": draft.dish.get("protein_type", "veg"),
                    "dairy": draft.dish.get("dairy", "dairy_free"),
                }
            )

        edited_review = st.data_editor(
            pd.DataFrame(review_rows),
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            key="ai_recipe_review_editor",
            column_config={
                "select": st.column_config.CheckboxColumn("select"),
                "draft_id": st.column_config.TextColumn("draft_id"),
                "status": st.column_config.TextColumn("status"),
                "score": st.column_config.NumberColumn("score", format="%.2f"),
                "attempts": st.column_config.NumberColumn("attempts", format="%d"),
                "gluten": st.column_config.SelectboxColumn("gluten", options=["gluten_free", "gluten"]),
                "protein_type": st.column_config.SelectboxColumn("protein_type", options=["veg", "meat"]),
                "dairy": st.column_config.SelectboxColumn("dairy", options=["dairy_free", "dairy"]),
                "calories_kcal": st.column_config.NumberColumn("calories_kcal", min_value=0.0, step=1.0),
                "carbs_g": st.column_config.NumberColumn("carbs_g", min_value=0.0, step=0.5),
                "protein_g": st.column_config.NumberColumn("protein_g", min_value=0.0, step=0.5),
                "fat_g": st.column_config.NumberColumn("fat_g", min_value=0.0, step=0.5),
            },
            disabled=["draft_id", "status", "score", "attempts"],
        )
        live_drafts = _sync_editor_rows_to_drafts(
            ai_drafts,
            edited_review,
            list(df["name_en"].astype(str).tolist()),
        )

        c1, c2, c3 = st.columns([1, 1, 1])
        with c1:
            if st.button("Save Review Edits", key="ai_recipe_save_edits"):
                _save_recipe_drafts(live_drafts)
                st.success("Draft edits saved.")
                st.rerun()
        with c2:
            if st.button("Approve Selected", type="primary", key="ai_recipe_approve_selected"):
                selected_ids = [
                    str(row.get("draft_id", "")).strip()
                    for _, row in edited_review.iterrows()
                    if bool(row.get("select", False))
                ]
                selected_drafts = [draft for draft in live_drafts if draft.draft_id in set(selected_ids)]
                invalid_drafts = [draft.dish.get("name_en", "") for draft in selected_drafts if not draft.validation.passed]
                records_to_save = [dish_record_from_draft(draft) for draft in selected_drafts if draft.validation.passed]
                if not selected_ids:
                    st.warning("Select at least one draft to approve.")
                elif not records_to_save:
                    st.warning("Selected drafts must pass validation before approval.")
                else:
                    added, updated, skipped, errors, saved_names = _save_candidate_rows(
                        records_to_save,
                        overwrite_if_exists=bool(overwrite_generated),
                        backend=dishes_backend,
                    )
                    if errors:
                        st.error("Some dishes could not be approved:\n" + "\n".join(errors))
                    else:
                        _save_recipe_drafts(live_drafts)
                        promoted_map = {
                            draft.draft_id: str(draft.dish.get("name_en", "")).strip()
                            for draft in selected_drafts
                            if str(draft.dish.get("name_en", "")).strip() in saved_names
                        }
                        approve_drafts(promoted_map.keys(), promoted_names=promoted_map, **_draft_storage_kwargs())
                        if invalid_drafts:
                            st.warning("Skipped invalid drafts: " + ", ".join(invalid_drafts))
                        if skipped:
                            st.warning("Skipped existing drafts: " + "; ".join(skipped))
                        st.success(f"Approved {len(promoted_map)} draft(s). Added {added}, updated {updated}.")
                        st.rerun()
        with c3:
            if st.button("Reject Selected", key="ai_recipe_reject_selected"):
                selected_ids = [
                    str(row.get("draft_id", "")).strip()
                    for _, row in edited_review.iterrows()
                    if bool(row.get("select", False))
                ]
                if not selected_ids:
                    st.warning("Select at least one draft to reject.")
                else:
                    _save_recipe_drafts(live_drafts)
                    reject_drafts(selected_ids, **_draft_storage_kwargs())
                    st.success(f"Rejected {len(selected_ids)} draft(s).")
                    st.rerun()

        st.markdown("### Draft Details")
        detail_drafts = {draft.draft_id: draft for draft in live_drafts}
        for draft in editable_drafts:
            current_draft = detail_drafts.get(draft.draft_id, draft)
            score = float(current_draft.evaluation.overall_score)
            with st.expander(f"{current_draft.dish.get('name_en', 'Untitled')} · {current_draft.status} · score {score:.2f}"):
                st.caption(f"Model: {current_draft.source_model} | Attempts: {current_draft.attempts}")
                recipe = current_draft.recipe
                st.markdown("**Ingredients**")
                for ingredient in recipe.get("ingredients", []) or []:
                    st.write(f"- {ingredient}")
                st.markdown("**Steps**")
                for index, step in enumerate(recipe.get("steps", []) or [], start=1):
                    st.write(f"{index}. {step}")
                st.caption(f"Yield servings: {recipe.get('yield_servings', 0)}")
                if current_draft.validation.errors:
                    st.error("\n".join(current_draft.validation.errors))
                if current_draft.validation.warnings:
                    st.warning("\n".join(current_draft.validation.warnings))
                if current_draft.evaluation.notes:
                    st.info("\n".join(current_draft.evaluation.notes))

                action_a, action_b, action_c = st.columns([1, 1, 1])
                with action_a:
                    if st.button("Approve This Draft", key=f"ai_recipe_approve_{current_draft.draft_id}"):
                        if not current_draft.validation.passed:
                            st.warning("This draft still has validation errors.")
                        else:
                            _save_recipe_drafts(live_drafts)
                            added, updated, skipped, errors, saved_names = _save_candidate_rows(
                                [dish_record_from_draft(current_draft)],
                                overwrite_if_exists=bool(overwrite_generated),
                                backend=dishes_backend,
                            )
                            if errors:
                                st.error("Approval failed:\n" + "\n".join(errors))
                            elif saved_names:
                                approve_drafts(
                                    [current_draft.draft_id],
                                    promoted_names={current_draft.draft_id: saved_names[0]},
                                    **_draft_storage_kwargs(),
                                )
                                if skipped:
                                    st.warning("Skipped: " + "; ".join(skipped))
                                st.success(f"Approved draft. Added {added}, updated {updated}.")
                                st.rerun()
                            else:
                                st.warning("Draft was not approved.")
                with action_b:
                    if st.button("Reject This Draft", key=f"ai_recipe_reject_{current_draft.draft_id}"):
                        _save_recipe_drafts(live_drafts)
                        reject_drafts([current_draft.draft_id], **_draft_storage_kwargs())
                        st.success("Draft rejected.")
                        st.rerun()
                with action_c:
                    if st.button("Regenerate", key=f"ai_recipe_regenerate_{current_draft.draft_id}"):
                        try:
                            _save_recipe_drafts(live_drafts)
                            regenerated = generate_dish_drafts(
                                request_from_draft(current_draft),
                                existing_dish_names=list(df["name_en"].astype(str).tolist()),
                            )
                            reject_drafts([current_draft.draft_id], status="superseded", **_draft_storage_kwargs())
                            _save_recipe_drafts(_replace_draft_in_list(_load_recipe_drafts(), regenerated))
                            st.success("Draft regenerated.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Regeneration failed: {e}")
    else:
        st.info("No active drafts yet. Generate a batch from the brief above.")

    if archived_drafts:
        with st.expander("Show approved / rejected history"):
            for draft in archived_drafts:
                st.write(
                    f"{draft.created_at} | {draft.status} | {draft.dish.get('name_en', '')} | "
                    f"approved as: {draft.approved_dish_name or '-'}"
                )

if layout_tuner_visible and active_workspace == "Layout Tuner":
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
