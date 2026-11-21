from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rights_facts.coverage import (  # noqa: E402
    COVERED,
    NOT_COVERED,
    UNDER_DETERMINATION,
    UNRESOLVED,
    evaluate_snapshot,
)
from rights_facts.model import SnapshotModel  # noqa: E402
from rights_facts.sources import SourceError, load_snapshot  # noqa: E402
from rights_facts.store import Lake, LakeError  # noqa: E402

SCENARIO = ROOT / "samples" / "scenario"


GRANT_HEADER = [
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
]
VERSION_HEADER = [
    "record_id",
    "version_id",
    "work_id",
    "parent_version",
    "created_at",
    "edit_kind",
    "channel_ref",
    "source_ref",
    "edit_note",
]
USE_HEADER = [
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
]


def write_snapshot(
    root: Path,
    *,
    grants: list[dict[str, str]],
    versions: list[dict[str, str]],
    uses: list[dict[str, str]],
) -> Path:
    """Write a snapshot directory from partial dicts with sensible defaults."""

    root.mkdir(parents=True, exist_ok=True)
    _write(root / "grants.csv", GRANT_HEADER, grants)
    _write(root / "content_versions.csv", VERSION_HEADER, versions)
    _write(root / "uses.csv", USE_HEADER, uses)
    return root


def _write(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    defaults = {
        "channel": "short-video",
        "territory": "CN",
        "coverage_scope": "all-versions",
        "parent_version": "",
        "supersedes_grant_id": "",
        "parent_claim_id": "",
        "claim_scope_note": "",
        "edit_note": "",
        "edit_kind": "original",
        "channel_ref": "short-video",
        "source_ref": "LOG",
        "batch_ref": "BATCH",
        "settlement_state": "unsettled",
        "claimed": "",
        "grant_state": "active",
    }
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=header)
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            filled = {key: row.get(key, defaults.get(key, "")) for key in header}
            filled.setdefault("record_id", f"rec-{index:02d}")
            writer.writerow(filled)


def default_version(
    version_id: str = "V-1",
    work_id: str = "WORK-1",
    *,
    parent: str = "",
    created_at: str = "2026-01-01T00:00:00Z",
    kind: str = "original",
) -> dict[str, str]:
    return {
        "record_id": f"cv-{version_id}",
        "version_id": version_id,
        "work_id": work_id,
        "parent_version": parent,
        "created_at": created_at,
        "edit_kind": kind,
    }


def default_use(
    use_id: str = "U-1",
    version_id: str = "V-1",
    *,
    used_at: str = "2026-02-01T00:00:00Z",
    reported_at: str = "2026-02-02T00:00:00Z",
    units: str = "1",
    channel: str = "short-video",
    territory: str = "CN",
    settlement: str = "unsettled",
) -> dict[str, str]:
    return {
        "record_id": f"use-{use_id}",
        "use_id": use_id,
        "version_id": version_id,
        "used_at": used_at,
        "reported_at": reported_at,
        "usage_units": units,
        "channel": channel,
        "territory": territory,
        "settlement_state": settlement,
    }


def default_grant(
    grant_id: str = "G-1",
    work_id: str = "WORK-1",
    *,
    record_id: str = "gr-01",
    state: str = "active",
    channel: str = "short-video",
    territory: str = "CN",
    valid_from: str = "2026-01-01",
    valid_to: str = "2026-12-31",
    received_at: str = "2026-01-01T00:00:00Z",
    scope: str = "all-versions",
    supersedes: str = "",
) -> dict[str, str]:
    return {
        "record_id": record_id,
        "grant_id": grant_id,
        "claimant_id": "ORG-1",
        "work_id": work_id,
        "channel": channel,
        "territory": territory,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "grant_state": state,
        "received_at": received_at,
        "supersedes_grant_id": supersedes,
        "coverage_scope": scope,
    }


class SnapshotCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def evaluate(self, name: str = "snap"):
        snapshot = load_snapshot(self.root, name=name)
        model = SnapshotModel.build(snapshot)
        return model, {c.use.use_id: c for c in evaluate_snapshot(model)}


class ScenarioEndToEndTests(unittest.TestCase):
    """The three shipped waves exercise the intended change narrative."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lake_path = Path(cls.tmp.name) / "lake.sqlite"
        cls.runs = []
        with Lake(cls.lake_path) as lake:
            for wave in ("wave-a", "wave-b", "wave-c"):
                info, _model, coverages = lake.ingest(SCENARIO / wave, snapshot_name=wave)
                cls.runs.append(info)
            cls.latest = cls.runs[-1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def outcome(self, use_id: str, run_index: int = -1) -> str:
        run = self.runs[run_index]
        with Lake(self.lake_path) as lake:
            row = lake.coverage_for_use(run.run_id, use_id)
        self.assertIsNotNone(row, f"{use_id} missing from {run.snapshot_name}")
        return row["outcome"]

    def test_runs_are_appended_in_order(self) -> None:
        self.assertEqual([run.sequence for run in self.runs], [1, 2, 3])
        self.assertEqual([run.snapshot_name for run in self.runs], ["wave-a", "wave-b", "wave-c"])

    def test_withdrawal_turns_settled_use_into_review(self) -> None:
        self.assertEqual(self.outcome("U-63", 0), COVERED)
        self.assertEqual(self.outcome("U-63"), UNDER_DETERMINATION)
        with Lake(self.lake_path) as lake:
            row = lake.coverage_for_use(self.latest.run_id, "U-63")
        self.assertEqual(row["settlement_state"], "settled")
        self.assertIn("grant_withdrawn", json.loads(row["reasons_json"]))

    def test_regional_withdrawal_does_not_erase_other_region(self) -> None:
        # Wave B withdraws G-30 for HK only; CN use stays covered.
        self.assertEqual(self.outcome("U-67", 1), COVERED)
        self.assertEqual(self.outcome("U-68", 1), UNDER_DETERMINATION)

    def test_later_supplement_retroactively_covers_earlier_use(self) -> None:
        self.assertEqual(self.outcome("U-66", 0), NOT_COVERED)
        self.assertEqual(self.outcome("U-66"), COVERED)

    def test_dispute_resolution_and_claim_to_grant(self) -> None:
        self.assertEqual(self.outcome("U-74", 0), UNDER_DETERMINATION)
        self.assertEqual(self.outcome("U-74"), COVERED)
        self.assertEqual(self.outcome("U-64", 0), UNDER_DETERMINATION)
        self.assertEqual(self.outcome("U-64"), COVERED)

    def test_missing_parent_recovered_in_later_wave(self) -> None:
        self.assertEqual(self.outcome("U-69", 0), UNDER_DETERMINATION)
        self.assertEqual(self.outcome("U-69"), COVERED)

    def test_unrelated_version_stays_unresolved(self) -> None:
        self.assertEqual(self.outcome("U-72"), UNRESOLVED)

    def test_changes_are_recorded_between_runs(self) -> None:
        with Lake(self.lake_path) as lake:
            changes = {
                row["use_id"]: row
                for row in lake.change_rows(self.latest.run_id)
            }
        self.assertEqual(changes["U-67"]["kind"], "outcome_changed")
        self.assertEqual(changes["U-67"]["from_outcome"], COVERED)
        self.assertEqual(changes["U-67"]["to_outcome"], UNDER_DETERMINATION)
        self.assertEqual(changes["U-69"]["to_outcome"], COVERED)
        self.assertEqual(changes["U-77"]["kind"], "new_use")

    def test_evidence_points_back_to_raw_source_rows(self) -> None:
        with Lake(self.lake_path) as lake:
            row = lake.coverage_for_use(self.latest.run_id, "U-63")
            evidence = json.loads(row["evidence_json"])
            origins = [item["provenance"] for item in evidence if "provenance" in item]
            self.assertTrue(origins)
            for origin in origins:
                raw = lake.source_record(
                    self.latest.run_id,
                    origin["source_file"],
                    origin["line_number"],
                )
                self.assertIsNotNone(raw)
                payload = json.loads(raw["raw_json"])
                self.assertEqual(payload["record_id"], origin["record_id"])

    def test_review_queue_puts_settled_withdrawn_use_first(self) -> None:
        with Lake(self.lake_path) as lake:
            rows = {row["use_id"]: row for row in lake.review_rows(self.latest.run_id)}
        self.assertEqual(rows["U-63"]["priority"], 1)
        self.assertIn("settlement_already_recorded", rows["U-63"]["reason_codes_json"])
        self.assertNotIn("U-71", rows)  # covered, settled, unchanged

    def test_work_report_follows_derivative_lineage(self) -> None:
        with Lake(self.lake_path) as lake:
            work_two = {
                row["use_id"] for row in lake.uses_for_work(self.latest.run_id, "WORK-2")
            }
        # U-61 uses V-21, a derivative of V-20 (WORK-2); its own row says WORK-2,
        # so include a cross-work derivative elsewhere to prove resolution.
        self.assertIn("U-61", work_two)
        self.assertIn("U-60", work_two)
        self.assertNotIn("U-63", work_two)

    def test_duplicate_use_rows_are_flagged_not_silently_summed(self) -> None:
        with Lake(self.lake_path) as lake:
            wave_a = lake.coverage_for_use(self.runs[0].run_id, "U-70")
        reasons = json.loads(wave_a["reasons_json"])
        self.assertIn("duplicate_use_id", reasons)
        self.assertEqual(wave_a["usage_units"], 2)

    def test_cli_reports_json_for_latest_run(self) -> None:
        env = {"PYTHONPATH": str(ROOT / "src")}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "rights_facts",
                "--lake",
                str(self.lake_path),
                "coverage",
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["run"]["sequence"], 3)
        self.assertEqual(len(payload["coverage"]), 19)


class CoverageRuleTests(SnapshotCase):
    def test_covered_original_use(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[default_version()],
            uses=[default_use()],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, COVERED)

    def test_derivative_not_in_original_only_grant(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant(scope="original-only")],
            versions=[
                default_version("V-1"),
                default_version("V-2", parent="V-1", kind="excerpt"),
            ],
            uses=[default_use(version_id="V-2")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, NOT_COVERED)
        codes = [item["code"] for item in results["U-1"].dict()["evidence"]]
        self.assertIn("grant_out_of_scope", codes)

    def test_derivative_covered_when_scope_includes_derivatives(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[
                default_version("V-1"),
                default_version("V-2", parent="V-1", kind="excerpt"),
            ],
            uses=[default_use(version_id="V-2")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, COVERED)

    def test_date_only_end_includes_whole_last_day(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant(valid_from="2026-01-01", valid_to="2026-01-31")],
            versions=[default_version()],
            uses=[default_use(used_at="2026-01-31T23:59:00Z")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, COVERED)

    def test_use_after_window_is_not_covered(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant(valid_to="2026-01-31")],
            versions=[default_version()],
            uses=[default_use(used_at="2026-02-01T00:00:00Z")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, NOT_COVERED)

    def test_territory_and_channel_must_match(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant(territory="HK", channel="download")],
            versions=[default_version()],
            uses=[default_use(territory="CN", channel="short-video")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, NOT_COVERED)

    def test_later_withdrawal_supersedes_earlier_active(self) -> None:
        write_snapshot(
            self.root,
            grants=[
                default_grant(record_id="gr-01", received_at="2026-01-01T00:00:00Z"),
                default_grant(
                    record_id="gr-02",
                    state="withdrawn",
                    received_at="2026-03-01T00:00:00Z",
                ),
            ],
            versions=[default_version()],
            uses=[default_use(settlement="settled")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, UNDER_DETERMINATION)
        self.assertIn("grant_withdrawn", results["U-1"].reasons)

    def test_withdrawal_scoped_to_one_region(self) -> None:
        write_snapshot(
            self.root,
            grants=[
                default_grant(
                    grant_id="G-1",
                    record_id="gr-01",
                    territory="*",
                    received_at="2026-01-01T00:00:00Z",
                ),
                default_grant(
                    grant_id="G-2",
                    record_id="gr-02",
                    territory="HK",
                    state="withdrawn",
                    received_at="2026-03-01T00:00:00Z",
                    supersedes="G-1",
                ),
            ],
            versions=[default_version()],
            uses=[
                default_use("U-CN", territory="CN"),
                default_use("U-HK", territory="HK"),
            ],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-CN"].outcome, COVERED)
        self.assertEqual(results["U-HK"].outcome, UNDER_DETERMINATION)

    def test_dispute_blocks_verdict(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant(state="disputed")],
            versions=[default_version()],
            uses=[default_use()],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, UNDER_DETERMINATION)

    def test_claim_without_boundary_is_not_a_licence(self) -> None:
        grant = default_grant(state="claimed")
        grant["parent_claim_id"] = "CLAIM-1"
        write_snapshot(
            self.root,
            grants=[grant],
            versions=[default_version()],
            uses=[default_use()],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, UNDER_DETERMINATION)
        self.assertIn("claim_without_grant", results["U-1"].reasons)

    def test_no_grant_is_never_silently_covered(self) -> None:
        write_snapshot(
            self.root,
            grants=[],
            versions=[default_version()],
            uses=[default_use()],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, UNDER_DETERMINATION)
        self.assertIn("no_evidence", results["U-1"].reasons)

    def test_missing_version_is_unresolved(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[default_version()],
            uses=[default_use(version_id="V-404")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, UNRESOLVED)

    def test_incomplete_lineage_is_not_attributed_upward(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[default_version("V-2", parent="V-1", kind="re-edit")],
            uses=[default_use(version_id="V-2")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, UNDER_DETERMINATION)
        self.assertIn("lineage_incomplete", results["U-1"].reasons)

    def test_cross_work_lineage_conflict_is_unresolved(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[
                default_version("V-1", "WORK-1"),
                default_version("V-2", "WORK-2", parent="V-1", kind="re-edit"),
            ],
            uses=[default_use(version_id="V-2")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, UNRESOLVED)
        self.assertIn("work_lineage_conflict", results["U-1"].reasons)

    def test_lineage_cycle_is_reported(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[
                default_version("V-1", parent="V-2", kind="re-edit"),
                default_version("V-2", parent="V-1", kind="re-edit"),
            ],
            uses=[default_use(version_id="V-1")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, UNDER_DETERMINATION)
        self.assertEqual(results["U-1"].lineage.status, "cycle")

    def test_duplicate_rows_downgrade_billable_certainty(self) -> None:
        first = default_use(settlement="unsettled")
        second = dict(first, record_id="use-dup", settlement_state="settled")
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[default_version()],
            uses=[first, second],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, UNDER_DETERMINATION)
        self.assertIn("duplicate_use_id", results["U-1"].reasons)

    def test_cross_work_derivative_conflict_surfaces_unresolved(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant(work_id="WORK-1")],
            versions=[
                default_version("V-1", "WORK-1"),
                # A re-edit row whose own work_id disagrees with its parent.
                default_version("V-2", "WORK-9", parent="V-1", kind="re-edit"),
            ],
            uses=[default_use(version_id="V-2")],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].lineage.status, "conflict")
        self.assertEqual(results["U-1"].outcome, UNRESOLVED)

    def test_late_report_is_flagged_but_coverage_holds(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[default_version()],
            uses=[
                default_use(
                    used_at="2026-01-05T00:00:00Z",
                    reported_at="2026-03-05T00:00:00Z",
                )
            ],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, COVERED)
        self.assertIn("late_report", results["U-1"].reasons)

    def test_latest_record_wins_even_when_earlier_is_active(self) -> None:
        write_snapshot(
            self.root,
            grants=[
                default_grant(
                    record_id="gr-01",
                    state="active",
                    received_at="2026-01-01T00:00:00Z",
                ),
                default_grant(
                    record_id="gr-02",
                    state="withdrawn",
                    received_at="2026-02-01T00:00:00Z",
                ),
                default_grant(
                    record_id="gr-03",
                    state="active",
                    received_at="2026-03-01T00:00:00Z",
                ),
            ],
            versions=[default_version()],
            uses=[default_use()],
        )
        _model, results = self.evaluate()
        self.assertEqual(results["U-1"].outcome, COVERED)
        self.assertEqual(results["U-1"].matched_grant.record_id, "gr-03")


class SourceValidationTests(SnapshotCase):
    def test_missing_file_is_structural_error(self) -> None:
        write_snapshot(
            self.root,
            grants=[],
            versions=[default_version()],
            uses=[default_use()],
        )
        (self.root / "uses.csv").unlink()
        with self.assertRaises(SourceError):
            load_snapshot(self.root)

    def test_header_mismatch_is_structural_error(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[default_version()],
            uses=[default_use()],
        )
        path = self.root / "grants.csv"
        path.write_text("record_id,grant_id\nx,y\n", encoding="utf-8")
        with self.assertRaises(SourceError):
            load_snapshot(self.root)

    def test_naive_timestamp_is_rejected(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[default_version(created_at="2026-01-01T00:00:00")],
            uses=[default_use()],
        )
        snapshot = load_snapshot(self.root)
        codes = {issue.code for issue in snapshot.issues}
        self.assertIn("bad_created_at", codes)

    def test_duplicate_record_id_is_reported_and_first_kept(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant(record_id="dup"), default_grant(record_id="dup")],
            versions=[default_version()],
            uses=[default_use()],
        )
        snapshot = load_snapshot(self.root)
        self.assertEqual(len(snapshot.grants), 1)
        self.assertIn("duplicate_record_id", {issue.code for issue in snapshot.issues})

    def test_unknown_state_and_scope_are_reported(self) -> None:
        bad_scope = default_grant(scope="galaxy")
        write_snapshot(
            self.root,
            grants=[bad_scope],
            versions=[default_version()],
            uses=[default_use()],
        )
        snapshot = load_snapshot(self.root)
        model = SnapshotModel.build(snapshot)
        codes = {issue.code for issue in model.issues}
        self.assertIn("unusable_grant", codes)

    def test_dangling_supersedes_is_reported(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant(supersedes="G-404")],
            versions=[default_version()],
            uses=[default_use()],
        )
        snapshot = load_snapshot(self.root)
        model = SnapshotModel.build(snapshot)
        self.assertIn("dangling_supersedes", {issue.code for issue in model.issues})


class LakeTests(SnapshotCase):
    def _write_simple(self, *, state: str = "active", grant_received: str = "2026-01-01T00:00:00Z") -> None:
        write_snapshot(
            self.root,
            grants=[default_grant(state=state, received_at=grant_received)],
            versions=[default_version()],
            uses=[default_use()],
        )

    def test_duplicate_ingest_is_rejected_unless_explicit(self) -> None:
        self._write_simple()
        lake_path = Path(self.tmp.name) / "lake.sqlite"
        with Lake(lake_path) as lake:
            lake.ingest(self.root, snapshot_name="one")
            with self.assertRaises(LakeError):
                lake.ingest(self.root, snapshot_name="one")
            info, _model, _coverage = lake.ingest(
                self.root, snapshot_name="one", allow_duplicate=True
            )
        self.assertEqual(info.sequence, 2)

    def test_previous_run_selector_and_sequence_selector(self) -> None:
        self._write_simple()
        lake_path = Path(self.tmp.name) / "lake.sqlite"
        with Lake(lake_path) as lake:
            lake.ingest(self.root, snapshot_name="one")
            self._write_simple(state="withdrawn", grant_received="2026-02-01T00:00:00Z")
            lake.ingest(self.root, snapshot_name="two")
            self.assertEqual(lake.resolve_run("latest").sequence, 2)
            self.assertEqual(lake.resolve_run("previous").sequence, 1)
            self.assertEqual(lake.resolve_run("1").sequence, 1)

    def test_settlement_state_change_is_tracked(self) -> None:
        write_snapshot(
            self.root,
            grants=[default_grant()],
            versions=[default_version()],
            uses=[default_use(settlement="unsettled")],
        )
        lake_path = Path(self.tmp.name) / "lake.sqlite"
        with Lake(lake_path) as lake:
            lake.ingest(self.root, snapshot_name="one")
            write_snapshot(
                self.root,
                grants=[default_grant()],
                versions=[default_version()],
                uses=[default_use(settlement="settled", reported_at="2026-02-05T00:00:00Z")],
            )
            info, _m, _c = lake.ingest(self.root, snapshot_name="two")
            changes = {row["use_id"]: row for row in lake.change_rows(info.run_id)}
        self.assertEqual(changes["U-1"]["kind"], "settlement_changed")


if __name__ == "__main__":
    unittest.main()
