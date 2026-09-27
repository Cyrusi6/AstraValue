"""Container-only entry point. Never import this module to execute user code on the host."""
from __future__ import annotations

import builtins
from collections.abc import Mapping
from decimal import Decimal
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import sys
import time
import traceback
from types import MappingProxyType

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


class BoundedLog:
    def __init__(self, limit: int):
        self.limit = limit
        self.content = bytearray()
        self.truncated = False

    def write(self, value):
        encoded = str(value).encode("utf-8", "replace")
        remaining = max(0, self.limit - len(self.content))
        self.content.extend(encoded[:remaining])
        self.truncated |= len(encoded) > remaining
        return len(str(value))

    def flush(self):
        pass

    def isatty(self):
        return False


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def json_value(value):
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, np.ndarray):
        return json_value(value.tolist())
    if isinstance(value, np.generic):
        return json_value(value.item())
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal output must be finite")
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Numeric output must be finite; represent missing values as null")
        return value
    raise TypeError(f"Unsupported result type: {type(value).__name__}; return JSON-compatible data")


def write_json(path: Path, value):
    path.write_text(json.dumps(json_value(value), ensure_ascii=False, allow_nan=False), encoding="utf-8")


def main():
    config = json.loads(Path("/input/config.json").read_text(encoding="utf-8"))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    file_limit = config["limits"]["output_mb"] * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    log = BoundedLog(config["limits"]["log_bytes"])
    sys.stdout = sys.stderr = log
    matplotlib.rcParams.update({
        "font.sans-serif": ["Noto Sans CJK SC", "DejaVu Sans"],
        "axes.unicode_minus": False,
        "figure.max_open_warning": 5,
        "savefig.dpi": 140,
    })
    started = time.monotonic()
    source_bytes = Path("/input/data.json").read_bytes()
    outcome = {"status": "succeeded", "error": None}
    try:
        namespace = {
            "__name__": "__research_sandbox__", "__builtins__": builtins.__dict__,
            "data": freeze(json.loads(source_bytes)), "np": np, "pd": pd,
            "plt": plt, "math": math, "Decimal": Decimal,
        }
        code = Path("/input/code.py").read_text(encoding="utf-8")
        exec(compile(code, "/input/code.py", "exec"), namespace, namespace)
        if config["mode"] == "calculation":
            if not isinstance(namespace.get("result"), Mapping):
                raise ValueError("Calculation code must assign result to a JSON object")
            write_json(Path("/output/result.json"), namespace["result"])
        else:
            fig = namespace.get("fig")
            if not isinstance(fig, matplotlib.figure.Figure):
                raise ValueError("Chart code must assign fig to a matplotlib Figure")
            chart_data = namespace.get("chart_data")
            if not isinstance(chart_data, (Mapping, list, tuple)):
                raise ValueError("Chart code must assign chart_data to a JSON object or array")
            dimensions = fig.get_size_inches() * float(fig.dpi)
            if np.any(~np.isfinite(dimensions)) or np.any(dimensions <= 0) or np.any(dimensions > 12000):
                raise ValueError("Chart dimensions must be positive and at most 12000 pixels per side")
            write_json(Path("/output/chart-data.json"), chart_data)
            fig.savefig("/output/figure.png", format="png", dpi=140, bbox_inches="tight")
            if "result" in namespace:
                if not isinstance(namespace["result"], Mapping):
                    raise ValueError("Optional result must be a JSON object")
                write_json(Path("/output/result.json"), namespace["result"])
        if Path("/input/data.json").read_bytes() != source_bytes:
            raise RuntimeError("Read-only input changed")
    except BaseException as exc:
        limited = isinstance(exc, MemoryError) or (isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EFBIG, errno.EMFILE, errno.EAGAIN})
        outcome = {
            "status": "resource_limited" if limited else "failed",
            "error": {"code": "resource_limit" if limited else "python_error",
                      "exception_type": type(exc).__name__, "message": str(exc)[:2000]},
        }
        traceback.print_exc(limit=8, file=log)
    finally:
        plt.close("all")
    outcome.update({"elapsed_seconds": round(time.monotonic() - started, 3),
                    "input_sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "log": bytes(log.content).decode("utf-8", "replace"),
                    "log_truncated": log.truncated})
    write_json(Path("/control/outcome.json"), outcome)
    return 0 if outcome["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
