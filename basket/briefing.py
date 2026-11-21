"""每周公开简报与站长片区视图。

简报把三类信息分开陈列，居民能分辨到底是价格变化、可选货品变了，
还是采价少了：
- 固定篮子可比部分（同尺度，含成本与变化）
- 只能区间比较的部分（替代/异尺度证据）
- 确实无法比较的部分（缺货与未采价分别标注，缺值不补零）

站长视图另外给出：物价证据清单、当日时段能兑齐的家庭、
尚无可承诺去处的家庭及原因。
"""

from __future__ import annotations

from typing import Any

from .data import Dataset, load_dataset
from .planner import best_plans, feasible_shops, usable_quote
from .compare import district_basket_comparison
from .aid import (
    district_cap_update,
    evaluate_all_claims,
    coupon_ledger,
    used_budget,
    reserved_budget,
    promo_pool_remaining,
)


def _evidence_rows(dataset: Dataset, week: str, district: str,
                   household: dict[str, Any]) -> list[dict[str, Any]]:
    """片区内每条有效报价证据，会员价与普通价分开标。"""
    rows = []
    for shop in dataset.district_shops(district):
        for quote in dataset.quotes:
            if quote['week'] != week or quote['shop'] != shop:
                continue
            rows.append({
                'shop': shop,
                'item': quote['item'],
                'status': quote['status'],
                'price_cents': quote['price_cents'],  # None 保持缺值语义
                'unit_grams': quote['grams'],
                'step': quote['step'],
                'window': [quote['from_minute'], quote['to_minute']],
                'members_only': quote['members_only'],
                'reachable': usable_quote(
                    dataset, week, shop, quote['item'], household)[0] is not None,
                'corrected': quote.get('corrected', False),
            })
    return rows


def build_weekly_briefing(folder: str = 'samples',
                          week: str | None = None,
                          today: str | None = None,
                          dataset: Dataset | None = None) -> dict[str, Any]:
    if dataset is None:
        dataset = load_dataset(folder)
    week = week or max(c['week'] for c in dataset.claims)
    today = today or week
    need_ids = [n['id'] for n in dataset.needs]
    household = dataset.households[0]

    districts: list[dict[str, Any]] = []
    for district in sorted({m['district'] for m in dataset.shops.values()}):
        shops = dataset.district_shops(district)
        comparison = district_basket_comparison(
            dataset, dataset.aid['baseline_weeks'], week,
            district, household, need_ids)
        cap = district_cap_update(dataset, week, district, household, need_ids)

        fixed_layer, interval_layer, unknown_layer = [], [], []
        for nid, level in comparison['need_levels'].items():
            need = dataset.need(nid)
            entry = {'need': nid, 'fixed_item': need['fixed'], 'level': level}
            if level == '固定可比':
                fixed_layer.append(entry)
            elif level == '区间比较':
                interval_layer.append(entry)
            else:
                unknown_layer.append(entry)

        # 各采购户在本片区的可行建议（固定与替代两个口径）
        advice = []
        for hh in dataset.households:
            outcome = best_plans(dataset, week, hh['id'], need_ids, shops=shops)
            advice.append({
                'household': hh['id'],
                'reachable_shops': feasible_shops(dataset, hh, week, shops),
                'fixed_feasible': outcome['fixed_feasible'],
                'flexible_feasible': outcome['flexible_feasible'],
                'fixed_best': _plan_summary(outcome['fixed_best']),
                'flexible_best': _plan_summary(outcome['flexible_best']),
                'missing': [
                    {'need': m.need_id, 'fixed_item': m.fixed_item,
                     'primary_reason': m.primary_reason()}
                    for m in outcome['missing']
                ],
            })

        districts.append({
            'district': district,
            'comparable_fixed_layer': fixed_layer,
            'interval_only_layer': interval_layer,
            'incomparable_layer': unknown_layer,
            'overall_level': comparison['overall_level'],
            'baseline_avg_cents': comparison['baseline_avg_cents'],
            'current_min_cents': comparison['current_min_cents'],
            'change_cents': comparison['change_cents'],
            'cap_recommendation': {
                'previous_cents': cap['previous_cap_cents'],
                'recommended_cents': cap['recommended_cap_cents'],
                'changed': cap['changed'],
                'reason': cap['reason'],
            },
            'evidence': _evidence_rows(dataset, week, district, household),
            'shopping_advice': advice,
        })

    claims = evaluate_all_claims(dataset, today)
    ledger = coupon_ledger(dataset, today)
    pool = promo_pool_remaining(dataset, week, today)

    return {
        'briefing_week': week,
        'as_of': dataset.as_of.isoformat() if dataset.as_of else '当前视图（含全部已收到更正）',
        'baseline_weeks': dataset.aid['baseline_weeks'],
        'districts': districts,
        'claims_evaluation': claims,
        'coupon_budget': {
            'total_cents': dataset.aid['total_cents'],
            'used_cents': used_budget(dataset, today),
            'reserved_in_flight_cents': reserved_budget(dataset, today),
            'free_cents': dataset.aid['total_cents']
                          - used_budget(dataset, today)
                          - reserved_budget(dataset, today),
        },
        'promo_pool_remaining': [
            {'week': w, 'shop': s, 'units': units}
            for (w, s), units in sorted(pool.items())
        ],
        'coupon_ledger': [
            {'id': cid, **{k: v for k, v in event.items() if k != 'id'}}
            for cid, event in sorted(ledger.items())
        ],
        'station_note': '已送达未核销仍占预算；核销转已用；取消或到期才释放。',
    }


def _plan_summary(plan) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        'net_cents': plan.net_cents,
        'gross_cents': plan.gross_cents,
        'discount_cents': plan.discount_cents,
        'total_grams': plan.total_grams,
        'pickups': plan.pickups,
        'shops': list(plan.shops),
        'allow_alternatives': plan.allow_alternatives,
        'lines': plan.line_items(),
    }
