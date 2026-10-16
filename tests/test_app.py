import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient  # noqa: E402

import ocr  # noqa: E402
import reference  # noqa: E402
import server  # noqa: E402

REF = reference.load_reference()
LINE = "01/09 4401 Qasr Pkg Cartons L2609-001 20 pcs B.H."


def row(*items, score=None):
    return [{"text": t, "cx": i * 100, "cy": 0, "h": 40, "score": score} for i, t in enumerate(items)]


def words(line, y=100):
    return [{"text": t, "x": x * 90, "y": y, "w": 80, "h": 40} for x, t in enumerate(line.split())]


# ---------------- OCR row building
def test_row_parsing_follows_format_rules():
    p = ocr.parse_row(row("04/09", "4405", "Saahil", "Pallets", "Wooden", "pallets", "L2609-005", "120", "pcs", "S.A."))
    assert (p["grn_no"], p["supplier"], p["item"], p["lot"], p["qty"], p["unit"]) == \
        ("4405", "Saahil Pallets", "Wooden pallets", "L2609-005", "120", "pcs") and p["flags"] == []


def test_bad_values_are_flagged_never_repaired():
    p = ocr.parse_row(row("04/09", "4405", "Saahil", "Pallets", "Wooden", "pallets", "12609-005", "1Z0", "pcs", "S.A."))
    assert p["lot"] == "12609-005" and "lot_illegible" in p["flags"]
    assert p["qty"] is None and "qty_illegible" in p["flags"]


def test_low_confidence_and_leading_zero_are_flagged():
    p = ocr.parse_row(row("04/09", "4405", "Saahil", "Pallets", "Wooden", "pallets", "L2609-005", "120", "pcs", "S.A.", score=0.5))
    assert {"date_illegible", "grn_illegible", "lot_illegible", "qty_illegible"} <= set(p["flags"])
    assert "qty_illegible" in ocr.parse_row(row("04/09", "4405", "Qasr", "Pkg", "Cartons", "L2609-005", "08", "pcs", "S.A."))["flags"]


def test_sequence_break_is_flagged():
    rows, _ = ocr.to_rows(words("01/09 4401 Qasr Pkg Cartons L2609-001 20 pcs B.H.", 100) + words("01/09 9902 Qasr Pkg Cartons L2609-002 30 pcs B.H.", 200))
    assert "grn_illegible" in rows[1]["flags"]


def test_ocr_failure_gives_manual_entry_and_cache_can_be_bypassed(monkeypatch, tmp_path):
    img = Path("data/t4_register_page1.jpg").read_bytes()
    calls = {"n": 0}

    def live(im):
        calls["n"] += 1
        return words(LINE)
    monkeypatch.setattr(ocr, "CACHE", tmp_path)
    monkeypatch.setattr(ocr, "rapid_words", live)
    ocr.read_page(img); ocr.read_page(img)
    assert calls["n"] == 1                                             # second read came from the cache
    ocr.read_page(img, use_cache=False)
    assert calls["n"] == 2                                             # bypass makes a live read
    r = ocr.read_page(img, simulate=True)
    assert r["engine"] == "manual" and r["rows"] == [] and r["log"][0]["ok"] is False


# ---------------- reference correction
def ocr_like(page_no=0, edits=(), drop=()):
    rows = copy.deepcopy(REF[page_no]["rows"])
    for g, f, v in edits:
        next(r for r in rows if r["grn_no"] == g)[f] = v
    return [r for r in rows if r["grn_no"] not in drop]


def test_wrong_cells_are_corrected_and_missing_rows_filled():
    rows, s = reference.correct_page(ocr_like(1, edits=[("4416", "qty", "2700"), ("4420", "lot", "L2609-12O")], drop=["4423"]), REF)
    assert s["applied"] and s["corrected"] == 2 and s["filled"] == 1 and s["exact"] == 13 * 8 - 2
    by = {r["grn_no"]: r for r in rows}
    assert by["4416"]["qty"] == "2200" and by["4416"]["corrections"][0] == {"field": "qty", "ocr": "2700", "reference": "2200"}
    assert by["4423"]["source"] == "filled" and len(rows) == 14


def test_reference_keeps_its_own_doubts_instead_of_inventing_values():
    rows, _ = reference.correct_page(ocr_like(1, edits=[("4428", "qty", "2")]), REF)
    r = next(x for x in rows if x["grn_no"] == "4428")
    assert r["qty"] is None and "qty_illegible" in r["flags"]           # the ink blot stays unreadable


def test_misread_delivery_number_is_matched_by_position():
    rows, s = reference.correct_page(ocr_like(0, edits=[("4408", "grn_no", "9908")]), REF)
    assert s["filled"] == 0 and next(r for r in rows if r["row"] == 8)["grn_no"] == "4408"


def test_page_that_does_not_match_the_reference_is_left_alone():
    other = [{"date": "01/10", "grn_no": str(7000 + i), "supplier": "X", "item": "Y", "lot": "L", "qty": "1", "unit": "pcs",
              "recd_by": "A.B.", "flags": [], "note": None} for i in range(10)]
    rows, s = reference.correct_page(copy.deepcopy(other), REF)
    assert s["applied"] is False and rows == other


def test_read_endpoint_applies_reference_and_reports_it(monkeypatch):
    bad = ocr_like(0, edits=[("4402", "qty", "2500")], drop=["4410"])
    monkeypatch.setattr(ocr, "read_page", lambda data, simulate=False, use_cache=True: {"engine": "rapidocr", "rows": bad, "month": "2026-09", "log": []})
    c = TestClient(server.app)
    img = Path("data/t4_register_page1.jpg").read_bytes()
    on = c.post("/api/read", files=[("files", ("p1.jpg", img, "image/jpeg"))], data={"reference": "true"}).json()
    assert on["pages"][0]["reference"]["corrected"] == 1 and on["pages"][0]["reference"]["filled"] == 1 and len(on["pages"][0]["rows"]) == 14
    off = c.post("/api/read", files=[("files", ("p1.jpg", img, "image/jpeg"))], data={"reference": "false"}).json()
    assert off["pages"][0]["reference"] is None and len(off["pages"][0]["rows"]) == 13


# ---------------- whole flow
def test_sample_flow_numbers():
    c = TestClient(server.app)
    doc = c.post("/api/read", data={"sample": "true"}).json()
    assert sum(len(p["rows"]) for p in doc["pages"]) == 28
    out = c.post("/api/compare", data={"register": json.dumps(doc), "sample": "true"}).json()
    assert out["ok"] and out["total"] == 86 and out["counts"] == {"A": 21, "B": 6, "C": 4}
    assert [(l["rows"], l["A"], l["B"], l["C"]) for l in out["lines"]] == [(28, 21, 6, 1), (28, 21, 6, 1), (30, 21, 6, 3)]
    assert c.get("/api/ledger.csv").text.count("\n") == 87


def test_missing_files_and_month_give_clear_errors():
    c = TestClient(server.app)
    assert c.post("/api/compare", data={"register": '{"pages":[]}'}).status_code == 400
    assert c.post("/api/compare", data={"register": '{"month":"2026-09","pages":[]}'}).status_code == 400
