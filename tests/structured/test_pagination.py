from __future__ import annotations

import hashlib

from analysis.structured.pagination import (
    PageManifest,
    PageSlice,
    PageStatus,
    collect_pages,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _page(number: int, keys, *, total: int, pages: int, terminal: bool = False, status=PageStatus.SUCCESS, payload=None):
    return PageSlice(
        page_number=number,
        row_keys=tuple(keys),
        response_sha256=_hash(payload or f"page-{number}"),
        status=status,
        declared_total=total,
        declared_pages=pages,
        terminal=terminal,
    )


def test_446_row_manifest_requires_and_accepts_the_exact_terminal_count() -> None:
    manifest = PageManifest("segments", partition={"ticker": "600519.SH"})
    for number in range(1, 5):
        start = (number - 1) * 120
        end = min(446, number * 120)
        manifest.add(_page(number, (f"r{i}" for i in range(start, end)), total=446, pages=4, terminal=number == 4))
    audit = manifest.audit()
    assert audit.complete
    assert audit.unique_row_count == 446
    assert audit.can_advance_watermark


def test_more_than_500_rows_are_not_truncated() -> None:
    manifest = PageManifest("fund_holds")
    for number in range(1, 7):
        start = (number - 1) * 100
        end = 537 if number == 6 else number * 100
        manifest.add(_page(number, (f"r{i}" for i in range(start, end)), total=537, pages=6, terminal=number == 6))
    assert manifest.audit().unique_row_count == 537
    assert manifest.audit().complete


def test_repeated_source_page_payload_blocks_watermark() -> None:
    manifest = PageManifest("segments")
    manifest.add(_page(1, ["a", "b"], total=4, pages=2, payload="same"))
    manifest.add(_page(2, ["c", "d"], total=4, pages=2, terminal=True, payload="same"))
    audit = manifest.audit()
    assert not audit.complete
    assert any(issue.startswith("repeated_page_payload") for issue in audit.issues)


def test_duplicate_row_across_pages_blocks_completion_even_if_declared_count_matches() -> None:
    manifest = PageManifest("segments")
    manifest.add(_page(1, ["a", "b"], total=3, pages=2))
    manifest.add(_page(2, ["b", "c"], total=3, pages=2, terminal=True))
    audit = manifest.audit()
    assert not audit.complete
    assert any(issue.startswith("duplicate_row_across_pages") for issue in audit.issues)


def test_total_drift_and_page_count_drift_are_explicit() -> None:
    manifest = PageManifest("segments")
    manifest.add(_page(1, ["a"], total=2, pages=2))
    manifest.add(_page(2, ["b"], total=3, pages=3, terminal=True))
    assert set(manifest.audit().issues) >= {"declared_total_drift", "declared_pages_drift"}


def test_missing_middle_page_is_incomplete() -> None:
    manifest = PageManifest("segments")
    manifest.add(_page(1, ["a"], total=2, pages=3))
    manifest.add(_page(3, ["b"], total=2, pages=3, terminal=True))
    assert "page_sequence_incomplete" in manifest.audit().issues


def test_failed_late_page_preserves_partial_rows_and_watermark() -> None:
    manifest = PageManifest("fund_holds")
    manifest.add(_page(1, ["a"], total=2, pages=2))
    manifest.add(_page(2, [], total=2, pages=2, status=PageStatus.FAILED))
    audit = manifest.audit()
    assert audit.unique_row_count == 1
    assert not audit.can_advance_watermark
    assert "page_failed:2" in audit.issues


def test_identical_recommit_is_idempotent() -> None:
    manifest = PageManifest("segments")
    page = _page(1, ["a"], total=1, pages=1, terminal=True)
    assert manifest.add(page)
    assert not manifest.add(page)
    assert manifest.audit().complete


def test_conflicting_same_page_is_not_rewritten() -> None:
    manifest = PageManifest("segments")
    manifest.add(_page(1, ["a"], total=1, pages=1, terminal=True))
    manifest.add(_page(1, ["b"], total=1, pages=1, terminal=True, payload="changed"))
    assert "page_conflict:1" in manifest.audit().issues
    assert manifest.pages[0].row_keys == ("a",)


def test_valid_zero_result_has_terminal_evidence() -> None:
    manifest = PageManifest("customers_peer")
    manifest.add(_page(1, [], total=0, pages=0, terminal=True, status=PageStatus.EMPTY))
    audit = manifest.audit()
    assert audit.complete and audit.unique_row_count == 0


def test_empty_page_without_terminal_is_not_complete() -> None:
    manifest = PageManifest("customers_peer")
    manifest.add(_page(1, [], total=0, pages=0, status=PageStatus.EMPTY))
    assert not manifest.audit().complete


def test_collect_pages_stops_at_terminal() -> None:
    called = []

    def fetch(number):
        called.append(number)
        return _page(number, [f"r{number}"], total=2, pages=2, terminal=number == 2)

    manifest = collect_pages("segments", fetch)
    assert called == [1, 2]
    assert manifest.audit().complete


def test_collect_pages_has_a_hard_bound() -> None:
    manifest = collect_pages(
        "segments",
        lambda number: _page(number, [f"r{number}"], total=100, pages=100),
        max_pages=2,
    )
    assert "page_limit_exhausted" in manifest.audit().issues
