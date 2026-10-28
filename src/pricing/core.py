"""订单计价 —— 客户 C# 版本（legacy/OrderPricing.cs）的 Python 等价实现。

规则以 C# 源码为唯一权威。任何「我觉得这样更合理」的改动都是 bug。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, localcontext
from enum import Enum


class Tier(Enum):
    NORMAL = "Normal"
    SILVER = "Silver"
    GOLD = "Gold"
    PLATINUM = "Platinum"


class PricingError(Exception):
    """计价规则不满足（对应 C# 的 PricingException）。"""


@dataclass
class Line:
    sku: str
    quantity: int
    unit_price: Decimal
    is_promotional: bool = False


@dataclass
class Order:
    order_id: str
    tier: Tier
    placed_at: datetime
    lines: list[Line] = field(default_factory=list)


@dataclass
class BatchResult:
    order_id: str
    total: Decimal | None
    error: str | None


@dataclass(frozen=True)
class PricingBreakdown:
    """一次计价的完整中间结果。

    存在的理由：审计要记 subtotal，但 calculate_total 只返回 total。
    让 API 层自己再算一遍小计 = 两套计价逻辑，早晚漂移；
    所以把中间结果升成一个值对象，calculate_total 退化成它的薄包装。
    """

    subtotal: Decimal
    tier_rate: Decimal
    volume_rate: Decimal
    total_rate: Decimal
    total: Decimal


# 常量已按 C# 原样给出，不要改数值
TIER_DISCOUNT: dict[Tier, Decimal] = {
    Tier.NORMAL: Decimal("0"),
    Tier.SILVER: Decimal("0.03"),
    Tier.GOLD: Decimal("0.07"),
    Tier.PLATINUM: Decimal("0.12"),
}
PROMOTIONAL_FACTOR = Decimal("0.5")
MAX_DISCOUNT_RATE = Decimal("0.30")
MIN_CHARGEABLE = Decimal("1.00")

# C# decimal 是 28~29 位有效数字 + 银行家舍入；这里显式钉住，避免调用方改动了
# 线程本地的 decimal context 后，计价结果随之漂移。
_DECIMAL_PRECISION = 28
_CENT = Decimal("0.01")
_ONE = Decimal("1")


def volume_discount(subtotal: Decimal) -> Decimal:
    """阶梯折扣。对应 C# 的 VolumeDiscount。

    边界按 >= 判断，与 C# 一致。
    """
    if subtotal >= Decimal("50000"):
        return Decimal("0.10")
    if subtotal >= Decimal("20000"):
        return Decimal("0.05")
    if subtotal >= Decimal("5000"):
        return Decimal("0.02")
    return Decimal("0")


def _round_money(value: Decimal) -> Decimal:
    """保留 2 位小数，等价于 C# 的 Math.Round(value, 2, AwayFromZero)。

    decimal 的 ROUND_HALF_UP 语义是「四舍五入，中点远离 0」，与 C# 的
    MidpointRounding.AwayFromZero 相同；Python 内置 round() 是银行家舍入，不能用。
    """
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def calculate_total(order: Order) -> Decimal:
    """计算订单应付金额。对应 C# 的 CalculateTotal。

    注意：舍入策略、最低收费、异常条件都必须与 C# 完全一致。
    """
    return calculate_breakdown(order).total


def calculate_breakdown(order: Order) -> PricingBreakdown:
    """计算并返回含中间倍率的计价明细。

    逻辑与 C# 的 CalculateTotal 逐行对应，只是把中间变量一并返回；
    任何一步的数值、顺序、舍入都不能改，否则 TASK-01 的 20 条等价性测试会红。
    """
    if not order.lines:
        # C# 对 Lines 为 null 和空列表都抛 PricingException，这里一并覆盖。
        raise PricingError(f"Order {order.order_id} has no lines")

    with localcontext() as ctx:
        # 全程 Decimal 精确算术，任何一步都不经过 float。
        ctx.prec = _DECIMAL_PRECISION
        ctx.rounding = ROUND_HALF_EVEN

        subtotal = Decimal("0")
        for line in order.lines:
            if line.quantity <= 0:
                raise PricingError(f"Invalid quantity {line.quantity} for SKU {line.sku}")
            if line.unit_price < 0:
                raise PricingError(f"Negative price for SKU {line.sku}")

            line_total = line.quantity * line.unit_price
            if line.is_promotional:
                line_total *= PROMOTIONAL_FACTOR
            subtotal += line_total

        tier_rate = TIER_DISCOUNT[order.tier]
        volume_rate = volume_discount(subtotal)
        total_rate = min(tier_rate + volume_rate, MAX_DISCOUNT_RATE)

        total = subtotal * (_ONE - total_rate)
        rounded = _round_money(total)
        return PricingBreakdown(
            subtotal=subtotal,
            tier_rate=tier_rate,
            volume_rate=volume_rate,
            total_rate=total_rate,
            total=max(rounded, MIN_CHARGEABLE),
        )


async def calculate_batch(
    orders: Iterable[Order],
    notify: Callable[[Order], Awaitable[None]],
    max_retry: int = 3,
) -> list[BatchResult]:
    """批量计价。对应 C# 的 CalculateBatchAsync。

    一条失败不影响其余：PricingError 直接记进结果，其它异常最多重试 max_retry 次。
    注意：重试次数用尽后异常会向上冒泡，整个批量调用失败——这是 C# 的原行为，
    测试中已固定下来（见 tests/test_pricing.py 的附加题用例）。
    """
    results: list[BatchResult] = []
    for order in orders:
        attempt = 0
        while True:
            try:
                total = calculate_total(order)
                await notify(order)
                results.append(BatchResult(order.order_id, total, None))
                break
            except PricingError as exc:
                results.append(BatchResult(order.order_id, None, str(exc)))
                break
            except Exception:
                # 对应 C# 的 when (attempt < maxRetry - 1)：过滤不成立时原样抛出。
                if attempt >= max_retry - 1:
                    raise
                attempt += 1
                await asyncio.sleep(0.2 * attempt)
    return results
