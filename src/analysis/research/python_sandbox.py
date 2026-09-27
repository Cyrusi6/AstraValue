"""Bounded Docker execution for model-authored Python; never executes it on the host."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid


DEFAULT_IMAGE = "astravalue-research-python:1"
DEFAULT_LIMITS = {"timeout_seconds": 30, "memory_mb": 512, "cpus": 1.0,
                  "pids": 64, "output_mb": 16, "log_bytes": 16384, "input_mb": 8}
MAX_LIMITS = {"timeout_seconds": 120, "memory_mb": 2048, "cpus": 2.0,
              "pids": 128, "output_mb": 64, "log_bytes": 65536, "input_mb": 32}
MIN_LIMITS = {"timeout_seconds": 1, "memory_mb": 128, "cpus": 0.1,
              "pids": 16, "output_mb": 1, "log_bytes": 256, "input_mb": 1}
ALLOWED_OUTPUTS = {"result.json", "figure.png", "chart-data.json"}
SETUP = "docker build -f docker/research-python/Dockerfile -t astravalue-research-python:1 ."


class SandboxOutputError(ValueError):
    pass


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _hash(raw: bytes):
    return hashlib.sha256(raw).hexdigest()


def _metadata(path: Path):
    raw = path.read_bytes()
    return {"path": str(path.resolve()), "sha256": _hash(raw), "size_bytes": len(raw)}


def _limits(overrides):
    chosen = dict(DEFAULT_LIMITS)
    if overrides:
        unknown = set(overrides) - set(chosen)
        if unknown:
            raise ValueError(f"Unknown sandbox limits: {sorted(unknown)}")
        chosen.update(overrides)
    for key, value in chosen.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Invalid numeric limit: {key}")
        if not MIN_LIMITS[key] <= value <= MAX_LIMITS[key]:
            raise ValueError(f"{key} must be between {MIN_LIMITS[key]} and {MAX_LIMITS[key]}")
        if key not in {"timeout_seconds", "cpus"} and value != int(value):
            raise ValueError(f"{key} must be an integer")
        if key not in {"timeout_seconds", "cpus"}:
            chosen[key] = int(value)
    return chosen


def _docker(arguments, *, timeout=10, byte_limit=65536, binary=False):
    """Bound both stdout and stderr, including logs produced with os.write()."""
    process = subprocess.Popen(["docker", *arguments], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    buffer = bytearray()
    oversized = threading.Event()

    def read():
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                break
            remaining = max(0, byte_limit - len(buffer))
            buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                oversized.set()
                process.kill()
                break

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        reader.join(timeout=5)
        raise
    reader.join(timeout=5)
    process.stdout.close()
    output = bytes(buffer)
    return process.returncode, output if binary else output.decode("utf-8", "replace"), oversized.is_set()


def _safe_archive(raw: bytes, *, allowed: set[str], max_bytes: int):
    """Read selected bounded regular files without extracting paths to disk."""
    found = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise SandboxOutputError("Output archive contains an unsafe path")
            normalized = str(path)
            if member.isdir() and normalized in {".", "output", "control"}:
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise SandboxOutputError("Only regular output files are permitted")
            if len(path.parts) > 1 or normalized not in allowed:
                raise SandboxOutputError(f"Unexpected sandbox output: {normalized}")
            if normalized in found:
                raise SandboxOutputError("Duplicate output archive member")
            total += member.size
            if member.size < 0 or total > max_bytes:
                raise SandboxOutputError("Sandbox output exceeds the configured size limit")
            handle = archive.extractfile(member)
            value = handle.read(max_bytes + 1)
            if len(value) != member.size:
                raise SandboxOutputError("Truncated sandbox output")
            found[normalized] = value
    return found


def _decode_json(raw):
    def invalid(value):
        raise SandboxOutputError(f"Non-finite JSON value: {value}")
    def finite_float(value):
        converted = float(value)
        if not math.isfinite(converted):
            invalid(value)
        return converted
    return json.loads(raw.decode("utf-8"), parse_constant=invalid, parse_float=finite_float)


def _validate_outputs(outputs, mode):
    required = {"result.json"} if mode == "calculation" else {"figure.png", "chart-data.json"}
    if not required <= outputs.keys():
        raise SandboxOutputError(f"Missing required outputs: {sorted(required - outputs.keys())}")
    if mode == "calculation" and set(outputs) != {"result.json"}:
        raise SandboxOutputError("Calculation may only export result.json")
    for name, raw in outputs.items():
        if name.endswith(".json"):
            decoded = _decode_json(raw)
            allowed = (dict, list) if name == "chart-data.json" else (dict,)
            if not isinstance(decoded, allowed):
                raise SandboxOutputError(f"Invalid top-level JSON type in {name}")
        elif name == "figure.png":
            if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
                raise SandboxOutputError("figure.png is not a PNG image")
            width, height = struct.unpack(">II", raw[16:24])
            if not (0 < width <= 12000 and 0 < height <= 12000 and width * height <= 16_000_000):
                raise SandboxOutputError("PNG dimensions exceed the image limit")
            try:
                from PIL import Image
                with Image.open(io.BytesIO(raw)) as image:
                    image.verify()
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
            except Exception as exc:
                raise SandboxOutputError("PNG output cannot be decoded") from exc


def _run_command(name, stage, image_id, limits):
    if "," in str(stage):
        raise ValueError("Sandbox staging path cannot contain commas")
    return ["run", "--detach", "--name", name, "--pull", "never",
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--user", "65532:65532",
            "--memory", f"{int(limits['memory_mb'])}m", "--memory-swap", f"{int(limits['memory_mb'])}m",
            "--cpus", str(limits["cpus"]), "--pids-limit", str(int(limits["pids"])),
            "--ulimit", "nofile=128:128", "--ulimit", "core=0:0",
            "--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=1", "--log-opt", "compress=false",
            "--mount", f"type=bind,source={stage.resolve()},target=/input,readonly",
            "--mount", f"type=volume,source={name}-output,target=/output,volume-nocopy",
            "--mount", f"type=volume,source={name}-control,target=/control,volume-nocopy",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--hostname", "research-python", "--init", "--entrypoint", "python", image_id,
            "-I", "-c", "import time; time.sleep(86400)"]


def _export_command(name, image_id, output_bound):
    return ["run", "--rm", "--name", name + "-export", "--pull", "never", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "65532:65532",
            "--memory", "128m", "--memory-swap", "128m", "--cpus", "1", "--pids-limit", "16",
            "--log-driver", "none", "--mount", f"type=volume,source={name}-output,target=/output,readonly,volume-nocopy",
            "--mount", f"type=volume,source={name}-control,target=/control,readonly,volume-nocopy",
            "--entrypoint", "python", image_id, "-I", "/opt/research-python/exporter.py", str(output_bound)]


def run_python(code: str, data: dict, *, mode: str, output_dir: Path,
               limits: dict | None = None, image: str | None = None) -> dict:
    """Run model code in a Linux container, returning immutable audit files and an outcome.

    `data` contains only caller-selected current-snapshot data. The caller owns research
    authorization and provenance; this module owns process and filesystem isolation.
    Fresh attempts require fresh output directories. No image is pulled implicitly.
    """
    started = time.monotonic()
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    result = {"status": "failed", "error": None, "retryable": False, "files": {},
              "execution": {"runtime": "docker-linux", "mode": mode}}
    reserved = {"code.py", "input.json", "run.json", *ALLOWED_OUTPUTS}
    if any((directory / name).exists() for name in reserved):
        result["error"] = {"code": "output_exists", "message": "Use a fresh output directory for each attempt", "exception_type": None}
        return result

    def finish(status, code=None, message=None, exception_type=None):
        result["status"] = status
        if code:
            result["error"] = {"code": code, "message": str(message)[:2000], "exception_type": exception_type}
        result["execution"]["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (directory / "run.json").write_bytes(_json_bytes(result))
        result["files"]["run.json"] = _metadata(directory / "run.json")
        return result

    try:
        chosen = _limits(limits)
        if mode not in {"chart", "calculation"}:
            raise ValueError("mode must be chart or calculation")
        if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > 256 * 1024:
            raise ValueError("code must be nonempty UTF-8 text no larger than 256 KiB")
        if not isinstance(data, dict):
            raise ValueError("data must be a JSON object")
        input_bytes = _json_bytes(data)
        if len(input_bytes) > chosen["input_mb"] * 1024 * 1024:
            raise ValueError("Selected input exceeds the input size limit")
    except (TypeError, ValueError) as exc:
        return finish("failed", "invalid_input", str(exc), type(exc).__name__)

    (directory / "code.py").write_bytes(code.encode("utf-8"))
    (directory / "input.json").write_bytes(input_bytes)
    result["files"].update({name: _metadata(directory / name) for name in ("code.py", "input.json")})
    result["execution"].update({"limits": chosen, "input_sha256": _hash(input_bytes),
                                "input_unchanged": True, "network": "none", "root_filesystem": "read_only",
                                "host_mounts": ["staged_input_readonly"], "log": "", "log_truncated": False})
    selected_image = image or os.environ.get("ASTRAVALUE_PYTHON_SANDBOX_IMAGE") or DEFAULT_IMAGE
    result["execution"]["requested_image"] = selected_image
    result["setup_command"] = SETUP
    if not shutil.which("docker"):
        return finish("unavailable", "docker_unavailable", "Docker CLI is unavailable. Start Docker Desktop in Linux-container mode and build the sandbox image.")
    try:
        rc, platform, _ = _docker(["info", "--format", "{{.OSType}}"], timeout=10)
        if rc or platform.strip() != "linux":
            return finish("unavailable", "docker_unavailable", "A running Linux Docker engine is required. " + platform[-1000:])
        rc, inspected, _ = _docker(["image", "inspect", selected_image, "--format", "{{json .}}"], timeout=10)
        if rc:
            return finish("unavailable", "image_unavailable", f"Sandbox image is not installed. From the repository root run: {SETUP}")
        image_info = json.loads(inspected)
        image_id = image_info["Id"]
        if image_info.get("Os") != "linux" or not image_id.startswith("sha256:"):
            return finish("unavailable", "image_invalid", "Sandbox image must be a local Linux image with a content digest")
        result["execution"].update({"image_id": image_id, "image_digest": image_id,
                                    "repository_digests": image_info.get("RepoDigests", [])})
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        return finish("unavailable", "docker_unavailable", str(exc), type(exc).__name__)

    name = "astravalue-python-" + uuid.uuid4().hex[:20]
    result["execution"]["container_name"] = name
    outcome = None
    exported = {}
    try:
        for suffix, size in (("output", chosen["output_mb"]), ("control", 1)):
            rc, response, _ = _docker(["volume", "create", "--driver", "local", "--label", "astravalue.research-python=true",
                                       "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt",
                                       f"o=size={int(size)}m,uid=65532,gid=65532,mode=0700,noexec,nosuid,nodev", name + "-" + suffix], timeout=10)
            if rc:
                return finish("unavailable", "volume_start_failed", response)
        with tempfile.TemporaryDirectory(prefix="astravalue-python-") as temporary:
            stage = Path(temporary)
            (stage / "data.json").write_bytes(input_bytes)
            (stage / "code.py").write_bytes(code.encode("utf-8"))
            (stage / "config.json").write_bytes(_json_bytes({"mode": mode, "limits": chosen}))
            rc, response, _ = _docker(_run_command(name, stage, image_id, chosen), timeout=20)
            if rc:
                return finish("unavailable", "container_start_failed", response)
            # Completion is the real Docker exec process exit, never a marker
            # that model-authored code could forge before going into a loop.
            try:
                worker_rc, direct_log, direct_truncated = _docker(["exec", name, "python", "-I", "/opt/research-python/worker.py"],
                                                                  timeout=chosen["timeout_seconds"], byte_limit=chosen["log_bytes"])
            except subprocess.TimeoutExpired:
                result["execution"]["exit_code"] = None
                return finish("timed_out", "execution_timeout", f"Python execution exceeded {chosen['timeout_seconds']} seconds")
            result["execution"].update({"exit_code": worker_rc, "log": direct_log, "log_truncated": direct_truncated})
            if direct_truncated:
                return finish("resource_limited", "log_limit", "Unbuffered process output exceeded the configured log size limit")
            if worker_rc in {137, 152, 153}:
                return finish("resource_limited", "resource_limit", "Python exceeded its memory or process/file resource limit")
            output_bound = chosen["output_mb"] * 1024 * 1024
            # Pause the whole workload (including fork/setsid descendants), then
            # read its bounded tmpfs volumes through a separate read-only helper.
            # Docker Desktop's cp/archive API does not include tmpfs contents.
            rc, response, _ = _docker(["pause", name], timeout=5)
            if rc:
                raise RuntimeError("Unable to freeze sandbox outputs: " + response)
            rc, control, truncated = _docker(_export_command(name, image_id, output_bound),
                                             binary=True, byte_limit=output_bound + 1024 * 1024, timeout=15)
            if rc or truncated:
                raise SandboxOutputError("Sandbox output is not a bounded set of approved regular files")
            exported = _safe_archive(control, allowed={"outcome.json", *ALLOWED_OUTPUTS}, max_bytes=output_bound + 96 * 1024)
            outcome = _decode_json(exported.pop("outcome.json"))
            combined_log = (direct_log + str(outcome.get("log", ""))).encode("utf-8")
            result["execution"].update({"worker_elapsed_seconds": outcome["elapsed_seconds"],
                                        "log": combined_log[:chosen["log_bytes"]].decode("utf-8", "ignore"),
                                        "log_truncated": bool(outcome.get("log_truncated")) or len(combined_log) > chosen["log_bytes"]})
            if outcome.get("input_sha256") != _hash(input_bytes) or (stage / "data.json").read_bytes() != input_bytes:
                result["execution"]["input_unchanged"] = False
                raise SandboxOutputError("Input hash changed during execution")
            if outcome["status"] != "succeeded":
                error = outcome.get("error") or {}
                return finish(outcome["status"] if outcome["status"] in {"failed", "resource_limited"} else "failed",
                              error.get("code", "python_error"), error.get("message", "Python failed"), error.get("exception_type"))
            if worker_rc != 0:
                return finish("failed", "python_exit_error", f"Python exited with status {worker_rc}; success metadata was rejected")
            if sum(len(raw) for raw in exported.values()) > output_bound:
                raise SandboxOutputError("Sandbox output exceeds the configured size limit")
            _validate_outputs(exported, mode)
            for filename, raw in exported.items():
                (directory / filename).write_bytes(raw)
                result["files"][filename] = _metadata(directory / filename)
    except SandboxOutputError as exc:
        return finish("failed", "invalid_output", str(exc), type(exc).__name__)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, RuntimeError, tarfile.TarError) as exc:
        return finish("failed", "runtime_error", str(exc), type(exc).__name__)
    finally:
        try:
            _docker(["rm", "--force", name, name + "-export"], timeout=10)
            rc, _, _ = _docker(["volume", "rm", name + "-output", name + "-control"], timeout=10)
            if rc:
                result["execution"]["cleanup_required"] = name
        except (OSError, subprocess.SubprocessError):
            result["execution"]["cleanup_required"] = name
    return finish("succeeded")
