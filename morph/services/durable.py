"""Files that survive a crash, a power cut or a full disk.

Morph's records are JSON files replaced whole and JSON-lines files that only grow. Either can be cut short: a process
stopped mid-write, a disk that filled up, a power cut after a rename the disk hadn't written yet. The helpers here are
what every writer uses so that a cut never costs more than the record being written:

    append_line(path, line)      one JSON line; a cut-off last line is ended first, so the new record never joins it

Standard library only. Outside pipeline_version (morph/services/), so editing this file never rebuilds a robot.
"""

from __future__ import annotations

import os


def append_line(path: str, line) -> bool:
    """Append `line` (str or bytes, without its newline) and a newline to a JSON-lines file. A last line left without its
    newline (an append cut off by a crash or a full disk) is ended first, so the new record starts on a line of its own
    and survives: readers skip the cut-off line, never the next one. True when there was such a line to end.

    One write per call, so lines from two processes appending at once never interleave."""
    data = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    with open(path, "ab+") as fh:
        end = fh.seek(0, os.SEEK_END)
        torn = False
        if end:
            fh.seek(end - 1)
            torn = fh.read(1) != b"\n"
        fh.write((b"\n" if torn else b"") + data + b"\n")
    return torn
