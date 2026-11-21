"""命令行入口：生成每周简报、采购建议、菜券试算与编辑分诊。

用法：
  python3 -m basket.cli briefing [--week 2026-08-31] [--today 2026-09-02]
                                 [--as-of 2026-08-30] [--data samples] [-o out.json]
  python3 -m basket.cli plan --week 2026-08-31 --household 单次取货 [--district 上街]
  python3 -m basket.cli triage [--week 2026-08-31]
  python3 -m basket.cli corrections

as-of 用于重放历史视图：传入早于某条更正 received 的日期，即可得到
更正前口径；缺省为含全部更正的当前视图。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / 'samples'

from .briefing import build_weekly_briefing
from .corrections import correction_impacts, editor_triage
from .data import load_dataset
from .planner import best_plans


def _default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f'不可序列化: {type(value)}')


def _dump(obj, out: str | None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=_default)
    if out:
        Path(out).write_text(text, encoding='utf-8')
        print(f'已写入 {out}')
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='邻里商圈价格篮子分析')
    parser.add_argument('--data', default=None,
                        help='资料目录（缺省为仓库内 samples/）')
    sub = parser.add_subparsers(dest='cmd', required=True)

    p_br = sub.add_parser('briefing', help='每周公开简报')
    p_br.add_argument('--week')
    p_br.add_argument('--today')
    p_br.add_argument('--as-of')
    p_br.add_argument('-o', '--out')

    p_pl = sub.add_parser('plan', help='单个采购户的最优采购方案')
    p_pl.add_argument('--week', required=True)
    p_pl.add_argument('--household', required=True)
    p_pl.add_argument('--district')
    p_pl.add_argument('--as-of')

    p_tr = sub.add_parser('triage', help='编辑三类分诊')
    p_tr.add_argument('--week')

    p_co = sub.add_parser('corrections', help='更正影响差分')

    args = parser.parse_args(argv)
    data_dir = str(Path(args.data).resolve()) if args.data else str(DEFAULT_DATA)

    if args.cmd == 'briefing':
        ds = load_dataset(data_dir, as_of=args.as_of)
        obj = build_weekly_briefing(data_dir, week=args.week,
                                    today=args.today, dataset=ds)
        _dump(obj, args.out)
        return 0

    if args.cmd == 'plan':
        ds = load_dataset(data_dir, as_of=args.as_of)
        shops = (ds.district_shops(args.district)
                 if args.district else list(ds.shops))
        outcome = best_plans(ds, args.week, args.household, shops=shops)
        _dump({
            'week': outcome['week'],
            'household': outcome['household'],
            'shops_scoped': outcome['shops_scoped'],
            'fixed_feasible': outcome['fixed_feasible'],
            'flexible_feasible': outcome['flexible_feasible'],
            'fixed_best': outcome['fixed_best'],
            'flexible_best': outcome['flexible_best'],
            'missing_fixed': outcome['missing_fixed'],
            'missing_flexible': outcome['missing_flexible'],
        }, None)
        return 0

    if args.cmd == 'triage':
        _dump(editor_triage(data_dir, current_week=args.week), None)
        return 0

    if args.cmd == 'corrections':
        _dump(correction_impacts(data_dir), None)
        return 0

    return 1


if __name__ == '__main__':
    sys.exit(main())
