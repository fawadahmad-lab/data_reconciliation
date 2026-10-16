import csv
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import reconcile  # noqa: E402

REG, ERP, STORE = ROOT / "data/register_rows.json", ROOT / "data/t4_erp_grn.csv", ROOT / "data/t4_store_sheet.xlsx"


@pytest.fixture(scope="module")
def res():
    return reconcile.run(REG, ERP, STORE)


def case_outcome(res, grn):
    return res.cases[f"GRN-{grn}"]["outcome"]


def test_row_counts_are_hand_verified(res):
    # 28 register rows (2 pages x 14), 28 ERP lines, 30 store rows: counted by eye / wc -l / sheet dimensions
    assert res.input_counts == {"register": 28, "erp": 28, "store": 30}


def test_every_row_lands_in_exactly_one_outcome(res):
    assert len(res.rows) == 86 == len(res.outcome)
    assert all(ok for *_, ok in res.proof)
    assert {r.ref for r in res.rows} == set(res.outcome)


def test_expected_findings(res):
    expect = {4412: "MISMATCH", 4415: "MISMATCH", 4417: "MISMATCH", 4419: "MISMATCH", 4425: "MISMATCH",
              4428: "REVIEW_UNREADABLE", 4421: "MISSING_IN_ERP", 4430: "MISSING_IN_REGISTER_AND_STORE"}
    for grn, oc in expect.items():
        assert case_outcome(res, grn) == oc, grn
    matched = [c for c in res.cases.values() if c["outcome"] == "MATCHED"]
    assert len(matched) == 29 - len(expect) == 21
    assert res.gaps == [4429]


def test_who_is_the_odd_one_out(res):
    odd = {(d["field"], res.cases[f"GRN-{g}"]["case_id"]): d["odd_one_out"]
           for g in (4412, 4415, 4417, 4419, 4425) for d in res.cases[f"GRN-{g}"]["diffs"]}
    assert odd[("lot", "GRN-4412")] == "erp"
    assert odd[("date", "GRN-4415")] == "erp"
    assert odd[("qty_uom", "GRN-4417")] == "erp"
    assert odd[("qty_uom", "GRN-4419")] == "store"
    assert odd[("qty_uom", "GRN-4425")] == "store"


def test_yards_vs_metres_compares_physical_length(res):
    c = res.cases["GRN-4425"]
    assert len(c["diffs"]) == 1 and c["diffs"][0]["field"] == "qty_uom"
    reg = next(r for r in res.rows if r.source == "register" and r.grn == 4425)
    erp = next(r for r in res.rows if r.source == "erp" and r.grn == 4425)
    assert reconcile.key(reg, "qty_uom") == reconcile.key(erp, "qty_uom")  # 1200 yd == 1097.28 m


def test_illegible_quantity_is_never_guessed(res):
    reg = next(r for r in res.rows if r.source == "register" and r.grn == 4428)
    assert reg.qty is None and "qty_uom" in reg.illegible
    assert case_outcome(res, 4428) == "REVIEW_UNREADABLE"
    assert res.cases["GRN-4428"]["diffs"] == []          # not reported as a mismatch either
    assert res.illegible["register"] == 1


def test_duplicate_and_return_rows(res):
    store = {r.ref: res.outcome[r.ref] for r in res.rows if r.source == "store"}
    assert store["store:R11"] == "DUPLICATE_ROW" and store["store:R10"] == "MATCHED"
    assert store["store:R31"] == "ADJUSTMENT_NOT_A_RECEIPT"


def test_text_date_in_store_sheet_is_read_as_month_day_year(res):
    r = next(r for r in res.rows if r.source == "store" and r.grn == 4416)
    assert str(r.date) == "2026-09-14" and any("M/D/Y" in n for n in r.notes)


def test_late_erp_entry_is_flagged(res):
    risk = res.cases["GRN-4430"]["risk"]
    assert any("admin2" in x for x in risk) and any("3 days" in x for x in risk)


def test_h202_handwriting_is_normalised_and_reported(res):
    reg = next(r for r in res.rows if r.source == "register" and r.grn == 4408)
    assert reg.item == "H2O2 50%" and any("H202" in n for n in reg.notes)


def test_a_lost_row_would_break_the_proof(tmp_path):
    erp = tmp_path / "erp.csv"
    lines = ERP.read_text(encoding="utf-8").splitlines()
    erp.write_text("\n".join(lines[:5] + lines[6:]) + "\n", encoding="utf-8")  # drop one ERP row
    r = reconcile.run(REG, erp, STORE)
    # the dropped row is not silently ignored: 4405 now shows as missing from ERP, and the proof still ties
    assert r.cases["GRN-4405"]["outcome"] == "MISSING_IN_ERP"
    assert r.input_counts["erp"] == 27 and all(ok for *_, ok in r.proof)


def test_review_decisions_resolve_items_without_changing_outcomes(tmp_path, res):
    dec = tmp_path / "d.csv"
    with open(dec, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_key", "decision", "value", "reviewer", "note"])
        w.writerow(["GRN-4419:qty_uom", "use_register", "", "controller", "paper shows the correction"])
        w.writerow(["GRN-4428:qty_uom:illegible", "manual", "250 bdl", "controller", "read from delivery challan"])
        w.writerow(["GRN-9999:lot", "use_erp", "", "controller", "unknown item"])
    r = reconcile.run(REG, ERP, STORE, dec)
    assert set(r.resolved) == {"GRN-4419:qty_uom", "GRN-4428:qty_uom:illegible"}
    assert r.resolved["GRN-4419:qty_uom"]["value"] == "66 drum"
    assert r.unknown_decisions == ["GRN-9999:lot"]
    assert r.outcome == res.outcome                      # decisions never rewrite the proof
    assert "8 items need you" in reconcile.summary_text(r)


def test_cli_end_to_end(tmp_path):
    out = tmp_path / "out"
    assert reconcile.main(["--out", str(out), "--decisions", str(tmp_path / "none.csv"),
                           "--register", str(REG), "--erp", str(ERP), "--store", str(STORE)]) == 0
    for name in ("row_outcomes.csv", "review_queue.csv", "proof.md", "controller_summary.txt", "reconciliation.xlsx"):
        assert (out / name).exists()


def test_any_extraction_doubt_is_routed_to_review_not_compared(tmp_path):
    doc = json.loads(REG.read_text())
    row = next(r for pg in doc["pages"] for r in pg["rows"] if r["grn_no"] == "4410")
    row["flags"], row["note"] = ["uncertain_digit"], "4 or 6?"
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(doc))
    r = reconcile.run(reg, ERP, STORE)
    assert r.cases["GRN-4410"]["outcome"] == "REVIEW_UNREADABLE"
    assert any(i["key"] == "GRN-4410:qty_uom:illegible" and "uncertain" in i["short"] for i in r.items)
    assert all(ok for *_, ok in r.proof)
