"""Human- and machine-readable reports built from lake queries.

Every report carries the run it describes (snapshot name, sequence, received
time, content hash) so a reader can tell which version of the facts they are
looking at and reproduce it.
"""

from __future__ import annotations

import json
from typing import Sequence

from .store import Lake, RunInfo
from .util import canonical_json

OUTCOME_ORDER = ("covered", "not_covered", "under_determination", "unresolved")


def run_header(run: RunInfo) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "sequence": run.sequence,
        "snapshot_name": run.snapshot_name,
        "received_at": run.received_at,
        "ingested_at": run.ingested_at,
        "content_hash": run.content_hash,
        "source_path": run.source_path,
    }


def coverage_report(lake: Lake, run: RunInfo, *, as_json: bool) -> str:
    rows = lake.coverage_rows(run.run_id)
    if as_json:
        payload = {
            "run": run_header(run),
            "coverage": [_coverage_row(row) for row in rows],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return _coverage_table(run, rows)


def _coverage_row(row) -> dict[str, object]:
    return {
        "use_id": row["use_id"],
        "version_id": row["version_id"],
        "work_id": row["work_id"],
        "outcome": row["outcome"],
        "matched_grant_id": row["matched_grant_id"],
        "lineage_status": row["lineage_status"],
        "reasons": json.loads(row["reasons_json"]),
        "usage_units": row["usage_units"],
        "channel": row["channel"],
        "territory": row["territory"],
        "used_at": row["used_at"],
        "settlement_state": row["settlement_state"],
        "late_report": bool(row["late_report"]),
        "summary": row["summary"],
    }


def _coverage_table(run: RunInfo, rows: Sequence) -> str:
    header = (
        f"快照 #{run.sequence} {run.snapshot_name}（接收于 {run.received_at}）"
        f"  内容哈希 {run.content_hash[:12]}"
    )
    counts = {outcome: 0 for outcome in OUTCOME_ORDER}
    lines = [header, ""]
    lines.append(
        f"{'use_id':<7} {'version':<7} {'work':<7} {'outcome':<20} {'grant':<6} "
        f"{'units':>5} {'ch/terr':<12} {'used_at':<20} {'settle':<9} 摘要"
    )
    lines.append("-" * 130)
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
        reasons = ", ".join(json.loads(row["reasons_json"]))
        lines.append(
            f"{row['use_id']:<7} {row['version_id']:<7} {row['work_id']:<7} "
            f"{row['outcome']:<20} {row['matched_grant_id'] or '-':<6} "
            f"{row['usage_units']:>5} {row['channel'] + '/' + row['territory']:<12} "
            f"{row['used_at']:<20} {row['settlement_state']:<9} {row['summary']}"
        )
        if reasons:
            lines.append(f"{'':<7} 依据: {reasons}")
    lines.append("")
    summary = "  ".join(f"{key}={value}" for key, value in counts.items() if value)
    lines.append(f"合计 {len(rows)} 条使用：{summary}")
    return "\n".join(lines)


def review_report(lake: Lake, run: RunInfo, *, as_json: bool) -> str:
    rows = lake.review_rows(run.run_id)
    if as_json:
        return json.dumps(
            {
                "run": run_header(run),
                "review": [
                    {
                        "use_id": row["use_id"],
                        "priority": row["priority"],
                        "reason_codes": json.loads(row["reason_codes_json"]),
                        "settlement_state": row["state"],
                        "detail": json.loads(row["detail_json"]),
                    }
                    for row in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    lines = [
        f"复核队列 快照 #{run.sequence} {run.snapshot_name}（接收于 {run.received_at}）",
        "",
    ]
    if not rows:
        lines.append("没有需要复核的使用记录。")
        return "\n".join(lines)
    current_priority = None
    for row in rows:
        if row["priority"] != current_priority:
            current_priority = row["priority"]
            lines.append(f"[优先级 {current_priority}]")
        detail = json.loads(row["detail_json"])
        codes = ", ".join(json.loads(row["reason_codes_json"]))
        lines.append(
            f"  {row['use_id']}  {detail['outcome']:<20} "
            f"结算={row['state']:<9} {detail['channel']}/{detail['territory']} "
            f"units={detail['usage_units']}  {codes}"
        )
        lines.append(f"      {detail['summary']}")
    return "\n".join(lines)


def changes_report(lake: Lake, run: RunInfo, *, as_json: bool) -> str:
    rows = lake.change_rows(run.run_id)
    previous = lake.previous_run(run.run_id)
    if as_json:
        return json.dumps(
            {
                "run": run_header(run),
                "previous_run": run_header(previous) if previous else None,
                "changes": [
                    {
                        "use_id": row["use_id"],
                        "kind": row["kind"],
                        "from_outcome": row["from_outcome"],
                        "to_outcome": row["to_outcome"],
                        "detail": json.loads(row["detail_json"]),
                    }
                    for row in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    lines = [
        f"相对上次快照的变化  #{run.sequence} {run.snapshot_name}"
        + (f"（上次 #{previous.sequence} {previous.snapshot_name}）" if previous else "")
    ]
    if previous is None:
        lines.append("")
        lines.append("这是首次入库，没有可比较的上一快照。")
        return "\n".join(lines)
    if not rows:
        lines.append("")
        lines.append("覆盖结论与结算状态均无变化。")
        return "\n".join(lines)
    lines.append("")
    for row in rows:
        detail = json.loads(row["detail_json"])
        lines.append(
            f"  {row['use_id']:<8} {row['kind']:<20} "
            f"{row['from_outcome'] or '-'} -> {row['to_outcome'] or '-'}  "
            f"{canonical_json(detail)}"
        )
    return "\n".join(lines)


def use_report(lake: Lake, use_id: str, *, as_json: bool) -> str:
    history = lake.use_history(use_id)
    if not history:
        raise KeyError(use_id)
    latest = history[-1]
    sources = []
    evidence = json.loads(latest["evidence_json"])
    for item in evidence:
        origin = item.get("provenance", {})
        if not origin:
            continue
        raw = lake.source_record(
            latest["run_id"], origin.get("source_file", ""), origin.get("line_number", 0)
        )
        sources.append(
            {
                "role": item["code"],
                "stance": item["stance"],
                "message": item["message"],
                "provenance": origin,
                "raw": json.loads(raw["raw_json"]) if raw else None,
            }
        )
    payload = {
        "use_id": use_id,
        "latest_run": {
            "run_id": latest["run_id"],
            "sequence": latest["sequence"],
            "snapshot_name": latest["snapshot_name"],
            "received_at": latest["run_received_at"],
        },
        "outcome": latest["outcome"],
        "summary": latest["summary"],
        "settlement_state": latest["settlement_state"],
        "evidence": sources,
        "history": [
            {
                "sequence": row["sequence"],
                "snapshot_name": row["snapshot_name"],
                "run_received_at": row["run_received_at"],
                "outcome": row["outcome"],
                "matched_grant_id": row["matched_grant_id"],
                "settlement_state": row["settlement_state"],
            }
            for row in history
        ],
    }
    if as_json:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    lines = [
        f"使用 {use_id} 的覆盖与凭证",
        f"最新快照 #{latest['sequence']} {latest['snapshot_name']}"
        f"（接收于 {latest['run_received_at']}）",
        f"结论：{latest['outcome']}  结算状态：{latest['settlement_state']}",
        latest["summary"],
        "",
        "凭证链：",
    ]
    for item in sources:
        origin = item["provenance"]
        lines.append(
            f"  [{item['stance']}] {item['message']}\n"
            f"      出处 {origin.get('source_file')}:{origin.get('line_number')}"
            f" record={origin.get('record_id')}"
            + (f" 生效于 {item['effective_at']}" if item.get("effective_at") else "")
        )
    lines.append("")
    lines.append("跨快照历史：")
    for row in history:
        lines.append(
            f"  #{row['sequence']:<2} {row['snapshot_name']:<10} "
            f"{row['outcome']:<20} grant={row['matched_grant_id'] or '-'} "
            f"settle={row['settlement_state']}"
        )
    return "\n".join(lines)


def work_report(lake: Lake, run: RunInfo, work_id: str, *, as_json: bool) -> str:
    grants = lake.grants_for_work(run.run_id, work_id)
    uses = lake.uses_for_work(run.run_id, work_id)
    coverage = {row["use_id"]: row for row in lake.coverage_rows(run.run_id)}
    # Show the current state of each claim family separately from its history,
    # so the newest observation is never mistaken for a second authorization.
    families: dict[str, list] = {}
    for row in grants:
        families.setdefault(row["grant_id"].casefold(), []).append(row)
    payload = {
        "run": run_header(run),
        "work_id": work_id,
        "claims": [
            {
                "grant_id": records[-1]["grant_id"],
                "current": _grant_row(max(records, key=lambda r: (r["received_at"], r["record_id"]))),
                "history": [_grant_row(record) for record in records],
            }
            for records in families.values()
        ],
        "uses": [
            _coverage_row(coverage[row["use_id"]])
            for row in uses
            if row["use_id"] in coverage
        ],
    }
    if as_json:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    lines = [f"作品 {work_id} 的权利主张、当前状态与实际使用", "", "授权/主张："]
    for records in families.values():
        latest = max(records, key=lambda r: (r["received_at"], r["record_id"]))
        lines.append(
            f"  {latest['grant_id']:<7} 当前 {latest['grant_state']:<10} "
            f"{latest['channel']:<12} {latest['territory']:<4} "
            f"{latest['valid_from_raw']}～{latest['valid_to_raw']} "
            f"{latest['coverage_scope']:<16} "
            f"{latest['record_id']} ({latest['source_file']}:{latest['line_number']})"
        )
        if latest["supersedes_grant_id"]:
            lines.append(f"          声明替代 {latest['supersedes_grant_id']}")
        if latest["note"]:
            lines.append(f"          备注：{latest['note']}")
        if len(records) > 1:
            trail = "；".join(
                f"{record['grant_state']}@{record['received_at']} ({record['record_id']})"
                for record in sorted(records, key=lambda r: (r["received_at"], r["record_id"]))
            )
            lines.append(f"          观察历史：{trail}")
    lines.append("")
    lines.append("实际使用：")
    for row in uses:
        cov = coverage.get(row["use_id"])
        outcome = cov["outcome"] if cov else "（无覆盖记录）"
        lines.append(
            f"  {row['use_id']:<7} {row['version_id']:<7} {row['channel']}/{row['territory']:<4} "
            f"{row['used_at']}  units={row['usage_units']}  {outcome}  {row['settlement_state']}"
        )
    return "\n".join(lines)


def _grant_row(row) -> dict[str, object]:
    return {
        "record_id": row["record_id"],
        "grant_id": row["grant_id"],
        "claimant_id": row["claimant_id"],
        "work_id": row["work_id"],
        "channel": row["channel"],
        "territory": row["territory"],
        "valid_from": row["valid_from_raw"],
        "valid_to": row["valid_to_raw"],
        "grant_state": row["grant_state"],
        "received_at": row["received_at"],
        "supersedes_grant_id": row["supersedes_grant_id"],
        "parent_claim_id": row["parent_claim_id"],
        "coverage_scope": row["coverage_scope"],
        "claim_scope_note": row["note"],
        "provenance": {"source_file": row["source_file"], "line_number": row["line_number"]},
    }


def runs_report(lake: Lake, *, as_json: bool) -> str:
    runs = lake.runs()
    if as_json:
        return json.dumps(
            {"runs": [run_header(run) | {"counts": dict(run.counts)} for run in runs]},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    lines = ["已入库快照："]
    for run in runs:
        counts = dict(run.counts)
        lines.append(
            f"  #{run.sequence:<2} {run.snapshot_name:<10} 接收 {run.received_at}  "
            f"使用 {counts.get('canonical_uses', '-')} 条  "
            f"问题 {counts.get('issue_errors', '-')} 错误/{counts.get('issue_warnings', '-')} 警告  "
            f"hash {run.content_hash[:12]}"
        )
    return "\n".join(lines)


def issues_report(lake: Lake, run: RunInfo, *, as_json: bool) -> str:
    rows = lake.issue_rows(run.run_id)
    if as_json:
        return json.dumps(
            {
                "run": run_header(run),
                "issues": [
                    {
                        "code": row["code"],
                        "severity": row["severity"],
                        "source_file": row["source_file"],
                        "record_id": row["record_id"],
                        "line_number": row["line_number"],
                        "message": row["message"],
                    }
                    for row in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    lines = [f"数据问题 快照 #{run.sequence} {run.snapshot_name}"]
    if not rows:
        lines.append("")
        lines.append("没有数据完整性问题。")
    for row in rows:
        lines.append(
            f"  [{row['severity']}/{row['code']}] {row['source_file']}:{row['line_number']} "
            f"{row['record_id']}  {row['message']}"
        )
    return "\n".join(lines)
