"""Domain model built from one source snapshot.

The model is pure data plus deterministic resolution rules.  It never writes to
the lake and never consults anything outside the snapshot it was built from, so
the same inputs always produce the same coverage facts.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .sources import (
    COVERAGE_SCOPES,
    Issue,
    SourceError,
    SourceRow,
    Snapshot,
    parse_effective_date,
    parse_instant,
)

UTC = dt.timezone.utc
LATE_REPORT_DAYS = 30
LATE_REPORT_TOLERANCE = dt.timedelta(days=LATE_REPORT_DAYS)


def id_key(value: str) -> str:
    """Identifiers are matched case-insensitively but displayed as supplied."""

    return value.strip().casefold()


def provenance(row: SourceRow) -> dict[str, object]:
    return {
        "record_id": row.record_id,
        "source_file": row.source_file,
        "line_number": row.line_number,
    }


@dataclass(frozen=True)
class Boundary:
    """A grant boundary; date-only and instant boundaries stay distinct."""

    raw: str
    date: dt.date
    precision: str  # "date" or "datetime"
    instant: dt.datetime | None

    def contains(self, moment: dt.datetime, *, is_end: bool) -> bool:
        if self.precision == "date":
            # A date-only start includes the whole day; a date-only end also
            # includes the whole final day.  Nothing is silently widened into a
            # timestamp; the precision travels with the fact.
            if is_end:
                return moment.date() <= self.date
            return moment.date() >= self.date
        assert self.instant is not None
        if is_end:
            return moment <= self.instant
        return moment >= self.instant


@dataclass(frozen=True)
class Grant:
    record_id: str
    grant_id: str
    claimant_id: str
    work_id: str
    channel: str
    territory: str
    valid_from: Boundary
    valid_to: Boundary
    valid_from_raw: str
    valid_to_raw: str
    grant_state: str
    received_at: dt.datetime
    supersedes_grant_id: str
    parent_claim_id: str
    coverage_scope: str
    note: str
    row: SourceRow

    @property
    def key(self) -> str:
        return id_key(self.grant_id)

    @property
    def is_claim_assertion(self) -> bool:
        """A claimed right without an authorization boundary is not a licence."""

        return bool(self.parent_claim_id) or self.grant_state == "claimed"

    @property
    def state(self) -> str:
        return self.grant_state

    @property
    def provenance(self) -> dict[str, object]:
        return provenance(self.row)

    def window_contains(self, moment: dt.datetime) -> bool:
        return self.valid_from.contains(moment, is_end=False) and self.valid_to.contains(
            moment, is_end=True
        )

    def channel_matches(self, channel: str) -> bool:
        return self.channel in ("", "*") or id_key(self.channel) == id_key(channel)

    def territory_matches(self, territory: str) -> bool:
        return self.territory in ("", "*") or id_key(self.territory) == id_key(territory)

    def scope_dict(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "claimant_id": self.claimant_id,
            "work_id": self.work_id,
            "channel": self.channel,
            "territory": self.territory,
            "valid_from": self.valid_from_raw,
            "valid_to": self.valid_to_raw,
            "valid_from_precision": self.valid_from.precision,
            "valid_to_precision": self.valid_to.precision,
            "grant_state": self.grant_state,
            "coverage_scope": self.coverage_scope,
            "received_at": _iso(self.received_at),
            "supersedes_grant_id": self.supersedes_grant_id,
            "parent_claim_id": self.parent_claim_id,
            "claim_scope_note": self.note,
            "record_id": self.record_id,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class Version:
    record_id: str
    version_id: str
    work_id: str
    parent_version: str
    created_at: dt.datetime
    edit_kind: str
    channel_ref: str
    source_ref: str
    edit_note: str
    row: SourceRow

    @property
    def key(self) -> str:
        return id_key(self.version_id)

    @property
    def is_original(self) -> bool:
        return not self.parent_version

    @property
    def provenance(self) -> dict[str, object]:
        return provenance(self.row)

    def dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "work_id": self.work_id,
            "parent_version": self.parent_version,
            "created_at": _iso(self.created_at),
            "edit_kind": self.edit_kind,
            "channel_ref": self.channel_ref,
            "source_ref": self.source_ref,
            "edit_note": self.edit_note,
            "record_id": self.record_id,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class Use:
    record_id: str
    use_id: str
    version_id: str
    channel: str
    territory: str
    used_at: dt.datetime
    usage_units: int
    source_ref: str
    reported_at: dt.datetime
    batch_ref: str
    settlement_state: str
    row: SourceRow

    @property
    def key(self) -> str:
        return id_key(self.use_id)

    @property
    def is_late(self) -> bool:
        return self.reported_at - self.used_at > LATE_REPORT_TOLERANCE

    @property
    def provenance(self) -> dict[str, object]:
        return provenance(self.row)

    def content_signature(self) -> tuple[object, ...]:
        """Everything that should identify one physical use."""

        return (
            id_key(self.version_id),
            id_key(self.channel),
            id_key(self.territory),
            self.used_at,
            self.usage_units,
            id_key(self.source_ref),
        )

    def dict(self) -> dict[str, object]:
        return {
            "use_id": self.use_id,
            "version_id": self.version_id,
            "channel": self.channel,
            "territory": self.territory,
            "used_at": _iso(self.used_at),
            "usage_units": self.usage_units,
            "source_ref": self.source_ref,
            "reported_at": _iso(self.reported_at),
            "batch_ref": self.batch_ref,
            "settlement_state": self.settlement_state,
            "record_id": self.record_id,
            "late_report": self.is_late,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class Lineage:
    """Result of walking a version's parent chain within one snapshot."""

    version_id: str
    chain: tuple[str, ...]
    status: str  # consistent | conflict | incomplete | cycle | missing
    works: tuple[str, ...]
    message: str
    evidence: tuple[Mapping[str, object], ...] = ()

    @property
    def effective_work(self) -> str | None:
        if self.status == "consistent" and self.works:
            return self.works[0]
        return None

    @property
    def is_original(self) -> bool:
        return len(self.chain) == 1

    def dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "chain": list(self.chain),
            "status": self.status,
            "works": list(self.works),
            "effective_work": self.effective_work,
            "is_original": self.is_original,
            "message": self.message,
            "evidence": [dict(item) for item in self.evidence],
        }


@dataclass
class SnapshotModel:
    snapshot: Snapshot
    received_at: dt.datetime
    grants: list[Grant]
    versions: list[Version]
    uses: list[Use]
    issues: list[Issue]
    versions_by_key: dict[str, Version] = field(default_factory=dict)
    uses_by_key: dict[str, list[Use]] = field(default_factory=dict)
    latest_grant_by_key: dict[str, Grant] = field(default_factory=dict)
    superseded_by: dict[str, Grant] = field(default_factory=dict)
    lineage_cache: dict[str, Lineage] = field(default_factory=dict)

    # -- construction -------------------------------------------------
    @classmethod
    def build(cls, snapshot: Snapshot, *, received_at: dt.datetime | None = None) -> "SnapshotModel":
        grants: list[Grant] = []
        versions: list[Version] = []
        uses: list[Use] = []
        issues = list(snapshot.issues)

        for row in snapshot.grants:
            try:
                grants.append(grant_from_row(row))
            except SourceError as exc:
                issues.append(_row_issue("unusable_grant", row, str(exc), "error"))
        for row in snapshot.versions:
            try:
                versions.append(version_from_row(row))
            except SourceError as exc:
                issues.append(_row_issue("unusable_version", row, str(exc), "error"))
        for row in snapshot.uses:
            try:
                uses.append(use_from_row(row))
            except SourceError as exc:
                issues.append(_row_issue("unusable_use", row, str(exc), "error"))

        moment = received_at or snapshot_received_at(snapshot, grants, uses)
        model = cls(
            snapshot=snapshot,
            received_at=moment,
            grants=grants,
            versions=versions,
            uses=uses,
            issues=issues,
        )
        model._index()
        model._resolve_grants()
        return model

    def _index(self) -> None:
        for version in self.versions:
            if version.key in self.versions_by_key:
                first = self.versions_by_key[version.key]
                self.issues.append(
                    _row_issue(
                        "duplicate_version_id",
                        version.row,
                        f"version_id {version.version_id} 与第 {first.row.line_number} 行重复，保留先出现的记录",
                        severity="error",
                    )
                )
                continue
            self.versions_by_key[version.key] = version
        for use in self.uses:
            self.uses_by_key.setdefault(use.key, []).append(use)
        for records in self.uses_by_key.values():
            records.sort(key=lambda u: (u.reported_at, u.record_id))

    def _resolve_grants(self) -> None:
        """Pick the newest observation per grant_id and link supersession.

        a claim family is all records sharing one ``grant_id``.  Successive
        observations describe the same authorization at different points in
        time (for example ``active`` then ``withdrawn``).  Only the newest
        observation by ``received_at`` describes the family's current state;
        earlier observations stay available as history.

        A record may also name a different ``grant_id`` in
        ``supersedes_grant_id``.  That is recorded as a supersession edge and
        kept for context and cycle detection, but each family keeps its own
        place of effect: a withdrawal is expressed as a new observation of the
        family it narrows, not by deleting another family.
        """

        grouped: dict[str, list[Grant]] = {}
        for grant in self.grants:
            grouped.setdefault(grant.key, []).append(grant)
        for key, records in grouped.items():
            records.sort(key=lambda g: (g.received_at, g.record_id))
            chosen = records[-1]
            self.latest_grant_by_key[key] = chosen
            if len(records) > 1:
                self.issues.append(
                    _row_issue(
                        "grant_observation_history",
                        chosen.row,
                        f"grant_id {chosen.grant_id} 有 {len(records)} 份观察记录，"
                        f"当前采用 received_at 最晚的 {chosen.record_id}（{chosen.grant_state}）",
                        severity="info",
                    )
                )

        for grant in self.latest_grant_by_key.values():
            target = id_key(grant.supersedes_grant_id)
            if not target:
                continue
            if target not in self.latest_grant_by_key:
                self.issues.append(
                    _row_issue(
                        "dangling_supersedes",
                        grant.row,
                        f"{grant.grant_id} 声明替代 {grant.supersedes_grant_id}，但该主张不在本快照中",
                    )
                )
                continue
            if target == grant.key:
                self.issues.append(
                    _row_issue("self_supersedes", grant.row, f"{grant.grant_id} 声明替代自身")
                )
                continue
            self.superseded_by[target] = grant

        for key in sorted(self.latest_grant_by_key):
            cycle = self._supersession_cycle(key)
            if cycle:
                grant = self.latest_grant_by_key[key]
                self.issues.append(
                    _row_issue(
                        "supersession_cycle",
                        grant.row,
                        "替代关系成环：" + " → ".join(cycle) + "；状态无法按链推断",
                        severity="error",
                    )
                )

    def _supersession_cycle(self, start: str) -> tuple[str, ...] | None:
        """Return the cycle if the supersession chain from ``start`` loops."""

        path: list[str] = []
        index: dict[str, int] = {}
        current = start
        while True:
            if current in index:
                return tuple(path[index[current]:] + [current])
            index[current] = len(path)
            path.append(current)
            grant = self.latest_grant_by_key.get(current)
            if grant is None:
                return None
            target = id_key(grant.supersedes_grant_id)
            if not target:
                return None
            current = target

    # -- queries ------------------------------------------------------
    def effective_grants(self) -> list[Grant]:
        """Newest observation of every claim family, sorted by grant_id."""

        result = list(self.latest_grant_by_key.values())
        result.sort(key=lambda g: (g.grant_id.casefold(), g.record_id))
        return result

    def grant_history(self, grant_id: str) -> list[Grant]:
        """Every observation of a claim family, oldest first."""

        return sorted(
            (g for g in self.grants if g.key == id_key(grant_id)),
            key=lambda g: (g.received_at, g.record_id),
        )

    def grants_for_work(self, work_id: str) -> list[Grant]:
        key = id_key(work_id)
        return [g for g in self.grants if id_key(g.work_id) == key]

    def lineage(self, version_id: str) -> Lineage:
        key = id_key(version_id)
        if key in self.lineage_cache:
            return self.lineage_cache[key]
        current = self.versions_by_key.get(key)
        if current is None:
            result = Lineage(
                version_id=version_id,
                chain=(),
                status="missing",
                works=(),
                message=f"版本 {version_id} 不在快照中，无法确定作品归属",
            )
            self.lineage_cache[key] = result
            return result

        chain: list[str] = []
        works: list[str] = []
        evidence: list[Mapping[str, object]] = []
        seen: dict[str, int] = {}
        status = "consistent"
        message = "版本链完整且作品引用一致"
        while current is not None:
            if current.key in seen:
                status = "cycle"
                message = f"版本链在 {current.version_id} 处成环"
                break
            seen[current.key] = len(chain)
            chain.append(current.version_id)
            works.append(current.work_id)
            evidence.append(current.provenance)
            parent = id_key(current.parent_version)
            if not parent:
                break
            parent_version = self.versions_by_key.get(parent)
            if parent_version is None:
                status = "incomplete"
                message = (
                    f"{current.version_id} 引用父版本 {current.parent_version}，"
                    "但该版本不在快照中，衍生关系无法核实"
                )
                break
            current = parent_version
        if status == "consistent":
            distinct = {id_key(w) for w in works}
            if len(distinct) > 1:
                status = "conflict"
                message = (
                    "版本链引用不同作品（" + "、".join(dict.fromkeys(works)) + "），"
                    "无法确认衍生版本归属"
                )
        result = Lineage(
            version_id=version_id,
            chain=tuple(chain),
            status=status,
            works=tuple(dict.fromkeys(works)),
            message=message,
            evidence=tuple(evidence),
        )
        self.lineage_cache[key] = result
        return result

    def canonical_uses(self) -> list[Use]:
        """One representative record per use_id; exact duplicates collapse."""

        return [sorted(records, key=lambda u: (u.reported_at, u.record_id))[0]
                for _, records in sorted(self.uses_by_key.items())]

    def duplicate_groups(self) -> dict[str, list[Use]]:
        return {key: recs for key, recs in self.uses_by_key.items() if len(recs) > 1}

    def content_duplicate_groups(self) -> list[list[Use]]:
        """Different use_ids that describe the same physical use."""

        buckets: dict[tuple[object, ...], list[Use]] = {}
        for use in self.canonical_uses():
            buckets.setdefault(use.content_signature(), []).append(use)
        return [sorted(group, key=lambda u: u.use_id) for group in buckets.values() if len(group) > 1]


def _row_issue(
    code: str, row: SourceRow, message: str, severity: str = "warning"
) -> Issue:
    return Issue(
        code=code,
        source_file=row.source_file,
        record_id=row.record_id,
        line_number=row.line_number,
        message=message,
        severity=severity,
    )


def grant_from_row(row: SourceRow) -> Grant:
    start = parse_effective_date(row.get("valid_from"), field_name="valid_from")
    end = parse_effective_date(row.get("valid_to"), field_name="valid_to")
    scope = row.get("coverage_scope") or "all-versions"
    if scope not in COVERAGE_SCOPES:
        raise SourceError(f"未知的 coverage_scope: {scope!r}")
    return Grant(
        record_id=row.record_id,
        grant_id=row.get("grant_id"),
        claimant_id=row.get("claimant_id"),
        work_id=row.get("work_id"),
        channel=row.get("channel") or "*",
        territory=row.get("territory") or "*",
        valid_from=Boundary(
            raw=row.get("valid_from"),
            date=start[0],
            precision=start[1],
            instant=_instant_or_none(row.get("valid_from")),
        ),
        valid_to=Boundary(
            raw=row.get("valid_to"),
            date=end[0],
            precision=end[1],
            instant=_instant_or_none(row.get("valid_to")),
        ),
        valid_from_raw=row.get("valid_from"),
        valid_to_raw=row.get("valid_to"),
        grant_state=row.get("grant_state"),
        received_at=parse_instant(row.get("received_at"), field_name="received_at"),
        supersedes_grant_id=row.get("supersedes_grant_id"),
        parent_claim_id=row.get("parent_claim_id"),
        coverage_scope=scope,
        note=row.get("claim_scope_note"),
        row=row,
    )


def version_from_row(row: SourceRow) -> Version:
    return Version(
        record_id=row.record_id,
        version_id=row.get("version_id"),
        work_id=row.get("work_id"),
        parent_version=row.get("parent_version"),
        created_at=parse_instant(row.get("created_at"), field_name="created_at"),
        edit_kind=row.get("edit_kind"),
        channel_ref=row.get("channel_ref"),
        source_ref=row.get("source_ref"),
        edit_note=row.get("edit_note"),
        row=row,
    )


def use_from_row(row: SourceRow) -> Use:
    units = row.get("usage_units")
    if not units.isdigit() or int(units) <= 0:
        raise SourceError(f"usage_units 必须为正整数: {units!r}")
    return Use(
        record_id=row.record_id,
        use_id=row.get("use_id"),
        version_id=row.get("version_id"),
        channel=row.get("channel"),
        territory=row.get("territory"),
        used_at=parse_instant(row.get("used_at"), field_name="used_at"),
        usage_units=int(units),
        source_ref=row.get("source_ref"),
        reported_at=parse_instant(row.get("reported_at"), field_name="reported_at"),
        batch_ref=row.get("batch_ref"),
        settlement_state=row.get("settlement_state"),
        row=row,
    )


def _instant_or_none(value: str) -> dt.datetime | None:
    if "T" not in value:
        return None
    try:
        return parse_instant(value, field_name="boundary")
    except SourceError:
        return None


def snapshot_received_at(
    snapshot: Snapshot, grants: Sequence[Grant], uses: Sequence[Use]
) -> dt.datetime:
    """Latest instant any record could have been received in this snapshot."""

    moments: list[dt.datetime] = [g.received_at for g in grants]
    moments.extend(u.reported_at for u in uses)
    if not moments:
        raise SourceError(f"快照 {snapshot.name} 没有任何带时间的记录，无法确定接收时间")
    return max(moments).astimezone(UTC)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
