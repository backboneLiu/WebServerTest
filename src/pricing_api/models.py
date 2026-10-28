"""HTTP 契约的 Pydantic 模型。

三个刻意的设计（每一条都和契约文档对应）：

1. **金额只收字符串**。`"5000"` → `Decimal("5000")`；传 JSON 数字直接 422。
   因为 JSON number 在到达我们之前已经过 float，`41500.00` 可能已经变成 `41500`，
   这时候"帮它转一下"只是把错误洗白。金融接口宁可拒收，也不能静默精度损失。

2. **模型层不校验业务规则**。`quantity <= 0`、负价、空 lines 都放行到计价库，
   由 `PricingError` 统一变成 400。若在这里加 `ge=0`/`min_length=1`，FastAPI 会先返回
   422，语义就从"这单算不了"变成"你 JSON 写错了"，经销商排查方向就跑偏了。

3. **camelCase 用 alias**。对外是 `orderId`/`unitPrice`，对内是 `order_id`/`unit_price`，
   靠 Pydantic 的 alias 桥接，而不是把 Python 字段命名搞成驼峰。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from pricing.core import Line, Order, Tier


def _parse_money(value: object) -> Decimal:
    """把对外金额（字符串）解析成 Decimal，并拒绝一切"已经丢过精度"的输入。"""
    if isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"金额必须是十进制数字符串，收到 {value!r}") from exc
        if not amount.is_finite():
            raise ValueError("金额不能是 NaN / Infinity")
        return amount
    raise ValueError(
        '金额必须以字符串传入，例如 "5000"：JSON 数字会先被解析成 float 而丢失精度，接口拒绝接收'
    )


def _non_blank(value: str) -> str:
    """拒绝纯空白字符串：`" "` 能过 min_length=1，但对账时毫无意义。"""
    if not value.strip():
        raise ValueError("不能为空白")
    return value.strip()


Money = Annotated[Decimal, BeforeValidator(_parse_money)]
NonBlankStr = Annotated[str, AfterValidator(_non_blank)]
StrictInt = Annotated[int, Field(strict=True)]


class TierName(StrEnum):
    """对外枚举。值与 C# 的 CustomerTier 完全一致，直接能映射回核心库。"""

    NORMAL = "Normal"
    SILVER = "Silver"
    GOLD = "Gold"
    PLATINUM = "Platinum"

    def to_core(self) -> Tier:
        return Tier(self.value)

    def __str__(self) -> str:
        return self.value


class QuoteLineIn(BaseModel):
    """单行。`quantity` 与 `unitPrice` 的正负一律不在这里判。"""

    model_config = ConfigDict(extra="forbid")

    sku: NonBlankStr = Field(max_length=64)
    quantity: StrictInt
    unit_price: Money = Field(alias="unitPrice")
    is_promotional: bool = Field(default=False, alias="isPromotional")


class QuoteRequest(BaseModel):
    """报价请求。`lines` 允许为空数组——空单是业务错误（400），不是格式错误。"""

    model_config = ConfigDict(extra="forbid")

    order_id: NonBlankStr = Field(alias="orderId", max_length=64)
    tier: TierName
    lines: list[QuoteLineIn]

    def to_order(self) -> Order:
        """翻译成核心库的 `Order`。

        `placed_at` 在 C# 的 CalculateTotal 里不参与任何计算（只用于展示/排序），
        这里填 now() 只为满足数据结构；不要让它进折扣判断。
        """
        return Order(
            order_id=self.order_id,
            tier=self.tier.to_core(),
            placed_at=datetime.now(UTC),
            lines=[
                Line(
                    sku=line.sku,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    is_promotional=line.is_promotional,
                )
                for line in self.lines
            ],
        )


class QuoteResponse(BaseModel):
    """成功响应。`total` 是字符串，2 位小数。"""

    model_config = ConfigDict(populate_by_name=True)

    order_id: str = Field(serialization_alias="orderId")
    total: str
    trace_id: str = Field(serialization_alias="traceId")


class HealthResponse(BaseModel):
    status: str
    version: str
