#!/usr/bin/env python3
"""Rebuild the demo lake from the three shipped scenario waves.

Usage::

    python scripts/build_demo.py [--lake data/rights_lake.sqlite] [--reset]

The script is deterministic apart from the wall-clock ``ingested_at`` field.
It prints each wave's change summary so the narrative can be checked at a
glance.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rights_facts.report import changes_report  # noqa: E402
from rights_facts.store import Lake  # noqa: E402

WAVES = ("wave-a", "wave-b", "wave-c")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lake",
        default=str(ROOT / "data" / "rights_lake.sqlite"),
        help="目标 SQLite 路径（默认 data/rights_lake.sqlite）",
    )
    parser.add_argument(
        "--scenario",
        default=str(ROOT / "samples" / "scenario"),
        help="场景根目录，内含 wave-a / wave-b / wave-c",
    )
    parser.add_argument("--reset", action="store_true", help="先删除已有库文件")
    args = parser.parse_args(argv)

    lake_path = Path(args.lake)
    if args.reset and lake_path.exists():
        lake_path.unlink()

    with Lake(lake_path) as lake:
        for wave in WAVES:
            wave_path = Path(args.scenario) / wave
            info, _model, coverages = lake.ingest(wave_path, snapshot_name=wave)
            print(
                f"已入库 #{info.sequence} {wave} "
                f"（接收于 {info.received_at}，使用 {len(coverages)} 条）"
            )
            print(changes_report(lake, info, as_json=False))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
