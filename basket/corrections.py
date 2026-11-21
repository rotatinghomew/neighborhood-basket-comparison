"""采价更正的影响分析与编辑分诊。

更正只修换算克重，但会沿两条链路扩散：
1. 比较链路：克重变化使跨周不再同尺度，固定可比降级为区间比较，
   进而可能让片区补贴上限失去调整依据——这属于“固定篮子覆盖不足”，
   不是物价变化。
2. 建议链路：满足同一需求所需的手数可能改变，依赖旧换算的购物建议、
   以及尚未发出、仍按更正前上限与优惠试算的菜券必须重核。

编辑分诊显式区分三件事：
- 固定篮子覆盖不足（比较层）
- 购物建议因促销过期/换算更正不可用（建议层）
- 真正需要改下次补贴上限（资金层，要求固定可比仍成立）
旧简报与已核销事实始终可查，不被覆盖。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .data import Dataset, load_dataset
from .planner import best_plans
from .compare import district_basket_comparison
from .aid import district_cap_update, coupon_ledger


def _units_for_need(dataset: Dataset, week: str, shop: str,
                    item: str, need_grams: int) -> dict[str, Any] | None:
    import math

    quote = dataset.quote_index().get((week, shop, item))
    if quote is None or quote['status'] != '有货' or quote['price_cents'] is None:
        return None
    lot_grams = quote['step'] * quote['grams']
    lots = max(1, math.ceil(need_grams / lot_grams))
    return {
        'lots': lots,
        'units': lots * quote['step'],
        'grams': lots * quote['step'] * quote['grams'],
        'unit_grams': quote['grams'],
        'step': quote['step'],
    }


def correction_impacts(folder: str = 'samples') -> list[dict[str, Any]]:
    """对每条更正在更正前/后两个视图间做差分。"""
    corrections = load_dataset(folder).corrections
    impacts: list[dict[str, Any]] = []

    for correction in corrections:
        received = correction['received']
        before = load_dataset(folder, as_of=_day_before(received))
        after = load_dataset(folder, as_of=received)
        need = next(n for n in before.needs if n['fixed'] == correction['item'])

        before_units = _units_for_need(
            before, correction['week'], correction['shop'],
            correction['item'], need['grams'])
        after_units = _units_for_need(
            after, correction['week'], correction['shop'],
            correction['item'], need['grams'])

        district = before.shops[correction['shop']]['district']
        household = after.households[0]
        need_ids = [n['id'] for n in after.needs]

        before_cmp = district_basket_comparison(
            before, before.aid['baseline_weeks'], correction['week'],
            district, household, need_ids)
        after_cmp = district_basket_comparison(
            after, after.aid['baseline_weeks'], correction['week'],
            district, household, need_ids)

        # 哪些尚未发出的券按更正前口径试算过、需要重核
        recheck = _coupons_requiring_recheck(before, after, correction)

        impacts.append({
            'correction': dict(correction),
            'need': need['id'],
            'district': district,
            'before_view_as_of': _day_before(received).isoformat(),
            'after_view_as_of': received,
            'purchase_units_before': before_units,
            'purchase_units_after': after_units,
            'units_changed': before_units != after_units,
            'comparability_before': before_cmp['overall_level'],
            'comparability_after': after_cmp['overall_level'],
            'need_levels_after': after_cmp['need_levels'],
            'cap_before': district_cap_update(
                before, correction['week'], district, household, need_ids),
            'cap_after': district_cap_update(
                after, correction['week'], district, household, need_ids),
            'coupons_to_recheck': recheck,
            'historical_briefings_preserved': [
                dict(p) for p in after.publication
                if p['scope'] == district and p['week'] == correction['week']
            ],
        })
    return impacts


def _coupons_requiring_recheck(before: Dataset, after: Dataset,
                               correction: dict[str, Any]) -> list[dict[str, Any]]:
    """更正后仍处于“拟发”的券，若其方案依赖被更正店/周则必须重核。

    已送达、已核销的券不动（有效期内不调低，履约事实不变）；
    已取消的券已释放，无需重核。
    """
    today = correction['received']
    ledger = coupon_ledger(after, today)
    out = []
    for coupon_id, coupon in ledger.items():
        # 已核销是店家实际履约的事实，不重核；已取消已释放。
        # 拟发与已送达（未核销）都仍按更正前口径挂着，需要重核。
        if coupon['kind'] not in ('拟发', '已送达'):
            continue
        claim = next(c for c in after.claims if c['id'] == coupon['claim'])
        # 更正周不晚于申请周、且更正店在申请片区内：该券的上限依据或
        # 采购口径建立在更正前的资料上，必须重核。
        touches = (correction['week'] <= claim['week']
                   and correction['shop'] in after.district_shops(claim['district']))
        if not touches:
            continue
        before_eval = best_plans(
            before, claim['week'], claim['household'], claim['needs'],
            shops=before.district_shops(claim['district']))
        after_eval = best_plans(
            after, claim['week'], claim['household'], claim['needs'],
            shops=after.district_shops(claim['district']))
        b = before_eval['fixed_best'] or before_eval['flexible_best']
        a = after_eval['fixed_best'] or after_eval['flexible_best']
        plan_changed = (b is None or a is None
                        or b.net_cents != a.net_cents
                        or b.line_items() != a.line_items())
        # 券预留的促销来自被更正片区的店：上限依据变化，预留也要复核
        promo_touched = (coupon.get('promo_shop')
                         and coupon['promo_shop'] in after.district_shops(claim['district']))
        # 券款高于重算后 min(上限, 可行成本)：旧口径多发，须重核
        cap_after = after.aid['district_caps_cents'][claim['district']]
        recomputed_funds = min(cap_after, a.net_cents) if a else 0
        overfunded = coupon['funds_cents'] > recomputed_funds
        out.append({
            'coupon': coupon_id,
            'claim': claim['id'],
            'status': coupon['kind'],
            'funds_cents': coupon['funds_cents'],
            'recomputed_funds_cents': recomputed_funds,
            'net_cents_before': b.net_cents if b else None,
            'net_cents_after': a.net_cents if a else None,
            'plan_changed': plan_changed,
            'promo_reservation_touched': bool(promo_touched),
            'overfunded': overfunded,
            'must_recheck': plan_changed or promo_touched or overfunded,
            'reason': '更正改变换算克重，原试算的手数/成本或上限依据失效',
        })
    return out


def editor_triage(folder: str = 'samples',
                  current_week: str | None = None) -> dict[str, Any]:
    """编辑视角的三类分诊清单。"""
    dataset = load_dataset(folder)
    current_week = current_week or max(c['week'] for c in dataset.claims)
    household = dataset.households[0]
    need_ids = [n['id'] for n in dataset.needs]

    coverage = []
    cap_changes = []
    expired_advice = []

    for district in sorted({m['district'] for m in dataset.shops.values()}):
        cap = district_cap_update(dataset, current_week, district,
                                  household, need_ids)
        if not cap['comparison']['usable_for_cap']:
            coverage.append({
                'district': district,
                'kind': '固定篮子覆盖不足',
                'need_levels': cap['comparison']['need_levels'],
                'action': '沿用旧上限并说明原因',
                'cap_cents': cap['recommended_cap_cents'],
            })
        elif cap['changed']:
            cap_changes.append({
                'district': district,
                'kind': '需要调整下次补贴上限',
                'from_cents': cap['previous_cap_cents'],
                'to_cents': cap['recommended_cap_cents'],
                'applied_change_cents': cap['applied_change_cents'],
            })

    # 促销过期：折扣只在登记周内有效。简报只提示最近一个已失效周，
    # 更早的历史周属于归档事实，不重复打扰编辑。
    discount_weeks = sorted(d['week'] for d in dataset.discounts)
    past_weeks = [w for w in discount_weeks if w < current_week]
    if past_weeks:
        latest = past_weeks[-1]
        expired_advice.append({
            'kind': '购物建议因促销过期不可用',
            'week': latest,
            'detail': f'{latest} 当周组合折扣在 {current_week} 已失效，'
                      '新时段不得沿用旧优惠；更早周次见归档简报',
        })

    impacts = correction_impacts(folder)
    for impact in impacts:
        if impact['units_changed']:
            expired_advice.append({
                'kind': '购物建议因换算更正不可用',
                'week': impact['correction']['week'],
                'shop': impact['correction']['shop'],
                'item': impact['correction']['item'],
                'detail': (
                    f"单位克重 {impact['purchase_units_before']['unit_grams']}"
                    f"→{impact['purchase_units_after']['unit_grams']}，"
                    f"满足需求的单位数 "
                    f"{impact['purchase_units_before']['units']}"
                    f"→{impact['purchase_units_after']['units']}"),
            })

    return {
        'as_of_week': current_week,
        'coverage_issues': coverage,
        'cap_changes': cap_changes,
        'advice_expired': expired_advice,
        'correction_impacts': impacts,
        'rule_note': '三类问题互不冲销：覆盖不足不改上限；促销/更正失效不改已发券；'
                     '只有固定可比成立时才允许调整上限。',
    }


def _day_before(iso_day: str) -> date:
    from datetime import timedelta
    return date.fromisoformat(iso_day) - timedelta(days=1)
