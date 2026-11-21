"""助购站补贴上限、菜券试算、促销兑换池与核销台账。

资金口径：
- 片区补贴上限只随“长期可比固定篮子”的成本变化调整；不可比较时沿用旧上限
  并注明原因，绝不拿任选替代的最低调价来调高或调低。
- 单周调整封顶 max_weekly_change_cents。
- 菜券按居民真正能去的店、能买齐的物品核算；同一组促销兑换份数不能给两户
  各开一张够用的券——促销池是共享约束。
- 券款 = min(可行采购成本, 片区上限)，超出部分列居民自付；买不齐时不给
  未获准替代凑账面，而是列缺项与可行的替代领券安排。
- 已送达未核销不释放预算；核销转已用额度；取消或到期才释放。
  有效期内的已发券不调低额度。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .data import Dataset
from .planner import BasketPlan, best_plans
from .compare import district_basket_comparison


# ---------------------------------------------------------------- 核销台账

ACTIVE_KINDS = ('拟发', '已送达', '已核销')


def coupon_ledger(dataset: Dataset, today: str | date) -> dict[str, dict[str, Any]]:
    """把追加式核销事件折叠成每张券截至 today 的当前状态。

    同一 id 的多事件按日期取最新；'已取消' 使券退出活动台账，
    其预留的促销份数与券款同时释放。历史事件仍全部保留可查。
    """
    today = date.fromisoformat(today) if isinstance(today, str) else today
    events = sorted(
        (e for e in dataset.redemptions if date.fromisoformat(e['day']) <= today),
        key=lambda e: (e['id'], e['day']),
    )
    ledger: dict[str, dict[str, Any]] = {}
    for event in events:
        ledger[event['id']] = dict(event)
    return ledger


def active_coupons(dataset: Dataset, today: str | date) -> list[dict[str, Any]]:
    """仍占用预算/促销池的券：未取消且未过期。"""
    today = date.fromisoformat(today) if isinstance(today, str) else today
    out = []
    for coupon in coupon_ledger(dataset, today).values():
        if coupon['kind'] == '已取消':
            continue
        if date.fromisoformat(coupon['valid_until']) < today:
            continue  # 到期自然释放
        out.append(coupon)
    return out


def used_budget(dataset: Dataset, today: str | date) -> int:
    """已核销券按店家实际履约转为已用额度。

    已送达但未核销的券不视为预算空闲，但也不计入已用，
    而是单独列为在途预留。
    """
    return sum(c['funds_cents']
               for c in active_coupons(dataset, today)
               if c['kind'] == '已核销')


def reserved_budget(dataset: Dataset, today: str | date) -> int:
    """在途（拟发、已送达未核销）占用的券款，不能当作空闲再分配。"""
    return sum(c['funds_cents']
               for c in active_coupons(dataset, today)
               if c['kind'] in ('拟发', '已送达'))


# ---------------------------------------------------------------- 促销兑换池

def promo_pool_remaining(dataset: Dataset, week: str,
                         today: str | date) -> dict[tuple[str, str], int]:
    """每周每店促销兑换份数的剩余量 = 配置上限 - 活动券预留量。

    已取消/到期券的预留份数随状态释放。
    """
    limits = {
        (entry['week'], entry['shop']): entry['units']
        for entry in dataset.aid['promo_coupon_limits']
        if entry['week'] == week
    }
    remaining = dict(limits)
    for coupon in active_coupons(dataset, today):
        if not coupon.get('promo_shop') or not coupon.get('reserved_units'):
            continue
        # 活动券按其申请周占用池子；这里只统计目标周的配置
        claim = next(c for c in dataset.claims if c['id'] == coupon['claim'])
        key = (claim['week'], coupon['promo_shop'])
        if key in remaining:
            remaining[key] -= coupon['reserved_units']
    return remaining


# ---------------------------------------------------------------- 上限调整

def district_cap_update(dataset: Dataset, current_week: str, district: str,
                        household: dict[str, Any],
                        need_ids: list[str]) -> dict[str, Any]:
    """依据长期可比固定篮子给出本周片区上限建议。"""
    aid = dataset.aid
    old_cap = aid['district_caps_cents'][district]
    comparison = district_basket_comparison(
        dataset, aid['baseline_weeks'], current_week, district,
        household, need_ids)

    result: dict[str, Any] = {
        'district': district,
        'week': current_week,
        'previous_cap_cents': old_cap,
        'recommended_cap_cents': old_cap,
        'changed': False,
        'reason': '',
        'comparison': comparison,
    }

    if not comparison['usable_for_cap']:
        bad = [nid for nid, level in comparison['need_levels'].items()
               if level != '固定可比']
        result['reason'] = (
            f"固定篮子覆盖不足或尺度不一致（{ '、'.join(bad) }），"
            '资料不足以同尺度比较，沿用旧上限，不按替代最低价调整')
        return result

    raw_change = comparison['change_cents']
    clamped = max(-aid['max_weekly_change_cents'],
                  min(aid['max_weekly_change_cents'], round(raw_change)))
    new_cap = max(0, old_cap + clamped)
    result.update({
        'recommended_cap_cents': new_cap,
        'changed': new_cap != old_cap,
        'raw_change_cents': raw_change,
        'applied_change_cents': new_cap - old_cap,
        'reason': '固定可比篮子成本变化（已按单周封顶调整）'
                  if new_cap != old_cap else '固定篮子成本变化为零或被封顶抵消',
    })
    return result


# ---------------------------------------------------------------- 申请评估

@dataclass
class ClaimEvaluation:
    claim_id: str
    feasible: bool
    plan: Optional[BasketPlan]
    missing: tuple[Any, ...]
    cap_cents: int
    coupon_cents: int
    self_pay_cents: int
    promo_shop: Optional[str]
    promo_units: int
    promo_blocked: bool
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            'claim': self.claim_id,
            'feasible': self.feasible,
            'plan': None if self.plan is None else {
                'net_cents': self.plan.net_cents,
                'gross_cents': self.plan.gross_cents,
                'discount_cents': self.plan.discount_cents,
                'total_grams': self.plan.total_grams,
                'pickups': self.plan.pickups,
                'shops': list(self.plan.shops),
                'lines': self.plan.line_items(),
            },
            'missing': [
                {'need': m.need_id, 'fixed_item': m.fixed_item,
                 'primary_reason': m.primary_reason(),
                 'detail': [{'where': where, 'reason': reason}
                            for where, reason in m.reasons]}
                for m in self.missing
            ],
            'cap_cents': self.cap_cents,
            'coupon_cents': self.coupon_cents,
            'self_pay_cents': self.self_pay_cents,
            'promo_shop': self.promo_shop,
            'promo_units': self.promo_units,
            'promo_blocked': self.promo_blocked,
            'notes': list(self.notes),
        }


def evaluate_claim(dataset: Dataset, claim: dict[str, Any],
                   today: str | date,
                   pool: Optional[dict[tuple[str, str], int]] = None
                   ) -> ClaimEvaluation:
    """按居民真正能去的店与能买齐的物品核算一张券。"""
    household = dataset.household(claim['household'])
    shops = dataset.district_shops(claim['district'])
    need_ids = claim['needs']
    cap = dataset.aid['district_caps_cents'][claim['district']]
    notes: list[str] = []

    outcome = best_plans(dataset, claim['week'], claim['household'],
                         need_ids, shops=shops)
    plan: Optional[BasketPlan] = outcome['fixed_best'] or outcome['flexible_best']
    missing = outcome['missing']

    promo_shop = None
    promo_units = 0
    promo_blocked = False

    if plan is None:
        # 买不齐：不用未获准替代凑账面，列缺项与可行的部分安排
        return ClaimEvaluation(
            claim_id=claim['id'], feasible=False, plan=None,
            missing=missing, cap_cents=cap, coupon_cents=0,
            self_pay_cents=0, promo_shop=None, promo_units=0,
            promo_blocked=False,
            notes=('无可行整篮方案，不发足额券；按缺项给出替代领券安排',),
        )

    if pool is None:
        pool = promo_pool_remaining(dataset, claim['week'], today)

    # 促销预留：方案里实际成交折扣的店
    if plan.discounts:
        discount = plan.discounts[0]
        need_units = 1  # 每个采购户每笔整单最多用一次该折扣（maximum=1）
        key = (claim['week'], discount.shop)
        if pool.get(key, 0) >= need_units:
            promo_shop = discount.shop
            promo_units = need_units
        else:
            promo_blocked = True
            notes.append(
                f'{discount.shop} 促销兑换份数已被其他券占满，'
                '本券按无折扣成本核算，不能同一份促销开两张券')

    effective_cost = plan.net_cents if promo_shop else plan.gross_cents
    coupon = min(cap, effective_cost)
    self_pay = effective_cost - coupon

    if plan.allow_alternatives:
        notes.append('方案使用了需求登记的许可替代，固定篮子不可买齐')
    if promo_shop:
        notes.append(f'预留 {promo_shop} 组合折扣 {promo_units} 份')

    return ClaimEvaluation(
        claim_id=claim['id'], feasible=True, plan=plan,
        missing=missing, cap_cents=cap, coupon_cents=coupon,
        self_pay_cents=self_pay, promo_shop=promo_shop,
        promo_units=promo_units, promo_blocked=promo_blocked,
        notes=tuple(notes),
    )


def evaluate_all_claims(dataset: Dataset, today: str | date,
                        pool: Optional[dict[tuple[str, str], int]] = None
                        ) -> list[dict[str, Any]]:
    """整批试算：促销池逐张扣减，模拟实际发券顺序的共享约束。

    pool 由调用方按评估日给定（不同日期活动券不同，池子余量不同）。
    """
    if pool is None:
        pool = promo_pool_remaining(dataset, _common_week(dataset), today)
    results = []
    for claim in dataset.claims:
        ev = evaluate_claim(dataset, claim, today, pool=dict(pool))
        if ev.promo_shop:
            key = (claim['week'], ev.promo_shop)
            pool[key] = pool.get(key, 0) - ev.promo_units
        results.append(ev.as_dict())
    return results


def _common_week(dataset: Dataset) -> str:
    weeks = {c['week'] for c in dataset.claims}
    if len(weeks) != 1:
        raise ValueError('多周申请需分别传 pool 调用 evaluate_claim')
    return next(iter(weeks))
