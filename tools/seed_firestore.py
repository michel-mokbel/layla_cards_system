#!/usr/bin/env python3
"""
Seed Firestore dishes collection from a CSV file.

Default behavior is safe: upsert rows from CSV and keep existing docs.
Use --replace to fully mirror Firestore to CSV (deletes missing docs).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict

import pandas as pd

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore


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
FIRESTORE_BATCH_LIMIT = 450


def _detect_local_service_account_path() -> Path | None:
    base_dir = Path(__file__).resolve().parent.parent
    candidates = sorted(base_dir.glob("*firebase-adminsdk*.json")) + sorted(
        base_dir.glob("*service-account*.json")
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _to_float(value: object) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).replace("\ufeff", "").strip() for c in out.columns]
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
    return out[out["name_en"] != ""].copy()


def _doc_id_from_name(name_en: str) -> str:
    base = "".join(ch for ch in name_en.lower().strip() if ch.isalnum() or ch in {"-", "_", " "})
    base = "-".join(base.split())
    return base or "dish"


def _record_from_row(row: pd.Series) -> Dict[str, object]:
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


def _load_existing_docs(coll: firestore.CollectionReference) -> list[tuple[str, Dict[str, object]]]:
    docs: list[tuple[str, Dict[str, object]]] = []
    for doc in coll.stream():
        data = doc.to_dict() or {}
        if isinstance(data, dict):
            docs.append((doc.id, data))
    return docs


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _build_records(
    df: pd.DataFrame,
    existing_docs: list[tuple[str, Dict[str, object]]],
) -> Dict[str, Dict[str, object]]:
    records: Dict[str, Dict[str, object]] = {}
    existing_by_name: Dict[str, list[str]] = {}
    existing_ids = {doc_id for doc_id, _ in existing_docs}
    for doc_id, data in existing_docs:
        name_key = str(data.get("name_en", "")).strip().lower()
        if not name_key:
            continue
        existing_by_name.setdefault(name_key, []).append(doc_id)

    used_doc_ids: set[str] = set()
    for _, row in df.iterrows():
        rec = _record_from_row(row)
        name_key = str(rec["name_en"]).strip().lower()
        doc_id = ""

        for candidate in existing_by_name.get(name_key, []):
            if candidate not in used_doc_ids:
                doc_id = candidate
                break

        if not doc_id:
            base = _doc_id_from_name(str(rec["name_en"]))
            doc_id = base
            suffix = 2
            while doc_id in used_doc_ids or doc_id in existing_ids:
                doc_id = f"{base}-{suffix}"
                suffix += 1

        used_doc_ids.add(doc_id)
        records[doc_id] = rec
    return records


def _commit_sync(
    client: firestore.Client,
    coll: firestore.CollectionReference,
    records: Dict[str, Dict[str, object]],
    *,
    delete_ids: set[str],
) -> None:
    for batch_ids in _chunked(list(records.keys()), FIRESTORE_BATCH_LIMIT):
        batch = client.batch()
        for doc_id in batch_ids:
            batch.set(coll.document(doc_id), records[doc_id])
        batch.commit()

    for batch_ids in _chunked(list(delete_ids), FIRESTORE_BATCH_LIMIT):
        batch = client.batch()
        for doc_id in batch_ids:
            batch.delete(coll.document(doc_id))
        batch.commit()


def _init_firestore(
    service_account_json: str | None,
    service_account_path: str | None,
    project_id: str | None,
) -> firestore.Client:
    cred = None

    if service_account_json:
        obj = json.loads(service_account_json)
        cred = credentials.Certificate(obj)
    elif service_account_path:
        cred = credentials.Certificate(service_account_path)
    elif _detect_local_service_account_path():
        cred = credentials.Certificate(str(_detect_local_service_account_path()))
    elif os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        cred = credentials.ApplicationDefault()
    else:
        raise RuntimeError(
            "Missing credentials. Provide --service-account-json, --service-account-path, "
            "or set GOOGLE_APPLICATION_CREDENTIALS."
        )

    if not firebase_admin._apps:
        options = {"projectId": project_id} if project_id else None
        firebase_admin.initialize_app(cred, options=options)
    return firestore.client()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="data/dishes.csv", help="Input CSV path")
    parser.add_argument("--collection", default="dishes", help="Firestore collection name")
    parser.add_argument("--project-id", default=os.getenv("FIREBASE_PROJECT_ID"))
    parser.add_argument("--service-account-json", default=os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON"))
    parser.add_argument("--service-account-path", default=os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH"))
    parser.add_argument("--replace", action="store_true", help="Delete Firestore docs not present in CSV")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    normalized = _normalize(df)

    print(f"CSV rows to seed: {len(normalized)}")
    if args.dry_run:
        print("Dry run complete. No writes performed.")
        return

    client = _init_firestore(
        service_account_json=args.service_account_json,
        service_account_path=args.service_account_path,
        project_id=args.project_id,
    )
    coll = client.collection(args.collection)

    existing_docs = _load_existing_docs(coll)
    records = _build_records(normalized, existing_docs)
    existing_ids = {doc_id for doc_id, _ in existing_docs}
    delete_ids = existing_ids - set(records.keys()) if args.replace else set()
    _commit_sync(client, coll, records, delete_ids=delete_ids)

    print(f"Upserted: {len(records)} documents")
    if args.replace:
        print(f"Deleted: {len(delete_ids)} documents")


if __name__ == "__main__":
    main()
