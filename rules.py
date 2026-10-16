"""Lookup tables and normalisers. Edit rules here, not in the pipeline.

Anything a normaliser cannot map returns None, and the pipeline then flags the
field for a person instead of guessing.
"""
import re

# canonical supplier code -> lowercase keywords seen in the register / store sheet
SUPPLIERS = {
    "S-101": ["qasr"],
    "S-102": ["taraz"],
    "S-103": ["zarnab"],
    "S-104": ["rehbar"],
    "S-105": ["saahil"],
    "S-106": ["nayab"],
    "S-107": ["shahtoot"],
}

UOM_MAP = {
    "pcs": "PCS", "pc": "PCS",
    "bag": "BAG", "bags": "BAG",
    "cone": "CONE", "cones": "CONE",
    "ctn": "CTN", "ctns": "CTN",
    "drum": "DRUM", "drums": "DRUM",
    "roll": "ROLL", "rolls": "ROLL",
    "bdl": "BDL", "bundle": "BDL", "bundles": "BDL",
    "m": "M", "mtr": "M", "metre": "M", "meter": "M",
    "yd": "YD", "yds": "YD", "yard": "YD", "yards": "YD",
}

# length units are compared as physical length, so 1200 yd == 1097.28 m
LENGTH_TO_M = {"M": 1.0, "YD": 0.9144}


def supplier_code(name):
    low = (name or "").lower()
    for code, keys in SUPPLIERS.items():
        if any(k in low for k in keys):
            return code
    return None


def norm_uom(raw):
    return UOM_MAP.get((raw or "").strip().lower())


def norm_item(raw):
    """Return (normalised_item, note). Upper-case, squeeze spaces, and read the
    handwritten 'H202' as 'H2O2' (digit zero vs letter O); the fix is reported."""
    s = re.sub(r"\s+", " ", (raw or "").strip().upper())
    fixed = re.sub(r"\bH2[0O]2\b", "H2O2", s)
    note = f"item '{raw.strip()}' read as '{fixed}'" if fixed != s else None
    return fixed, note


def physical(qty, uom):
    """(unit family, quantity in base unit) used for comparing and totalling."""
    if qty is None or uom is None:
        return None
    if uom in LENGTH_TO_M:
        return ("LENGTH_M", round(qty * LENGTH_TO_M[uom], 3))
    return (uom, round(qty, 3))


def fmt_qty(q):
    if q is None:
        return "?"
    if float(q) == int(q):
        return str(int(q))
    return f"{q:.3f}".rstrip("0").rstrip(".")
