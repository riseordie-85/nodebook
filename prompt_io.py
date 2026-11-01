"""Excel (and CSV) import/export for the prompt builder.

File layout - one column per category, header in row 1, one item per cell:

    | Style        | Camera       | Subject      | ...
    | Dark fantasy | Dutch tilt   | A cyber...   | ...

Headers match a category by its key ("camera") or its label
("Camera & Framing"), ignoring case, spaces and punctuation. Columns can be
different lengths; blank cells are skipped. A two-column "Category | Item"
layout (one row per item) is accepted too.

If the workbook has a sheet named "Prompts" that one is read, otherwise the
first sheet. Anything else in the workbook (e.g. a "How to use" tab) is
ignored.
"""
import csv
import io
import re

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

MAX_ITEM_LENGTH = 2000
MAX_ITEMS_PER_CATEGORY = 5000
_LEADING_NUMBER = re.compile(r"^\d{1,4}[.)]\s+")  # "12. text" -> "text"
_LONG_ITEM_HEADERS = {"item", "items", "prompt", "prompts", "text"}


class ImportError_(ValueError):
    """The file couldn't be read as a prompt list (message is user-facing)."""


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def clean_item(value):
    """Cell value -> item text, or None to skip it."""
    if value is None:
        return None
    text = " ".join(str(value).split())  # collapse newlines/runs of spaces
    text = _LEADING_NUMBER.sub("", text).strip()
    if not text:
        return None
    return text[:MAX_ITEM_LENGTH]


def dedupe(items):
    """Drop blanks and case-insensitive duplicates, keeping first-seen order."""
    seen, out = set(), []
    for raw in items:
        text = clean_item(raw)
        if text is None:
            continue
        k = text.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(text)
    return out[:MAX_ITEMS_PER_CATEGORY]


def _read_rows(filename, data):
    name = (filename or "").lower()
    if name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")  # Excel's "CSV" on Windows
        return [row for row in csv.reader(io.StringIO(text))]
    if name.endswith((".xlsx", ".xlsm")):
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:
            raise ImportError_("That file couldn't be opened as an Excel workbook.")
        try:
            ws = wb["Prompts"] if "Prompts" in wb.sheetnames else wb.worksheets[0]
            return [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    raise ImportError_("Upload an .xlsx or .csv file.")


def parse(filename, data, categories):
    """Read an uploaded file against the existing categories.

    categories: iterable of rows/dicts with id, key, label.
    Returns {"categories": {category_id: [items...]}, "unmatched": [header, ...]}
    """
    rows = [r for r in _read_rows(filename, data) if any(clean_item(c) for c in r)]
    if not rows:
        raise ImportError_("The file is empty.")

    lookup = {}
    for c in categories:
        lookup[_norm(c["key"])] = c["id"]
        lookup[_norm(c["label"])] = c["id"]

    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    normed = [_norm(h) for h in header]
    found = {}
    unmatched = []

    if "category" in normed and any(h in _LONG_ITEM_HEADERS for h in normed):
        # Long layout: Category | Item
        ci = normed.index("category")
        ii = next(i for i, h in enumerate(normed) if h in _LONG_ITEM_HEADERS)
        for r in rows[1:]:
            cat = r[ci] if ci < len(r) else None
            item = r[ii] if ii < len(r) else None
            cid = lookup.get(_norm(cat))
            if cid is None:
                if clean_item(cat) and clean_item(cat) not in unmatched:
                    unmatched.append(clean_item(cat))
                continue
            found.setdefault(cid, []).append(item)
    else:
        # Wide layout: one column per category
        for col, (h, n) in enumerate(zip(header, normed)):
            if not n:
                continue
            cid = lookup.get(n)
            if cid is None:
                unmatched.append(h)
                continue
            found.setdefault(cid, []).extend(r[col] if col < len(r) else None for r in rows[1:])

    result = {cid: dedupe(items) for cid, items in found.items()}
    result = {cid: items for cid, items in result.items() if items}
    if not result:
        names = ", ".join(c["label"] for c in categories)
        raise ImportError_(
            "No columns matched a category. Row 1 should hold category names: " + names + ".")
    return {"categories": result, "unmatched": unmatched}


def build_workbook(categories):
    """categories: list of {"label": str, "items": [str, ...]} -> xlsx bytes."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Prompts"
    head_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F2937")
    body_font = Font(name="Arial", size=10)
    wrap = Alignment(wrap_text=True, vertical="top")
    for col, cat in enumerate(categories, start=1):
        cell = ws.cell(row=1, column=col, value=cat["label"])
        cell.font, cell.fill = head_font, head_fill
        for row, text in enumerate(cat["items"], start=2):
            c = ws.cell(row=row, column=col, value=text)
            c.font, c.alignment = body_font, wrap
        ws.column_dimensions[cell.column_letter].width = 48
    ws.freeze_panes = "A2"

    help_ws = wb.create_sheet("How to use")
    lines = [
        "Nodebook prompt builder - import file",
        "",
        "Edit the Prompts sheet: one column per category, one prompt per cell.",
        "Keep the category names in row 1. Columns can be different lengths; blank cells are ignored.",
        "Import from the Generator page. Per category you can Add new (skips prompts already there) or Replace all.",
    ]
    for i, line in enumerate(lines, start=1):
        help_ws.cell(row=i, column=1, value=line).font = Font(name="Arial", size=10, bold=(i == 1))
    help_ws.column_dimensions["A"].width = 110

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
