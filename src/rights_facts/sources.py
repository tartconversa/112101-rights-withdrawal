"""Strict CSV loading for immutable source snapshots.

The loader never infers values and never repairs malformed input.  Every row is
returned with its physical line number and remains addressable by ``record_id``
so downstream facts can cite exactly which credential they came from.
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping


GRANT_COLUMNS = (
    "record_id",
    "grant_id",
    "claimant_id",
    "work_id",
    "channel",
    "territory",
    "valid_from",
    "valid_to",
    "grant_state",
    "received_at",
    "supersedes_grant_id",
    "parent_claim_id",
    "coverage_scope",
    "claim_scope_note",
)

COVERAGE_SCOPES = frozenset({"all-versions", "original-only", "derivatives-only"})

VERSION_COLUMNS = (
    "record_id",
    "version_id",
    "work_id",
    "parent_version",
    "created_at",
    "edit_kind",
    "channel_ref",
    "source_ref",
    "edit_note",
)

USE_COLUMNS = (
    "record_id",
    "use_id",
    "version_id",
    "channel",
    "territory",
    "used_at",
    "usage_units",
    "source_ref",
    "reported_at",
    "batch_ref",
    "settlement_state",
)

GRANT_STATES = frozenset({"active", "withdrawn", "disputed", "claimed"})
SETTLEMENT_STATES = frozenset({"unsettled", "settled", "void"})
EDIT_KINDS = frozenset(
    {"original", "excerpt", "re-edit", "subtitle", "dub", "other"}
)

DATE_PRECISION = frozenset({"date", "datetime"})


class SourceError(ValueError):
    """Raised when a snapshot cannot be interpreted without guessing."""


@dataclass(frozen=True)
class SourceRow:
    """One source record with its provenance."""

    row: Mapping[str, str]
    line_number: int
    source_file: str

    @property
    def record_id(self) -> str:
        return self.row["record_id"]

    def get(self, name: str) -> str:
        return self.row.get(name, "").strip()

    def provenance(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "source_file": self.source_file,
            "line_number": self.line_number,
        }


@dataclass(frozen=True)
class Snapshot:
    """A directory of three CSV files representing one received state."""

    name: str
    path: Path
    grants: tuple[SourceRow, ...]
    versions: tuple[SourceRow, ...]
    uses: tuple[SourceRow, ...]
    issues: tuple["Issue", ...] = field(default_factory=tuple)

    def all_rows(self) -> Iterable[SourceRow]:
        yield from self.grants
        yield from self.versions
        yield from self.uses


@dataclass(frozen=True)
class Issue:
    """A data condition that must stay visible to data users.

    ``severity`` separates conditions that are expected and merely recorded
    (``info``) from rows that were ignored (``warning``) or that need review
    before any conclusion (``error``).
    """

    code: str
    source_file: str
    record_id: str
    line_number: int
    message: str
    severity: str = "warning"

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "source_file": self.source_file,
            "record_id": self.record_id,
            "line_number": self.line_number,
            "message": self.message,
        }


def parse_instant(value: str, *, field_name: str) -> dt.datetime:
    """Parse an ISO 8601 instant that must carry a UTC offset."""

    if not value:
        raise SourceError(f"{field_name} 不得为空")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError(f"{field_name} 不是合法的 ISO 8601 时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise SourceError(f"{field_name} 缺少时区偏移: {value!r}")
    return parsed.astimezone(dt.timezone.utc)


def parse_effective_date(value: str, *, field_name: str) -> tuple[dt.date, str]:
    """Parse a grant boundary expressed either as a date or an instant.

    Returns the UTC date and the precision that was supplied.  Dates are not
    widened into instants: callers decide how a date-only boundary interacts
    with a timestamped use.
    """

    if not value:
        raise SourceError(f"{field_name} 不得为空")
    try:
        if "T" in value:
            instant = parse_instant(value, field_name=field_name)
            return instant.date(), "datetime"
        return dt.date.fromisoformat(value), "date"
    except ValueError as exc:
        raise SourceError(f"{field_name} 不是合法日期: {value!r}") from exc


def read_csv(
    path: Path,
    expected_columns: tuple[str, ...],
    *,
    source_file: str,
) -> tuple[list[SourceRow], list[Issue]]:
    """Read a CSV with a fixed header, preserving line numbers."""

    if not path.is_file():
        raise SourceError(f"缺少源文件: {path}")
    rows: list[SourceRow] = []
    issues: list[Issue] = []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        header = tuple(reader.fieldnames or ())
        if header != expected_columns:
            missing = sorted(set(expected_columns) - set(header))
            extra = sorted(set(header) - set(expected_columns))
            detail = []
            if missing:
                detail.append("缺少列 " + ", ".join(missing))
            if extra:
                detail.append("出现未知列 " + ", ".join(extra))
            if not detail:  # same names, different order
                detail.append("列顺序与约定不一致")
            raise SourceError(f"{source_file} 表头不符合约定: " + "；".join(detail))
        seen: dict[str, int] = {}
        for offset, raw in enumerate(reader, start=2):
            if None in raw:
                issues.append(
                    Issue(
                        code="extra_columns",
                        source_file=source_file,
                        record_id=(raw.get("record_id") or "").strip(),
                        line_number=offset,
                        message="该行字段数多于表头，多出的值被忽略",
                        severity="warning",
                    )
                )
            cleaned = {name: (raw.get(name) or "").strip() for name in expected_columns}
            record_id = cleaned["record_id"]
            if not record_id:
                issues.append(
                    Issue(
                        code="missing_record_id",
                        source_file=source_file,
                        record_id="",
                        line_number=offset,
                        message="记录缺少 record_id，无法作为凭证引用",
                        severity="error",
                    )
                )
                continue
            if record_id in seen:
                issues.append(
                    Issue(
                        code="duplicate_record_id",
                        source_file=source_file,
                        record_id=record_id,
                        line_number=offset,
                        message=f"record_id 与第 {seen[record_id]} 行重复",
                        severity="error",
                    )
                )
                continue
            seen[record_id] = offset
            rows.append(SourceRow(row=cleaned, line_number=offset, source_file=source_file))
    return rows, issues


def load_snapshot(path: str | Path, *, name: str | None = None) -> Snapshot:
    """Load and lightly validate a snapshot directory.

    Structural problems (missing files or malformed headers) raise
    :class:`SourceError`.  Per-row problems are returned as :class:`Issue`
    objects so that the remaining rows can still be ingested and reviewed.
    """

    root = Path(path)
    if not root.is_dir():
        raise SourceError(f"快照目录不存在: {root}")
    snapshot_name = name or root.name
    grants, grant_issues = read_csv(
        root / "grants.csv", GRANT_COLUMNS, source_file="grants.csv"
    )
    versions, version_issues = read_csv(
        root / "content_versions.csv", VERSION_COLUMNS, source_file="content_versions.csv"
    )
    uses, use_issues = read_csv(
        root / "uses.csv", USE_COLUMNS, source_file="uses.csv"
    )
    issues = [*grant_issues, *version_issues, *use_issues]

    def note(
        code: str, row: SourceRow, message: str, severity: str = "warning"
    ) -> None:
        issues.append(
            Issue(
                code=code,
                source_file=row.source_file,
                record_id=row.record_id,
                line_number=row.line_number,
                message=message,
                severity=severity,
            )
        )

    for row in grants:
        state = row.get("grant_state")
        if state not in GRANT_STATES:
            note("unknown_grant_state", row, f"未知的 grant_state: {state!r}")
        for column in ("valid_from", "valid_to"):
            try:
                parse_effective_date(row.get(column), field_name=column)
            except SourceError as exc:
                note("bad_grant_boundary", row, str(exc))
        try:
            parse_instant(row.get("received_at"), field_name="received_at")
        except SourceError as exc:
            note("bad_received_at", row, str(exc))
        start = row.get("valid_from")
        end = row.get("valid_to")
        if start and end:
            try:
                if parse_effective_date(start, field_name="valid_from")[0] > parse_effective_date(
                    end, field_name="valid_to"
                )[0]:
                    note("inverted_grant_window", row, "valid_from 晚于 valid_to")
            except SourceError:
                pass
        if not row.get("claimant_id"):
            note("missing_claimant", row, "权利主张缺少 claimant_id")
        if not row.get("work_id"):
            note("missing_work", row, "权利主张缺少 work_id")

    for row in versions:
        kind = row.get("edit_kind")
        if kind not in EDIT_KINDS:
            note("unknown_edit_kind", row, f"未知的 edit_kind: {kind!r}")
        try:
            parse_instant(row.get("created_at"), field_name="created_at")
        except SourceError as exc:
            note("bad_created_at", row, str(exc))
        parent = row.get("parent_version")
        if parent and parent == row.get("version_id"):
            note("self_parent", row, "版本引用自身作为父版本", severity="error")

    for row in uses:
        try:
            parse_instant(row.get("used_at"), field_name="used_at")
        except SourceError as exc:
            note("bad_used_at", row, str(exc))
        try:
            parse_instant(row.get("reported_at"), field_name="reported_at")
        except SourceError as exc:
            note("bad_reported_at", row, str(exc))
        units = row.get("usage_units")
        if not units.isdigit() or int(units) <= 0:
            note("bad_usage_units", row, f"usage_units 必须为正整数: {units!r}")
        if row.get("settlement_state") not in SETTLEMENT_STATES:
            note(
                "unknown_settlement_state",
                row,
                f"未知的 settlement_state: {row.get('settlement_state')!r}",
            )
        if not row.get("territory"):
            note("missing_territory", row, "使用记录缺少 territory")

    return Snapshot(
        name=snapshot_name,
        path=root,
        grants=tuple(grants),
        versions=tuple(versions),
        uses=tuple(uses),
        issues=tuple(issues),
    )
