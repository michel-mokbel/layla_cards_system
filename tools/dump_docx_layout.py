#!/usr/bin/env python3
"""
Dump absolute/anchored layout primitives from a DOCX into JSON.

Extracts:
- page size + margins (section properties)
- drawing anchors/inlines (position, extent, wrap behavior)
- textbox text content
- embedded image relationship ids and resolved targets

Usage:
  python tools/dump_docx_layout.py \
    --docx "/path/to/template.docx" \
    --out "out/docx_layout.json"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import zipfile
from xml.etree import ElementTree as ET


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

EMU_PER_INCH = 914400.0
TWIP_PER_INCH = 1440.0
MM_PER_INCH = 25.4


def emu_to_mm(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return round((float(value) / EMU_PER_INCH) * MM_PER_INCH, 3)
    except Exception:
        return None


def twip_to_mm(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return round((float(value) / TWIP_PER_INCH) * MM_PER_INCH, 3)
    except Exception:
        return None


def _all_text(parent: ET.Element, xpath: str) -> str:
    nodes = parent.findall(xpath, NS)
    chunks = []
    for node in nodes:
        text = (node.text or "").strip()
        if text:
            chunks.append(text)
    return " ".join(chunks)


def _load_relationships(docx: zipfile.ZipFile, rels_path: str) -> dict[str, str]:
    if rels_path not in docx.namelist():
        return {}
    root = ET.fromstring(docx.read(rels_path))
    mapping: dict[str, str] = {}
    for rel in root.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"):
        rel_id = rel.attrib.get("Id", "")
        target = rel.attrib.get("Target", "")
        if rel_id:
            mapping[rel_id] = target
    return mapping


def _find_parts(docx: zipfile.ZipFile) -> list[str]:
    names = set(docx.namelist())
    parts = ["word/document.xml"]
    for name in sorted(names):
        if name.startswith("word/header") and name.endswith(".xml"):
            parts.append(name)
        if name.startswith("word/footer") and name.endswith(".xml"):
            parts.append(name)
    return parts


def _section_props(root: ET.Element) -> list[dict]:
    out: list[dict] = []
    for sect in root.findall(".//w:sectPr", NS):
        pg_sz = sect.find("w:pgSz", NS)
        pg_mar = sect.find("w:pgMar", NS)
        out.append(
            {
                "page_size_twip": {
                    "w": (pg_sz.attrib.get(f"{{{NS['w']}}}w") if pg_sz is not None else None),
                    "h": (pg_sz.attrib.get(f"{{{NS['w']}}}h") if pg_sz is not None else None),
                },
                "page_size_mm": {
                    "w": twip_to_mm(pg_sz.attrib.get(f"{{{NS['w']}}}w") if pg_sz is not None else None),
                    "h": twip_to_mm(pg_sz.attrib.get(f"{{{NS['w']}}}h") if pg_sz is not None else None),
                },
                "margins_twip": {
                    "top": (pg_mar.attrib.get(f"{{{NS['w']}}}top") if pg_mar is not None else None),
                    "right": (pg_mar.attrib.get(f"{{{NS['w']}}}right") if pg_mar is not None else None),
                    "bottom": (pg_mar.attrib.get(f"{{{NS['w']}}}bottom") if pg_mar is not None else None),
                    "left": (pg_mar.attrib.get(f"{{{NS['w']}}}left") if pg_mar is not None else None),
                },
                "margins_mm": {
                    "top": twip_to_mm(pg_mar.attrib.get(f"{{{NS['w']}}}top") if pg_mar is not None else None),
                    "right": twip_to_mm(pg_mar.attrib.get(f"{{{NS['w']}}}right") if pg_mar is not None else None),
                    "bottom": twip_to_mm(pg_mar.attrib.get(f"{{{NS['w']}}}bottom") if pg_mar is not None else None),
                    "left": twip_to_mm(pg_mar.attrib.get(f"{{{NS['w']}}}left") if pg_mar is not None else None),
                },
            }
        )
    return out


def _extract_drawing(drawing: ET.Element, kind: str, rels: dict[str, str]) -> dict:
    anchor = drawing.find(f"wp:{kind}", NS)
    if anchor is None:
        return {}

    extent = anchor.find("wp:extent", NS)
    pos_h = anchor.find("wp:positionH", NS)
    pos_v = anchor.find("wp:positionV", NS)
    offset_h = pos_h.find("wp:posOffset", NS) if pos_h is not None else None
    offset_v = pos_v.find("wp:posOffset", NS) if pos_v is not None else None

    blips = anchor.findall(".//a:blip", NS)
    images = []
    for blip in blips:
        rid = blip.attrib.get(f"{{{NS['r']}}}embed")
        images.append(
            {
                "rel_id": rid,
                "target": rels.get(rid or "", None),
            }
        )

    textbox_text = _all_text(anchor, ".//w:txbxContent//w:t")
    plain_text = _all_text(anchor, ".//w:t")
    doc_pr = anchor.find("wp:docPr", NS)
    wrap_node = None
    for child in anchor:
        if child.tag.startswith(f"{{{NS['wp']}}}wrap"):
            wrap_node = child
            break

    return {
        "kind": kind,
        "doc_pr": {
            "id": (doc_pr.attrib.get("id") if doc_pr is not None else None),
            "name": (doc_pr.attrib.get("name") if doc_pr is not None else None),
            "descr": (doc_pr.attrib.get("descr") if doc_pr is not None else None),
        },
        "extent_emu": {
            "cx": (extent.attrib.get("cx") if extent is not None else None),
            "cy": (extent.attrib.get("cy") if extent is not None else None),
        },
        "extent_mm": {
            "w": emu_to_mm(extent.attrib.get("cx") if extent is not None else None),
            "h": emu_to_mm(extent.attrib.get("cy") if extent is not None else None),
        },
        "position": {
            "horizontal_relative_from": (pos_h.attrib.get("relativeFrom") if pos_h is not None else None),
            "horizontal_offset_emu": (offset_h.text if offset_h is not None else None),
            "horizontal_offset_mm": emu_to_mm(offset_h.text if offset_h is not None else None),
            "vertical_relative_from": (pos_v.attrib.get("relativeFrom") if pos_v is not None else None),
            "vertical_offset_emu": (offset_v.text if offset_v is not None else None),
            "vertical_offset_mm": emu_to_mm(offset_v.text if offset_v is not None else None),
        },
        "wrap": {
            "tag": (wrap_node.tag.rsplit("}", 1)[-1] if wrap_node is not None else None),
            "attrs": (wrap_node.attrib if wrap_node is not None else {}),
        },
        "images": images,
        "text": {
            "textbox_text": textbox_text,
            "all_text_in_shape": plain_text,
        },
        "raw_anchor_attrs": anchor.attrib,
    }


def dump_layout(docx_path: Path) -> dict:
    result = {
        "docx_path": str(docx_path),
        "parts": [],
    }
    with zipfile.ZipFile(docx_path, "r") as docx:
        for part in _find_parts(docx):
            if part not in docx.namelist():
                continue
            root = ET.fromstring(docx.read(part))
            rels_path = part.replace("word/", "word/_rels/") + ".rels"
            rels = _load_relationships(docx, rels_path)
            drawings = []
            for drawing in root.findall(".//w:drawing", NS):
                anchor_data = _extract_drawing(drawing, "anchor", rels)
                if anchor_data:
                    drawings.append(anchor_data)
                inline_data = _extract_drawing(drawing, "inline", rels)
                if inline_data:
                    drawings.append(inline_data)
            result["parts"].append(
                {
                    "part": part,
                    "sections": _section_props(root),
                    "drawings": drawings,
                    "drawing_count": len(drawings),
                }
            )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump DOCX layout coordinates to JSON.")
    parser.add_argument("--docx", required=True, help="Input .docx path")
    parser.add_argument("--out", required=True, help="Output JSON path")
    args = parser.parse_args()

    docx_path = Path(args.docx).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    if not docx_path.exists():
        raise FileNotFoundError(f"DOCX not found: {docx_path}")

    data = dump_layout(docx_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
