"""Photo of a register page -> rows, using RapidOCR (local).

Rule: a cell that is unclear, low-confidence or fails its column format is flagged, never repaired.
"""
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

import cv2
import numpy as np

import rules

CACHE = Path("ocr_cache")
MIN_CONF = float(os.getenv("MIN_CONF", "0.80"))      # RapidOCR word score below this = unclear
HEADER_WORDS = {"goods", "receiving", "register", "month:", "month", "page", "no.", "date", "grn", "supplier",
                "item", "lot", "qty", "unit", "recd", "by", "/", "description"}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


class Unavailable(Exception):
    pass


# ------------------------------------------------------------ image
def load(data):
    im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if im is None:
        raise ValueError("this file is not a readable image")
    edges = cv2.Canny(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 720, 200, minLineLength=im.shape[1] // 3, maxLineGap=20)
    ang = [np.degrees(np.arctan2(y2 - y1, x2 - x1)) for x1, y1, x2, y2 in (lines[:, 0] if lines is not None else [])
           if abs(y2 - y1) < abs(x2 - x1)]
    a = float(np.median(ang)) if ang else 0.0
    if 0.3 < abs(a) < 15:                      # straighten a tilted photo
        h, w = im.shape[:2]
        im = cv2.warpAffine(im, cv2.getRotationMatrix2D((w / 2, h / 2), a, 1), (w, h), borderValue=(255, 255, 255))
    return im


# ------------------------------------------------------------ engine: image -> words {text,x,y,w,h,score}
_RAPID = None


def rapid_words(im):
    global _RAPID
    try:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            from rapidocr import RapidOCR
    except ImportError:
        raise Unavailable("RapidOCR is not installed")
    _RAPID = _RAPID or RapidOCR()
    out = _RAPID(im)
    if isinstance(out, tuple):
        items = [(b, t, float(sc)) for b, t, sc in (out[0] or [])]
    else:
        items = list(zip(out.boxes, out.txts, out.scores)) if getattr(out, "boxes", None) is not None else []
    if not items:
        raise RuntimeError("RapidOCR found no text")
    return [{"text": t, "x": min(p[0] for p in b), "y": min(p[1] for p in b),
             "w": max(p[0] for p in b) - min(p[0] for p in b), "h": max(p[1] for p in b) - min(p[1] for p in b),
             "score": float(sc)} for b, t, sc in items]


def status():
    return {"rapidocr": importlib.util.find_spec("rapidocr_onnxruntime") is not None
            or importlib.util.find_spec("rapidocr") is not None}


# ------------------------------------------------------------ words -> rows
def tokens(words):
    out = []
    for w in words:
        n, pos = max(len(w["text"]), 1), 0
        for p in w["text"].split():
            i = w["text"].index(p, pos)
            pos = i + len(p)
            out.append({"text": p, "cx": w["x"] + w["w"] * (i + len(p) / 2) / n, "cy": w["y"] + w["h"] / 2,
                        "h": w["h"], "score": w.get("score")})
    return out


def group_rows(toks):
    toks = sorted(toks, key=lambda t: t["cy"])
    gap = 0.6 * sorted(t["h"] for t in toks)[len(toks) // 2]
    rows = [[toks[0]]]
    for t in toks[1:]:
        if t["cy"] - rows[-1][-1]["cy"] <= gap:
            rows[-1].append(t)
        else:
            rows.append([t])
    return [sorted(r, key=lambda t: t["cx"]) for r in rows]


def lotlike(s):
    return "-" in s and sum(c.isdigit() for c in s) >= 5


def parse_row(r):
    t = [x["text"] for x in r]
    low = lambda i: i is not None and r[i]["score"] is not None and r[i]["score"] < MIN_CONF
    fl, note = [], []
    d = next((i for i, s in enumerate(t) if re.fullmatch(r"\d{1,2}/\d{1,2}", s)), None)
    g = next((i for i, s in enumerate(t) if i != d and re.fullmatch(r"\d{4}", s)), None)
    rest = list(range((g if g is not None else d if d is not None else -1) + 1, len(t)))
    by = unit = qi = lot = None
    if rest and re.fullmatch(r"[A-Za-z]\.[A-Za-z]\.?", t[rest[-1]]):
        by = rest.pop()
    if rest and t[rest[-1]].lower().strip(".") in rules.UOM_MAP:
        unit = rest.pop()
    if rest and not lotlike(t[rest[-1]]):
        qi = rest.pop()
    if rest and lotlike(t[rest[-1]]):
        lot = rest.pop()
    mid = [t[i] for i in rest]
    k = next((i for i, s in enumerate(mid) if rules.supplier_code(s)), None)
    supplier = " ".join(mid[k:k + 2]) if k is not None else None
    item = " ".join(mid[k + 2:] if k is not None else mid) or None

    row = {"date": t[d] if d is not None else None, "grn_no": t[g] if g is not None else None,
           "supplier": supplier, "item": item, "lot": t[lot] if lot is not None else None,
           "qty": None, "unit": t[unit] if unit is not None else None, "recd_by": t[by] if by is not None else None}
    if row["date"] is None or low(d):
        fl.append("date_illegible")
    if row["grn_no"] is None or low(g):
        fl.append("grn_illegible")
    if row["lot"] is None or low(lot) or not re.fullmatch(r"L\d{4}-\d{3}", row["lot"]):
        fl.append("lot_illegible")
        note.append(f"lot read as '{row['lot']}'" if row["lot"] else "lot not found")
    qraw = t[qi] if qi is not None else None
    if qraw is not None and re.fullmatch(r"\d+", qraw.replace(",", "")):
        row["qty"] = qraw.replace(",", "")
        if low(qi) or row["qty"].startswith("0"):    # a quantity starting with 0 is a misread
            fl.append("qty_illegible")
    else:
        fl.append("qty_illegible")
        note.append(f"qty read as '{qraw}'" if qraw else "qty not found")
    if row["unit"] is None:
        fl.append("unit_unknown")
    row["flags"], row["note"] = sorted(set(fl)), "; ".join(note) or None
    return row


def month_of(rows_tokens):
    flat = [x["text"].lower().strip(":") for r in rows_tokens for x in r]
    for i, w in enumerate(flat):
        if w[:3] in MONTHS and len(w) <= 4:
            y = next((v for v in flat[i + 1:i + 3] if re.fullmatch(r"20\d\d", v)), None)
            if y:
                return f"{y}-{MONTHS.index(w[:3]) + 1:02d}"
    return ""


def to_rows(words):
    grouped = group_rows(tokens(words))
    month, rows = month_of(grouped), []
    for r in grouped:
        texts = {x["text"].lower() for x in r}
        if texts & HEADER_WORDS:
            continue
        p = parse_row(r)
        if p["date"] or p["grn_no"] or (p["lot"] and lotlike(p["lot"])):
            rows.append(p)
    prev = None
    for p in rows:                                     # a break in the number sequence is flagged, not fixed
        n = int(p["grn_no"]) if p["grn_no"] else None
        if n and prev and n != prev + 1 and "grn_illegible" not in p["flags"]:
            p["flags"] = sorted(set(p["flags"]) | {"grn_illegible"})
            p["note"] = "; ".join(filter(None, [p["note"], f"number {n} does not follow {prev}"]))
        prev = n or prev
    for i, p in enumerate(rows, 1):
        p["row"] = i
    return rows, month


# ------------------------------------------------------------ one page, with fallback
def cached(engine, data, fn, use_cache=True):
    path = CACHE / f"{hashlib.sha256(data).hexdigest()}.{engine}.json"
    if use_cache and path.exists():
        return json.loads(path.read_text()), True
    words = fn()
    CACHE.mkdir(exist_ok=True)
    path.write_text(json.dumps(words))
    return words, False


def read_page(data, simulate=False, use_cache=True):
    """Read one page with RapidOCR. If it fails, the page comes back empty (manual entry)."""
    im, log = load(data), []
    try:
        if simulate:
            raise RuntimeError("simulated failure (demo option)")
        words, hit = cached("rapidocr", data, lambda: rapid_words(im), use_cache)
        rows, month = to_rows(words)
        if not rows:
            raise RuntimeError("no table rows found in the text")
    except Exception as e:
        log.append({"engine": "rapidocr", "ok": False, "why": str(e)[:160]})
        return {"engine": "manual", "rows": [], "month": "", "log": log}
    unclear = sum(bool(r["flags"]) for r in rows) / len(rows)
    log.append({"engine": "rapidocr", "ok": True, "rows": len(rows), "unclear_rows": round(unclear, 2),
                "why": "saved reading (no live call)" if hit else "read live"})
    return {"engine": "rapidocr", "rows": rows, "month": month, "log": log}
