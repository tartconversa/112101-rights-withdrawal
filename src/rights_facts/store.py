"""Append-only SQLite lake for source snapshots, facts and coverage results.

Design constraints:

* Every ingestion appends a new ``ingest_run``.  Nothing is updated in place, so
  history can always be replayed.
* Raw source rows are stored verbatim next to the normalised facts they produced,
  which keeps every conclusion traceable to a credential and a line.
* Coverage is derived and stored per run.  Comparisons between consecutive runs
  produce explicit change events, including state changes for uses that were
  already settled.
* The lake never stores a legal verdict.  ``covered``/``not_covered`` describe
  documentary coverage only.

The module only depends on the standard library.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from . import __version__
from .coverage import Coverage, evaluate_snapshot
from .model import SnapshotModel
from .sources import Snapshot, load_snapshot
from .util import canonical_json, format_instant, now_utc, sha256_text

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS lake_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_run (
    run_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    snapshot_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    received_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    counts_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_record (
    run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    source_file TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    record_id TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, source_file, line_number)
);

CREATE TABLE IF NOT EXISTS grant_record (
    run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    record_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    claimant_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    territory TEXT NOT NULL,
    valid_from_raw TEXT NOT NULL,
    valid_to_raw TEXT NOT NULL,
    from_precision TEXT NOT NULL,
    to_precision TEXT NOT NULL,
    grant_state TEXT NOT NULL,
    received_at TEXT NOT NULL,
    supersedes_grant_id TEXT NOT NULL,
    parent_claim_id TEXT NOT NULL,
    coverage_scope TEXT NOT NULL,
    note TEXT NOT NULL,
    source_file TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    PRIMARY KEY (run_id, record_id)
);

CREATE TABLE IF NOT EXISTS version_record (
    run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    record_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    parent_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edit_kind TEXT NOT NULL,
    channel_ref TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    edit_note TEXT NOT NULL,
    source_file TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    PRIMARY KEY (run_id, record_id)
);

CREATE TABLE IF NOT EXISTS use_record (
    run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    record_id TEXT NOT NULL,
    use_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    territory TEXT NOT NULL,
    used_at TEXT NOT NULL,
    usage_units INTEGER NOT NULL,
    source_ref TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    batch_ref TEXT NOT NULL,
    settlement_state TEXT NOT NULL,
    late_report INTEGER NOT NULL,
    source_file TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    PRIMARY KEY (run_id, record_id)
);

CREATE TABLE IF NOT EXISTS issue_record (
    run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    source_file TEXT NOT NULL,
    record_id TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_record (
    run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    use_id TEXT NOT NULL,
    use_record_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    summary TEXT NOT NULL,
    matched_grant_id TEXT NOT NULL,
    supporting_grant_ids TEXT NOT NULL,
    lineage_status TEXT NOT NULL,
    lineage_chain_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    usage_units INTEGER NOT NULL,
    channel TEXT NOT NULL,
    territory TEXT NOT NULL,
    used_at TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    settlement_state TEXT NOT NULL,
    late_report INTEGER NOT NULL,
    PRIMARY KEY (run_id, use_id)
);

CREATE TABLE IF NOT EXISTS coverage_change (
    from_run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    to_run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    use_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_outcome TEXT NOT NULL,
    to_outcome TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (to_run_id, use_id, kind)
);

CREATE TABLE IF NOT EXISTS review_item (
    run_id TEXT NOT NULL REFERENCES ingest_run(run_id),
    use_id TEXT NOT NULL,
    priority INTEGER NOT NULL,
    reason_codes_json TEXT NOT NULL,
    state TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (run_id, use_id)
);
"""


class LakeError(RuntimeError):
    """Raised for lake-level problems (unknown run, duplicated ingest)."""


# Reasons worth surfacing in the review queue.  A covered use keeps its
# supporting reasons in coverage_record but does not need review on that basis.
REVIEW_REASON_CODES = frozenset(
    {
        "no_applicable_grant",
        "claim_without_grant",
        "no_evidence",
        "work_lineage_conflict",
        "lineage_incomplete",
        "lineage_cycle",
        "version_missing",
        "grant_withdrawn",
        "grant_disputed",
        "duplicate_use_id",
        "late_report",
    }
)


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    sequence: int
    snapshot_name: str
    source_path: str
    content_hash: str
    received_at: str
    ingested_at: str
    counts: Mapping[str, object]

    def dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "snapshot_name": self.snapshot_name,
            "source_path": self.source_path,
            "content_hash": self.content_hash,
            "received_at": self.received_at,
            "ingested_at": self.ingested_at,
            "counts": dict(self.counts),
        }


class Lake:
    """A file-backed append-only lake."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self.connection.execute(
            "INSERT OR IGNORE INTO lake_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO lake_meta(key, value) VALUES('tool_version', ?)",
            (__version__,),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Lake":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- ingest -------------------------------------------------------
    def ingest(
        self,
        snapshot_path: str | Path,
        *,
        snapshot_name: str | None = None,
        received_at: dt.datetime | None = None,
        allow_duplicate: bool = False,
    ) -> tuple[RunInfo, SnapshotModel, list[Coverage]]:
        """Ingest one snapshot directory and derive coverage for it."""

        snapshot = load_snapshot(snapshot_path, name=snapshot_name)
        model = SnapshotModel.build(snapshot, received_at=received_at)
        coverages = evaluate_snapshot(model)
        content_hash = snapshot_content_hash(snapshot)

        existing = self.connection.execute(
            "SELECT run_id, sequence FROM ingest_run WHERE content_hash = ?"
            " AND received_at = ?",
            (content_hash, format_instant(model.received_at)),
        ).fetchone()
        if existing and not allow_duplicate:
            raise LakeError(
                f"该快照内容与接收时间已入库（快照 #{existing['sequence']}，"
                f"run_id={existing['run_id']}）；如需重复入库请显式允许"
            )

        sequence = self.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM ingest_run"
        ).fetchone()["next"]
        # Sequence is part of the identity so an explicitly allowed duplicate
        # becomes a distinct, orderable run instead of a primary-key conflict.
        run_id = sha256_text(
            content_hash + "|" + format_instant(model.received_at) + f"|{sequence}"
        )[:24]
        counts = {
            "grants": len(model.grants),
            "versions": len(model.versions),
            "uses": len(model.uses),
            "canonical_uses": len(model.canonical_uses()),
            "coverage": len(coverages),
            "issues": len(model.issues),
            "issue_errors": sum(1 for i in model.issues if i.severity == "error"),
            "issue_warnings": sum(1 for i in model.issues if i.severity == "warning"),
        }
        ingested_at = now_utc()
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO ingest_run(run_id, sequence, snapshot_name, source_path,"
                    " content_hash, received_at, ingested_at, tool_version, counts_json)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        sequence,
                        snapshot.name,
                        str(snapshot.path.resolve()),
                        content_hash,
                        format_instant(model.received_at),
                        format_instant(ingested_at),
                        __version__,
                        canonical_json(counts),
                    ),
                )
                self._insert_raw(run_id, snapshot)
                self._insert_grants(run_id, model)
                self._insert_versions(run_id, model)
                self._insert_uses(run_id, model)
                self._insert_issues(run_id, model)
                self._insert_coverage(run_id, coverages)
                self._record_changes(run_id)
                self._build_review_queue(run_id)
        except sqlite3.IntegrityError as exc:
            raise LakeError(f"入库冲突：{exc}") from exc

        info = self.get_run(run_id)
        assert info is not None
        return info, model, coverages

    def _insert_raw(self, run_id: str, snapshot: Snapshot) -> None:
        for row in snapshot.all_rows():
            self.connection.execute(
                "INSERT INTO source_record(run_id, source_file, line_number, record_id, raw_json)"
                " VALUES(?,?,?,?,?)",
                (
                    run_id,
                    row.source_file,
                    row.line_number,
                    row.record_id,
                    canonical_json(dict(row.row)),
                ),
            )

    def _insert_grants(self, run_id: str, model: SnapshotModel) -> None:
        for grant in model.grants:
            self.connection.execute(
                "INSERT INTO grant_record VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    grant.record_id,
                    grant.grant_id,
                    grant.claimant_id,
                    grant.work_id,
                    grant.channel,
                    grant.territory,
                    grant.valid_from_raw,
                    grant.valid_to_raw,
                    grant.valid_from.precision,
                    grant.valid_to.precision,
                    grant.grant_state,
                    format_instant(grant.received_at),
                    grant.supersedes_grant_id,
                    grant.parent_claim_id,
                    grant.coverage_scope,
                    grant.note,
                    str(grant.provenance["source_file"]),
                    int(grant.provenance["line_number"]),
                ),
            )

    def _insert_versions(self, run_id: str, model: SnapshotModel) -> None:
        for version in model.versions:
            self.connection.execute(
                "INSERT INTO version_record VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    version.record_id,
                    version.version_id,
                    version.work_id,
                    version.parent_version,
                    format_instant(version.created_at),
                    version.edit_kind,
                    version.channel_ref,
                    version.source_ref,
                    version.edit_note,
                    str(version.provenance["source_file"]),
                    int(version.provenance["line_number"]),
                ),
            )

    def _insert_uses(self, run_id: str, model: SnapshotModel) -> None:
        for use in model.uses:
            self.connection.execute(
                "INSERT INTO use_record VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    use.record_id,
                    use.use_id,
                    use.version_id,
                    use.channel,
                    use.territory,
                    format_instant(use.used_at),
                    use.usage_units,
                    use.source_ref,
                    format_instant(use.reported_at),
                    use.batch_ref,
                    use.settlement_state,
                    1 if use.is_late else 0,
                    str(use.provenance["source_file"]),
                    int(use.provenance["line_number"]),
                ),
            )

    def _insert_issues(self, run_id: str, model: SnapshotModel) -> None:
        for issue in model.issues:
            self.connection.execute(
                "INSERT INTO issue_record VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    issue.code,
                    issue.severity,
                    issue.source_file,
                    issue.record_id,
                    issue.line_number,
                    issue.message,
                ),
            )

    def _insert_coverage(self, run_id: str, coverages: Sequence[Coverage]) -> None:
        for coverage in coverages:
            payload = coverage.dict()
            self.connection.execute(
                "INSERT INTO coverage_record VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    coverage.use.use_id,
                    coverage.use.record_id,
                    coverage.use.version_id,
                    coverage.lineage.effective_work or "",
                    coverage.outcome,
                    coverage.summary,
                    coverage.matched_grant.grant_id if coverage.matched_grant else "",
                    canonical_json([g.grant_id for g in coverage.supporting_grants]),
                    coverage.lineage.status,
                    canonical_json(list(coverage.lineage.chain)),
                    canonical_json(list(coverage.reasons)),
                    canonical_json(payload["evidence"]),
                    coverage.use.usage_units,
                    coverage.use.channel,
                    coverage.use.territory,
                    format_instant(coverage.use.used_at),
                    format_instant(coverage.use.reported_at),
                    coverage.use.settlement_state,
                    1 if coverage.use.is_late else 0,
                ),
            )

    def _record_changes(self, run_id: str) -> None:
        previous = self.previous_run(run_id)
        if previous is None:
            return
        previous_rows = {
            row["use_id"]: row
            for row in self.connection.execute(
                "SELECT use_id, outcome, settlement_state, matched_grant_id"
                " FROM coverage_record WHERE run_id = ?",
                (previous.run_id,),
            )
        }
        for row in self.connection.execute(
            "SELECT use_id, outcome, settlement_state, matched_grant_id"
            " FROM coverage_record WHERE run_id = ?",
            (run_id,),
        ):
            before = previous_rows.get(row["use_id"])
            if before is None:
                self._insert_change(
                    previous.run_id,
                    run_id,
                    row["use_id"],
                    "new_use",
                    "",
                    row["outcome"],
                    {"settlement_state": row["settlement_state"]},
                )
                continue
            if before["outcome"] != row["outcome"]:
                self._insert_change(
                    previous.run_id,
                    run_id,
                    row["use_id"],
                    "outcome_changed",
                    before["outcome"],
                    row["outcome"],
                    {
                        "from_grant": before["matched_grant_id"],
                        "to_grant": row["matched_grant_id"],
                        "settlement_state": row["settlement_state"],
                    },
                )
            elif before["settlement_state"] != row["settlement_state"]:
                self._insert_change(
                    previous.run_id,
                    run_id,
                    row["use_id"],
                    "settlement_changed",
                    before["outcome"],
                    row["outcome"],
                    {
                        "from_settlement": before["settlement_state"],
                        "to_settlement": row["settlement_state"],
                    },
                )
        for use_id in sorted(set(previous_rows) - self._run_use_ids(run_id)):
            before = previous_rows[use_id]
            self._insert_change(
                previous.run_id,
                run_id,
                use_id,
                "use_disappeared",
                before["outcome"],
                "",
                {"settlement_state": before["settlement_state"]},
            )

    def _insert_change(
        self,
        from_run: str,
        to_run: str,
        use_id: str,
        kind: str,
        from_outcome: str,
        to_outcome: str,
        detail: Mapping[str, object],
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO coverage_change VALUES(?,?,?,?,?,?,?)",
            (
                from_run,
                to_run,
                use_id,
                kind,
                from_outcome,
                to_outcome,
                canonical_json(detail),
            ),
        )

    def _run_use_ids(self, run_id: str) -> set[str]:
        return {
            row["use_id"]
            for row in self.connection.execute(
                "SELECT use_id FROM coverage_record WHERE run_id = ?", (run_id,)
            )
        }

    def _build_review_queue(self, run_id: str) -> None:
        """Materialise the review list for one run from its coverage facts.

        Priority 1 means access or money may already have moved on an
        assumption that no longer holds: withdrawn or disputed rights, changes
        to an already-settled use, or a version whose lineage cannot be read.
        Priority 2 is an unconfirmed coverage gap or duplicate.  Priority 3 is
        a late report that needs a billing-period decision.
        """

        for row in self.connection.execute(
            "SELECT * FROM coverage_record WHERE run_id = ?", (run_id,)
        ):
            reasons = json.loads(row["reasons_json"])
            outcome = row["outcome"]
            state = row["settlement_state"]
            changes = [
                dict(change)
                for change in self.connection.execute(
                    "SELECT kind, from_outcome, to_outcome, detail_json FROM coverage_change"
                    " WHERE to_run_id = ? AND use_id = ?",
                    (run_id, row["use_id"]),
                )
            ]
            material_changes = [
                change for change in changes if change["kind"] != "new_use"
            ]
            codes: list[str] = []

            if outcome == "under_determination" or outcome == "unresolved":
                priority = 1
            elif outcome == "not_covered":
                priority = 2
            else:
                priority = 3

            if "duplicate_use_id" in reasons:
                priority = min(priority, 2)
            for reason in reasons:
                if reason in REVIEW_REASON_CODES:
                    codes.append(reason)
            if row["late_report"]:
                codes.append("late_report")
                priority = max(priority, 3)

            # A settled use is the risky case: money or access already moved.
            # It enters the queue whenever its documentary coverage is not
            # simply covered-and-unchanged.
            if state == "settled" and (outcome != "covered" or material_changes):
                codes.append("settlement_already_recorded")
                priority = 1
            elif material_changes:
                priority = min(priority, 2)

            for change in changes:
                codes.append(f"change:{change['kind']}")
                if change["kind"] in ("outcome_changed", "use_disappeared"):
                    # Losing coverage on a settled use, or any non-covered
                    # result after a change, needs a human before billing.
                    if state == "settled" or outcome != "covered":
                        priority = 1
                    else:
                        priority = min(priority, 2)
                elif change["kind"] == "settlement_changed" and outcome != "covered":
                    priority = min(priority, 2)

            if outcome == "covered" and not codes:
                continue

            detail = {
                "use_id": row["use_id"],
                "version_id": row["version_id"],
                "work_id": row["work_id"],
                "outcome": outcome,
                "matched_grant_id": row["matched_grant_id"],
                "settlement_state": state,
                "usage_units": row["usage_units"],
                "used_at": row["used_at"],
                "channel": row["channel"],
                "territory": row["territory"],
                "summary": row["summary"],
                "changes": changes,
            }
            self.connection.execute(
                "INSERT INTO review_item VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    row["use_id"],
                    priority,
                    canonical_json(sorted(set(codes))),
                    state,
                    canonical_json(detail),
                ),
            )

    # -- run queries --------------------------------------------------
    def runs(self) -> list[RunInfo]:
        return [
            self._run_info(row)
            for row in self.connection.execute(
                "SELECT * FROM ingest_run ORDER BY sequence"
            )
        ]

    def get_run(self, run_id: str) -> RunInfo | None:
        row = self.connection.execute(
            "SELECT * FROM ingest_run WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._run_info(row) if row else None

    def latest_run(self) -> RunInfo | None:
        row = self.connection.execute(
            "SELECT * FROM ingest_run ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        return self._run_info(row) if row else None

    def previous_run(self, run_id: str) -> RunInfo | None:
        current = self.get_run(run_id)
        if current is None:
            raise LakeError(f"未知 run_id: {run_id}")
        row = self.connection.execute(
            "SELECT * FROM ingest_run WHERE sequence < ? ORDER BY sequence DESC LIMIT 1",
            (current.sequence,),
        ).fetchone()
        return self._run_info(row) if row else None

    def resolve_run(self, selector: str | None) -> RunInfo:
        if selector in (None, "", "latest"):
            run = self.latest_run()
        elif selector == "previous":
            latest = self.latest_run()
            if latest is None:
                raise LakeError("库中还没有任何快照")
            run = self.previous_run(latest.run_id)
        elif selector.isdigit():
            row = self.connection.execute(
                "SELECT * FROM ingest_run WHERE sequence = ?", (int(selector),)
            ).fetchone()
            run = self._run_info(row) if row else None
        else:
            run = self.get_run(selector)
        if run is None:
            raise LakeError(f"找不到快照: {selector!r}")
        return run

    def _run_info(self, row: sqlite3.Row) -> RunInfo:
        return RunInfo(
            run_id=row["run_id"],
            sequence=row["sequence"],
            snapshot_name=row["snapshot_name"],
            source_path=row["source_path"],
            content_hash=row["content_hash"],
            received_at=row["received_at"],
            ingested_at=row["ingested_at"],
            counts=json.loads(row["counts_json"]),
        )

    # -- coverage queries ---------------------------------------------
    def coverage_rows(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM coverage_record WHERE run_id = ?"
                " ORDER BY work_id, used_at, use_id",
                (run_id,),
            )
        )

    def coverage_for_use(self, run_id: str, use_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM coverage_record WHERE run_id = ? AND use_id = ?",
            (run_id, use_id),
        ).fetchone()

    def coverage_for_version(
        self, run_id: str, version_id: str
    ) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM coverage_record WHERE run_id = ? AND version_id = ?"
                " ORDER BY used_at, use_id",
                (run_id, version_id),
            )
        )

    def use_history(self, use_id: str) -> list[sqlite3.Row]:
        """Coverage of one use across every run, oldest first."""

        return list(
            self.connection.execute(
                "SELECT c.*, r.sequence, r.received_at AS run_received_at, r.snapshot_name"
                " FROM coverage_record c JOIN ingest_run r ON r.run_id = c.run_id"
                " WHERE c.use_id = ? ORDER BY r.sequence",
                (use_id,),
            )
        )

    def review_rows(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM review_item WHERE run_id = ?"
                " ORDER BY priority, use_id",
                (run_id,),
            )
        )

    def change_rows(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM coverage_change WHERE to_run_id = ?"
                " ORDER BY use_id, kind",
                (run_id,),
            )
        )

    def issue_rows(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM issue_record WHERE run_id = ?"
                " ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,"
                " source_file, line_number",
                (run_id,),
            )
        )

    def grant_family_rows(self, run_id: str, grant_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM grant_record WHERE run_id = ? AND lower(grant_id) = lower(?)"
                " ORDER BY received_at, record_id",
                (run_id, grant_id),
            )
        )

    def grants_for_work(self, run_id: str, work_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM grant_record WHERE run_id = ? AND work_id = ?"
                " ORDER BY grant_id, received_at",
                (run_id, work_id),
            )
        )

    def uses_for_work(self, run_id: str, work_id: str) -> list[sqlite3.Row]:
        """Uses whose coverage resolved to ``work_id``.

        Derivative versions inherit their work through the parent chain, so the
        version's own row is not enough; the coverage result already carries the
        resolved work.
        """

        return list(
            self.connection.execute(
                "SELECT u.* FROM use_record u"
                " JOIN coverage_record c ON c.run_id = u.run_id AND c.use_id = u.use_id"
                " WHERE u.run_id = ? AND c.work_id = ?"
                " ORDER BY u.used_at, u.use_id",
                (run_id, work_id),
            )
        )

    def source_record(
        self, run_id: str, source_file: str, line_number: int
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM source_record WHERE run_id = ? AND source_file = ?"
            " AND line_number = ?",
            (run_id, source_file, line_number),
        ).fetchone()


def snapshot_content_hash(snapshot: Snapshot) -> str:
    """Hash the three raw source files' bytes, in a fixed order."""

    parts: list[str] = []
    for filename in ("grants.csv", "content_versions.csv", "uses.csv"):
        payload = (snapshot.path / filename).read_bytes()
        parts.append(filename)
        parts.append(str(len(payload)))
        parts.append(sha256_text(payload.decode("utf-8")))
    return sha256_text("\n".join(parts))


def ingest_into_lake(
    lake_path: str | Path,
    snapshot_path: str | Path,
    **kwargs: object,
) -> tuple[RunInfo, SnapshotModel, list[Coverage]]:
    """Convenience helper used by the CLI and tests."""

    with Lake(lake_path) as lake:
        return lake.ingest(snapshot_path, **kwargs)  # type: ignore[arg-type]
