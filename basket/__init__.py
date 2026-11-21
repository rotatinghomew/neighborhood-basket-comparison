"""邻里商圈价格篮子分析引擎。

模块划分：
- data：原始资料装载与更正应用，区分观测缺值与零
- planner：按采购窗口、商户范围、会员与携带限制求可行采购方案
- compare：固定篮子、区间可比与无法比较的三级分类
- aid：补贴上限、菜券试算、促销兑换池与核销台账
- corrections：更正影响与编辑分诊
- briefing：每周公开简报
"""

from .data import Dataset, load_dataset, apply_corrections
from .planner import (
    Fill,
    BasketPlan,
    feasible_shops,
    shop_basket_plans,
    best_plans,
    MissingNeed,
)
from .compare import need_comparability, district_basket_comparison, basket_cost
from .aid import (
    coupon_ledger,
    promo_pool_remaining,
    evaluate_claim,
    district_cap_update,
)
from .corrections import correction_impacts, editor_triage
from .briefing import build_weekly_briefing

__all__ = [
    'Dataset',
    'load_dataset',
    'apply_corrections',
    'Fill',
    'BasketPlan',
    'feasible_shops',
    'shop_basket_plans',
    'best_plans',
    'MissingNeed',
    'need_comparability',
    'district_basket_comparison',
    'basket_cost',
    'coupon_ledger',
    'promo_pool_remaining',
    'evaluate_claim',
    'district_cap_update',
    'correction_impacts',
    'editor_triage',
    'build_weekly_briefing',
]
