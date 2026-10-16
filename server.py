"""Register Check: read the handwritten register, compare three sources, prove nothing was missed."""
import csv
import io
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import ocr  # noqa: E402
import reference as ref  # noqa: E402
import reconcile as rc  # noqa: E402
import rules  # noqa: E402

app = FastAPI(title="Register Check")
DATA = Path("data")
SRC = ("register", "erp", "store")
NAME = {"register": "Paper register", "erp": "ERP", "store": "Store sheet"}
LAST = {"ledger": []}


def group(outcome):
    return "A" if outcome == "MATCHED" else "B" if outcome in ("MISMATCH", "REVIEW_UNREADABLE") else "C"


@app.get("/api/health")
def health():
    return ocr.status()


@app.post("/api/read")
async def read(files: list[UploadFile] = File(default=[]), simulate: bool = Form(False), sample: bool = Form(False),
               cache: bool = Form(True), reference: bool = Form(True)):
    if sample:
        doc = json.loads((DATA / "register_rows.json").read_text(encoding="utf-8"))
        for p in doc["pages"]:
            p["engine"], p["log"] = "saved reading", []
        return doc
    if not files:
        raise HTTPException(400, "Add at least one photo of the register.")
    pages, month = [], ""
    for n, f in enumerate(files, 1):
        data = await f.read()
        if len(data) > 15_000_000:
            raise HTTPException(400, f"{f.filename} is too large (15 MB max).")
        try:
            r = ocr.read_page(data, simulate, cache)
        except ValueError as e:
            raise HTTPException(400, f"{f.filename}: {e}")
        month = month or r["month"]
        rows, summ = r["rows"], None
        if reference:
            rows, summ = ref.correct_page(r["rows"], ref.load_reference())
        pages.append({"page": n, "image": f.filename, "engine": r["engine"], "log": r["log"], "rows": rows, "reference": summ})
    return {"month": month, "pages": pages}


def save(upload, folder, name):
    path = folder / (name + Path(upload.filename or "").suffix.lower())
    return path


@app.post("/api/compare")
async def compare(register: str = Form(...), erp: UploadFile | None = File(None),
                  store: UploadFile | None = File(None), sample: bool = Form(False)):
    doc = json.loads(register)
    if not doc.get("month"):
        raise HTTPException(400, "Pick the register month first.")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "reg.json").write_text(json.dumps(doc), encoding="utf-8")
        if sample:
            erp_p, store_p = DATA / "t4_erp_grn.csv", DATA / "t4_store_sheet.xlsx"
        else:
            if not (erp and store and erp.filename and store.filename):
                raise HTTPException(400, "Add both the ERP file (.csv) and the store sheet (.xlsx).")
            erp_p, store_p = save(erp, tmp, "erp"), save(store, tmp, "store")
            erp_p.write_bytes(await erp.read())
            store_p.write_bytes(await store.read())
        try:
            res = rc.run(tmp / "reg.json", erp_p, store_p)
        except Exception as e:
            raise HTTPException(400, f"Could not read the files: {type(e).__name__}: {e}")
    out = summarise(res)
    LAST["ledger"] = out["ledger"]
    return out


def summarise(res):
    cnt = Counter((r.source, group(res.outcome[r.ref])) for r in res.rows)
    lines = [{"source": s, "rows": res.input_counts[s], "A": cnt[(s, "A")], "B": cnt[(s, "B")], "C": cnt[(s, "C")]}
             for s in SRC]
    ok = all(p[3] for p in res.proof) and all(x["rows"] == x["A"] + x["B"] + x["C"] for x in lines)
    cases = res.cases

    A, tot = [], defaultdict(dict)
    for c in cases.values():
        if c["outcome"] == "MATCHED":
            v = c["values"]
            A.append({"id": c["case_id"], "item": v["erp"]["item"], "date": v["erp"]["date"], "lot": v["erp"]["lot"],
                      "qty": {s: v[s]["qty_uom"] for s in SRC}})
    for r in res.rows:
        if res.outcome[r.ref] == "MATCHED":
            fam, b = rules.physical(r.qty, r.uom)
            tot[fam][r.source] = round(tot[fam].get(r.source, 0) + b, 3)

    B, C = [], []
    for i in res.items:
        c, k = cases.get(i["case_id"], {}), i["kind"]
        if k in ("field_mismatch", "unreadable"):
            f = i["key"].split(":")[1]
            some = c["values"].get("erp") or next(iter(c["values"].values()))
            B.append({"id": i["case_id"], "field": f, "item": some["item"], "values": i["values"],
                      "odd": next((d["odd_one_out"] for d in c["diffs"] if d["field"] == f), None),
                      "blank": next((u["source"] for u in c["unreadable"] if u["field"] == f), None) if k == "unreadable" else None,
                      "note": i.get("hint", "")})
        else:
            src = next((p for p in i["key"].split(":") if p in SRC), None)
            if k == "missing":
                label = ("Only in " + NAME[c["present"][0]]) if len(c["present"]) == 1 else \
                        "Missing from " + " and ".join(NAME[a] for a in c["absent"])
                pres = {s: s in c["present"] for s in SRC}
            else:
                label = {"duplicate": "Repeated line in " + NAME.get(src, ""), "duplicate_conflict": "Repeated line, different values",
                         "adjustment": "Return with no delivery number"}.get(k, "No delivery number")
                pres = {s: s == src for s in SRC}
            C.append({"id": i["case_id"] if i["case_id"] != "-" else i["key"].split(":")[1], "label": label,
                      "present": pres, "note": i["short"]})

    ledger = [{"source": r.source, "row": r.ref, "grn": r.grn or "", "group": group(res.outcome[r.ref]),
               "outcome": res.outcome[r.ref]} for r in res.rows]
    return {"ok": ok, "total": sum(res.input_counts.values()), "lines": lines, "checks": [
                {"name": p[0], "expected": p[1], "actual": p[2], "ok": p[3]} for p in res.proof],
            "counts": {"A": len(A), "B": len({x["id"] for x in B}), "C": len(C)},
            "A": A, "B": B, "C": C, "totals": [{"unit": u, **v} for u, v in tot.items()],
            "gaps": res.gaps, "ledger": ledger}


@app.get("/api/ledger.csv", response_class=PlainTextResponse)
def ledger():
    buf = io.StringIO()
    w = csv.DictWriter(buf, ["source", "row", "grn", "group", "outcome"])
    w.writeheader()
    w.writerows(LAST["ledger"])
    return PlainTextResponse(buf.getvalue(), headers={"Content-Disposition": "attachment; filename=ledger.csv"})


app.mount("/", StaticFiles(directory="static", html=True), name="static")
