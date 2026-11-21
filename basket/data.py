"""原始资料装载、索引与更正应用。

约定（与 README 一致）：
- price_cents 为空表示没有观测，绝不当作数值零参与计算。
- status='缺货' 与 status='未采价' 是两种不同状态：前者证明该时段无货，
  后者只是没有采到价，不能据此声称买不到，也不能补零。
- 更正（corrections）只修正换算克重，不改变状态与价格；按 received 日期
  生效。历史时点的简报与已核销事实保留更正前视图，可分别重放。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


def _iso(value: str) -> date:
    return date.fromisoformat(value)


@dataclass(frozen=True)
class Dataset:
    """一份自洽的资料视图（可表示更正前或更正后）。"""

    quotes: tuple[dict[str, Any], ...]
    shops: dict[str, dict[str, Any]]
    needs: tuple[dict[str, Any], ...]
    discounts: tuple[dict[str, Any], ...]
    households: tuple[dict[str, Any], ...]
    corrections: tuple[dict[str, Any], ...]
    publication: tuple[dict[str, Any], ...]
    aid: dict[str, Any]
    claims: tuple[dict[str, Any], ...]
    redemptions: tuple[dict[str, Any], ...]
    as_of: date | None = None

    def quote_index(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        return {(q['week'], q['shop'], q['item']): q for q in self.quotes}

    def household(self, household_id: str) -> dict[str, Any]:
        return next(h for h in self.households if h['id'] == household_id)

    def need(self, need_id: str) -> dict[str, Any]:
        return next(n for n in self.needs if n['id'] == need_id)

    def district_shops(self, district: str) -> list[str]:
        return [s for s, meta in self.shops.items() if meta['district'] == district]


def load_dataset(folder: str | Path = 'samples',
                 as_of: str | date | None = None) -> Dataset:
    """从资料目录装载一份视图。

    as_of 给定时，仅应用 received <= as_of 的更正；不给定时应用全部更正，
    即“当前视图”。要重放更正前的旧简报，传入早于 received 的日期。
    """
    folder = Path(folder)
    raw: dict[str, Any] = {}
    for path in sorted(folder.glob('*.json')):
        raw[path.stem] = json.loads(path.read_text(encoding='utf-8'))

    cutoff = _iso(as_of) if isinstance(as_of, str) else as_of
    corrections = tuple(raw['corrections'])
    active = tuple(
        c for c in corrections
        if cutoff is None or _iso(c['received']) <= cutoff
    )
    quotes = apply_corrections(tuple(raw['quotes']), active)

    return Dataset(
        quotes=quotes,
        shops=dict(raw['shops']),
        needs=tuple(raw['needs']),
        discounts=tuple(raw['discounts']),
        households=tuple(raw['households']),
        corrections=corrections,
        publication=tuple(raw['publication']),
        aid=dict(raw['aid']),
        claims=tuple(raw['claims']),
        redemptions=tuple(raw['redemptions']),
        as_of=cutoff,
    )


def apply_corrections(quotes: tuple[dict[str, Any], ...],
                      corrections: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """把更正中的克重覆盖到对应报价；价格与状态不动。

    更正只改换算关系，因此同一条报价的 step*grams 可能跨过需求克重，
    改变满足该需求所需的购买单位数。
    """
    patched = {(c['week'], c['shop'], c['item']): c for c in corrections}
    out: list[dict[str, Any]] = []
    for quote in quotes:
        key = (quote['week'], quote['shop'], quote['item'])
        row = dict(quote)
        if key in patched:
            row['grams'] = patched[key]['grams']
            row['corrected'] = True
        out.append(row)
    return tuple(out)
