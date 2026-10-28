"""客户订单计价逻辑的 Python 等价实现。

对外只暴露这一层，调用方不需要知道 core 里怎么算的。
"""

from pricing.core import (
    MAX_DISCOUNT_RATE,
    MIN_CHARGEABLE,
    PROMOTIONAL_FACTOR,
    TIER_DISCOUNT,
    BatchResult,
    Line,
    Order,
    PricingBreakdown,
    PricingError,
    Tier,
    calculate_batch,
    calculate_breakdown,
    calculate_total,
    volume_discount,
)

__all__ = [
    "MAX_DISCOUNT_RATE",
    "MIN_CHARGEABLE",
    "PROMOTIONAL_FACTOR",
    "TIER_DISCOUNT",
    "BatchResult",
    "Line",
    "Order",
    "PricingBreakdown",
    "PricingError",
    "Tier",
    "calculate_batch",
    "calculate_breakdown",
    "calculate_total",
    "volume_discount",
]
