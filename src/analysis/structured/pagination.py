from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Sequence


class PageStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    FAILED = "failed"


def _require_sha256(value: str) -> str:
    normalized = str(value).lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("response_sha256 must be a full SHA-256")
    return normalized


@dataclass(frozen=True, slots=True)
class PageSlice:
    page_number: int
    row_keys: tuple[str, ...]
    response_sha256: str
    status: PageStatus = PageStatus.SUCCESS
    declared_total: int | None = None
    declared_pages: int | None = None
    terminal: bool = False
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise ValueError("page number must be positive")
        object.__setattr__(self, "response_sha256", _require_sha256(self.response_sha256))
        if self.declared_total is not None and self.declared_total < 0:
            raise ValueError("declared_total cannot be negative")
        if self.declared_pages is not None and self.declared_pages < 0:
            raise ValueError("declared_pages cannot be negative")
        if self.status is PageStatus.EMPTY and self.row_keys:
            raise ValueError("an empty page cannot carry row keys")
        if self.status is PageStatus.FAILED and self.terminal:
            raise ValueError("a failed page cannot prove a terminal page")
        if any(not key for key in self.row_keys):
            raise ValueError("stable row keys cannot be empty")


@dataclass(frozen=True, slots=True)
class PageAudit:
    complete: bool
    page_count: int
    unique_row_count: int
    declared_total: int | None
    declared_pages: int | None
    terminal_page: int | None
    issues: tuple[str, ...]
    partition: tuple[tuple[str, str], ...]

    @property
    def can_advance_watermark(self) -> bool:
        return self.complete


@dataclass(slots=True)
class PageManifest:
    """Append-only page audit with exact totals and terminal evidence."""

    dataset_id: str
    partition: Mapping[str, str] = field(default_factory=dict)
    _pages: dict[int, PageSlice] = field(default_factory=dict, init=False)
    _issues: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if not self.dataset_id:
            raise ValueError("dataset_id is required")
        self.partition = dict(self.partition)

    @property
    def pages(self) -> tuple[PageSlice, ...]:
        return tuple(self._pages[number] for number in sorted(self._pages))

    def add(self, page: PageSlice) -> bool:
        existing = self._pages.get(page.page_number)
        if existing is not None:
            if existing == page:
                return False
            self._issues.append(f"page_conflict:{page.page_number}")
            return False

        for prior in self._pages.values():
            if (
                prior.response_sha256 == page.response_sha256
                and (prior.row_keys or page.row_keys)
            ):
                self._issues.append(
                    f"repeated_page_payload:{prior.page_number}:{page.page_number}"
                )

        duplicated_within = len(page.row_keys) - len(set(page.row_keys))
        if duplicated_within:
            self._issues.append(
                f"duplicate_row_within_page:{page.page_number}:{duplicated_within}"
            )
        previous_keys = {key for prior in self._pages.values() for key in prior.row_keys}
        cross_duplicates = previous_keys.intersection(page.row_keys)
        if cross_duplicates:
            self._issues.append(
                f"duplicate_row_across_pages:{page.page_number}:{len(cross_duplicates)}"
            )
        self._pages[page.page_number] = page
        if page.status is PageStatus.FAILED:
            self._issues.append(f"page_failed:{page.page_number}")
        return True

    def audit(self) -> PageAudit:
        pages = self.pages
        issues = list(dict.fromkeys(self._issues))
        if not pages:
            issues.append("no_pages")
            return self._audit_result(issues=issues, terminal_page=None)

        page_numbers = [page.page_number for page in pages]
        expected_sequence = list(range(1, max(page_numbers) + 1))
        if page_numbers != expected_sequence:
            issues.append("page_sequence_incomplete")

        totals = {page.declared_total for page in pages if page.declared_total is not None}
        page_totals = {page.declared_pages for page in pages if page.declared_pages is not None}
        if len(totals) > 1:
            issues.append("declared_total_drift")
        if len(page_totals) > 1:
            issues.append("declared_pages_drift")

        terminal_pages = [page.page_number for page in pages if page.terminal]
        terminal_page = terminal_pages[-1] if terminal_pages else None
        if not terminal_pages:
            issues.append("terminal_page_missing")
        elif len(terminal_pages) > 1:
            issues.append("multiple_terminal_pages")
        elif terminal_page != max(page_numbers):
            issues.append("records_after_terminal_page")

        declared_pages = next(iter(page_totals)) if len(page_totals) == 1 else None
        if declared_pages is not None:
            if declared_pages == 0:
                if not (
                    len(pages) == 1
                    and pages[0].status is PageStatus.EMPTY
                    and pages[0].terminal
                ):
                    issues.append("zero_page_count_contract_mismatch")
            elif terminal_page != declared_pages or len(pages) != declared_pages:
                issues.append("declared_page_count_mismatch")

        keys = [key for page in pages for key in page.row_keys]
        declared_total = next(iter(totals)) if len(totals) == 1 else None
        if declared_total is not None and len(set(keys)) != declared_total:
            issues.append("declared_record_count_mismatch")
        if pages[-1].status is PageStatus.EMPTY and keys and pages[-1].declared_total == 0:
            issues.append("empty_terminal_conflicts_with_prior_rows")

        return self._audit_result(issues=issues, terminal_page=terminal_page)

    def _audit_result(
        self,
        *,
        issues: Sequence[str],
        terminal_page: int | None,
    ) -> PageAudit:
        pages = self.pages
        totals = {page.declared_total for page in pages if page.declared_total is not None}
        page_totals = {page.declared_pages for page in pages if page.declared_pages is not None}
        keys = {key for page in pages for key in page.row_keys}
        normalized_issues = tuple(dict.fromkeys(issues))
        return PageAudit(
            complete=not normalized_issues,
            page_count=len(pages),
            unique_row_count=len(keys),
            declared_total=next(iter(totals)) if len(totals) == 1 else None,
            declared_pages=next(iter(page_totals)) if len(page_totals) == 1 else None,
            terminal_page=terminal_page,
            issues=normalized_issues,
            partition=tuple(sorted((str(k), str(v)) for k, v in self.partition.items())),
        )


def collect_pages(
    dataset_id: str,
    fetch_page: Callable[[int], PageSlice],
    *,
    partition: Mapping[str, str] | None = None,
    max_pages: int = 10_000,
) -> PageManifest:
    """Fetch sequential pages with an explicit upper bound and no hidden retries."""

    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    manifest = PageManifest(dataset_id=dataset_id, partition=partition or {})
    for page_number in range(1, max_pages + 1):
        page = fetch_page(page_number)
        if page.page_number != page_number:
            raise ValueError("fetch_page returned a different page number")
        manifest.add(page)
        if page.status is PageStatus.FAILED or page.terminal:
            return manifest
    manifest._issues.append("page_limit_exhausted")
    return manifest
