"""Deterministic coverage evaluation for one use against one snapshot.

Nothing here consults global state.  Each evaluation returns both the outcome
and the evidence chain that produced it, so a data user can answer "which
credential, and when did it take effect".

Evaluation order and rules:

1. **Version lineage** - the use must map to a version, and the version's parent
   chain must reach an original version without conflicts.  A missing parent
   leaves the derivative's work unresolved; a chain that crosses works is a
   conflict.  Neither is silently attributed to a grant.
2. **Structural applicability** - work, channel, territory, validity window and
   version scope (original/derivative/all).  These checks are independent of
   the grant's state.
3. **State precedence** - among structurally applicable records, the one with
   the latest ``received_at`` governs.  This is what lets a later withdrawal
   affect only the region or channel it names, and lets a later active record
   resolve a dispute.
4. **Settlement integrity** - duplicate rows and late reports change billable
   units; they are surfaced but never invented into a verdict.

Missing evidence never becomes a licence and never becomes infringement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .model import Grant, Lineage, SnapshotModel, Use, id_key
from .util import format_instant

SUPPORTING = "supporting"
AGAINST = "against"
CONTEXT = "context"

COVERED = "covered"
NOT_COVERED = "not_covered"
UNDER_DETERMINATION = "under_determination"
UNRESOLVED = "unresolved"

OUTCOME_LABELS = {
    COVERED: "落在当前有效授权内",
    NOT_COVERED: "未找到覆盖该使用的授权",
    UNDER_DETERMINATION: "证据不足或状态待确认，不能判定",
    UNRESOLVED: "版本或归属无法关联，待核实",
}


@dataclass(frozen=True)
class Evidence:
    code: str
    stance: str
    message: str
    record_id: str = ""
    source_file: str = ""
    line_number: int = 0
    grant_id: str = ""
    effective_at: str = ""

    def dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "stance": self.stance,
            "message": self.message,
        }
        origin = {
            "record_id": self.record_id,
            "source_file": self.source_file,
            "line_number": self.line_number,
        }
        if any(origin.values()):
            result["provenance"] = origin
        if self.grant_id:
            result["grant_id"] = self.grant_id
        if self.effective_at:
            result["effective_at"] = self.effective_at
        return result


@dataclass
class Coverage:
    use: Use
    outcome: str
    summary: str
    lineage: Lineage
    evidence: list[Evidence] = field(default_factory=list)
    matched_grant: Grant | None = None
    supporting_grants: tuple[Grant, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.use.key

    def dict(self) -> dict[str, object]:
        return {
            "use_id": self.use.use_id,
            "record_id": self.use.record_id,
            "outcome": self.outcome,
            "outcome_label": OUTCOME_LABELS[self.outcome],
            "summary": self.summary,
            "reasons": list(self.reasons),
            "lineage": self.lineage.dict(),
            "matched_grant_id": self.matched_grant.grant_id if self.matched_grant else "",
            "supporting_grant_ids": [g.grant_id for g in self.supporting_grants],
            "evidence": [item.dict() for item in self.evidence],
            "use": self.use.dict(),
        }


def _grant_evidence(grant: Grant, stance: str, message: str, code: str) -> Evidence:
    return Evidence(
        code=code,
        stance=stance,
        message=message,
        record_id=grant.record_id,
        source_file=str(grant.provenance["source_file"]),
        line_number=int(grant.provenance["line_number"]),
        grant_id=grant.grant_id,
        effective_at=format_instant(grant.received_at),
    )


def _use_evidence(use: Use, stance: str, message: str, code: str) -> Evidence:
    return Evidence(
        code=code,
        stance=stance,
        message=message,
        record_id=use.record_id,
        source_file=str(use.provenance["source_file"]),
        line_number=int(use.provenance["line_number"]),
    )


def _scope_matches(grant: Grant, lineage: Lineage) -> tuple[bool, str]:
    if grant.coverage_scope == "all-versions":
        return True, ""
    if grant.coverage_scope == "original-only":
        if lineage.is_original:
            return True, ""
        return False, "该授权只覆盖原始版本，本次使用的衍生版本不在范围内"
    if grant.coverage_scope == "derivatives-only":
        if lineage.is_original:
            return False, "该授权只覆盖衍生版本，本次使用的是原始版本"
        return True, ""
    return False, f"未知的 coverage_scope: {grant.coverage_scope}"


def _structural_mismatches(grant: Grant, use: Use, lineage: Lineage) -> list[str]:
    """Reasons a grant cannot apply to this use, independent of its state."""

    notes: list[str] = []
    if id_key(grant.work_id) != id_key(lineage.effective_work or ""):
        notes.append(f"作品不匹配（{grant.work_id}）")
    if not grant.channel_matches(use.channel):
        notes.append(f"渠道不覆盖（{grant.channel}）")
    if not grant.territory_matches(use.territory):
        notes.append(f"地区不覆盖（{grant.territory}）")
    if not grant.window_contains(use.used_at):
        notes.append(
            f"使用时间 {format_instant(use.used_at)} 不在有效期 "
            f"{grant.valid_from_raw}～{grant.valid_to_raw}"
        )
    scope_ok, scope_note = _scope_matches(grant, lineage)
    if not scope_ok:
        notes.append(scope_note)
    return notes


def _lineage_evidence(model: SnapshotModel, lineage: Lineage) -> list[Evidence]:
    steps: list[Evidence] = []
    for position, version_id in enumerate(lineage.chain, start=1):
        version = model.versions_by_key.get(id_key(version_id))
        if version is None:
            continue
        steps.append(
            Evidence(
                code="lineage_step",
                stance=CONTEXT,
                message=(
                    f"[{position}] {version.version_id}（{version.work_id}，"
                    f"{version.edit_kind}，{format_instant(version.created_at)}）"
                ),
                record_id=version.record_id,
                source_file=str(version.provenance["source_file"]),
                line_number=int(version.provenance["line_number"]),
            )
        )
    return steps


def _history_evidence(grant: Grant, model: SnapshotModel) -> list[Evidence]:
    family = model.grant_history(grant.grant_id)
    if len(family) <= 1:
        return []
    return [
        _grant_evidence(
            prior,
            CONTEXT,
            f"{prior.grant_id} 较早的观察记录：{prior.grant_state}，"
            f"渠道 {prior.channel}，地区 {prior.territory}，"
            f"有效期 {prior.valid_from_raw}～{prior.valid_to_raw}",
            "grant_history",
        )
        for prior in family
        if prior.record_id != grant.record_id
    ]


@dataclass
class _Candidate:
    grant: Grant
    mismatches: list[str]

    @property
    def applies(self) -> bool:
        return not self.mismatches


def _latest_per_family(grants: Sequence[Grant]) -> list[Grant]:
    latest: dict[str, Grant] = {}
    for grant in grants:
        current = latest.get(grant.key)
        if current is None or (grant.received_at, grant.record_id) > (
            current.received_at,
            current.record_id,
        ):
            latest[grant.key] = grant
    return sorted(latest.values(), key=lambda g: (g.grant_id.casefold(), g.record_id))


def _lineage_verdict(
    model: SnapshotModel, use: Use, lineage: Lineage, evidence: list[Evidence]
) -> Coverage | None:
    if not use.version_id or lineage.status == "missing":
        evidence.append(
            _use_evidence(
                use,
                AGAINST,
                f"使用记录引用的版本 {use.version_id or '（空）'} 不在内容版本资料中",
                "version_missing",
            )
        )
        return Coverage(
            use=use,
            outcome=UNRESOLVED,
            summary="版本无法关联，使用量暂不归入授权或未授权判定",
            lineage=lineage,
            evidence=evidence,
            reasons=("version_missing",),
        )

    if lineage.status == "conflict":
        first = lineage.evidence[0] if lineage.evidence else {}
        evidence.append(
            Evidence(
                code="lineage_conflict",
                stance=AGAINST,
                message=lineage.message,
                record_id=str(first.get("record_id", "")),
                source_file=str(first.get("source_file", "")),
                line_number=int(first.get("line_number", 0)),
            )
        )
        return Coverage(
            use=use,
            outcome=UNRESOLVED,
            summary="衍生版本引用不同作品，不能确认落在哪一项权利主张之下",
            lineage=lineage,
            evidence=evidence,
            reasons=("work_lineage_conflict",),
        )

    if lineage.status in ("incomplete", "cycle"):
        evidence.append(
            Evidence(
                code=f"lineage_{lineage.status}",
                stance=AGAINST,
                message=lineage.message,
            )
        )
        return Coverage(
            use=use,
            outcome=UNDER_DETERMINATION,
            summary="衍生版本关系不完整，尚不能判定授权覆盖",
            lineage=lineage,
            evidence=evidence,
            reasons=(f"lineage_{lineage.status}",),
        )
    return None


def evaluate_use(model: SnapshotModel, use: Use) -> Coverage:
    """Evaluate one canonical use record.  Ambiguity always yields a non-verdict."""

    lineage = model.lineage(use.version_id)
    evidence: list[Evidence] = [
        _use_evidence(
            use,
            CONTEXT,
            f"使用明细原始记录：{use.usage_units} 单位，{use.channel}/{use.territory}，"
            f"发生于 {format_instant(use.used_at)}，报告于 {format_instant(use.reported_at)}",
            "use_record",
        ),
        *_lineage_evidence(model, lineage),
    ]

    early = _lineage_verdict(model, use, lineage, evidence)
    if early is not None:
        return early

    families = _latest_per_family(model.grants_for_work(lineage.effective_work or ""))
    if not families:
        evidence.append(
            Evidence(
                code="no_claim_record",
                stance=AGAINST,
                message=f"没有与作品 {lineage.effective_work} 相关的授权或主张记录",
            )
        )
        return Coverage(
            use=use,
            outcome=UNDER_DETERMINATION,
            summary="缺少任何权利凭证，不能默认归入授权或侵权",
            lineage=lineage,
            evidence=evidence,
            reasons=("no_evidence",),
        )

    candidates = [
        _Candidate(grant, _structural_mismatches(grant, use, lineage)) for grant in families
    ]
    for candidate in candidates:
        if candidate.applies:
            continue
        evidence.append(
            _grant_evidence(
                candidate.grant,
                AGAINST,
                "；".join(candidate.mismatches),
                "grant_out_of_scope",
            )
        )

    applicable = [candidate.grant for candidate in candidates if candidate.applies]
    if not applicable:
        evidence.append(
            Evidence(
                code="no_applicable_grant",
                stance=AGAINST,
                message="没有任何记录同时满足作品、渠道、地区和期限要求",
            )
        )
        return Coverage(
            use=use,
            outcome=NOT_COVERED,
            summary="未找到覆盖该使用的有效授权；这是覆盖缺口，不是侵权结论",
            lineage=lineage,
            evidence=evidence,
            reasons=("no_applicable_grant",),
        )

    # The most recently received applicable record governs this use.  Because
    # applicability includes territory and channel, a late withdrawal that only
    # names HK does not erase a grant that still covers CN.
    governing = max(applicable, key=lambda g: (g.received_at, g.record_id))
    for grant in applicable:
        if grant.record_id != governing.record_id:
            evidence.append(
                _grant_evidence(
                    grant,
                    CONTEXT,
                    "该记录同样在结构上适用；按最近收到的适用记录确定当前依据，"
                    "此记录保留为并存凭证",
                    "grant_also_applicable",
                )
            )
            evidence.extend(_history_evidence(grant, model))

    if governing.is_claim_assertion:
        evidence.append(
            _grant_evidence(
                governing,
                AGAINST,
                "最新记录是权利主张，不含授权边界；主张本身既不能证明授权，"
                "也不能单独证明侵权",
                "claim_only",
            )
        )
        return Coverage(
            use=use,
            outcome=UNDER_DETERMINATION,
            summary="只有权利主张、没有可核对的授权边界，需补充凭证后才能判定",
            lineage=lineage,
            evidence=evidence,
            reasons=("claim_without_grant",),
        )

    if governing.grant_state == "active":
        evidence.append(
            _grant_evidence(
                governing,
                SUPPORTING,
                f"覆盖本次使用：渠道 {governing.channel}、地区 {governing.territory}、"
                f"有效期 {governing.valid_from_raw}～{governing.valid_to_raw}、"
                f"范围 {governing.coverage_scope}",
                "grant_covers_use",
            )
        )
        evidence.extend(_history_evidence(governing, model))
        return Coverage(
            use=use,
            outcome=COVERED,
            summary="对应当前有效授权，且渠道、地区、期限与版本范围均匹配",
            lineage=lineage,
            evidence=evidence,
            matched_grant=governing,
            supporting_grants=(governing,),
            reasons=("grant_covers_use",),
        )

    if governing.grant_state == "withdrawn":
        evidence.append(
            _grant_evidence(
                governing,
                AGAINST,
                "最新适用记录显示授权已撤回；撤回范围内的使用需要复核，"
                "已结算使用保留原凭证但标记复核",
                "grant_withdrawn",
            )
        )
        return Coverage(
            use=use,
            outcome=UNDER_DETERMINATION,
            summary="授权已撤回，本次使用的可结算状态必须重新确认",
            lineage=lineage,
            evidence=evidence,
            reasons=("grant_withdrawn",),
        )

    if governing.grant_state == "disputed":
        evidence.append(
            _grant_evidence(
                governing,
                AGAINST,
                "最新适用记录的授权主体存在争议，覆盖结论待确认",
                "grant_disputed",
            )
        )
        return Coverage(
            use=use,
            outcome=UNDER_DETERMINATION,
            summary="存在看似适用的授权，但授权状态有争议，不能判定",
            lineage=lineage,
            evidence=evidence,
            reasons=("grant_disputed",),
        )

    evidence.append(
        _grant_evidence(
            governing,
            AGAINST,
            f"未知授权状态 {governing.grant_state!r}",
            "unknown_grant_state",
        )
    )
    return Coverage(
        use=use,
        outcome=UNDER_DETERMINATION,
        summary="最新适用记录状态无法解释，不能判定",
        lineage=lineage,
        evidence=evidence,
        reasons=("unknown_grant_state",),
    )


def evaluate_snapshot(model: SnapshotModel) -> list[Coverage]:
    """Evaluate every canonical use, flagging duplicate and late records."""

    results: list[Coverage] = []
    for use in model.canonical_uses():
        coverage = evaluate_use(model, use)
        duplicates = [
            other
            for other in model.uses_by_key.get(use.key, [])
            if other.record_id != use.record_id
        ]
        if duplicates:
            coverage = _apply_duplicate_flags(coverage, duplicates)
        if use.is_late:
            coverage.evidence.append(
                _use_evidence(
                    use,
                    CONTEXT,
                    f"迟到的使用明细：使用后 {_days_late(use)} 天才报告，可能影响结算期间",
                    "late_report",
                )
            )
            coverage.reasons = (*coverage.reasons, "late_report")
        results.append(coverage)
    return results


def _days_late(use: Use) -> int:
    return (use.reported_at - use.used_at).days


def _apply_duplicate_flags(coverage: Coverage, duplicates: Sequence[Use]) -> Coverage:
    """Duplicate rows make billable units uncertain, so coverage needs review."""

    units = {coverage.use.usage_units, *(d.usage_units for d in duplicates)}
    states = {coverage.use.settlement_state, *(d.settlement_state for d in duplicates)}
    for other in duplicates:
        coverage.evidence.append(
            _use_evidence(
                other,
                CONTEXT,
                f"同一 use_id 的重复记录（记录 {other.record_id}，"
                f"结算状态 {other.settlement_state}，报告于 {format_instant(other.reported_at)}）",
                "duplicate_use_id",
            )
        )
    note = (
        f"同一 use_id 出现 {len(duplicates) + 1} 次，涉及 {len(units)} 种使用量、"
        f"{len(states)} 种结算状态；须先合并才能确定可计费使用量"
    )
    coverage.evidence.append(
        Evidence(code="duplicate_use_id", stance=AGAINST, message=note)
    )
    coverage.reasons = (*coverage.reasons, "duplicate_use_id")
    coverage.summary = coverage.summary + "；" + note
    if coverage.outcome == COVERED:
        coverage.outcome = UNDER_DETERMINATION
    return coverage
