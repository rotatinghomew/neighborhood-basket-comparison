"""价格篮子引擎的业务规则测试。

测试分两层：
- 直接基于 samples/ 合成记录的端到端断言（手算可复核的数字）；
- 在 tests/fixtures 中构造最小场景，专门验证会员、窗口、凑单、
  携带限制等边界，避免污染样本资料。
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from basket.data import load_dataset
from basket.planner import (
    best_plans,
    shop_basket_plans,
    usable_quote,
    feasible_shops,
)
from basket.compare import (
    need_comparability,
    district_basket_comparison,
    basket_cost,
)
from basket.aid import (
    coupon_ledger,
    active_coupons,
    used_budget,
    reserved_budget,
    promo_pool_remaining,
    evaluate_claim,
    evaluate_all_claims,
    district_cap_update,
)
from basket.corrections import correction_impacts, editor_triage
from basket.briefing import build_weekly_briefing

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / 'samples'
NEED_IDS = ['主粮', '配餐粮', '蔬菜', '豆制品']
CLAIM_WEEK = '2026-08-31'


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset(SAMPLES)

    def test_shangjie_single_trip_north_shop_bundle_math(self):
        """北口固定篮：粳米1+小米1+白菜2斤+豆腐2盒，满减 160。"""
        plans = shop_basket_plans(
            self.ds, CLAIM_WEEK, '单次取货',
            allow_alternatives=False, shops=['北口菜摊'])
        plan = plans[0]
        # 230+230+960+460 = 1880 毛额，减 160 = 1720 净额
        self.assertEqual(plan.gross_cents, 1880)
        self.assertEqual(plan.discount_cents, 160)
        self.assertEqual(plan.net_cents, 1720)
        self.assertEqual(plan.total_grams, 3100)
        self.assertEqual(plan.pickups, 1)
        # 自然采购量已达折扣门槛，不应产生凑单
        self.assertEqual(plan.topups, ())

    def test_step_rounding_overage_is_real_money_and_weight(self):
        """750g 白菜、500g/斤、step=2：必须买 2 斤=1000g，多出的 250g 计价计重。"""
        plans = shop_basket_plans(
            self.ds, CLAIM_WEEK, '单次取货',
            allow_alternatives=False, shops=['南口菜摊'])
        veg = next(f for f in plans[0].fills if f.need_id == '蔬菜')
        self.assertEqual((veg.unit_count, veg.grams), (2, 1000))
        self.assertEqual(veg.cost_cents, 700)

    def test_same_item_cannot_fill_two_needs(self):
        """河街粳米同时是主粮与配餐粮候选，但一笔采购不能共用。"""
        outcome = best_plans(
            self.ds, CLAIM_WEEK, '单次取货',
            shops=self.ds.district_shops('河街'))
        self.assertFalse(outcome['fixed_feasible'])
        self.assertFalse(outcome['flexible_feasible'])
        missing_ids = {m.need_id for m in outcome['missing_flexible']}
        self.assertEqual(missing_ids, set(NEED_IDS))
        conflict = next(m for m in outcome['missing_flexible']
                        if m.need_id == '配餐粮')
        self.assertIn('商品需同时填两个需求，一笔采购不可共用',
                      conflict.primary_reason())

    def test_missing_quote_never_becomes_zero(self):
        """未采价与缺货都不产生价格，缺值保持 None。"""
        hh = self.ds.household('单次取货')
        quote, reason = usable_quote(
            self.ds, '2026-08-31', '河沿市场', '豆腐', hh)
        self.assertIsNone(quote)
        self.assertEqual(reason, '未采价')
        raw = self.ds.quote_index()[('2026-08-31', '河沿市场', '豆腐')]
        self.assertIsNone(raw['price_cents'])

    def test_membership_gate_blocks_non_member(self):
        """会员价对非会员采购户不可成交，会员户可成交。"""
        non_member = self.ds.household('单次取货')
        member = self.ds.household('分两次取货')
        # 河沿市场是分两次取货户的会员店；07-06 嫩豆腐为有货会员价
        quote, reason = usable_quote(
            self.ds, '2026-07-06', '河沿市场', '嫩豆腐', non_member)
        self.assertIsNone(quote)
        self.assertEqual(reason, '会员限制')
        quote, _ = usable_quote(
            self.ds, '2026-07-06', '河沿市场', '嫩豆腐', member)
        self.assertIsNotNone(quote)

    def test_alternative_requires_need_level_permission(self):
        """替代只能走需求登记；规划器不会自行发明替代关系。"""
        # 蔬菜只登记了娃娃菜作替代；豆腐与嫩豆腐的关系挂在豆制品需求上
        need = self.ds.need('蔬菜')
        self.assertEqual(need['alternatives'], ['娃娃菜'])
        outcome = best_plans(
            self.ds, CLAIM_WEEK, '单次取货',
            shops=['北口菜摊'])
        # 北口固定品全部有货，固定与替代最优解相同且都是固定品
        self.assertTrue(outcome['fixed_feasible'])
        self.assertTrue(all(f.kind == '固定'
                            for f in outcome['flexible_best'].fills))

    def test_carry_limit_and_pickup_count_are_hard_constraints(self):
        """全部方案都必须满足 max_grams 与 max_shops。"""
        for week in {q['week'] for q in self.ds.quotes}:
            for hh_id in ('单次取货', '分两次取货'):
                hh = self.ds.household(hh_id)
                plans = shop_basket_plans(
                    self.ds, week, hh_id, allow_alternatives=True)
                for plan in plans:
                    self.assertLessEqual(plan.total_grams, hh['max_grams'])
                    self.assertLessEqual(len(plan.shops), hh['max_shops'])

    def test_expired_discount_not_carried_into_later_window(self):
        """最早到店时刻晚于折扣 end 时，不沿用旧优惠，按原价核算。"""
        import dataclasses
        base = self.ds.household('单次取货')
        late = dict(base)
        late['id'] = '晚出发'
        late['window'] = [600, 700]  # 最早 608 到店，折扣 590 已结束
        ds_late = dataclasses.replace(
            self.ds, households=self.ds.households + (late,))
        plans = shop_basket_plans(
            ds_late, CLAIM_WEEK, '晚出发',
            allow_alternatives=False, shops=['北口菜摊'])
        plan = plans[0]
        self.assertEqual(plan.discount_cents, 0)
        self.assertEqual(plan.net_cents, 1880)


class ComparabilityTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset(SAMPLES)
        self.hh = self.ds.household('单次取货')

    def test_three_level_classification(self):
        shops = self.ds.district_shops('河街')
        # 配餐粮固定品小米在河街长期无有效观测 -> 无法比较
        row = need_comparability(
            self.ds, '2026-08-17', CLAIM_WEEK, shops,
            self.ds.need('配餐粮'), self.hh)
        self.assertEqual(row['level'], '无法比较')

    def test_correction_downgrades_fixed_to_interval(self):
        """更正后白菜克重跨周不一致，固定可比降级为区间比较。"""
        before = load_dataset(SAMPLES, as_of='2026-08-30')
        shops = self.ds.district_shops('上街')
        cmp_before = district_basket_comparison(
            before, before.aid['baseline_weeks'], CLAIM_WEEK,
            '上街', before.household('单次取货'), NEED_IDS)
        cmp_after = district_basket_comparison(
            self.ds, self.ds.aid['baseline_weeks'], CLAIM_WEEK,
            '上街', self.hh, NEED_IDS)
        self.assertEqual(cmp_before['overall_level'], '固定可比')
        self.assertEqual(cmp_after['overall_level'], '区间比较')
        self.assertFalse(cmp_after['usable_for_cap'])

    def test_basket_cost_excludes_shops_missing_any_fixed_item(self):
        """任一固定品缺观测的店不产生整篮成本，绝不补零。"""
        shops = self.ds.district_shops('河街')
        costs = basket_cost(self.ds, CLAIM_WEEK, shops, self.hh, NEED_IDS)
        self.assertEqual(costs, [])

    def test_no_cheapest_alternative_used_for_price_change(self):
        """补贴口径只看固定篮子，替代价再低也不进入 change_cents。"""
        cmp = district_basket_comparison(
            self.ds, self.ds.aid['baseline_weeks'], CLAIM_WEEK,
            '上街', self.hh, NEED_IDS)
        # 更正后上街只能区间比较，change 必须为 None
        self.assertIsNone(cmp['change_cents'])


class AidTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset(SAMPLES)
        self.hh = self.ds.household('单次取货')

    def test_cap_held_when_not_comparable_with_reason(self):
        cap = district_cap_update(
            self.ds, CLAIM_WEEK, '河街', self.hh, NEED_IDS)
        self.assertEqual(cap['recommended_cap_cents'], 1300)
        self.assertFalse(cap['changed'])
        self.assertIn('沿用旧上限', cap['reason'])

    def test_cap_unchanged_when_fixed_change_is_zero(self):
        before = load_dataset(SAMPLES, as_of='2026-08-30')
        cap = district_cap_update(
            before, CLAIM_WEEK, '上街',
            before.household('单次取货'), NEED_IDS)
        self.assertTrue(cap['comparison']['usable_for_cap'])
        self.assertEqual(cap['recommended_cap_cents'], 1500)
        self.assertEqual(round(cap['raw_change_cents']), 0)

    def test_weekly_change_clamp(self):
        """单周调整封顶：构造远超封顶的涨幅也只能加 max_weekly_change_cents。"""
        ds = load_dataset(SAMPLES, as_of='2026-08-30')
        # 把当前周上街两家店全部有效报价抬高，保证片区最小整篮成本大涨
        for q in ds.quotes:
            if q['week'] == CLAIM_WEEK and q['shop'] in ('北口菜摊', '南口菜摊') \
                    and q['price_cents'] is not None:
                q['price_cents'] += 400
        cap = district_cap_update(
            ds, CLAIM_WEEK, '上街',
            ds.household('单次取货'), NEED_IDS)
        self.assertGreater(round(cap['raw_change_cents']),
                           ds.aid['max_weekly_change_cents'])
        self.assertEqual(cap['applied_change_cents'],
                         ds.aid['max_weekly_change_cents'])
        self.assertEqual(cap['recommended_cap_cents'],
                         1500 + ds.aid['max_weekly_change_cents'])

    def test_promo_pool_is_shared_across_claims(self):
        """同一组促销份数不能给两户各开一张够用的券。"""
        pool = promo_pool_remaining(self.ds, CLAIM_WEEK, '2026-09-02')
        self.assertEqual(pool[(CLAIM_WEEK, '北口菜摊')], 1)
        results = evaluate_all_claims(
            self.ds, '2026-09-02', pool=dict(pool))
        promo_users = [r['claim'] for r in results if r['promo_shop']]
        self.assertEqual(promo_users, ['申请0'])
        blocked = next(r for r in results if r['claim'] == '申请1')
        self.assertTrue(blocked['promo_blocked'])
        # 被挡住的券按无折扣毛额核算，不把优惠重复计入
        self.assertEqual(blocked['plan']['gross_cents'], 1880)

    def test_claim_without_feasible_basket_gets_no_full_coupon(self):
        """河街买不齐：不发足额券、不用未获准替代凑账面，列缺项。"""
        claim = next(c for c in self.ds.claims if c['id'] == '申请5')
        ev = evaluate_claim(self.ds, claim, '2026-08-31')
        self.assertFalse(ev.feasible)
        self.assertEqual(ev.coupon_cents, 0)
        self.assertTrue(ev.missing)

    def test_coupon_is_min_of_cost_and_cap(self):
        claim = next(c for c in self.ds.claims if c['id'] == '申请1')
        ev = evaluate_claim(self.ds, claim, '2026-09-02',
                            pool={(CLAIM_WEEK, '北口菜摊'): 0})
        self.assertEqual(ev.coupon_cents, min(1500, ev.plan.gross_cents))
        self.assertEqual(ev.self_pay_cents,
                         ev.plan.gross_cents - ev.coupon_cents)

    def test_redemption_lifecycle_budget_rules(self):
        # 08-31：券甲已送达未核销（在途），券乙已核销（已用），券丙拟发（在途）
        self.assertEqual(used_budget(self.ds, '2026-08-31'), 1100)
        self.assertEqual(reserved_budget(self.ds, '2026-08-31'), 2400)
        active = {c['id'] for c in active_coupons(self.ds, '2026-08-31')}
        self.assertEqual(active, {'券甲', '券乙', '券丙'})
        # 09-02 券甲取消：预算与促销份数释放，历史事件仍可查
        self.assertEqual(reserved_budget(self.ds, '2026-09-02'), 1200)
        ledger = coupon_ledger(self.ds, '2026-09-02')
        self.assertEqual(ledger['券甲']['kind'], '已取消')
        self.assertNotIn('券甲',
                         {c['id'] for c in active_coupons(self.ds, '2026-09-02')})


class CorrectionTests(unittest.TestCase):
    def test_correction_changes_lots_and_comparability(self):
        impacts = correction_impacts(SAMPLES)
        self.assertEqual(len(impacts), 1)
        impact = impacts[0]
        self.assertTrue(impact['units_changed'])
        self.assertEqual(impact['purchase_units_before']['units'], 2)
        self.assertEqual(impact['purchase_units_after']['units'], 4)
        self.assertEqual(impact['comparability_before'], '固定可比')
        self.assertEqual(impact['comparability_after'], '区间比较')

    def test_unissued_coupons_flagged_for_recheck(self):
        impact = correction_impacts(SAMPLES)[0]
        ids = {c['coupon']: c for c in impact['coupons_to_recheck']}
        self.assertIn('券丙', ids)
        self.assertTrue(ids['券丙']['must_recheck'])
        # 已核销的券乙不在重核清单（履约事实不动）
        self.assertNotIn('券乙', ids)

    def test_editor_triage_separates_three_concerns(self):
        triage = editor_triage(SAMPLES)
        kinds = {item['kind'] for item in triage['coverage_issues']}
        self.assertIn('固定篮子覆盖不足', kinds)
        self.assertEqual(triage['cap_changes'], [])
        expired_kinds = {item['kind'] for item in triage['advice_expired']}
        self.assertIn('购物建议因换算更正不可用', expired_kinds)
        self.assertIn('购物建议因促销过期不可用', expired_kinds)

    def test_historical_briefing_records_preserved(self):
        impact = correction_impacts(SAMPLES)[0]
        self.assertTrue(impact['historical_briefings_preserved'])


class BriefingTests(unittest.TestCase):
    def test_briefing_layers_and_budget(self):
        briefing = build_weekly_briefing(SAMPLES)
        self.assertEqual(briefing['briefing_week'], CLAIM_WEEK)
        by_district = {d['district']: d for d in briefing['districts']}
        shangjie = by_district['上街']
        self.assertEqual(len(shangjie['comparable_fixed_layer']), 3)
        self.assertEqual(len(shangjie['interval_only_layer']), 1)
        # 证据行保留 None 缺值，不补零
        self.assertTrue(
            any(row['price_cents'] is None
                for row in shangjie['evidence']))
        budget = briefing['coupon_budget']
        self.assertEqual(
            budget['free_cents'],
            budget['total_cents'] - budget['used_cents']
            - budget['reserved_in_flight_cents'])

    def test_briefing_is_json_serializable(self):
        briefing = build_weekly_briefing(SAMPLES)
        blob = json.dumps(briefing, ensure_ascii=False, default=str)
        self.assertIn('上街', blob)


if __name__ == '__main__':
    unittest.main()
