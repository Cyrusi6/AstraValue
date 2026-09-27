from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

from analysis.research import python_sandbox as sandbox


def archive(members):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w") as output:
        for name, value, kind in members:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            if kind == tarfile.REGTYPE:
                entry.size = len(value)
                output.addfile(entry, io.BytesIO(value))
            else:
                entry.linkname = value.decode()
                output.addfile(entry)
    return target.getvalue()


@pytest.mark.parametrize("name,kind", [("../result.json", tarfile.REGTYPE),
                                         ("/result.json", tarfile.REGTYPE),
                                         ("sub/result.json", tarfile.REGTYPE),
                                         ("result.json", tarfile.SYMTYPE),
                                         ("result.json", tarfile.LNKTYPE)])
def test_export_rejects_paths_links_and_nested_outputs(name, kind):
    with pytest.raises(sandbox.SandboxOutputError):
        sandbox._safe_archive(archive([(name, b"{}", kind)]), allowed={"result.json"}, max_bytes=10)


def test_export_is_bounded_and_does_not_extract():
    with pytest.raises(sandbox.SandboxOutputError, match="size limit"):
        sandbox._safe_archive(archive([("result.json", b"01234567890", tarfile.REGTYPE)]), allowed={"result.json"}, max_bytes=10)
    raw = archive([("result.json", b'{"value":3}', tarfile.REGTYPE)])
    assert sandbox._safe_archive(raw, allowed={"result.json"}, max_bytes=100)["result.json"] == b'{"value":3}'


def test_nan_and_wrong_output_shape_rejected():
    with pytest.raises(sandbox.SandboxOutputError):
        sandbox._validate_outputs({"result.json": b'{"x": NaN}'}, "calculation")
    with pytest.raises(sandbox.SandboxOutputError):
        sandbox._validate_outputs({"result.json": b'[]'}, "calculation")
    with pytest.raises(sandbox.SandboxOutputError):
        sandbox._validate_outputs({"result.json": b'{"x": 1e999}'}, "calculation")


def test_png_must_decode_not_just_have_a_header():
    import struct
    broken = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 100, 100)
    with pytest.raises(sandbox.SandboxOutputError, match="decoded"):
        sandbox._validate_outputs({"figure.png": broken, "chart-data.json": b'[]'}, "chart")


def test_no_host_python_fallback_and_audit_files_retained(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    result = sandbox.run_python("raise RuntimeError('host code must never run')", {"x": 3}, mode="calculation", output_dir=tmp_path)
    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "docker_unavailable"
    assert result["retryable"] is False
    assert {"code.py", "input.json", "run.json"} == result["files"].keys()
    assert json.loads((tmp_path / "input.json").read_text())["x"] == 3


def test_limits_rejected_before_container_start(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **kw: pytest.fail("must not contact Docker"))
    result = sandbox.run_python("result = {}", {}, mode="calculation", output_dir=tmp_path, limits={"memory_mb": 100000})
    assert result["error"]["code"] == "invalid_input"
    assert isinstance(sandbox._limits({"log_bytes": 1024.0})["log_bytes"], int)


def test_command_has_strict_isolation_and_no_host_output_mount(tmp_path):
    command = sandbox._run_command("name", tmp_path, "sha256:fixed", sandbox.DEFAULT_LIMITS)
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--user") + 1] == "65532:65532"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges:true" in command
    assert command[command.index("--mount") + 1].endswith("target=/input,readonly")
    assert sum(argument == "--mount" for argument in command) == 3
    assert sum("type=bind" in argument for argument in command) == 1
    assert "--memory-swap" in command and "--pids-limit" in command
    assert not any("docker.sock" in argument for argument in command)
    assert command[command.index("--entrypoint") + 2] == "sha256:fixed"


def _live_available():
    if os.environ.get("RUN_RESEARCH_SANDBOX_LIVE") != "1":
        return False
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", sandbox.DEFAULT_IMAGE],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


live = pytest.mark.skipif(not _live_available(), reason="Set RUN_RESEARCH_SANDBOX_LIVE=1 after building the sandbox image")


@live
def test_live_decimal_and_chinese_chart(tmp_path):
    calculation = sandbox.run_python("result = {'value': Decimal(data['amount']) / Decimal('4'), 'total': pd.DataFrame(data['rows'])['x'].sum()}",
                                     {"amount": "123.4", "rows": [{"x": 3}, {"x": 4}]}, mode="calculation", output_dir=tmp_path / "计算")
    assert calculation["status"] == "succeeded", calculation
    assert json.loads(Path(calculation["files"]["result.json"]["path"]).read_text())["value"] == "30.85"
    assert json.loads(Path(calculation["files"]["result.json"]["path"]).read_text())["total"] == 7
    assert calculation["execution"]["input_unchanged"]
    chart = sandbox.run_python("fig, ax = plt.subplots(); ax.plot(data['years'], data['values']); ax.set_title('营业收入变化'); chart_data = {'years': data['years'], 'values': data['values'], 'unit': '亿元'}",
                               {"years": [2023, 2024, 2025], "values": [120, 150, 160]}, mode="chart", output_dir=tmp_path / "图表")
    assert chart["status"] == "succeeded", chart
    assert Path(chart["files"]["figure.png"]["path"]).stat().st_size > 3000
    assert "Glyph" not in chart["execution"]["log"]


@live
def test_live_no_network_no_host_files_no_ambient_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAVALUE_HOST_SECRET_TEST", "should-not-appear")
    code = """import os, socket
checks = {}
try:
    socket.create_connection(('1.1.1.1', 443), timeout=0.5)
    checks['network_blocked'] = False
except OSError:
    checks['network_blocked'] = True
checks['secret_absent'] = 'ASTRAVALUE_HOST_SECRET_TEST' not in os.environ
checks['socket_absent'] = not os.path.exists('/var/run/docker.sock')
checks['nonroot'] = os.getuid() == 65532
checks['host_absent'] = not os.path.exists('D:/估值模型') and not os.path.exists('/mnt/host')
for key, path in [('root_readonly', '/etc/malicious'), ('input_readonly', '/input/data.json')]:
    try:
        open(path, 'w').write('evil')
        checks[key] = False
    except OSError:
        checks[key] = True
try:
    data['new'] = 1
    checks['data_readonly'] = False
except TypeError:
    checks['data_readonly'] = True
checks['isolated_pid_namespace'] = len([p for p in os.listdir('/proc') if p.isdigit()]) < 15
result = checks
"""
    result = sandbox.run_python(code, {"untouched": 1}, mode="calculation", output_dir=tmp_path)
    assert result["status"] == "succeeded", result
    checks = json.loads(Path(result["files"]["result.json"]["path"]).read_text())
    assert all(checks.values()), checks
    assert result["execution"]["input_unchanged"]


@live
@pytest.mark.parametrize("code,status,error_type", [
    ("result = {'x': 1 / 0}", "failed", "ZeroDivisionError"),
    ("while True: pass", "timed_out", None),
    ("x = bytearray(1024 * 1024 * 1024); result = {}", "resource_limited", None),
    ("open('/output/too-large', 'wb').write(b'x' * 2_000_000); result = {}", "resource_limited", "OSError"),
])
def test_live_execution_limits_and_exact_exceptions(tmp_path, code, status, error_type):
    result = sandbox.run_python(code, {}, mode="calculation", output_dir=tmp_path,
                                limits={"timeout_seconds": 5, "memory_mb": 256, "output_mb": 1})
    assert result["status"] == status, result
    if error_type:
        assert result["error"]["exception_type"] == error_type
    assert "result.json" not in result["files"]


@live
def test_live_symlink_export_rejected(tmp_path):
    result = sandbox.run_python("import os; os.symlink('/etc/passwd', '/output/escaped.txt'); result = {'ok': True}",
                                {}, mode="calculation", output_dir=tmp_path)
    assert result["status"] == "failed", result
    assert result["error"]["code"] == "invalid_output"
    assert "result.json" not in result["files"]


@live
def test_live_logs_are_bounded(tmp_path):
    result = sandbox.run_python("print('文' * 100000); result = {'ok': True}", {}, mode="calculation", output_dir=tmp_path,
                                limits={"log_bytes": 1024})
    assert result["status"] == "succeeded", result
    assert len(result["execution"]["log"].encode("utf-8")) <= 1024
    assert result["execution"]["log_truncated"]


@live
def test_live_forged_completion_cannot_bypass_timeout(tmp_path):
    code = """import json, hashlib
from pathlib import Path
Path('/output/result.json').write_text('{"fake": true}')
Path('/control/outcome.json').write_text(json.dumps({'status': 'succeeded', 'error': None, 'elapsed_seconds': 0,
    'input_sha256': hashlib.sha256(Path('/input/data.json').read_bytes()).hexdigest(), 'log': '', 'log_truncated': False}))
Path('/control/complete').write_text('done')
while True:
    pass
"""
    result = sandbox.run_python(code, {}, mode="calculation", output_dir=tmp_path, limits={"timeout_seconds": 3})
    assert result["status"] == "timed_out", result
    assert "result.json" not in result["files"]


@live
def test_live_raw_stdout_is_bounded(tmp_path):
    result = sandbox.run_python("import os; os.write(1, b'x' * 1000000); result = {}", {}, mode="calculation", output_dir=tmp_path,
                                limits={"log_bytes": 1024})
    assert result["status"] == "resource_limited", result
    assert result["error"]["code"] == "log_limit"
    assert len(result["execution"]["log"].encode()) <= 1024


@live
def test_live_process_count_is_capped(tmp_path):
    code = """import subprocess, errno
children = []
capped = False
try:
    for index in range(40):
        children.append(subprocess.Popen(['/bin/sleep', '20']))
except OSError as exc:
    capped = exc.errno == errno.EAGAIN
finally:
    for child in children:
        child.terminate()
    for child in children:
        child.wait(timeout=2)
result = {'capped': capped, 'started': len(children)}
"""
    result = sandbox.run_python(code, {}, mode="calculation", output_dir=tmp_path, limits={"pids": 16})
    assert result["status"] == "succeeded", result
    checks = json.loads(Path(result["files"]["result.json"]["path"]).read_text())
    assert checks["capped"] is True and 0 < checks["started"] < 16
