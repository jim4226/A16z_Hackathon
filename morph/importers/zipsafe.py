"""Safe archive extraction for imports: our own extractor, never ZipFile.extractall.

  files, warnings = extract("robot.zip", "work/unpacked")

Limits (contracts section 9): at most 2,000 entries, 300 MB in total, 100 MB per file and a 100x compression
ratio, counted while the bytes are written (a lying size header cannot get past the counter). Absolute, `..`,
drive-letter and NUL paths refuse the whole archive; symlinks, hidden files and nested archives are skipped
with a warning. Tar archives go through the same checks member by member (regular files only).
"""

from __future__ import annotations

import os
import re
import stat
import tarfile
import zipfile
import zlib

from .common import LIMITS, MB, ImportFailed, contained

NESTED = re.compile(r"\.(zip|tar|tgz|gz|bz2|xz|7z|rar|whl|jar|apk)$", re.I)
SKIP = re.compile(r"(^|/)(__MACOSX|\.git|\.svn|\.hg)(/|$)|(^|/)\.[^/]*$|(^|/)Thumbs\.db$", re.I)


def _clean(name: str) -> str | None:
    """Archive member name -> safe relative path; None for directory entries. Raises on hostile names."""
    n = name.replace("\\", "/")
    if n.endswith("/"):
        return None
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or "\x00" in n or ":" in n or any(ord(c) < 32 for c in n):
        raise ImportFailed(f"The archive has an unsafe path ({name[:80]!r}); Morph refuses archives with absolute "
                           "or drive paths.")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ImportFailed(f"The archive has a '..' path ({name[:80]!r}) that would write outside its folder.")
    if not parts or any(len(p) > 255 for p in parts):
        raise ImportFailed(f"The archive has an invalid path ({name[:80]!r}).")
    return "/".join(parts)


def _limits(lim: dict | None) -> dict:
    out = {k: LIMITS[k] for k in ("zip_entries", "zip_total", "zip_file", "zip_ratio")}
    out.update(lim or {})
    return out


def _copy(src, dst_path: str, size_hint: int, budget: dict, lim: dict, name: str):
    """Stream src to a new file, counting real bytes against the per-file and total limits."""
    written = 0
    with open(dst_path, "xb") as out:
        while True:
            chunk = src.read(MB)
            if not chunk:
                break
            written += len(chunk)
            budget["total"] += len(chunk)
            if written > lim["zip_file"]:
                raise ImportFailed(f"{name} unpacks to more than {lim['zip_file'] // MB} MB; the per-file limit is "
                                   f"{lim['zip_file'] // MB} MB.")
            if budget["total"] > lim["zip_total"]:
                raise ImportFailed(f"The archive unpacks to more than {lim['zip_total'] // MB} MB, the import limit.")
            if size_hint >= 0 and written > size_hint:
                raise ImportFailed(f"{name} is larger than its archive header says; the archive looks tampered with.")
            out.write(chunk)
    return written


def _target(dest: str, rel: str) -> str:
    path = os.path.join(dest, *rel.split("/"))
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if contained(dest, parent) is None:      # a symlinked folder already in dest: never write through it
        raise ImportFailed(f"Refused to write {rel[:80]!r} outside the import folder.")
    return path


def is_symlink_zipinfo(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16) if info.create_system == 3 else False


def extract_zip(path: str, dest: str, limits: dict | None = None) -> tuple[list, list]:
    lim = _limits(limits)
    os.makedirs(dest, exist_ok=True)
    files, warns, budget = [], [], {"total": 0}
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ImportFailed(f"{os.path.basename(path)} is not a readable zip ({exc}).") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > lim["zip_entries"]:
            raise ImportFailed(f"The archive has {len(infos):,} entries; the limit is {lim['zip_entries']:,}. "
                               "Zip only the robot's folder.")
        declared = sum(max(i.file_size, 0) for i in infos)
        if declared > lim["zip_total"]:
            raise ImportFailed(f"The archive unpacks to {declared / MB:.0f} MB; the limit is {lim['zip_total'] // MB} MB.")
        archive_size = max(os.path.getsize(path), 1)
        if declared > 10 * MB and declared / archive_size > lim["zip_ratio"]:
            raise ImportFailed(f"The archive expands {declared / archive_size:.0f}x; Morph refuses archives over "
                               f"{lim['zip_ratio']}x (a zip bomb guard).")
        seen = set()
        for info in infos:
            rel = _clean(info.filename)
            if rel is None:
                continue
            if SKIP.search(rel):
                continue
            if is_symlink_zipinfo(info):
                warns.append(f"skipped the symlink {rel}")
                continue
            if info.flag_bits & 0x1:
                raise ImportFailed("The archive is password-protected; unzip it and drop the folder instead.")
            if NESTED.search(rel):
                warns.append(f"skipped the nested archive {rel}: unpack it and drop the folder instead")
                continue
            if info.file_size > lim["zip_file"]:
                raise ImportFailed(f"{rel} is {info.file_size / MB:.0f} MB; the per-file limit is {lim['zip_file'] // MB} MB.")
            if info.file_size > MB and info.file_size / max(info.compress_size, 1) > lim["zip_ratio"]:
                raise ImportFailed(f"{rel} expands {info.file_size / max(info.compress_size, 1):.0f}x; Morph refuses "
                                   f"entries over {lim['zip_ratio']}x (a zip bomb guard).")
            key = rel.lower()                  # Windows and macOS folders are case-insensitive
            if key in seen:
                warns.append(f"skipped a second copy of {rel}")
                continue
            seen.add(key)
            target = _target(dest, rel)
            try:
                with zf.open(info) as src:
                    _copy(src, target, info.file_size, budget, lim, rel)
            except FileExistsError:
                warns.append(f"skipped a second copy of {rel}")
                continue
            except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError, ValueError) as exc:
                if isinstance(exc, ImportFailed):
                    raise
                raise ImportFailed(f"{rel} could not be unpacked ({exc}); the archive may be damaged.") from None
            files.append(rel)
    return files, warns


def extract_tar(path: str, dest: str, limits: dict | None = None) -> tuple[list, list]:
    lim = _limits(limits)
    os.makedirs(dest, exist_ok=True)
    files, warns, budget, n, seen = [], [], {"total": 0}, 0, set()
    try:
        tf = tarfile.open(path, "r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ImportFailed(f"{os.path.basename(path)} is not a readable tar archive ({exc}).") from None
    with tf:
        try:
            for m in tf:
                n += 1
                if n > lim["zip_entries"]:
                    raise ImportFailed(f"The archive has more than {lim['zip_entries']:,} entries; zip only the robot's folder.")
                if m.isdir():
                    continue
                rel = _clean(m.name)
                if rel is None or SKIP.search(rel):
                    continue
                if not m.isreg():
                    warns.append(f"skipped {rel}: links and special files are not imported")
                    continue
                if NESTED.search(rel):
                    warns.append(f"skipped the nested archive {rel}: unpack it and drop the folder instead")
                    continue
                if m.size > lim["zip_file"]:
                    raise ImportFailed(f"{rel} is {m.size / MB:.0f} MB; the per-file limit is {lim['zip_file'] // MB} MB.")
                if rel.lower() in seen:
                    warns.append(f"skipped a second copy of {rel}")
                    continue
                seen.add(rel.lower())
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src:
                    _copy(src, _target(dest, rel), m.size, budget, lim, rel)
                files.append(rel)
        except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
            raise ImportFailed(f"The archive could not be unpacked ({exc}); it may be damaged.") from None
    archive_size = max(os.path.getsize(path), 1)
    if budget["total"] > 10 * MB and budget["total"] / archive_size > lim["zip_ratio"]:
        raise ImportFailed(f"The archive expands {budget['total'] / archive_size:.0f}x; Morph refuses archives over "
                           f"{lim['zip_ratio']}x (a zip bomb guard).")
    return files, warns


def extract(path: str, dest: str, kind: str | None = None, limits: dict | None = None) -> tuple[list, list]:
    """Unpack a zip or tar(.gz/.bz2/.xz) into dest. Returns (relative file paths, warnings)."""
    if kind is None:
        from .detect import sniff
        kind = sniff(path)
    if kind in ("zip", "xlsx", "3mf", "morph_project"):
        return extract_zip(path, dest, limits)
    if kind in ("tar", "gzip", "bz2", "xz"):
        return extract_tar(path, dest, limits)
    raise ImportFailed(f"{os.path.basename(path)} is not an archive Morph can unpack (zip or tar).")


def read_member(zf: zipfile.ZipFile, name: str, max_bytes: int) -> bytes:
    """One small member (a manifest, a design.json), size-checked before and while reading."""
    info = zf.getinfo(name)
    if info.file_size > max_bytes:
        raise ImportFailed(f"{name} is larger than {max_bytes // 1024} KB.")
    with zf.open(info) as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ImportFailed(f"{name} is larger than {max_bytes // 1024} KB.")
    return data
