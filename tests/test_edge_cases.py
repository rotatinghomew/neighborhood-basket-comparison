"""基于最小合成夹具的边界规则测试（与 samples 完全隔离）。

夹具场景（tests/fixtures/edge_cases）：
- 南口粳米是会员价，非会员被挡；
- 北口折扣门槛高于自然采购量，需要凑单；
- 单次取货携带上限 3500g：基础篮 3100g 可过，凑单后 4400g 超重；
- 会员户（分两次取货）上限 5000g，可容纳凑单。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from basket.data import load_dataset
from basket.planner import shop_basket_plans, usable_quote

FIXTURE = ROOT / 'tests' / 'fixtures' / 'edge_cases'
NEED_IDS = ['主粮', '配餐粮', '蔬菜', '豆制品']


class EdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.ds = load_dataset(FIXTURE)

    def test_membership_gate(self):
        non_member = self.ds.household('单次取货')
        member = self.ds.household('分两次取货')
        self.assertEqual(
            usable_quote(self.ds, '2026-08-31', '南口菜摊', '粳米', non_member),
            (None, '会员限制'))
        quote, reason = usable_quote(
            self.ds, '2026-08-31', '南口菜摊', '粳米', member)
        self.assertIsNotNone(quote)
        self.assertIsNone(reason)

    def test_topup_overweight_is_rejected(self):
        """凑单后超重：折扣再深也不能突破携带限制。"""
        plans = shop_basket_plans(
            self.ds, '2026-08-31', '单次取货',
            need_ids=NEED_IDS, allow_alternatives=False,
            shops=['北口菜摊'])
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.topups, ())
        self.assertEqual(plan.discount_cents, 0)
        self.assertEqual(plan.total_grams, 3100)

    def test_profitable_topup_counted_for_member_household(self):
        """会员户容量足够时，凑单的额外支出与重量如实计入，净支出下降才采纳。"""
        plans = shop_basket_plans(
            self.ds, '2026-08-31', '分两次取货',
            need_ids=NEED_IDS, allow_alternatives=False,
            shops=['北口菜摊'])
        plan = plans[0]
        # 凑单：粳米 +1 袋(1000g/100分)、豆腐 +1 盒(300g/50分)
        topups = {(t.item, t.unit_count) for t in plan.topups}
        self.assertEqual(topups, {('粳米', 1), ('豆腐', 1)})
        self.assertEqual(plan.total_grams, 4400)
        self.assertEqual(plan.gross_cents, 650)
        self.assertEqual(plan.discount_cents, 300)
        self.assertEqual(plan.net_cents, 350)

    def test_unprofitable_discount_not_forced(self):
        """把优惠调到小于凑单成本时，优化器保留无折扣方案。"""
        ds = load_dataset(FIXTURE)
        for d in ds.discounts:
            d['off_cents'] = 100  # 凑单花 150，只省 100
        plans = shop_basket_plans(
            ds, '2026-08-31', '分两次取货',
            need_ids=NEED_IDS, allow_alternatives=False,
            shops=['北口菜摊'])
        plan = plans[0]
        self.assertEqual(plan.discount_cents, 0)
        self.assertEqual(plan.net_cents, 500)
        self.assertEqual(plan.topups, ())


if __name__ == '__main__':
    unittest.main()
