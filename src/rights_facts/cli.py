"""Command line entry point: ``python -m rights_facts <command>``.

Commands are read-only except ``ingest``.  All reports are deterministic for a
given lake state and run, and every report states which run it describes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .report import (
    changes_report,
    coverage_report,
    issues_report,
    review_report,
    runs_report,
    use_report,
    work_report,
)
from .sources import SourceError
from .store import Lake, LakeError
from .util import parse_instant_arg


DEFAULT_LAKE = "data/rights_lake.sqlite"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rights_facts",
        description="内容授权覆盖事实库：接收快照变化并给出可追溯的覆盖结论。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--lake",
        default=DEFAULT_LAKE,
        help=f"SQLite 库路径（默认 {DEFAULT_LAKE}）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="接收一个快照目录并派生覆盖事实")
    ingest.add_argument("path", help="快照目录，需含 grants.csv / content_versions.csv / uses.csv")
    ingest.add_argument("--name", help="快照名称（默认取目录名）")
    ingest.add_argument(
        "--received-at",
        help="快照接收时间，带时区的 ISO 8601；默认取快照内最晚记录时间",
    )
    ingest.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="允许相同内容与接收时间重复入库（仍会新增一条运行记录）",
    )
    ingest.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    coverage = sub.add_parser("coverage", help="列出某快照的授权覆盖结论")
    _add_run_args(coverage)
    coverage.add_argument("--json", action="store_true")

    review = sub.add_parser("review", help="列出某快照的待复核使用")
    _add_run_args(review)
    review.add_argument("--json", action="store_true")

    changes = sub.add_parser("changes", help="比较某快照与上一快照的结论变化")
    _add_run_args(changes)
    changes.add_argument("--json", action="store_true")

    issues = sub.add_parser("issues", help="列出某快照的数据完整性问题")
    _add_run_args(issues)
    issues.add_argument("--json", action="store_true")

    use = sub.add_parser("use", help="追溯单条使用记录的最新结论、凭证与跨快照历史")
    use.add_argument("use_id")
    use.add_argument("--json", action="store_true")

    work = sub.add_parser("work", help="按作品汇总主张、当前状态与使用")
    _add_run_args(work)
    work.add_argument("work_id")
    work.add_argument("--json", action="store_true")

    runs = sub.add_parser("runs", help="列出已入库快照")
    runs.add_argument("--json", action="store_true")

    return parser


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--run",
        default="latest",
        help="快照选择：latest（默认）、previous、序号，或 run_id",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    lake_path = Path(args.lake)
    try:
        if args.command == "runs":
            with Lake(lake_path) as lake:
                print(runs_report(lake, as_json=args.json))
            return 0

        if args.command == "ingest":
            received_at = (
                parse_instant_arg(args.received_at) if args.received_at else None
            )
            with Lake(lake_path) as lake:
                info, _model, _coverage = lake.ingest(
                    args.path,
                    snapshot_name=args.name,
                    received_at=received_at,
                    allow_duplicate=args.allow_duplicate,
                )
                if args.json:
                    print(
                        json.dumps(
                            {
                                "run": info.dict(),
                                "review": [
                                    {
                                        "use_id": row["use_id"],
                                        "priority": row["priority"],
                                        "reason_codes": json.loads(row["reason_codes_json"]),
                                    }
                                    for row in lake.review_rows(info.run_id)
                                ],
                            },
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                    )
                else:
                    print(
                        f"已入库快照 #{info.sequence} {info.snapshot_name}"
                        f"（接收于 {info.received_at}，run_id={info.run_id}）"
                    )
                    print(
                        "  使用 {canonical_uses} 条（原始记录 {uses} 条），"
                        "覆盖 {coverage} 条，问题 {issues} 项".format(**dict(info.counts))
                    )
                    review = lake.review_rows(info.run_id)
                    if review:
                        print(f"  待复核 {len(review)} 条：见 `rights_facts review`")
            return 0

        with Lake(lake_path) as lake:
            if args.command == "coverage":
                run = lake.resolve_run(args.run)
                print(coverage_report(lake, run, as_json=args.json))
            elif args.command == "review":
                run = lake.resolve_run(args.run)
                print(review_report(lake, run, as_json=args.json))
            elif args.command == "changes":
                run = lake.resolve_run(args.run)
                print(changes_report(lake, run, as_json=args.json))
            elif args.command == "issues":
                run = lake.resolve_run(args.run)
                print(issues_report(lake, run, as_json=args.json))
            elif args.command == "use":
                print(use_report(lake, args.use_id, as_json=args.json))
            elif args.command == "work":
                run = lake.resolve_run(args.run)
                print(work_report(lake, run, args.work_id, as_json=args.json))
            else:  # pragma: no cover - argparse prevents this
                parser.error(f"未知命令: {args.command}")
        return 0
    except (LakeError, SourceError, KeyError) as exc:
        if isinstance(exc, KeyError):
            print(f"找不到记录: {exc.args[0]!r}", file=sys.stderr)
        else:
            print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
