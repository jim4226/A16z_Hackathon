"""Frames from a video the browser can't play (an iPhone HEVC .mov on Linux, older Windows, Firefox): the Studio
computer runs an ffmpeg program once over the clip and keeps a few evenly spaced JPEG frames.

    from morph.studio import ffmpeg
    ffmpeg.find()                                  # MORPH_FFMPEG, then ffmpeg on PATH, then imageio-ffmpeg (the video extra)
    ffmpeg.extract("clip.mov", count=8)            # {"frames": [{"t_s", "jpeg", "w", "h"}], "duration_s", "ms", "decoder"}

One pass from start to end (seeking per frame is ~5x slower on 4K HEVC), the first 180 s only, long edge <= 1280 px,
rotation from the clip's display matrix applied by ffmpeg. ffmpeg runs as a separate program with an argument list
(never a shell), a minimal environment, the demuxer forced from the sniffed container (so a playlist or concat file
can't make it open other files or URLs), a timeout and capped output. Morph never bundles ffmpeg: imageio-ffmpeg's
binary is GPLv3 and only used when the person installed it.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import time

COUNT, MAX_COUNT = 8, 12
LONG_EDGE = 1280
MAX_SECONDS = 180.0
TIMEOUT_S = 60.0           # the whole decode; probing has its own 15 s
MAX_FRAME_BYTES = 4 << 20  # same cap as frames sent from the browser
MAX_LOG = 64 << 10         # bytes of ffmpeg's stderr ever read back
PREFIX = "video-"          # work folders under MORPH_HOME/.uploads
# container -> the demuxer ffmpeg is forced to use (MP4, MOV, M4V and 3GP are all "mov" to ffmpeg)
FORMATS = {"mov": "mov", "matroska": "matroska", "avi": "avi"}
_QT_ATOMS = (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot")


class FFmpegError(Exception):
    """`code` is no_ffmpeg | not_video | timeout | failed | no_frames; the message is safe to show."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _exe_ok(path) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def find() -> str | None:
    """The ffmpeg program to use, or None: MORPH_FFMPEG (a path or a name on PATH), ffmpeg on PATH, then the copy
    imageio-ffmpeg installed (the video extra)."""
    env = (os.environ.get("MORPH_FFMPEG") or "").strip().strip('"')
    if env:
        exe = env if os.path.dirname(env) else shutil.which(env)
        if _exe_ok(exe):
            return os.path.abspath(exe)
    exe = shutil.which("ffmpeg")
    if _exe_ok(exe):
        return exe
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001  (not installed, or no binary for this platform: Windows on ARM)
        return None
    return exe if _exe_ok(exe) else None


def status() -> dict:
    """For /api/video/info and `morph doctor`: whether frames can be extracted here, and with what."""
    exe = find()
    env = (os.environ.get("MORPH_FFMPEG") or "").strip()
    out = {"available": bool(exe), "path": exe, "version": version(exe) if exe else None}
    if env and (not exe or os.path.basename(exe).lower() != os.path.basename(env.strip('"')).lower()):
        out["warning"] = "MORPH_FFMPEG does not point to a runnable ffmpeg program, so it was ignored."
    return out


def version(exe: str) -> str | None:
    try:
        r = _run([exe, "-hide_banner", "-version"], timeout=10)
    except FFmpegError:
        return None
    m = re.search(rb"ffmpeg version (\S+)", r[1] or b"")
    return m.group(1).decode("ascii", "replace")[:40] if m else None


def _env() -> dict:
    env = {"PATH": os.environ.get("PATH", "")}
    if os.name == "nt":  # Windows programs need these to start (DLL search, Winsock)
        for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
            if os.environ.get(k):
                env[k] = os.environ[k]
    return env


def _run(args: list, timeout: float, log_path: str | None = None) -> tuple[int, bytes]:
    """(returncode, the last MAX_LOG bytes of stdout+stderr). stdout and stderr go to a file when `log_path` is given,
    so a clip that makes ffmpeg print warnings for every frame can't fill memory."""
    kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    try:
        if log_path:
            with open(log_path, "wb") as log:
                rc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=_env(),
                                    timeout=timeout, check=False, **kw).returncode
            with open(log_path, "rb") as log:
                size = log.seek(0, os.SEEK_END)
                log.seek(max(0, size - MAX_LOG))
                return rc, log.read(MAX_LOG)
        r = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, env=_env(), timeout=timeout, check=False, **kw)
        return r.returncode, (r.stdout + r.stderr)[-MAX_LOG:]
    except subprocess.TimeoutExpired:
        raise FFmpegError("timeout", f"ffmpeg took longer than {timeout:g} s, so it was stopped.") from None
    except OSError as exc:
        raise FFmpegError("failed", f"Couldn't start ffmpeg ({exc.__class__.__name__}).") from None


def sniff(head: bytes) -> str | None:
    """The container from the first bytes ("mov", "matroska", "avi"), or None: never the file name or browser type."""
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mov"  # MP4, MOV, M4V, 3GP
    if len(head) >= 8 and head[4:8] in _QT_ATOMS and int.from_bytes(head[:4], "big") >= 8:
        return "mov"  # older QuickTime files start with another atom
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"  # WebM, MKV
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "avi"
    return None


def uploads_dir(home: str) -> str:
    return os.path.join(home, ".uploads")


def new_workdir(home: str) -> str:
    """A private, empty folder for one upload: MORPH_HOME/.uploads/video-<random>."""
    root = uploads_dir(home)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, PREFIX + secrets.token_hex(8))
    os.makedirs(path, mode=0o700)
    return path


def cleanup(home: str, older_than_s: float = 0.0) -> int:
    """Delete leftover work folders (a crash mid-upload, a killed Studio). At startup older_than_s=0 clears them all;
    before an upload, older ones only, so another upload in progress keeps its folder. Returns how many went."""
    root = uploads_dir(home)
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    now, n = time.time(), 0
    for name in names:
        path = os.path.join(root, name)
        if not name.startswith(PREFIX) or os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            if now - os.path.getmtime(path) < older_than_s:
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
        n += not os.path.exists(path)
    return n


def _seconds(h, m, s) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


def probe(path: str, *, exe: str | None = None, fmt: str | None = None, timeout: float = 15.0, log_path: str | None = None) -> dict:
    """{"duration_s": float | None, "video": bool} from `ffmpeg -i` (imageio-ffmpeg ships no ffprobe). Clips recorded in a
    browser often carry no duration; then one fast pass that copies packets (no decoding) measures it."""
    exe = exe or find()
    if not exe:
        raise FFmpegError("no_ffmpeg", "No ffmpeg program was found on this computer.")
    head = _input_args(path, fmt)
    rc, log = _run([exe, "-hide_banner", "-nostdin", *head], timeout, log_path)
    text = log.decode("utf-8", "replace")
    video = bool(re.search(r"Stream #.*Video: ", text))
    m = re.search(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)", text)
    dur = _seconds(*m.groups()) if m else None
    if video and not dur:
        rc, log = _run([exe, "-hide_banner", "-nostdin", *head, "-map", "0:v:0", "-c", "copy", "-f", "null", "-"], timeout, log_path)
        times = re.findall(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", log.decode("utf-8", "replace"))
        dur = _seconds(*times[-1]) if times else None
    return {"duration_s": dur, "video": video}


def _input_args(path: str, fmt: str | None) -> list:
    args = ["-protocol_whitelist", "file"]
    if fmt in FORMATS:
        args += ["-f", FORMATS[fmt]]
    return args + ["-i", path]


def _filter(start: float, step: float, long_edge: int) -> str:
    # the first frame at or after start + k*step, for k = number already kept: one pass, no seeking
    return (f"setpts=PTS-STARTPTS,select='gte(t\\,{start:.3f}+selected_n*{step:.3f})',showinfo,"
            f"scale='min({long_edge},iw)':'min({long_edge},ih)':force_original_aspect_ratio=decrease")


def extract(path: str, *, count: int = COUNT, long_edge: int = LONG_EDGE, max_seconds: float = MAX_SECONDS,
            fmt: str | None = None, exe: str | None = None, timeout: float = TIMEOUT_S, workdir: str | None = None) -> dict:
    """`count` evenly spaced frames (at the middle of equal slices) from the first `max_seconds` of the clip, as JPEG.
    Returns {"frames": [{"t_s", "jpeg", "w", "h"}], "duration_s", "ms", "decoder": "ffmpeg"}. Raises FFmpegError.
    The frames are written into `workdir` (default: next to the clip); the caller deletes it."""
    from PIL import Image

    t0 = time.time()
    exe = exe or find()
    if not exe:
        raise FFmpegError("no_ffmpeg", "No ffmpeg program was found on this computer.")
    if fmt is None:
        with open(path, "rb") as fh:
            fmt = sniff(fh.read(64))
        if fmt is None:
            raise FFmpegError("not_video", "That file doesn't look like a video (MP4, MOV, WebM, MKV or AVI).")
    count = max(1, min(int(count), MAX_COUNT))
    work = workdir or os.path.dirname(os.path.abspath(path))
    log_path = os.path.join(work, "ffmpeg.log")
    info = probe(path, exe=exe, fmt=fmt, timeout=min(15.0, timeout), log_path=log_path)
    if not info["video"]:
        raise FFmpegError("not_video", "ffmpeg found no video track in that file.")
    span = min(info["duration_s"] or max_seconds, max_seconds)
    step = max(span / count, 0.001)
    out_dir = os.path.join(work, "frames")
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "f%02d.jpg")
    base = [exe, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-t", f"{max_seconds:g}",
            *_input_args(path, fmt), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", _filter(step / 2, step, long_edge)]
    tail = ["-frames:v", str(count), "-q:v", "3", "-y", pattern]
    left = max(1.0, timeout - (time.time() - t0))
    rc, log = _run(base + ["-fps_mode", "passthrough"] + tail, left, log_path)
    if rc and b"fps_mode" in log and (b"Unrecognized option" in log or b"Option not found" in log):
        left = max(1.0, timeout - (time.time() - t0))  # ffmpeg 4.x: the same passthrough under its old name
        rc, log = _run(base + ["-vsync", "0"] + tail, left, log_path)
    # showinfo sits after select, so its n is the kept frame's index: n -> time
    times = {int(n): float(t) for n, t in re.findall(rb"\bn:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:\s*(-?[0-9.]+)", log)}
    names = sorted(n for n in os.listdir(out_dir) if re.fullmatch(r"f\d{2}\.jpg", n))[:count]
    frames = []
    for i, name in enumerate(names):
        fp = os.path.join(out_dir, name)
        if os.path.getsize(fp) > MAX_FRAME_BYTES:
            continue
        with open(fp, "rb") as fh:
            data = fh.read()
        if data[:3] != b"\xff\xd8\xff":
            continue
        try:
            with Image.open(fp) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001  (a truncated last frame)
            continue
        t = times.get(i, step * (i + 0.5))
        frames.append({"t_s": round(max(0.0, t), 2), "jpeg": data, "w": w, "h": h})
    if not frames:
        last = log.decode("utf-8", "replace").strip().splitlines()[-1:] if log else []
        if rc:
            raise FFmpegError("failed", "ffmpeg couldn't decode this video" + (f" ({last[0][:160]})" if last else "") + ".")
        raise FFmpegError("no_frames", "ffmpeg read the clip but found no frames to keep.")
    return {"frames": frames, "duration_s": round(info["duration_s"], 2) if info["duration_s"] else None,
            "ms": round((time.time() - t0) * 1000), "decoder": "ffmpeg"}
