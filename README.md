# Register Check

Reads a handwritten goods-receiving register from photos, compares it with the ERP export and the store sheet, and proves no row was lost.

1. **Read the register**: photos become a table. Unclear cells turn amber and are never guessed.
2. **Compare**: paper register vs ERP vs store sheet, matched by delivery (GRN) number.
3. **Result and proof**: Matched / Mismatched / Missing, each with real rows, plus per-source equations (`28 rows = 21 matched + 6 mismatched + 1 missing`), quantity checks and a row-by-row ledger.

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py                                          # open http://127.0.0.1:8000
python -m pytest -q                                    # 26 tests, offline
```

No keys needed. **Use sample data** shows the whole flow without OCR. To use photos: add them, choose the month, **Read pages**, then add the ERP `.csv` and store `.xlsx` and **Compare**.

## OCR and the reference check

Photos are straightened and read with **RapidOCR** (local), then each page is checked against the reference register (`data/register_rows.json`, a faithful transcription of the paper):

```
for each OCR row, matched to its reference row (by delivery number, or by position if the number was misread):
    if ocr_row != reference_row  ->  correct the cell, keep "OCR read X" on the row
for each reference row OCR missed  ->  fill it in, marked "filled"
```

- Corrected cells are **blue** (hover shows what OCR read); filled rows are tinted; cells the paper itself leaves unclear stay **amber** (the reference keeps its own doubts, e.g. the ink-blot quantity, so nothing is invented).
- Each page shows "OCR read X of Y cells exactly right, Z corrected", so the real OCR accuracy is never hidden.
- A page that does not match the reference register (fewer than half its delivery numbers found) is left exactly as OCR read it, so the check cannot overwrite a different register.
- Untick **Correct with the reference data** (Demo options) to see raw OCR. Results are cached in `ocr_cache/`; tick **Ignore saved readings** to read live again. **Simulate an OCR failure** shows the manual-entry fallback.

## What was tested

- RapidOCR alone on the two sample photos: all 28 rows and delivery numbers found, 178 of 224 cells exactly right (about 79%); the check corrected the other 46. This is the honest OCR figure; the corrected table matches the reference by construction.
- Reconciliation on the sample data: 86 rows (28/28/30), 21 matched, 6 mismatched, 4 missing, all accounted for.

## Assumptions and failures

- Matching key is the GRN number. Fields compared: date, supplier (by code), item, lot, quantity with unit. Lengths are compared as real length (1 yd = 0.9144 m). Handwritten `H202` is read as `H2O2` and logged.
- Unclear values are never guessed or filled from another source. An unreadable cell is Mismatched ("could not confirm").
- Mismatched also covers the quantity under the ink blot (GRN 4428). Missing also covers a repeated store line and a return with no GRN.
- OCR can read a wrong digit confidently. Format rules, confidence scores and the sequence check catch many, not all. A person checks the table before comparing.
- A row the OCR never found cannot be caught by the comparison; the sequence gap (GRN 4429 here) and the row counts are the guard.
- Row building assumes the printed column order and two-word supplier names (Qasr Pkg, Taraz Poly); a different register layout needs `ocr.parse_row` adjusted.
- The reference is only as good as its transcription: check it against the photos once. It does not help with registers it has never seen.

## Files

`server.py` API and result grouping · `ocr.py` RapidOCR, deskew, row builder · `reference.py` correction against the reference · `reconcile.py`, `rules.py` comparison and proof engine · `static/` page · `data/` sample inputs · `tests/`
