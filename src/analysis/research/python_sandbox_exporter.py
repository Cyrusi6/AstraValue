"""Trusted read-only helper exports tmpfs volumes while the workload is paused."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile


def regular_bytes(path: Path, bound: int):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > bound:
            raise ValueError("Output must be a bounded regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(bound + 1)
        if len(raw) != info.st_size or len(raw) > bound:
            raise ValueError("Output size changed")
        return raw
    finally:
        os.close(fd)


def main():
    outcome_raw = regular_bytes(Path("/control/outcome.json"), 96 * 1024)
    outcome = json.loads(outcome_raw)
    files = {"outcome.json": outcome_raw}
    if outcome.get("status") == "succeeded":
        bound = int(sys.argv[1])
        allowed = {"figure.png", "chart-data.json", "result.json"}
        for entry in Path("/output").iterdir():
            if entry.name not in allowed:
                raise ValueError("Unexpected sandbox output: " + entry.name)
            value = regular_bytes(entry, bound)
            bound -= len(value)
            files[entry.name] = value
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for name, value in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            member.mode = 0o444
            archive.addfile(member, io.BytesIO(value))


if __name__ == "__main__":
    main()
