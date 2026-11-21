"""跨周物价比较与三级可比性分类。

每期来源不完全一样，比较必须分三层：
1. 固定可比：同一片区、同一固定商品、同一换算克重（同尺度），两周都有有货
   且有价格观测——才能同尺度算物价变化。
2. 只能区间比较：固定商品在某周只有替代尺度（不同 grams）或缺货/未采价，
   或仅替代商品有观测，只能给区间，不进固定篮子变化。
3. 无法比较：两周都没有固定商品的有效观测。

补贴口径只允许使用第 1 层；禁止用各街区各自最便宜的替代结果算变化。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .data import Dataset
from .planner import usable_quote


def _valid_fixed_quotes(dataset: Dataset, week: str, shops: list[str],
                        item: str, household: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for shop in shops:
        quote, reason = usable_quote(dataset, week, shop, item, household)
        if quote is not None:
            out.append(quote)
    return out


def need_comparability(dataset: Dataset, baseline_week: str, current_week: str,
                       shops: list[str], need: dict[str, Any],
                       household: dict[str, Any]) -> dict[str, Any]:
    """单个需求在两周之间的可比性判定。"""
    item = need['fixed']
    base = _valid_fixed_quotes(dataset, baseline_week, shops, item, household)
    curr = _valid_fixed_quotes(dataset, current_week, shops, item, household)
    base_scales = {q['grams'] for q in base}
    curr_scales = {q['grams'] for q in curr}

    if not base or not curr:
        level = '无法比较'
    elif base_scales == curr_scales and len(base_scales) == 1:
        level = '固定可比'
    else:
        level = '区间比较'

    # 区间层证据：替代商品在哪些店有观测（仅供简报展示，不进补贴口径）
    alt_evidence = []
    if level != '固定可比':
        for week in (baseline_week, current_week):
            for shop in shops:
                for alt in need['alternatives']:
                    quote, reason = usable_quote(dataset, week, shop, alt, household)
                    if quote is not None:
                        alt_evidence.append({
                            'week': week, 'shop': shop, 'item': alt,
                            'grams': quote['grams'],
                            'price_cents': quote['price_cents'],
                        })
    return {
        'need': need['id'],
        'fixed_item': item,
        'level': level,
        'baseline_scales_grams': sorted(base_scales),
        'current_scales_grams': sorted(curr_scales),
        'alternative_evidence': alt_evidence,
    }


@dataclass(frozen=True)
class ShopBasketCost:
    shop: str
    cost_cents: int
    grams: int
    lines: tuple[dict[str, Any], ...]


def basket_cost(dataset: Dataset, week: str, shops: list[str],
                household: dict[str, Any], need_ids: list[str]) -> list[ShopBasketCost]:
    """每家能独立买齐固定篮子的店，按固定商品口径给出整篮成本。

    任一固定需求在该店缺货或未采价，该店本周就不产生固定篮子成本
    （缺值不补零）。成本含凑包步长，不含跨店与组合折扣——折扣属于
    具体采购建议，不进长期可比口径。
    """
    import math

    costs: list[ShopBasketCost] = []
    for shop in shops:
        lines = []
        total = 0
        grams = 0
        for nid in need_ids:
            need = dataset.need(nid)
            quote, reason = usable_quote(dataset, week, shop, need['fixed'], household)
            if quote is None:
                total = None  # type: ignore[assignment]
                break
            lot_grams = quote['step'] * quote['grams']
            lots = max(1, math.ceil(need['grams'] / lot_grams))
            units = lots * quote['step']
            line = {
                'need': nid,
                'item': need['fixed'],
                'units': units,
                'grams': units * quote['grams'],
                'cost_cents': units * quote['price_cents'],
                'unit_grams': quote['grams'],
            }
            lines.append(line)
            total += line['cost_cents']
            grams += line['grams']
        if total is not None:
            costs.append(ShopBasketCost(
                shop=shop,
                cost_cents=total,
                grams=grams,
                lines=tuple(lines),
            ))
    return sorted(costs, key=lambda c: c.cost_cents)


def district_basket_comparison(dataset: Dataset, baseline_weeks: list[str],
                               current_week: str, district: str,
                               household: dict[str, Any],
                               need_ids: list[str]) -> dict[str, Any]:
    """片区级三层分类与固定篮子成本序列。

    baseline_weeks 给多周时取各周可买齐店成本均值作为基准；
    只有全部需求在基准周与当前周都属“固定可比”，才输出可用于补贴的变化。
    """
    shops = dataset.district_shops(district)
    needs = [dataset.need(nid) for nid in need_ids]

    per_need = [
        need_comparability(dataset, bw, current_week, shops, need, household)
        for bw in baseline_weeks
        for need in needs
    ]
    # 同一需求在多个基准周都固定可比才算固定可比
    levels_by_need: dict[str, set[str]] = {}
    for row in per_need:
        levels_by_need.setdefault(row['need'], set()).add(row['level'])

    overall = '固定可比'
    need_levels = {}
    for nid in need_ids:
        levels = levels_by_need[nid]
        if levels == {'固定可比'}:
            need_levels[nid] = '固定可比'
        elif '无法比较' in levels and '固定可比' not in levels and '区间比较' not in levels:
            need_levels[nid] = '无法比较'
            overall = '无法比较' if overall != '区间比较' else overall
        else:
            need_levels[nid] = '区间比较'
            overall = '区间比较'

    baseline_costs = [
        basket_cost(dataset, bw, shops, household, need_ids)
        for bw in baseline_weeks
    ]
    current_costs = basket_cost(dataset, current_week, shops, household, need_ids)

    def _avg(cost_lists: list[list[ShopBasketCost]]) -> Optional[float]:
        priced = [c.cost_cents for week in cost_lists for c in week]
        return sum(priced) / len(priced) if priced else None

    baseline_avg = _avg(baseline_costs)
    current_min = current_costs[0].cost_cents if current_costs else None
    change = (current_min - baseline_avg
              if overall == '固定可比' and baseline_avg is not None
              and current_min is not None else None)

    return {
        'district': district,
        'current_week': current_week,
        'baseline_weeks': list(baseline_weeks),
        'overall_level': overall,
        'need_levels': need_levels,
        'baseline_shop_costs': [
            {'week': bw, 'shops': [c.__dict__ for c in costs]}
            for bw, costs in zip(baseline_weeks, baseline_costs)
        ],
        'current_shop_costs': [c.__dict__ for c in current_costs],
        'baseline_avg_cents': baseline_avg,
        'current_min_cents': current_min,
        'change_cents': change,
        'usable_for_cap': overall == '固定可比'
                           and baseline_avg is not None
                           and current_min is not None,
    }
