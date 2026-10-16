"""Parts lists someone already has -> BOM rows Morph and Darwin understand.

  res = parse("README.md")       # Markdown tables, CSV, TSV or XLSX (read with the standard library)
  res["rows"][0] -> {"item": "STS3215 Servo 7.4V, 1/345 gear (C001)", "qty": 7, "unit_price": 13.89,
                     "ext_price": 97.23, "currency": "USD", "url": "https://...", "make_or_buy": "buy",
                     "category": "actuator", "catalog": "sts3215_7v4", ...}
  darwin_parts(res["rows"])      # [(item, Darwin query)] for sourcing

Headers are matched by synonym (Part/Item/Description, Qty/Amount, MPN/Part number, Unit Cost/Price, Buy/Link,
Ref/Designator, ...). With several price columns the US dollar one wins (the SO-ARM100 README lists US, EU, RMB
and JPY prices side by side). Total rows are skipped and recomputed.
"""

from __future__ import annotations

import csv
import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile

from .common import MB, ImportFailed, check_xml_text

FIELDS = [  # matched in this order, so "Part number" becomes the MPN before "Part" can become the item
    ("mpn", r"\bmpn\b|mfr\.?\s*(part|p/?n|#)|manufacturer\s*part|part\s*(number|no\.?|#)|\bp/?n\b|lcsc|digi-?key\s*(part|#)"),
    ("ref", r"^(ref|refs|reference|references|designator|designators)\b"),
    ("ext_price", r"ext(ended)?\.?\s*(price|cost)|line\s*total|sub-?total|total\s*(price|cost)"),
    ("unit_price", r"unit\s*(cost|price)|^price|^cost|price\s*\(|cost\s*\(|\bprice\b|\bcost\b"),
    ("qty", r"^(qty|quantity|amount|count|pcs|q'?ty|number|no\.?|#|units)\b"),
    ("url", r"\b(buy|link|url|where\s*to\s*buy|purchase|shop|store\s*link|source)\b"),
    ("manufacturer", r"^(manufacturer|mfr|mfg|brand|maker)\b"),
    ("supplier", r"^(supplier|vendor|distributor|seller|store)\b"),
    ("item", r"^(part|parts|item|items|component|components|name|product|designation|material|value)\b"),
    ("description", r"^(description|desc\.?|details|notes?|comments?|specs?|specification)\b"),  # the part name if there is no other
]
CURRENCY = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
            "¥": "CNY", "￥": "CNY", "rmb": "CNY", "cny": "CNY", "jpy": "JPY", "yen": "JPY"}
HEAD_CUR = [(r"\(us\)|\busd\b|\bus\b|\$", "USD"), (r"\(eu\)|\beur\b|€", "EUR"), (r"\brmb\b|\bcny\b|\bcn\b", "CNY"),
            (r"\bjpy\b|\bjp\b|\byen\b", "JPY"), (r"\bgbp\b|£", "GBP")]
SKIP_ITEM = re.compile(r"^(total|totals|sum|subtotal|grand total)\b", re.I)


def _cell(s) -> tuple[str, str | None]:
    """Markdown/HTML cell -> (text, first link URL)."""
    s = "" if s is None else str(s)
    url = None
    m = re.search(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", s)
    if m:
        url = m.group(2)
    else:
        m2 = re.search(r"https?://[^\s|)\"'<>]+", s)
        url = m2.group(0) if m2 else None
    s = re.sub(r"<sup>.*?</sup>", "", s, flags=re.S | re.I)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"<[^>]+>|\*\*|__|`", "", s)
    return re.sub(r"\s+", " ", s).strip(), url


def _money(s: str, default_cur: str | None = None) -> tuple[float | None, str | None]:
    t = (s or "").strip()
    if not t or re.fullmatch(r"[-–—/]+|n/?a|tbd|\?", t, re.I):
        return None, None
    cur = None
    for sym, code in CURRENCY.items():
        if sym in t.lower():
            cur = code
            break
    m = re.search(r"(\d[\d,.\s]*)", t)
    if not m:
        return None, cur or default_cur
    num = m.group(1).strip().replace(" ", "")
    if re.fullmatch(r"\d+,\d{1,2}", num):          # decimal comma: 12,20
        num = num.replace(",", ".")
    num = num.replace(",", "")
    try:
        return round(float(num), 4), cur or default_cur
    except ValueError:
        return None, cur or default_cur


def _qty(s: str) -> int | None:
    m = re.search(r"\d+(\.\d+)?", s or "")
    if not m:
        return None
    try:
        return max(int(round(float(m.group(0)))), 0)
    except ValueError:
        return None


# a motor named by its kind and up to three words before it (a brand or the joint it drives: "Feetech servo", "Gripper
# servo"), and a count after it ("Servo (x6)"); a line that is any of the accessories below is never one
GENERIC_MOTOR = re.compile(r"(?:[\w.-]+\s+){0,3}?(?:servo|motor|actuator)(?:\s+motor)?s?"
                           r"(?:\s*\(\s*(?:x\s*)?\d+\s*(?:pcs?)?\s*\)|\s+x\s*\d+)?", re.I)
# a part for a motor, named after it: "STS3215 servo horn", "Bus servo driver board for STS3215", "12V power supply for
# STS3215 servos", "M3 screw for STS3215": the thing the name names is one of these (_accessory). What a
# line comes with or is described by ("... with horn", "(incl. screws)", "..., horn included", "... - plastic case",
# "... built-in controller", "... Servo Motor 12V Metal Case") doesn't count
ACCESSORY = re.compile(r"\b(?:horns?|brackets?|mounts?|mounting|holders?|plates?|covers?|cases?|adapters?|clips?|caps?|"
                       r"boards?|drivers?|controllers?|supply|supplies|psus?|screws?|bolts?|nuts?|washers?|cables?|"
                       r"wires?|harness(?:es)?|hubs?|extensions?|testers?|chargers?|batter(?:y|ies))\b", re.I)
_COMES_WITH = re.compile(r"\(|\b(?:with|incl|including|includes|included|built[- ]?in|for)\b|\bw/|\+|[,;]|\s[-\u2013\u2014]\s",
                         re.I)
_SPEC = re.compile(r"\d+(?:[.,]\d+)?(?:v|vdc|kg(?:[.]?cm|f)?|n[\u00b7.]?m|ncm|m?a|mah|rpm|w|g)", re.I)   # 12V, 30kg.cm
_MOTOR_WORD = re.compile(r"(?:servo|motor|actuator)s?", re.I)
_GROUP = re.compile(r"(?:kit|set|pack|pair|bundle|pcs?|pieces?)", re.I)


def _accessory(item: str) -> bool:
    """Whether a part name names an accessory (ACCESSORY): its head noun, the last plain word before what it comes with
    or is described by ("STS3215 servo horn", "Servo horn kit": horn), or a word before the motor word ("Power supply
    servo"). After the motor word, a spec ends the name ("Servo Motor 12V Metal Case" is a motor), and part numbers and
    specs aren't words ("STS3215 12V": none)."""
    head = re.sub(r"(\d)\s+(v|vdc|a|ma|mah|kg|w|g|rpm)\b", r"\1\2", _COMES_WITH.split(item, 1)[0], flags=re.I)
    plain, before, motor = [], [], False
    for w in head.split():
        w = w.strip(".:")
        if motor and _SPEC.fullmatch(w):
            break                                          # "STS3215 Servo Motor 12V Metal Case": what follows describes it
        if _MOTOR_WORD.fullmatch(w):
            motor = True
        if re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+)*", w):
            plain.append(w)
            if not motor:
                before.append(w)
    while plain and _GROUP.fullmatch(plain[-1]):
        plain.pop()
    return bool(plain and ACCESSORY.fullmatch(plain[-1])) or any(ACCESSORY.fullmatch(w) for w in before)


def _catalog_key(item: str, mpn: str = "", detail: str = "") -> str | None:
    """The catalog actuator a BOM line names. Only its part name and MPN can name one: a screw or a driver board whose
    notes mention the servo it is for is not a servo, and neither is a horn, a bracket, a power supply or a board named
    after the servo it is for (ACCESSORY, the name's head noun: not what it comes with). The description, notes or specs
    (`detail`) then only pick the variant of the motor the name already names, when the name doesn't say: "STS3215"
    with "12V" in its description is the 12 V servo. A line named only as a motor ("Servo", "Bus servo", "Feetech
    servo", "Gripper servo", "Servo (x6)") is what its description says."""
    from .autoprofile import TEXT_RX, VARIANT_RX, VARIANTS
    if _accessory(item):
        return None
    name = " ".join(x for x in (item, mpn) if x)
    if detail and GENERIC_MOTOR.fullmatch(item.strip()) and not any(rx.search(name) for rx, _ in TEXT_RX):
        name = f"{name} {detail}"
    key = next((k for rx, k in TEXT_RX if rx.search(name)), None)
    motor = VARIANTS.get(key)
    if motor:
        for text in (name, detail):
            pick = next((k for rx, k in VARIANT_RX if VARIANTS.get(k) == motor and rx.search(text)), None)
            if pick:
                return pick
    return key


def _catalog_name(key: str | None) -> str | None:
    if not key:
        return None
    from ..robot import catalog, catalog_extra  # noqa: F401  (catalog_extra adds the 7.4 V STS3215)
    return (catalog.ACTUATORS.get(key) or {}).get("name")


def _category(item: str, key: str | None) -> str:
    t = item.lower()
    if key:
        return "actuator"
    if re.search(r"\b(screw|bolt|nut|washer|insert|standoff|fastener)s?\b", t):
        return "fastener"
    if re.search(r"\bcable|wire|harness\b", t):
        return "cable"
    if re.search(r"\b(board|controller|adapter|servo driver|motor driver|power supply|psu|battery|arduino|esp32|"
                 r"raspberry pi|sensor|camera|u2d2)\b", t):
        return "electronics"
    if re.search(r"filament|pla\b|petg|resin|sheet|plate stock", t):
        return "material"
    return "purchased"


def map_table(rows: list) -> tuple[list, dict, list]:
    """Rows of cells (first row = header, possibly after title rows) -> (BOM rows, {field: header}, warnings)."""
    warns = []
    hi, head = None, None
    for i, r in enumerate(rows[:10]):
        cells = [_cell(c)[0].lower() for c in r]
        hits = sum(any(re.search(rx, c) for _, rx in FIELDS) for c in cells if c)
        if hits >= 2:
            hi, head = i, cells
            break
    if head is None:
        return [], {}, ["no header row with a part and a quantity or price column"]
    cols = {}
    used = set()
    for field, rx in FIELDS:
        idx = [i for i, h in enumerate(head) if h and i not in used and re.search(rx, h)]
        if not idx:
            continue
        if field in ("unit_price", "ext_price", "url") and len(idx) > 1:
            usd = [i for i in idx if any(re.search(p, head[i]) for p, c in HEAD_CUR if c == "USD")]
            idx = usd or idx
        cols[field] = idx[0]
        used.add(idx[0])
    if "item" not in cols and "description" in cols:
        cols["item"] = cols.pop("description")
    if "item" not in cols:
        cand = [i for i, h in enumerate(head) if i not in used and h]
        if "mpn" in cols and not cand:
            cols["item"] = cols["mpn"]
        elif cand:
            cols["item"] = cand[0]
            warns.append(f"used the column {head[cand[0]]!r} as the part name")
        else:
            return [], {}, ["no part name column"]
    head_cur = None
    if "unit_price" in cols:
        head_cur = next((c for p, c in HEAD_CUR if re.search(p, head[cols["unit_price"]])), None)
    out = []
    for r in rows[hi + 1:]:
        get = lambda f: r[cols[f]] if f in cols and cols[f] < len(r) else ""  # noqa: E731
        item, link = _cell(get("item"))
        if not item or SKIP_ITEM.match(item) or set(item) <= set("-:| "):
            continue
        qty = _qty(_cell(get("qty"))[0]) if "qty" in cols else None
        if "qty" in cols and qty == 0 and not get("qty").strip():
            qty = None
        unit, cur = _money(_cell(get("unit_price"))[0], head_cur) if "unit_price" in cols else (None, None)
        ext, cur2 = _money(_cell(get("ext_price"))[0], head_cur) if "ext_price" in cols else (None, None)
        url = _cell(get("url"))[1] if "url" in cols else None
        url = url or link
        q = qty if qty is not None else 1
        if ext is None and unit is not None:
            ext = round(unit * q, 2)
        if unit is None and ext is not None and q:
            unit = round(ext / q, 4)
        mpn = _cell(get("mpn"))[0] if "mpn" in cols else ""
        desc = _cell(get("description"))[0] if "description" in cols else ""
        key = _catalog_key(item, mpn, desc)
        out.append({"item": item[:200], "qty": q, "unit_price": unit, "ext_price": ext, "currency": cur or cur2,
                    "url": url if url and re.match(r"^https?://", url) else None, "mpn": mpn[:80] or None,
                    "manufacturer": _cell(get("manufacturer"))[0][:80] or None if "manufacturer" in cols else None,
                    "supplier": _cell(get("supplier"))[0][:80] or None if "supplier" in cols else None,
                    "ref": _cell(get("ref"))[0][:200] or None if "ref" in cols else None,
                    "description": desc[:200] or None, "make_or_buy": "buy", "category": _category(item, key),
                    "catalog": key, "catalog_name": _catalog_name(key), "source": "import"})
    return out, {f: rows[hi][i] if i < len(rows[hi]) else "" for f, i in cols.items()}, warns


# ------------------------------------------------------------------- readers
def markdown_tables(text: str) -> list:
    """[(title, rows)]: every pipe table with a separator row; title = the nearest heading above it."""
    out, rows, title, cur_title = [], [], None, None
    lines = text.splitlines() + [""]
    for ln in lines:
        s = ln.strip()
        if s.startswith("#"):
            cur_title = s.lstrip("#").strip()
        if s.startswith("|"):
            s = s.replace("\\|", "\x00")
            cells = [c.replace("\x00", "|").strip() for c in s.strip("|").split("|")]
            if rows and len(rows) == 1 and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                rows.append(None)                     # separator marker
                continue
            if not rows:
                title = cur_title
            rows.append(cells)
        elif rows:
            if len(rows) > 2 and rows[1] is None:
                out.append((title, [rows[0]] + rows[2:]))
            rows = []
    return out


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", errors="replace")


def csv_rows(text: str, fmt: str) -> list:
    sample = text[:16384]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t" if fmt == "tsv" else ",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if fmt == "tsv" else csv.excel
    rows = []
    for i, r in enumerate(csv.reader(io.StringIO(text), dialect)):
        if i > 20000:
            raise ImportFailed("The table has more than 20,000 rows; that doesn't look like a parts list.")
        rows.append([c[:2000] for c in r[:60]])
    return rows


def xlsx_rows(path: str, sheet: int = 0) -> list:
    """First worksheet of an .xlsx as rows of strings, with the standard library only."""
    from .zipsafe import read_member

    def col(ref):
        n = 0
        for ch in re.match(r"[A-Z]+", ref).group(0):
            n = n * 26 + ord(ch) - 64
        return n - 1

    def xml(zf, name, cap=20 * MB):
        data = read_member(zf, name, cap)
        check_xml_text(data, name)
        return ET.fromstring(data)

    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ImportFailed(f"{os.path.basename(path)} is not a readable .xlsx file.") from None
    with zf:
        if len(zf.infolist()) > 2000:
            raise ImportFailed("The workbook has too many parts.")
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
              "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        names = set(zf.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in xml(zf, "xl/sharedStrings.xml").findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        target = "xl/worksheets/sheet1.xml"
        if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
            wb = xml(zf, "xl/workbook.xml")
            sheets = wb.findall("m:sheets/m:sheet", ns)
            rels = {r.get("Id"): r.get("Target") for r in xml(zf, "xl/_rels/workbook.xml.rels")}
            if sheets:
                rid = sheets[min(sheet, len(sheets) - 1)].get(f"{{{ns['r']}}}id")
                t = (rels.get(rid) or "").lstrip("/")
                t = t if t.startswith("xl/") else "xl/" + t
                if t in names:
                    target = t
        if target not in names:
            raise ImportFailed("The workbook has no worksheet.")
        rows = []
        for row in xml(zf, target, 50 * MB).iter(f"{{{ns['m']}}}row"):
            cells = {}
            for c in row.findall("m:c", ns):
                ref, typ = c.get("r") or "", c.get("t")
                v = c.find("m:v", ns)
                if typ == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                    val = shared[int(v.text)]
                elif typ == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter(f"{{{ns['m']}}}t"))
                else:
                    val = v.text if v is not None and v.text is not None else ""
                if ref:
                    cells[col(ref)] = val
            if cells:
                width = min(max(cells) + 1, 60)
                rows.append([cells.get(i, "") for i in range(width)])
            if len(rows) > 20000:
                raise ImportFailed("The sheet has more than 20,000 rows; that doesn't look like a parts list.")
        return rows


def parse(path: str, fmt: str | None = None, table: int | None = None) -> dict:
    """{"rows", "columns", "tables": [{"index", "title", "rows"}], "table", "total", "currency", "warnings"}"""
    from .detect import sniff

    fmt = fmt or sniff(path)
    if os.path.getsize(path) > 20 * MB:
        raise ImportFailed(f"{os.path.basename(path)} is over 20 MB; that's not a parts list.")
    if fmt == "xlsx":
        tables = [(None, xlsx_rows(path))]
    else:
        with open(path, "rb") as fh:
            text = _decode(fh.read())
        if fmt in ("markdown", "readme", "text") and "|" in text:
            tables = markdown_tables(text)
        elif fmt in ("csv", "tsv", "text"):
            tables = [(None, csv_rows(text, fmt))]
        else:
            raise ImportFailed(f"{os.path.basename(path)} is not a table Morph can read.")
    mapped = []
    for i, (title, rows) in enumerate(tables):
        out, cols, warns = map_table(rows)
        if out and ("qty" in cols or "unit_price" in cols or "ext_price" in cols or "mpn" in cols):
            mapped.append({"index": len(mapped), "title": title, "rows": out, "columns": cols, "warnings": warns})
    if not mapped:
        raise ImportFailed(f"No parts list found in {os.path.basename(path)}: Morph looks for a table with a part "
                           "column and a quantity or price column.")
    pick = mapped[table] if table is not None and 0 <= table < len(mapped) else mapped[0]
    rows = pick["rows"]
    curs = {r["currency"] for r in rows if r.get("currency")}
    total = round(sum(r["ext_price"] for r in rows if r.get("ext_price") is not None), 2) if len(curs) <= 1 else None
    warns = list(pick["warnings"])
    if len(curs) > 1:
        warns.append(f"prices in several currencies ({', '.join(sorted(curs))}); no total")
    if any(r["unit_price"] is None for r in rows):
        warns.append(f"{sum(r['unit_price'] is None for r in rows)} rows have no price")
    return {"rows": rows, "columns": pick["columns"], "table": pick["index"], "title": pick["title"],
            "tables": [{"index": t["index"], "title": t["title"], "rows": len(t["rows"])} for t in mapped],
            "total": total, "currency": next(iter(curs)) if len(curs) == 1 else None, "warnings": warns}


def darwin_parts(rows: list) -> list:
    """[(item, query)] Darwin can search: one per purchased line (fasteners and cables as one kit each)."""
    from .. import darwin

    out, seen, kits = [], set(), {}
    for r in rows:
        if r.get("make_or_buy") != "buy":
            continue
        if r.get("category") in ("fastener", "cable"):
            kits.setdefault(r["category"], r["item"])
            continue
        q = darwin.short_item(r.get("mpn") or r["item"]) if r.get("mpn") else darwin.short_item(r["item"])
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append((r["item"], darwin.part_query(q)))
    for cat, item in kits.items():
        out.append((item, darwin.part_query(darwin.short_item(item))))
    return out


def actuator_keys(rows: list) -> dict:
    """{catalog key: qty} for the actuators a parts list names (seeds the import's actuator picker)."""
    out = {}
    for r in rows:
        if r.get("catalog"):
            out[r["catalog"]] = out.get(r["catalog"], 0) + int(r.get("qty") or 1)
    return out
