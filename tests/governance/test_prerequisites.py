from __future__ import annotations

from pathlib import Path

from scripts.check_governance_prerequisites import (
    REQUIRED_IDENTITY_FIELDS,
    validate_prerequisites,
)


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_prerequisite_checker_blocks_when_shared_kernel_is_absent(tmp_path: Path):
    assert validate_prerequisites(tmp_path) == ["shared acquisition kernel尚未合入"]


def test_prerequisite_checker_requires_identity_on_every_control_object(tmp_path: Path):
    fields = "\n".join(f"    {name}: str" for name in REQUIRED_IDENTITY_FIELDS)
    classes = "\n\n".join(
        f"class {name}:\n{fields}" for name in (
            "AcquisitionRun",
            "CoverageEntry",
            "SourceCheckpoint",
            "EvidenceSnapshotManifest",
        )
    )
    _write(tmp_path / "src/analysis/acquisition/models.py", classes)
    _write(
        tmp_path / "src/analysis/acquisition/selectors.py",
        "# latest consume\n" + "\n".join(REQUIRED_IDENTITY_FIELDS),
    )
    identity_text = "\n".join(REQUIRED_IDENTITY_FIELDS)
    _write(tmp_path / "src/analysis/api.py", identity_text)
    _write(tmp_path / "src/analysis/cli.py", identity_text)
    assert validate_prerequisites(tmp_path) == []

    broken = classes.replace("    acquisition_scope: str\n", "", 1)
    _write(tmp_path / "src/analysis/acquisition/models.py", broken)
    errors = validate_prerequisites(tmp_path)
    assert any("AcquisitionRun缺少" in error for error in errors)
