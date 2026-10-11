"""Machine connectors: the step from a file on disk to a part on the bed.

GRBL (most desktop CNC routers / diode lasers) over USB serial, and OctoPrint
for FDM printers. Both are optional; dry_run prints what would be sent.
"""

from __future__ import annotations

import os
import time


def stream_grbl(port: str, gcode_path: str, baud: int = 115200, dry_run: bool = False) -> dict:
    """Simple send-and-wait-for-ok streaming (safe, a bit slower than character counting)."""
    with open(gcode_path, encoding="utf-8") as fh:
        lines = [ln.split(";")[0].strip() for ln in fh]
    lines = [ln for ln in lines if ln and not ln.startswith("(")]
    if dry_run:
        return {"port": port, "lines": len(lines), "sent": 0, "dry_run": True}
    import serial  # pyserial

    t0 = time.time()
    with serial.Serial(port, baud, timeout=2) as s:
        s.write(b"\r\n\r\n")
        time.sleep(2)
        s.reset_input_buffer()
        for i, ln in enumerate(lines, 1):
            s.write((ln + "\n").encode())
            while True:
                resp = s.readline().decode(errors="ignore").strip()
                if resp.startswith("ok"):
                    break
                if resp.startswith("error") or resp.startswith("ALARM"):
                    raise RuntimeError(f"GRBL {resp} at line {i}: {ln}")
            if i % 200 == 0:
                print(f"  {i}/{len(lines)} lines")
    return {"port": port, "lines": len(lines), "sent": len(lines), "minutes": round((time.time() - t0) / 60, 1)}


def octoprint_upload(url: str, api_key: str, path: str, start: bool = False, dry_run: bool = False) -> dict:
    if dry_run:
        return {"url": url, "file": os.path.basename(path), "dry_run": True}
    import requests

    with open(path, "rb") as fh:
        r = requests.post(f"{url.rstrip('/')}/api/files/local", headers={"X-Api-Key": api_key},
                          files={"file": (os.path.basename(path), fh)},
                          data={"select": "true", "print": "true" if start else "false"}, timeout=60)
    r.raise_for_status()
    return {"url": url, "file": os.path.basename(path), "started": start, "response": r.json()}
