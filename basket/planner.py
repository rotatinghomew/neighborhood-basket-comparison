"""按采购窗口与可到达商户求可行的一篮子采购方案。

关键口径：
- 只有 status='有货' 且 price_cents 有观测的报价能成交；'缺货' 是确凿无货，
  '未采价' 是没有观测，两者分开报告，缺值绝不补零。
- 满足一个需求所需的手数 n = ceil(需求克重 / (step * 单位克重))，
  凑整多出来的克重照算钱、照算携带重量，但不算新的需求满足。
- 替代许可挂在需求上；同一商品在一笔采购里至多填满一个需求。
- 组合折扣只有整单单位数达到 units 门槛才计 off_cents；为凑门槛额外购买的
  部分计入支出与重量。maximum 限制折扣可用次数，end 限制可成交的时段。
- 采购户的窗口、步数（max_shops）、携带克重（max_grams）、会员店全部硬约束。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any, Optional

from .data import Dataset


@dataclass(frozen=True)
class Fill:
    """一个需求由某店某商品的若干手填满。"""

    need_id: str
    item: str
    shop: str
    lots: int            # 购买手数（每手 step 个单位）
    unit_count: int      # 总单位数 = lots * step
    grams: int           # 实际到手克重（含凑包多出的量）
    cost_cents: int
    kind: str            # '固定' 或 '替代'
    members_only: bool


@dataclass(frozen=True)
class TopUp:
    """为达成组合折扣门槛而额外购买的单位（不满足任何需求）。"""

    item: str
    shop: str
    unit_count: int
    grams: int
    cost_cents: int


@dataclass(frozen=True)
class AppliedDiscount:
    week: str
    shop: str
    off_cents: int
    times: int


@dataclass
class BasketPlan:
    week: str
    household_id: str
    fills: tuple[Fill, ...]
    topups: tuple[TopUp, ...]
    discounts: tuple[AppliedDiscount, ...]
    missing: tuple['MissingNeed', ...]
    gross_cents: int
    discount_cents: int
    total_grams: int
    shops: tuple[str, ...]
    pickups: int
    allow_alternatives: bool

    @property
    def net_cents(self) -> int:
        return self.gross_cents - self.discount_cents

    @property
    def feasible(self) -> bool:
        return not self.missing

    def line_items(self) -> list[dict[str, Any]]:
        rows = [
            {
                'need': f.need_id,
                'item': f.item,
                'shop': f.shop,
                'kind': f.kind,
                'lots': f.lots,
                'units': f.unit_count,
                'grams': f.grams,
                'cost_cents': f.cost_cents,
            }
            for f in self.fills
        ]
        for t in self.topups:
            rows.append({
                'need': None,
                'item': t.item,
                'shop': t.shop,
                'kind': '凑折扣',
                'lots': None,
                'units': t.unit_count,
                'grams': t.grams,
                'cost_cents': t.cost_cents,
            })
        return rows


@dataclass(frozen=True)
class MissingNeed:
    need_id: str
    fixed_item: str
    reasons: tuple[tuple[str, str], ...]  # (店铺, 原因)

    def primary_reason(self) -> str:
        texts = {reason for _, reason in self.reasons}
        # 任一店有货但被会员/窗口挡住，与全片区无观测要区分开
        for priority in ('会员限制', '窗口不可达',
                         '商品需同时填两个需求，一笔采购不可共用'):
            if priority in texts:
                return priority
        if '缺货' in texts:
            return '缺货'
        if '未采价' in texts:
            return '未采价'
        return '无可到达报价'


# ---------------------------------------------------------------- 可行性

def _window_overlaps(quote: dict[str, Any], household: dict[str, Any]) -> bool:
    win_start, win_end = household['window']
    return quote['from_minute'] <= win_end and quote['to_minute'] >= win_start


def _shop_reachable(quote: dict[str, Any], shop_meta: dict[str, Any],
                    household: dict[str, Any]) -> bool:
    """窗口内能到店：最早开门时刻加上路程仍在关窗之前，且时段相交。"""
    win_start, win_end = household['window']
    travel = shop_meta['pickup_minutes']
    if not _window_overlaps(quote, household):
        return False
    return quote['from_minute'] + travel <= win_end


def usable_quote(dataset: Dataset, week: str, shop: str, item: str,
                 household: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """返回可成交报价；不可用时返回 (None, 原因)。"""
    quote = dataset.quote_index().get((week, shop, item))
    if quote is None:
        return None, '无报价记录'
    if quote['status'] == '缺货':
        return None, '缺货'
    if quote['status'] == '未采价' or quote['price_cents'] is None:
        return None, '未采价'
    shop_meta = dataset.shops[shop]
    if not _shop_reachable(quote, shop_meta, household):
        return None, '窗口不可达'
    if quote['members_only'] and shop not in household['member_shops']:
        return None, '会员限制'
    return quote, None


def feasible_shops(dataset: Dataset, household: dict[str, Any], week: str,
                   shops: Optional[list[str]] = None) -> list[str]:
    """该采购户本周窗口内可到达、时段相交的店铺。"""
    candidates = shops or list(dataset.shops)
    result = []
    win_start, win_end = household['window']
    for shop in candidates:
        meta = dataset.shops[shop]
        shop_quotes = [q for q in dataset.quotes
                       if q['week'] == week and q['shop'] == shop]
        if any(q['from_minute'] <= win_end
               and q['to_minute'] >= win_start
               and q['from_minute'] + meta['pickup_minutes'] <= win_end
               for q in shop_quotes):
            result.append(shop)
    return result


# ---------------------------------------------------------------- 单店候选

def _lots_needed(quote: dict[str, Any], need_grams: int) -> int:
    lot_grams = quote['step'] * quote['grams']
    return max(1, math.ceil(need_grams / lot_grams))


def _candidate_fills(dataset: Dataset, week: str, shop: str,
                     household: dict[str, Any], need: dict[str, Any],
                     allow_alternatives: bool) -> tuple[list[Fill], list[tuple[str, str]]]:
    """一个需求在一家店的全部填法（固定优先登记），以及失败原因。"""
    fills: list[Fill] = []
    failures: list[tuple[str, str]] = []
    candidates = [(need['fixed'], '固定')]
    if allow_alternatives:
        candidates += [(item, '替代') for item in need['alternatives']]
    for item, kind in candidates:
        quote, reason = usable_quote(dataset, week, shop, item, household)
        if quote is None:
            failures.append((item, reason or '不可用'))
            continue
        lots = _lots_needed(quote, need['grams'])
        unit_count = lots * quote['step']
        fills.append(Fill(
            need_id=need['id'],
            item=item,
            shop=shop,
            lots=lots,
            unit_count=unit_count,
            grams=unit_count * quote['grams'],
            cost_cents=unit_count * quote['price_cents'],
            kind=kind,
            members_only=quote['members_only'],
        ))
    return fills, failures


# ---------------------------------------------------------------- 折扣

def _shop_discounts(dataset: Dataset, week: str, shop: str,
                    household: dict[str, Any]) -> list[dict[str, Any]]:
    """该店本周对该采购户仍可成交的折扣：最早能到店时刻不晚于 end。"""
    win_start, _ = household['window']
    earliest_arrival = win_start + dataset.shops[shop]['pickup_minutes']
    return [
        d for d in dataset.discounts
        if d['week'] == week and d['shop'] == shop
        and earliest_arrival <= d['end']
    ]


def _discount_options(dataset: Dataset, week: str, shop: str,
                      household: dict[str, Any],
                      bought_units: dict[str, int]) -> list[tuple[int, list[TopUp], int, AppliedDiscount]]:
    """返回这家店的折扣方案：(次数, 凑单, 优惠分值, 记录)，含不使用折扣。"""
    options: list[tuple[int, list[TopUp], int, Optional[AppliedDiscount]]] = [
        (0, [], 0, None)
    ]
    for discount in _shop_discounts(dataset, week, shop, household):
        for times in range(1, discount['maximum'] + 1):
            topups: list[TopUp] = []
            for item, threshold in discount['units'].items():
                required = threshold * times
                have = bought_units.get(item, 0)
                if have < required:
                    quote, reason = usable_quote(dataset, week, shop, item, household)
                    if quote is None:
                        # 凑门槛的商品本身买不到，该次数方案不成立
                        topups = []
                        break
                    extra = required - have
                    topups.append(TopUp(
                        item=item,
                        shop=shop,
                        unit_count=extra,
                        grams=extra * quote['grams'],
                        cost_cents=extra * quote['price_cents'],
                    ))
            else:
                options.append((
                    times,
                    topups,
                    times * discount['off_cents'],
                    AppliedDiscount(
                        week=week,
                        shop=shop,
                        off_cents=discount['off_cents'],
                        times=times,
                    ),
                ))
    return options


# ---------------------------------------------------------------- 缺项诊断

def _diagnose_missing(dataset: Dataset, week: str, shops: list[str],
                      household: dict[str, Any], need_ids: list[str],
                      allow_alternatives: bool) -> list['MissingNeed']:
    """整篮不可行时，逐需求说明它为什么无法被任何可行分配覆盖。

    先用 DFS 找出至少能进入某个完整分配的需求；其余需求再汇总
    各店各候选商品的失败原因（缺货/未采价/会员/窗口），以及
    “商品已被另一需求占用”这类结构性冲突。
    """
    needs = [dataset.need(nid) for nid in need_ids]
    candidates: dict[str, list[Fill]] = {}
    raw_failures: dict[str, list[tuple[str, str]]] = {}
    for need in needs:
        cands: list[Fill] = []
        fails: list[tuple[str, str]] = []
        for shop in shops:
            fills, shop_fail = _candidate_fills(
                dataset, week, shop, household, need, allow_alternatives)
            cands.extend(fills)
            fails.extend((f'{shop}/{item}', reason)
                         for item, reason in shop_fail)
        candidates[need['id']] = cands
        raw_failures[need['id']] = fails

    def _can_enter_full_assignment(target: str) -> bool:
        """目标需求是否存在一条满足全部硬约束的完整分配把它包含在内。"""
        found = False

        def anchored(index: int, used_items: set[str], used_shops: set[str],
                     anchored_done: bool) -> None:
            nonlocal found
            if found:
                return
            if index == len(needs):
                found = anchored_done
                return
            need = needs[index]
            for fill in candidates[need['id']]:
                if fill.item in used_items:
                    continue
                new_shops = used_shops | {fill.shop}
                if len(new_shops) > household['max_shops']:
                    continue
                is_anchor = need['id'] == target
                if is_anchor and anchored_done:
                    continue
                anchored(index + 1, used_items | {fill.item}, new_shops,
                         anchored_done or is_anchor)

        anchored(0, set(), set(), False)
        return found

    coverable = {n['id'] for n in needs if _can_enter_full_assignment(n['id'])}

    out: list[MissingNeed] = []
    for need in needs:
        if need['id'] in coverable:
            continue
        reasons = list(raw_failures[need['id']])
        # 候选存在却进不了任何完整分配：结构性冲突（商品共用/步数/超重）。
        # 这是整篮层面的原因，优先于逐店逐品的单点失败。
        if candidates[need['id']]:
            shared = sorted({
                f.item for f in candidates[need['id']]
                if any(f.item in {g.item for g in candidates[other['id']]}
                       for other in needs if other['id'] != need['id'])
            })
            structural = [
                (item, '商品需同时填两个需求，一笔采购不可共用')
                for item in shared
            ]
            if structural:
                reasons = structural + reasons
        out.append(MissingNeed(
            need_id=need['id'],
            fixed_item=need['fixed'],
            reasons=tuple(reasons),
        ))
    return out


# ---------------------------------------------------------------- 整篮搜索

def _enumerate_assignments(dataset: Dataset, week: str, shops: list[str],
                           household: dict[str, Any], need_ids: list[str],
                           allow_alternatives: bool):
    """DFS 枚举“需求 -> (店, 商品填法)”的分配，强制商品全局不重复。"""
    needs = [dataset.need(nid) for nid in need_ids]
    per_need: list[list[Fill]] = []
    failures: dict[str, list[tuple[str, str]]] = {}
    for need in needs:
        cands: list[Fill] = []
        fail: list[tuple[str, str]] = []
        for shop in shops:
            fills, shop_fail = _candidate_fills(
                dataset, week, shop, household, need, allow_alternatives)
            cands.extend(fills)
            for item, reason in shop_fail:
                fail.append((f'{shop}/{item}', reason))
        per_need.append(cands)
        failures[need['id']] = fail

    results: list[tuple[Fill, ...]] = []

    def dfs(index: int, chosen: list[Fill], used_items: set[str]):
        if index == len(needs):
            results.append(tuple(chosen))
            return
        for fill in per_need[index]:
            if fill.item in used_items:
                continue
            chosen.append(fill)
            used_items.add(fill.item)
            dfs(index + 1, chosen, used_items)
            used_items.remove(fill.item)
            chosen.pop()

    dfs(0, [], set())
    return results, failures


def shop_basket_plans(dataset: Dataset, week: str, household_id: str,
                      need_ids: Optional[list[str]] = None, *,
                      allow_alternatives: bool = False,
                      shops: Optional[list[str]] = None) -> list[BasketPlan]:
    """列出满足约束的全部整篮方案（按净支出升序）。

    固定模式 allow_alternatives=False 用于长期可比篮子；
    替代模式用于给居民的实用建议，替代许可只取自需求登记。
    """
    household = dataset.household(household_id)
    reachable = feasible_shops(dataset, household, week, shops)
    need_ids = need_ids or [n['id'] for n in dataset.needs]

    assignments, _ = _enumerate_assignments(
        dataset, week, reachable, household, need_ids, allow_alternatives)

    plans: list[BasketPlan] = []
    for fills in assignments:
        used_shops = sorted({f.shop for f in fills})
        if len(used_shops) > household['max_shops']:
            continue
        # 每店每商品的整单单位数（同一商品只可能出现在一个需求里）
        bought: dict[str, dict[str, int]] = {}
        grams_by_shop: dict[str, int] = {}
        for f in fills:
            bought.setdefault(f.shop, {})[f.item] = (
                bought.get(f.shop, {}).get(f.item, 0) + f.unit_count)
            grams_by_shop[f.shop] = grams_by_shop.get(f.shop, 0) + f.grams

        all_topups: list[TopUp] = []
        all_discounts: list[AppliedDiscount] = []
        discount_total = 0
        # 各店折扣独立选择；携带上限是整篮口径，先把各店候选方案组合后
        # 按总重量过滤，再取净优惠最大的组合（优惠不跨店）。
        per_shop_options: dict[str, list] = {}
        for shop in used_shops:
            per_shop_options[shop] = _discount_options(
                dataset, week, shop, household, bought[shop])

        base_grams = sum(grams_by_shop.values())
        combos = []
        for combo in itertools.product(*(per_shop_options[s] for s in used_shops)):
            added_grams = sum(t.grams for option in combo for t in option[1])
            if base_grams + added_grams <= household['max_grams']:
                combos.append(combo)
        if not combos:
            continue  # 理论上不会发生：无折扣组合重量即基础篮，已在可行分配内

        best_combo = max(
            combos,
            key=lambda c: sum(o[2] - sum(t.cost_cents for t in o[1]) for o in c))
        for option in best_combo:
            _, topups, off, applied = option
            all_topups.extend(topups)
            if applied is not None:
                all_discounts.append(applied)
            discount_total += off

        gross = sum(f.cost_cents for f in fills) + sum(t.cost_cents for t in all_topups)
        total_grams = sum(f.grams for f in fills) + sum(t.grams for t in all_topups)
        if total_grams > household['max_grams']:
            continue  # 携带限制是硬约束，凑单超重的方案直接淘汰

        plans.append(BasketPlan(
            week=week,
            household_id=household_id,
            fills=tuple(fills),
            topups=tuple(all_topups),
            discounts=tuple(all_discounts),
            missing=(),
            gross_cents=gross,
            discount_cents=discount_total,
            total_grams=total_grams,
            shops=tuple(used_shops),
            pickups=len(used_shops),
            allow_alternatives=allow_alternatives,
        ))

    plans.sort(key=lambda p: (p.net_cents, p.total_grams))
    return plans


def best_plans(dataset: Dataset, week: str, household_id: str,
               need_ids: Optional[list[str]] = None, *,
               shops: Optional[list[str]] = None
               ) -> dict[str, Any]:
    """同时给出固定篮子与含许可替代的最优方案和缺项说明。"""
    need_ids = need_ids or [n['id'] for n in dataset.needs]
    fixed = shop_basket_plans(
        dataset, week, household_id, need_ids,
        allow_alternatives=False, shops=shops)
    flexible = shop_basket_plans(
        dataset, week, household_id, need_ids,
        allow_alternatives=True, shops=shops)

    household = dataset.household(household_id)
    reachable = feasible_shops(dataset, household, week, shops)
    missing_fixed = (_diagnose_missing(dataset, week, reachable, household,
                                       need_ids, False)
                     if not fixed else [])
    missing_flex = (_diagnose_missing(dataset, week, reachable, household,
                                      need_ids, True)
                    if not flexible else [])

    return {
        'week': week,
        'household': household_id,
        'shops_scoped': tuple(sorted(shops)) if shops else tuple(sorted(dataset.shops)),
        'fixed_best': fixed[0] if fixed else None,
        'flexible_best': flexible[0] if flexible else None,
        'fixed_feasible': bool(fixed),
        'flexible_feasible': bool(flexible),
        'missing': tuple(missing_flex or missing_fixed),
        'missing_fixed': tuple(missing_fixed),
        'missing_flexible': tuple(missing_flex),
        'all_fixed_plans': fixed,
        'all_flexible_plans': flexible,
    }
