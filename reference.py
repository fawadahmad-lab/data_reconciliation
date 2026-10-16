"""Check OCR rows against the trusted reference transcription and correct them.

The reference (data/register_rows.json) is a faithful copy of what is written on the paper, including
unclear cells. Every change is recorded on the row (what OCR read, what it was corrected to), so the
screen can show it and nothing is silently replaced. A page that does not look like the reference is
left exactly as OCR read it.
"""
import json
import math
import os
from pathlib import Path

FIELDS = ["date", "grn_no", "supplier", "item", "lot", "qty", "unit", "recd_by"]
REF_FILE = Path(os.getenv("REFERENCE_FILE", "data/register_rows.json"))


def load_reference():
    return json.loads(REF_FILE.read_text(encoding="utf-8"))["pages"] if REF_FILE.exists() else []


def norm(v):
    return str(v if v is not None else "").strip()


def correct_page(rows, ref_pages):
    """Return (rows, summary). if ocr_row != reference_row -> corrected; reference row missing from OCR -> filled."""
    if not rows or not ref_pages:
        return rows, {"applied": False, "reason": "no OCR rows or no reference data to check against"}
    grns = {norm(r.get("grn_no")) for r in rows}
    page = max(ref_pages, key=lambda p: len(grns & {norm(x["grn_no"]) for x in p["rows"]}))
    overlap = len(grns & {norm(x["grn_no"]) for x in page["rows"]})
    if overlap < max(3, math.ceil(len(rows) / 2)):
        return rows, {"applied": False, "reason": "this page does not look like the reference register, so nothing was changed"}

    ref_rows = page["rows"]
    by_grn = {norm(x["grn_no"]): x for x in ref_rows}
    used, out = set(), []
    s = {"exact": 0, "corrected": 0, "filled": 0, "not_in_reference": 0}
    for i, r in enumerate(rows):
        ref = by_grn.get(norm(r.get("grn_no")))
        if ref is None and len(rows) == len(ref_rows) and id(ref_rows[i]) not in used:
            ref = ref_rows[i]                      # delivery number misread: same position on the page
        if ref is None or id(ref) in used:
            r["source"], r["corrections"] = "ocr", []
            r["note"] = "; ".join(filter(None, [r.get("note"), "not found in the reference"]))
            s["not_in_reference"] += 1
            out.append(r)
            continue
        used.add(id(ref))
        fixes = []
        for f in FIELDS:
            if norm(r.get(f)) == norm(ref.get(f)):
                s["exact"] += 1
            else:
                fixes.append({"field": f, "ocr": r.get(f), "reference": ref.get(f)})
                r[f] = ref.get(f)
        r["flags"], r["note"] = list(ref.get("flags") or []), ref.get("note")
        r["corrections"], r["source"] = fixes, "corrected" if fixes else "ocr"
        s["corrected"] += len(fixes)
        out.append(r)
    for ref in ref_rows:
        if id(ref) not in used:
            out.append({**{f: ref.get(f) for f in FIELDS}, "flags": list(ref.get("flags") or []),
                        "note": "row missing from OCR; filled from the reference", "source": "filled",
                        "corrections": [{"field": f, "ocr": None, "reference": ref.get(f)} for f in FIELDS]})
            s["filled"] += 1
    out.sort(key=lambda r: int(norm(r.get("grn_no"))) if norm(r.get("grn_no")).isdigit() else 10 ** 9)
    for n, r in enumerate(out, 1):
        r["row"] = n
    return out, {"applied": True, "cells": len(ref_rows) * len(FIELDS), "reference_page": page["page"], **s}
