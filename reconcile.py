#!/usr/bin/env python3
"""Three-way reconciliation: handwritten register vs ERP GRNs vs storekeeper sheet.

Every source row lands in exactly one outcome. Anything doubtful goes to a review
queue; a person resolves only those items. Nothing is guessed or auto-corrected.

    python reconcile.py                      # run on data/, write out/
    python reconcile.py --decisions review_decisions.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Font

import rules

SOURCES = ("register", "erp", "store")
FIELDS = ("date", "supplier", "item", "lot", "qty_uom")
# extraction flag -> the comparable field it puts in doubt (compared fields are skipped, not guessed)
FLAG_FIELD = {
    "qty_illegible": "qty_uom", "uncertain_digit": "qty_uom", "unit_unknown": "qty_uom",
    "date_illegible": "date", "date_unexpected": "date",
    "lot_illegible": "lot", "lot_format": "lot",
    "supplier_unknown": "supplier", "other_unclear": "item",
}
OUTCOMES = [
    "MATCHED", "MISMATCH", "REVIEW_UNREADABLE",
    "MISSING_IN_ERP", "MISSING_IN_REGISTER", "MISSING_IN_STORE",
    "MISSING_IN_REGISTER_AND_STORE", "MISSING_IN_REGISTER_AND_ERP", "MISSING_IN_ERP_AND_STORE",
    "DUPLICATE_ROW", "DUPLICATE_CONFLICT", "ADJUSTMENT_NOT_A_RECEIPT", "UNKEYED_ROW",
]


@dataclass
class Row:
    source: str
    ref: str                      # e.g. register:p2r5, erp:L12, store:R14
    grn: int | None
    date: date | None
    supplier: str | None          # canonical code
    item: str | None
    lot: str | None
    qty: float | None
    uom: str | None
    raw: dict
    notes: list = field(default_factory=list)      # normalisations applied (FYI)
    illegible: dict = field(default_factory=dict)  # field -> why; never guessed
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- parsing
def parse_grn(v):
    m = re.search(r"(\d{3,})", str(v)) if v is not None else None
    return int(m.group(1)) if m else None


def parse_num(v):
    if v is None or str(v).strip() == "":
        return None
    return float(str(v).replace(",", "").strip())


def parse_date_text(s, year, month, notes):
    """ISO, D/M[/Y] (register, Pakistani style) or text M/D/Y (seen in the store sheet).
    A reading is accepted only if exactly one candidate date falls in the reporting month."""
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", s)
    if not m:
        return None
    a, b, y = int(m[1]), int(m[2]), int(m[3] or year)
    readings = []
    for label, (dd, mm) in (("D/M", (a, b)), ("M/D", (b, a))):
        try:
            readings.append((label, date(y, mm, dd)))
        except ValueError:
            pass
    in_month = [r for r in readings if r[1].month == month]
    pick = in_month or readings
    if len({d for _, d in pick}) != 1:
        return None  # ambiguous
    label, d = pick[0]
    if label == "M/D" and a != b:
        notes.append(f"text date '{s}' read as M/D/Y")
    return d


def _finish(row):
    """Fill in illegible markers for fields that failed to normalise."""
    if row.date is None and "date" not in row.illegible:
        row.illegible["date"] = "unparseable or ambiguous date"
    if row.supplier is None and "supplier" not in row.illegible:
        row.illegible["supplier"] = "unknown supplier"
    if row.uom is None and "qty_uom" not in row.illegible:
        row.illegible["qty_uom"] = "unknown unit"
    if row.qty is None and "qty_uom" not in row.illegible:
        row.illegible["qty_uom"] = "missing quantity"
    return row


def load_register(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    year, month = map(int, data["month"].split("-"))
    rows = []
    for pg in data["pages"]:
        for r in pg["rows"]:
            notes, ill = [], {}
            flags = set(r.get("flags", []))
            item, note = rules.norm_item(r.get("item"))
            if note:
                notes.append(note)
            if "handwritten_correction" in flags:
                notes.append(f"handwritten correction: {r.get('note')}")
            row = Row(
                "register", f"register:p{pg['page']}r{r['row']}", parse_grn(r.get("grn_no")),
                parse_date_text(r["date"], year, month, notes) if r.get("date") else None,
                rules.supplier_code(r.get("supplier")), item, (r.get("lot") or "").strip() or None,
                parse_num(r.get("qty")), rules.norm_uom(r.get("unit")), r, notes, ill,
                {"recd_by": r.get("recd_by")},
            )
            if "grn_illegible" in flags:           # unreadable delivery number: no key, goes to review
                row.grn = None
            for fl in flags:                       # every doubt raised at extraction goes to a person
                if fl in FLAG_FIELD:
                    ill.setdefault(FLAG_FIELD[fl], r.get("note") or fl.replace("_", " "))
            rows.append(_finish(row))
    return rows


def load_erp(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):
            notes = []
            item, note = rules.norm_item(r["item_desc"])
            if note:
                notes.append(note)
            rows.append(_finish(Row(
                "erp", f"erp:L{i}", parse_grn(r["grn_no"]),
                parse_date_text(r["grn_date"], 0, 0, notes), r["supplier_code"].strip() or None,
                item, r["lot_no"].strip(), parse_num(r["qty"]), rules.norm_uom(r["uom"]), r, notes, {},
                {"entered_by": r["entered_by"],
                 "entered_at": datetime.strptime(r["entered_at"], "%Y-%m-%d %H:%M")},
            )))
    return rows


def load_store(path, month_year):
    year, month = month_year
    ws = openpyxl.load_workbook(path).worksheets[0]
    head = {str(c.value).strip().lower(): i for i, c in enumerate(ws[1]) if c.value}
    rows = []
    for n, cells in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if all(v is None for v in cells):
            continue
        g = lambda k: cells[head[k]] if k in head else None
        notes = []
        d = g("date")
        if isinstance(d, datetime):
            d = d.date()
        elif isinstance(d, str):
            d = parse_date_text(d, year, month, notes)
        item, note = rules.norm_item(str(g("material") or ""))
        if note:
            notes.append(note)
        raw = dict(zip(head, cells))
        rows.append(_finish(Row(
            "store", f"store:R{n}", parse_grn(g("grn")), d, rules.supplier_code(str(g("supplier") or "")),
            item, (str(g("lot") or "")).strip() or None, parse_num(g("qty received")),
            rules.norm_uom(str(g("uom") or "")), raw, notes, {},
            {"remarks": g("remarks") or ""},
        )))
    return rows


# ---------------------------------------------------------------- comparing
def key(row, f):
    if f == "qty_uom":
        return rules.physical(row.qty, row.uom)
    return getattr(row, f)


def disp(row, f):
    v = key(row, f)
    if v is None:
        return None
    if f == "date":
        return row.date.isoformat()
    if f == "qty_uom":
        s = f"{rules.fmt_qty(row.qty)} {row.uom.lower()}"
        if row.uom in rules.LENGTH_TO_M and row.uom != "M":
            s += f" (= {rules.fmt_qty(v[1])} m)"
        return s
    return str(v)


def diffs_between(rows_by_src):
    """Field-level differences across the rows present. Illegible fields are skipped, not compared."""
    out = []
    for f in FIELDS:
        vals = {s: key(r, f) for s, r in rows_by_src.items() if key(r, f) is not None and f not in r.illegible}
        if len(set(vals.values())) > 1:
            cnt = Counter(vals.values())
            odd = None
            if len(vals) == 3 and sorted(cnt.values()) == [1, 2]:
                odd = next(s for s, v in vals.items() if cnt[v] == 1)
            out.append({"field": f, "values": {s: disp(rows_by_src[s], f) for s in vals}, "odd_one_out": odd})
    return out


def same_content(a, b):
    return all(key(a, f) == key(b, f) for f in FIELDS) and a.illegible == b.illegible


# ---------------------------------------------------------------- core
@dataclass
class Result:
    rows: list
    outcome: dict            # ref -> outcome
    case_of: dict            # ref -> case id
    cases: dict              # case id -> dict
    items: list              # review items
    gaps: list
    proof: list
    input_counts: dict
    input_totals: dict
    month: str


def classify(rows):
    by_grn = defaultdict(lambda: defaultdict(list))
    unkeyed = []
    for r in rows:
        (unkeyed if r.grn is None else by_grn[r.grn][r.source]).append(r)

    erp_users = Counter(r.extra["entered_by"] for r in rows if r.source == "erp")
    usual_user = erp_users.most_common(1)[0][0] if erp_users else None

    outcome, case_of, cases, items = {}, {}, {}, []

    for grn in sorted(by_grn):
        cid = f"GRN-{grn}"
        prim, dup_rows = {}, []
        for s in SOURCES:
            lst = by_grn[grn].get(s, [])
            if lst:
                prim[s] = lst[0]
                dup_rows += [(lst[0], x) for x in lst[1:]]
        absent = [s for s in SOURCES if s not in prim]
        diffs = diffs_between(prim)
        unread = [{"source": s, "field": f, "why": why} for s, r in prim.items() for f, why in r.illegible.items()]
        risk = []
        if "erp" in prim:
            e = prim["erp"]
            if usual_user and e.extra["entered_by"] != usual_user:
                risk.append(f"ERP entry made by '{e.extra['entered_by']}' (usual: '{usual_user}')")
            if e.date and (e.extra["entered_at"].date() - e.date).days > 1:
                risk.append(f"ERP entry made {(e.extra['entered_at'].date() - e.date).days} days after the GRN date")
        if absent:
            oc = "MISSING_IN_" + "_AND_".join(s.upper() for s in absent)
        elif diffs:
            oc = "MISMATCH"
        elif unread:
            oc = "REVIEW_UNREADABLE"
        else:
            oc = "MATCHED"
        cases[cid] = {"case_id": cid, "grn": grn, "present": list(prim), "absent": absent, "outcome": oc,
                      "diffs": diffs, "unreadable": unread, "risk": risk,
                      "values": {s: {f: disp(r, f) for f in FIELDS} for s, r in prim.items()}}
        for s, r in prim.items():
            outcome[r.ref], case_of[r.ref] = oc, cid
        for first, extra in dup_rows:
            same = same_content(first, extra)
            outcome[extra.ref] = "DUPLICATE_ROW" if same else "DUPLICATE_CONFLICT"
            case_of[extra.ref] = cid
            items.append({"key": f"{cid}:duplicate:{extra.ref}", "case_id": cid,
                          "kind": "duplicate" if same else "duplicate_conflict",
                          "values": {extra.source: f"{extra.ref} vs {first.ref}"},
                          "short": (f"{extra.source.title()} lists {cid} twice ({first.ref.split(':')[1]} and "
                                    f"{extra.ref.split(':')[1]}, {'identical' if same else 'DIFFERENT values'})")})
        # review items for this case
        if oc == "MISMATCH" or (diffs and absent):
            for d in diffs:
                vals = d["values"]
                groups = defaultdict(list)
                for s, v in vals.items():
                    groups[v].append(s)
                text = "; ".join(f"{'/'.join(ss)} {v}" for v, ss in groups.items())
                hint = (f"{d['odd_one_out']} differs from the other two, which agree" if d["odd_one_out"] else "")
                corr = [n for r in prim.values() for n in r.notes if n.startswith("handwritten correction")]
                if corr and d["field"] == "qty_uom":
                    hint += ("; " if hint else "") + "register has a " + corr[0]
                items.append({"key": f"{cid}:{d['field']}", "case_id": cid, "kind": "field_mismatch",
                              "values": vals, "hint": hint, "short": f"{cid} {d['field'].replace('_uom', '')}: {text}"})
        for u in unread:
            others = {s: cases[cid]["values"][s][u["field"]] for s in prim if s != u["source"]}
            agree = len(set(others.values())) == 1 and others
            tail = (f"; {' & '.join(others)} both say {next(iter(others.values()))}" if agree
                    else f"; others: {others}")
            items.append({"key": f"{cid}:{u['field']}:illegible", "case_id": cid, "kind": "unreadable",
                          "values": others, "hint": "not guessed; read the paper register",
                          "short": f"{cid} {u['source']} {u['field'].replace('_uom', '')} {'unreadable' if key(prim[u['source']], u['field']) is None else 'uncertain'} ({u['why']}){tail}"})
        if absent:
            qtys = {s: cases[cid]["values"][s]["qty_uom"] for s in prim}
            if len(prim) == 1:
                s = next(iter(prim))
                txt = f"{cid} only in {s} ({qtys[s]}" + (f"; {'; '.join(risk)}" if risk else "") + ")"
            elif len(set(qtys.values())) == 1:
                txt = f"{cid} missing from {' and '.join(absent)}; {' & '.join(prim)} both have it: {next(iter(qtys.values()))}"
            else:
                txt = f"{cid} missing from {' and '.join(absent)}; " + "; ".join(f"{s} {q}" for s, q in qtys.items())
            items.append({"key": f"{cid}:missing", "case_id": cid, "kind": "missing",
                          "values": {s: cases[cid]["values"][s]["qty_uom"] for s in prim},
                          "hint": "; ".join(risk), "short": txt})

    for r in unkeyed:
        is_adj = (r.qty is not None and r.qty < 0) or re.search(r"return|reject|debit", str(r.extra.get("remarks", "")), re.I)
        outcome[r.ref] = "ADJUSTMENT_NOT_A_RECEIPT" if is_adj else "UNKEYED_ROW"
        case_of[r.ref] = "-"
        what = r.extra.get("remarks") or "no GRN number"
        items.append({"key": f"{r.ref}:{'adjustment' if is_adj else 'unkeyed'}", "case_id": "-",
                      "kind": "adjustment" if is_adj else "unkeyed", "values": {r.source: disp(r, "qty_uom")},
                      "hint": "confirm a return/debit note exists in the ERP; lot " + str(r.lot),
                      "short": f"{r.source.title()} sheet {r.ref.split(':')[1]}: {rules.fmt_qty(r.qty)} {r.uom.lower()} lot {r.lot} - '{what}' (no GRN; not in register/ERP)"})

    seen = sorted(by_grn)
    gaps = [n for n in range(seen[0], seen[-1] + 1) if n not in by_grn] if seen else []
    return outcome, case_of, cases, items, gaps


def input_pass(register, erp, store):
    """Second, deliberately naive pass over the RAW files: row counts and quantity totals per
    source and unit family. It is what the outcome table must tie back to."""
    counts = {s: 0 for s in SOURCES}
    totals = {s: Counter() for s in SOURCES}
    illegible = {s: 0 for s in SOURCES}

    def add(src, qty, unit):
        if qty in (None, ""):
            illegible[src] += 1
            return
        fam, base = rules.physical(float(str(qty).replace(",", "")), rules.norm_uom(unit))
        totals[src][fam] += base

    for pg in json.loads(Path(register).read_text(encoding="utf-8"))["pages"]:
        for r in pg["rows"]:
            counts["register"] += 1
            add("register", r.get("qty"), r["unit"])
    with open(erp, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            counts["erp"] += 1
            add("erp", r["qty"], r["uom"])
    ws = openpyxl.load_workbook(store).worksheets[0]
    for cells in ws.iter_rows(min_row=2, values_only=True):
        if any(v is not None for v in cells):
            counts["store"] += 1
            add("store", cells[5], cells[6])
    return counts, totals, illegible


def run(register, erp, store, decisions=None):
    month = json.loads(Path(register).read_text(encoding="utf-8"))["month"]
    y, m = map(int, month.split("-"))
    rows = load_register(register) + load_erp(erp) + load_store(store, (y, m))
    outcome, case_of, cases, items, gaps = classify(rows)

    counts, totals, illegible = input_pass(register, erp, store)
    proof = []
    refs = [r.ref for r in rows]
    proof.append(("every row has exactly one outcome", len(refs), len(outcome), len(refs) == len(set(refs)) == len(outcome)))
    for s in SOURCES:
        landed = sum(1 for r in rows if r.source == s and r.ref in outcome)
        proof.append((f"{s}: rows in input vs rows landed", counts[s], landed, counts[s] == landed))
    for s in SOURCES:
        got = Counter()
        for r in rows:
            if r.source == s and r.qty is not None and r.uom:
                fam, base = rules.physical(r.qty, r.uom)
                got[fam] += base
        for fam in sorted(set(got) | set(totals[s])):
            exp, act = round(totals[s][fam], 3), round(got[fam], 3)
            proof.append((f"{s}: qty total {fam}", exp, act, abs(exp - act) < 0.0005))
    res = Result(rows, outcome, case_of, cases, items, gaps, proof, counts, totals, month)
    res.illegible = illegible
    apply_decisions(res, decisions)
    return res


# ---------------------------------------------------------------- review step
def apply_decisions(res, path):
    """A person's decisions (CSV: item_key,decision,value,reviewer,note). Decisions are recorded
    against review items; they never change the outcome of a source row."""
    res.resolved, res.unknown_decisions = {}, []
    if not path or not Path(path).exists():
        return
    by_key = {i["key"]: i for i in res.items}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for d in csv.DictReader(f):
            k = d["item_key"].strip()
            if k not in by_key:
                res.unknown_decisions.append(k)
                continue
            choice = d["decision"].strip().lower()
            if choice.startswith("use_"):
                src = choice[4:]
                val = by_key[k]["values"].get(src)
                if val is None:
                    res.unknown_decisions.append(f"{k} (no value for {src})")
                    continue
            elif choice == "manual" and d.get("value", "").strip():
                val = d["value"].strip()
            elif choice == "accept_as_is":
                val = "accepted as is"
            else:
                res.unknown_decisions.append(f"{k} (invalid decision '{choice}')")
                continue
            res.resolved[k] = {"decision": choice, "value": val, "reviewer": d.get("reviewer", ""), "note": d.get("note", "")}


# ---------------------------------------------------------------- outputs
def summary_text(res):
    n_cases = len(res.cases)
    matched = sum(1 for c in res.cases.values() if c["outcome"] == "MATCHED")
    open_items = [i for i in res.items if i["key"] not in res.resolved]
    mon = datetime.strptime(res.month, "%Y-%m").strftime("%b %Y")
    c = res.input_counts
    lines = [f"{mon} receiving check (register / ERP / store sheet): {sum(c.values())} rows read "
             f"({c['register']}/{c['erp']}/{c['store']}), every row accounted for.",
             f"{matched} of {n_cases} GRNs agree in all three. {len(open_items)} items need you:"]
    for n, i in enumerate(open_items, 1):
        lines.append(f"{n}. {i['short']}")
    if res.gaps:
        lines.append("Number gap: " + ", ".join(f"GRN-{g}" for g in res.gaps) + " is in none of the three.")
    return "\n".join(lines)


def proof_markdown(res):
    out = [f"# Completeness proof - {res.month}", "", "| check | expected (raw input) | actual (outcome table) | result |", "|---|---:|---:|---|"]
    for name, exp, act, ok in res.proof:
        out.append(f"| {name} | {rules.fmt_qty(exp)} | {rules.fmt_qty(act)} | {'OK' if ok else 'FAIL'} |")
    out += ["", "## Rows by outcome", "", "| outcome | " + " | ".join(SOURCES) + " |", "|---|---:|---:|---:|"]
    cnt = Counter((r.source, res.outcome[r.ref]) for r in res.rows)
    for o in OUTCOMES:
        if any(cnt[(s, o)] for s in SOURCES):
            out.append(f"| {o} | " + " | ".join(str(cnt[(s, o)]) for s in SOURCES) + " |")
    out.append("| **total** | " + " | ".join(f"**{res.input_counts[s]}**" for s in SOURCES) + " |")
    if any(res.illegible.values()):
        out += ["", "Rows whose quantity is illegible (excluded from quantity totals, flagged for review): "
                + ", ".join(f"{s} {n}" for s, n in res.illegible.items() if n)]
    out += ["", f"GRN numbers seen: {len(res.cases)}; gaps in the sequence: {', '.join(map(str, res.gaps)) or 'none'}."]
    return "\n".join(out) + "\n"


def write_outputs(res, outdir):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "row_outcomes.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source", "ref", "grn", "date", "supplier", "item", "lot", "qty", "uom", "outcome", "case_id", "notes"])
        for r in sorted(res.rows, key=lambda r: (SOURCES.index(r.source), r.grn or 10**9, r.ref)):
            w.writerow([r.source, r.ref, r.grn or "", r.date or "", r.supplier or "", r.item or "", r.lot or "",
                        "" if r.qty is None else rules.fmt_qty(r.qty), r.uom or "", res.outcome[r.ref],
                        res.case_of[r.ref], " | ".join(r.notes)])
    with open(out / "review_queue.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item_key", "case_id", "kind", "what", "register", "erp", "store", "hint",
                    "decision", "value", "reviewer", "note", "status"])
        for i in res.items:
            v = i["values"]
            res_ = res.resolved.get(i["key"])
            w.writerow([i["key"], i["case_id"], i["kind"], i["short"], v.get("register", ""), v.get("erp", ""),
                        v.get("store", ""), i.get("hint", ""), res_["decision"] if res_ else "",
                        res_["value"] if res_ else "", res_["reviewer"] if res_ else "", res_["note"] if res_ else "",
                        "RESOLVED" if res_ else "OPEN"])
    (out / "proof.md").write_text(proof_markdown(res), encoding="utf-8")
    (out / "controller_summary.txt").write_text(summary_text(res) + "\n", encoding="utf-8")
    write_xlsx(res, out / "reconciliation.xlsx")


def write_xlsx(res, path):
    wb = openpyxl.Workbook()
    wp, wr, wv = wb.active, wb.create_sheet("Rows"), wb.create_sheet("Review")
    wp.title = "Proof"
    bold = Font(name="Arial", bold=True)
    base = Font(name="Arial")
    blue = Font(name="Arial", color="0000FF")

    hdr = ["source", "ref", "grn", "date", "supplier", "item", "lot", "qty_raw", "uom", "qty_base", "unit_family", "outcome", "case_id"]
    wr.append(hdr)
    for r in sorted(res.rows, key=lambda r: (SOURCES.index(r.source), r.grn or 10**9, r.ref)):
        ph = rules.physical(r.qty, r.uom)
        wr.append([r.source, r.ref, r.grn, r.date.isoformat() if r.date else None, r.supplier, r.item, r.lot,
                   r.qty, r.uom, ph[1] if ph else None, ph[0] if ph else None, res.outcome[r.ref], res.case_of[r.ref]])
    last = len(res.rows) + 1
    rng = lambda col: f"Rows!${col}$2:${col}${last}"

    wp["A1"], wp["A1"].font = f"Completeness proof - {res.month}", Font(name="Arial", bold=True, size=13)
    wp["A2"] = ("Blue = counted straight from the raw input files by a separate code path. Black = formulas over the Rows sheet. "
                "Every row must land in exactly one outcome.")
    wp["A2"].font = base
    wp.append([])
    wp.append(["source", "rows in input", "rows landed", "sum of outcomes", "check"] + OUTCOMES)
    first = wp.max_row
    for s in SOURCES:
        r = wp.max_row + 1
        wp.append([s, res.input_counts[s], f'=COUNTIF({rng("A")},A{r})', f"=SUM(F{r}:{openpyxl.utils.get_column_letter(5 + len(OUTCOMES))}{r})",
                   f'=IF(AND(B{r}=C{r},C{r}=D{r}),"OK","CHECK")'] +
                  [f'=COUNTIFS({rng("A")},$A{r},{rng("L")},{openpyxl.utils.get_column_letter(6 + k)}${first})' for k in range(len(OUTCOMES))])
        wp.cell(r, 2).font = blue
    wp.append([])
    wp.append(["source", "unit family", "input total", "landed total", "difference", "check", "rows with illegible qty"])
    qfirst = wp.max_row + 1
    for s in SOURCES:
        for fam in sorted({rules.physical(r.qty, r.uom)[0] for r in res.rows if r.source == s and r.qty is not None and r.uom}):
            r = wp.max_row + 1
            wp.append([s, fam, round(res.input_totals[s][fam], 3),
                       f'=SUMIFS({rng("J")},{rng("A")},A{r},{rng("K")},B{r})', f"=C{r}-D{r}",
                       f'=IF(ABS(E{r})<0.0005,"OK","CHECK")', res.illegible[s] if fam == sorted({rules.physical(x.qty, x.uom)[0] for x in res.rows if x.source == s and x.qty is not None and x.uom})[0] else None])
            wp.cell(r, 3).font = blue
    wp.append([])
    wp.append(["GRN numbers seen", len(res.cases)])
    wp.append(["gaps in GRN sequence", ", ".join(map(str, res.gaps)) or "none"])

    wv.append(["item_key", "case_id", "kind", "what", "register", "erp", "store", "hint", "decision", "value", "reviewer", "status"])
    for i in res.items:
        v = i["values"]
        d = res.resolved.get(i["key"])
        wv.append([i["key"], i["case_id"], i["kind"], i["short"], v.get("register"), v.get("erp"), v.get("store"),
                   i.get("hint"), d["decision"] if d else None, d["value"] if d else None,
                   d["reviewer"] if d else None, "RESOLVED" if d else "OPEN"])

    for ws in (wp, wr, wv):
        for row in ws.iter_rows():
            for c in row:
                if c.font == Font() or c.font.name != "Arial":
                    c.font = Font(name="Arial", bold=c.font.bold, color=c.font.color, size=c.font.size)
        for c in ws[1 if ws is not wp else 4]:
            c.font = bold
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(60, max(10, max(len(str(c.value or "")) for c in col[:30]) + 2))
    wp.column_dimensions["A"].width = 24
    wb.save(path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--register", default="data/register_rows.json")
    ap.add_argument("--erp", default="data/t4_erp_grn.csv")
    ap.add_argument("--store", default="data/t4_store_sheet.xlsx")
    ap.add_argument("--decisions", default="review_decisions.csv")
    ap.add_argument("--out", default="out")
    a = ap.parse_args(argv)
    res = run(a.register, a.erp, a.store, a.decisions)
    write_outputs(res, a.out)
    print(summary_text(res))
    bad = [p for p in res.proof if not p[3]]
    for u in res.unknown_decisions:
        print(f"WARNING: decision ignored: {u}", file=sys.stderr)
    if bad:
        print("\nPROOF FAILED:", *(f"\n  {b[0]}: expected {b[1]}, got {b[2]}" for b in bad), file=sys.stderr)
        return 2
    print(f"\nProof OK ({len(res.proof)} checks). Outputs in {a.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
