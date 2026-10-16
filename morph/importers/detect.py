"""What is this file? Magic bytes and content, never the extension alone.

  sniff("so101.urdf")        -> "urdf"
  scan("work/bundle")        -> [{"path", "format", "size", "kind"}, ...]

Formats: urdf, xacro, mjcf, sdf, package_xml, dae, stl, obj, ply, off, glb, gltf, 3mf, step, iges, kicad_pcb,
kicad_sch, kicad_legacy, csv, tsv, markdown, xlsx, json, morph_project, zip, tar, gzip, bz2, xz, 7z, rar,
license, readme, image, native_cad, usd, binary, text, unknown. An extension only breaks ties between text
formats (a .csv that is plain text); a file is never trusted because of its name.
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile

from .common import walk_files

KIND = {  # format -> what Morph does with it
    "urdf": "robot", "xacro": "robot", "mjcf": "robot", "sdf": "robot",
    "stl": "mesh", "obj": "mesh", "ply": "mesh", "off": "mesh", "glb": "mesh", "gltf": "mesh", "3mf": "mesh",
    "dae": "mesh", "step": "cad", "iges": "cad",
    "kicad_pcb": "board", "kicad_sch": "board", "kicad_legacy": "board",
    "csv": "table", "tsv": "table", "markdown": "table", "xlsx": "table",
    "morph_project": "project", "zip": "archive", "tar": "archive", "gzip": "archive", "bz2": "archive",
    "xz": "archive", "7z": "archive", "rar": "archive",
    "package_xml": "meta", "license": "meta", "readme": "meta", "json": "meta",
    "native_cad": "refuse", "usd": "refuse", "image": "ignore", "binary": "ignore", "text": "ignore",
    "unknown": "ignore",
}
LICENSE_RX = re.compile(r"^(licen[sc]e|copying|notice)([._-].*)?$", re.I)
README_RX = re.compile(r"^readme(\.(md|markdown|txt|rst))?$", re.I)
NATIVE_EXT = re.compile(r"\.(sldprt|sldasm|slddrw|f3d|f3z|ipt|iam|prt|asm|catpart|catproduct|par|psm|blend|skp|3dm|fcstd)$", re.I)
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _root_tag(text: str) -> str:
    """First element name of an XML document (after the declaration, comments and processing instructions)."""
    t = re.sub(r"<\?.*?\?>|<!--.*?-->", "", text, flags=re.S).lstrip()
    m = re.match(r"<\s*([A-Za-z_][\w:.-]*)", t)
    return m.group(1).lower() if m else ""


def _zip_format(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist()[:5000])
    except (zipfile.BadZipFile, OSError, ValueError):
        return "unknown"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "3D/3dmodel.model" in names or any(n.lower().endswith("3dmodel.model") for n in names):
        return "3mf"
    if any(n.lower().endswith(".usdc") or n.lower().endswith(".usda") for n in names) and len(names) < 50 and \
            os.path.splitext(path)[1].lower() == ".usdz":
        return "usd"
    tops = {n.split("/", 2)[-1] if n.count("/") >= 1 else n for n in names}
    if {"export-manifest.json", "morph_export.json"} & (names | tops) or \
            ("manifest.json" in names and "meta.json" in names):
        return "morph_project"
    return "zip"


def sniff(path: str, name: str | None = None) -> str:
    """Format id from the first bytes (and the size for binary STL)."""
    name = name or os.path.basename(path)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return "unknown"
    ext = os.path.splitext(name)[1].lower()
    if LICENSE_RX.match(name) and not head.startswith(b"PK"):
        return "license"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return _zip_format(path)
    if head.startswith(b"glTF"):
        return "glb"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"BZh"):
        return "bz2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if len(head) > 262 and head[257:262] == b"ustar":
        return "tar"
    if head.startswith(OLE2) or head.startswith(b"BLENDER") or NATIVE_EXT.search(name):
        return "native_cad"
    if head.startswith(b"PXR-USDC") or head.startswith(b"#usda"):
        return "usd"
    if head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8\xff") or head[:4] == b"GIF8" or \
            (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if head.startswith(b"MZ") or head.startswith(b"\x7fELF") or head.startswith(b"\xca\xfe\xba\xbe") or \
            head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        return "binary"
    # binary STL: an 80-byte header, a triangle count, 50 bytes per triangle (the header may say "solid")
    if size >= 84:
        n = int.from_bytes(head[80:84], "little")
        if 84 + 50 * n == size:
            return "stl"
    if head.startswith(b"ply\n") or head.startswith(b"ply\r\n"):
        return "ply"
    if b"\x00" in head[:8192]:
        return "binary"
    if head[:3] == b"\xef\xbb\xbf":
        text = head[3:].decode("utf-8", errors="replace")
    elif head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = head.decode("utf-16", errors="replace")
    else:
        text = head.decode("utf-8", errors="replace")
    s = text.lstrip()
    if s.startswith("ISO-10303-21"):
        return "step"
    if re.match(r"^.{72}S\s*0*1\s*$", text.split("\n", 1)[0].rstrip("\r")):
        return "iges"
    if s.startswith("(kicad_pcb"):
        return "kicad_pcb"
    if s.startswith("(kicad_sch"):
        return "kicad_sch"
    if s.startswith("EESchema") or s.startswith("PCBNEW-BOARD"):
        return "kicad_legacy"
    if s.startswith("<"):
        tag = _root_tag(s)
        if tag == "robot":
            return "xacro" if ("xmlns:xacro" in text or "<xacro:" in text) else "urdf"
        if tag == "mujoco":
            return "mjcf"
        if tag == "sdf":
            return "sdf"
        if tag == "package":
            return "package_xml"
        if tag == "collada":
            return "dae"
    if re.match(r"^solid\b", s) and "facet" in s[:4096]:
        return "stl"
    if s.startswith("OFF") and re.match(r"^OFF\s", s):
        return "off"
    if s.startswith("{"):
        if '"asset"' in s[:4096] and ('"meshes"' in s or '"accessors"' in s or '"buffers"' in s):
            return "gltf"
        return "json"
    if README_RX.match(name) or ext in (".md", ".markdown"):
        return "markdown" if re.search(r"^\s*\|.*\|\s*$\n^\s*\|?\s*:?-{3,}", text, re.M) else "readme"
    lines = [ln for ln in text.splitlines()[:200] if ln.strip() and not ln.lstrip().startswith("#")]
    if lines and sum(bool(re.match(r"^(v|vn|vt|f|o|g|s|mtllib|usemtl|l)\s", ln)) for ln in lines) >= 0.8 * len(lines) \
            and any(ln.startswith("v ") for ln in lines):
        return "obj"
    if re.search(r"^\s*\|.*\|\s*$\n^\s*\|?\s*:?-{3,}", text, re.M):
        return "markdown"
    if ext in (".csv", ".tsv", ".txt") or "\t" in text[:2048] or text.count(",") > 4:
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
            rows = [r for r in csv.reader(io.StringIO(text[:8192]), dialect) if r]
            lead = 0                                   # a KiCad grouped BOM starts "Source:", "Date:", "Tool:", ...
            while lead < min(10, len(rows) - 2) and len(rows[lead]) <= 2 and rows[lead][0].strip().endswith(":"):
                lead += 1
            widths = [len(r) for r in rows[lead:]]
            if len(widths) >= 2 and max(widths) >= 2 and widths.count(widths[0]) >= 0.6 * len(widths):
                return "tsv" if dialect.delimiter == "\t" else "csv"
        except csv.Error:
            pass
    return "text"


def kind(fmt: str) -> str:
    return KIND.get(fmt, "ignore")


def scan(root: str, max_files: int = 20000) -> list:
    """Every regular file under root with its sniffed format (symlinks and hidden files skipped)."""
    out = []
    for rel in walk_files(root, max_files):
        p = os.path.join(root, *rel.split("/"))
        fmt = sniff(p, os.path.basename(rel))
        out.append({"path": rel, "format": fmt, "kind": kind(fmt), "size": os.path.getsize(p)})
    return out


REFUSE = {
    "native_cad": "{name} is a native CAD file Morph can't read. Export a URDF (onshape-to-robot, fusion2urdf or "
                  "sw_urdf_exporter) for a robot, or a STEP or STL for a part.",
    "usd": "{name} is a USD scene; Morph imports URDF or MuJoCo XML robots and STL/OBJ/STEP meshes.",
    "sdf": "{name} is an SDF model; Morph reads URDF and MuJoCo XML. Convert it to a URDF first (sdf2urdf).",
    "kicad_legacy": "{name} is a KiCad 5 or older file; open it in KiCad 6 or newer and save it again.",
    "7z": "{name} is a 7-Zip archive; unpack it and drop the folder (or a .zip) instead.",
    "rar": "{name} is a RAR archive; unpack it and drop the folder (or a .zip) instead.",
}


def refusal(fmt: str, name: str) -> str | None:
    msg = REFUSE.get(fmt)
    return msg.format(name=name) if msg else None
