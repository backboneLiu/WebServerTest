"""计价测试。

覆盖 README 三、必须通过的 16 条用例，外加附加题（批量重试语义）。
所有期望值都按 C# 的 decimal 语义手算过，不是「跑一遍看看输出多少」抄下来的。
"""

import asyncio
from datetime import datetime
from decimal import Decimal

import pytest

from pricing.core import (
    MAX_DISCOUNT_RATE,
    TIER_DISCOUNT,
    Line,
    Order,
    PricingError,
    Tier,
    calculate_batch,
    calculate_total,
    volume_discount,
)


def _order(tier: Tier, *lines: Line, order_id: str = "T-001") -> Order:
    return Order(order_id=order_id, tier=tier, placed_at=datetime(2026, 1, 1), lines=list(lines))


def test_case01_plain_order() -> None:
    """#1 普通单，无阶梯：1 × 100 = 100.00"""
    assert calculate_total(_order(Tier.NORMAL, Line("A", 1, Decimal("100")))) == Decimal("100.00")


def test_case01b_result_is_quantized_decimal() -> None:
    """金额必须落成 2 位小数的 Decimal，不是 float。"""
    total = calculate_total(_order(Tier.NORMAL, Line("A", 3, Decimal("33.335"))))
    assert isinstance(total, Decimal)
    assert str(total) == "100.01"


def test_case02_promotional_line() -> None:
    """#2 促销行五折：1 × 100 × 0.5 = 50.00"""
    line = Line("A", 1, Decimal("100"), is_promotional=True)
    assert calculate_total(_order(Tier.NORMAL, line)) == Decimal("50.00")


def test_case03_volume_tier_lower_boundary() -> None:
    """#3 阶梯边界·下：4999.99 未达 5000，无阶梯折扣。"""
    assert calculate_total(_order(Tier.NORMAL, Line("A", 1, Decimal("4999.99")))) == Decimal(
        "4999.99"
    )


def test_case04_volume_tier_upper_boundary() -> None:
    """#4 阶梯边界·上：5000 恰好命中 2% 档（C# 用 >=）。"""
    assert calculate_total(_order(Tier.NORMAL, Line("A", 1, Decimal("5000")))) == Decimal("4900.00")


def test_case05_volume_tier_2pct_crest() -> None:
    """#5 2% 档上边界：19999.99 × 0.98 = 19599.9902 → 19599.99"""
    assert calculate_total(_order(Tier.NORMAL, Line("A", 1, Decimal("19999.99")))) == Decimal(
        "19599.99"
    )


def test_case06_volume_tier_5pct() -> None:
    """#6 阶梯 5%：4 × 5000 = 20000 × 0.95 = 19000.00"""
    assert calculate_total(_order(Tier.NORMAL, Line("A", 4, Decimal("5000")))) == Decimal(
        "19000.00"
    )


def test_case07_volume_tier_10pct() -> None:
    """#7 阶梯 10%：10 × 5000 = 50000 × 0.90 = 45000.00"""
    assert calculate_total(_order(Tier.NORMAL, Line("A", 10, Decimal("5000")))) == Decimal(
        "45000.00"
    )


def test_case08_gold_stacks_with_volume_tier() -> None:
    """#8 会员 + 阶梯叠加：7% + 10% = 17% → 50000 × 0.83 = 41500.00"""
    assert calculate_total(_order(Tier.GOLD, Line("A", 10, Decimal("5000")))) == Decimal("41500.00")


def test_case09_platinum_stacks_with_volume_tier() -> None:
    """#9 最高档叠加：12% + 10% = 22% → 50000 × 0.78 = 39000.00"""
    assert calculate_total(_order(Tier.PLATINUM, Line("A", 10, Decimal("5000")))) == Decimal(
        "39000.00"
    )


def test_case10_multi_line_merged_subtotal_crosses_tier() -> None:
    """#10 多行先合并再判档：30000 + 25000 = 55000 → 10% → 49500.00"""
    total = calculate_total(
        _order(Tier.NORMAL, Line("A", 1, Decimal("30000")), Line("B", 1, Decimal("25000")))
    )
    assert total == Decimal("49500.00")


def test_case11_min_chargeable_floor() -> None:
    """#11 最低收费兜底：1 × 0.5 = 0.50 < 1.00 → 1.00"""
    assert calculate_total(_order(Tier.NORMAL, Line("A", 1, Decimal("0.5")))) == Decimal("1.00")


def test_case12_half_away_from_zero_rounding() -> None:
    """#12 分水岭：3 × 0.335 = 1.005，AwayFromZero 进位到 1.01（不是 1.00）。"""
    total = calculate_total(_order(Tier.NORMAL, Line("A", 3, Decimal("0.335"))))
    assert total == Decimal("1.01")
    assert str(total) == "1.01"


def test_case13_silver_with_small_volume_tier() -> None:
    """#13 会员 + 小阶梯：3% + 2% = 5% → 10000 × 0.95 = 9500.00"""
    assert calculate_total(_order(Tier.SILVER, Line("A", 1, Decimal("10000")))) == Decimal(
        "9500.00"
    )


def test_case14_empty_order_raises() -> None:
    """#14 空订单抛 PricingError"""
    with pytest.raises(PricingError, match="has no lines"):
        calculate_total(_order(Tier.NORMAL))


def test_case15_invalid_quantity_raises() -> None:
    """#15 数量非法（<= 0）抛 PricingError"""
    with pytest.raises(PricingError, match="Invalid quantity 0"):
        calculate_total(_order(Tier.NORMAL, Line("A", 0, Decimal("100"))))

    with pytest.raises(PricingError, match="Invalid quantity -2"):
        calculate_total(_order(Tier.NORMAL, Line("A", -2, Decimal("100"))))


def test_case16_negative_price_raises() -> None:
    """#16 价格为负抛 PricingError"""
    with pytest.raises(PricingError, match="Negative price"):
        calculate_total(_order(Tier.NORMAL, Line("A", 1, Decimal("-1"))))


def test_case17_max_discount_rate_is_currently_unreachable() -> None:
    """现状固化：单笔最高叠加是 12% + 10% = 22%，MAX_DISCOUNT_RATE=0.30 永远碰不到。

    这里不是断言「上限没用」，而是把观察写死：哪天商务侧真加了新档位（比如 Platinum
    提到 18%），这条会先红，逼人重算真实口径，而不是让 Math.Min 静默吃掉。
    按 README 要求，不修改这个常量。
    """
    best_stackable = TIER_DISCOUNT[Tier.PLATINUM] + volume_discount(Decimal("50000"))
    assert best_stackable == Decimal("0.22")
    assert best_stackable < MAX_DISCOUNT_RATE


def test_case18_batch_keeps_going_after_pricing_error() -> None:
    """批量里一条 PricingError 只记结果，不影响其余订单。"""
    notified: list[str] = []

    async def notify(order: Order) -> None:
        notified.append(order.order_id)

    orders = [
        _order(Tier.NORMAL, Line("A", 1, Decimal("100")), order_id="OK-1"),
        _order(Tier.NORMAL, order_id="EMPTY"),
        _order(Tier.NORMAL, Line("A", 1, Decimal("100")), order_id="OK-2"),
    ]

    results = asyncio.run(calculate_batch(orders, notify))

    assert [r.order_id for r in results] == ["OK-1", "EMPTY", "OK-2"]
    assert results[0].total == Decimal("100.00")
    assert results[1].total is None
    assert results[1].error is not None and "has no lines" in results[1].error
    assert results[2].total == Decimal("100.00")
    assert notified == ["OK-1", "OK-2"]


def test_case19_batch_third_non_pricing_failure_aborts_whole_batch() -> None:
    """附加题：连续 3 次非 PricingError 异常时，整个批量方法抛出，已算好的结果全丢。"""
    notified: list[str] = []
    bad_attempts = 0

    async def notify(order: Order) -> None:
        nonlocal bad_attempts
        if order.order_id == "BAD":
            bad_attempts += 1
            raise RuntimeError("ERP 网关超时")
        notified.append(order.order_id)

    orders = [
        _order(Tier.NORMAL, Line("A", 1, Decimal("100")), order_id="OK-1"),
        _order(Tier.NORMAL, Line("A", 1, Decimal("100")), order_id="BAD"),
        _order(Tier.NORMAL, Line("A", 1, Decimal("100")), order_id="OK-2"),
    ]

    with pytest.raises(RuntimeError, match="ERP 网关超时"):
        asyncio.run(calculate_batch(orders, notify, max_retry=3))

    # maxRetry=3 → 只尝试 3 次（第 3 次过滤条件不成立，原样抛出）
    assert bad_attempts == 3
    # OK-1 已成功但结果随异常一起丢弃；OK-2 根本没被处理
    assert notified == ["OK-1"]
