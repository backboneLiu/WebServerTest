# 报价服务 API 契约 v1

> **本文件是对外承诺。** 一旦发给经销商，任何字段名、状态码、错误结构的改动都算破坏性变更。
> 实现见 `src/pricing_api/`，测试见 `tests/test_api.py` —— 契约与测试必须同步，测试是契约的可执行形式。

---

## 0. 通用约定

| 项 | 约定 |
|---|---|
| Base URL | `https://<host>`（本地 `http://127.0.0.1:8000`） |
| 传输 | HTTPS（生产强制）、JSON（UTF-8） |
| 鉴权 | `Authorization: Bearer <JWT>`，算法 HS256 |
| 请求追踪 | 请求可带 `X-Request-Id`；**所有响应**都会回显 `X-Request-Id` |
| 版本 | 路径版本 `/v1`；破坏性改动只会在 `/v2` 出现 |

### 0.1 金额表示（金融接口硬约定）

**所有金额字段在请求和响应里都是字符串，不是 JSON 数字。**

```
✅ "unitPrice": "5000",   "total": "41500.00"
❌ "unitPrice": 5000,     "total": 41500.00
```

原因：JSON 数字经由 JavaScript/Go/Java 的 `float64` 解析会丢精度，`41500.00` 可能变成 `41500`、`0.1+0.2` 类误差会直接进账。
因此请求里传数字会被 **422 拒绝**，错误信息会提示改成字符串，而不是"帮你猜一下"。

- 字符串格式：可选符号 + 十进制数，例如 `"5000"`、`"4999.99"`、`"0"`。
- 不接受 `NaN` / `Infinity` / 科学计数法之外的其它表示。
- 小数位数不限制（业务上允许 3 位成本价），**返回的 `total` 永远是 2 位小数**。

### 0.2 请求追踪

| 方向 | 头 | 说明 |
|---|---|---|
| 请求 → 服务 | `X-Request-Id` | 可选。若提供，服务沿用它；若缺失或格式非法，服务生成 UUID 并回显 |
| 服务 → 响应 | `X-Request-Id` | **总是**返回，与响应体 `traceId` 完全一致 |

合法值：1–64 个字符，仅 `A-Z a-z 0-9 _ - .`。其它值（含超长、含空格）会被**丢弃并重新生成**，防止日志注入。

---

## 1. `GET /health`

存活探针。**不需要认证**（否则负载均衡/编排系统探不到）。

**200 OK**

```json
{"status": "ok", "version": "0.2.0"}
```

不查数据库、不查下游 —— 探针只回答"进程还在吗"，不回答"业务好着吗"，否则数据库抖动会导致容器被反复重启。

---

## 2. `POST /v1/quote`

计算一笔订单报价，并把结果写入审计表。

**需要认证。** `Content-Type: application/json`。

### 2.1 请求体

```json
{
  "orderId": "SO-20261027-001",
  "tier": "Gold",
  "lines": [
    {"sku": "A-100", "quantity": 10, "unitPrice": "5000", "isPromotional": false}
  ]
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `orderId` | string | ✅ | 1–64 字符，非空白。客户侧订单号，用于对账 |
| `tier` | enum | ✅ | `Normal` / `Silver` / `Gold` / `Platinum`，大小写敏感 |
| `lines` | array | ✅ | **可以是空数组**（空单属于业务错误 → 400，不是格式错误 → 422） |
| `lines[].sku` | string | ✅ | 1–64 字符 |
| `lines[].quantity` | integer | ✅ | **不在此处校验正负**（≤0 属于业务错误 → 400） |
| `lines[].unitPrice` | string | ✅ | 金额字符串；**负数不在此处校验**（→ 400） |
| `lines[].isPromotional` | boolean | ❌ | 默认 `false` |

> 设计原则：**422 = 语法/类型错（调用方写错了 JSON），400 = 语义/业务错（JSON 合法但这单算不了）。**
> 这个分界让经销商能自助定位问题：422 改代码，400 改数据。

### 2.2 响应 200

```json
{"orderId": "SO-20261027-001", "total": "41500.00", "traceId": "8f3c1d9e4b7a4c2f9b0e5d6a1c3f7e2b"}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `orderId` | string | 原样回显 |
| `total` | string | 应付金额，**2 位小数**，与 ERP 逐分一致 |
| `traceId` | string | 与 `X-Request-Id` 响应头相同 |

### 2.3 错误

所有错误响应都是 `{"detail": <string 或数组>}`，并带 `X-Request-Id`。

| 场景 | 状态码 | `detail` |
|---|---|---|
| 缺 token / token 过期 / 签名错 / 格式错 | `401` | `"missing bearer token"` / `"token expired"` / `"invalid token"`；带 `WWW-Authenticate: Bearer` |
| `orderId` 缺失、`tier` 非法、`quantity` 非整数、`unitPrice` 传了数字、JSON 语法错 | `422` | 字段级错误数组（FastAPI 标准结构） |
| `lines` 为空 | `400` | `"Order SO-xxx has no lines"` |
| 某行 `quantity <= 0` | `400` | `"Invalid quantity 0 for SKU A-100"` |
| 某行 `unitPrice < 0` | `400` | `"Negative price for SKU A-100"` |
| 服务内部错误 | `500` | `"internal error"`（**不含堆栈**；排查靠 `X-Request-Id` 查日志） |

`400` 直接透传计价库 `PricingError` 的原文 —— 因为这句话就是给经销商看的，措辞不允许二次包装。

### 2.4 示例

```bash
# 无 token → 401
curl -i -X POST https://<host>/v1/quote \
  -H 'content-type: application/json' \
  -d '{"orderId":"SO-1","tier":"Gold","lines":[]}'

# 带 token → README 第 8 条：Gold，10 × 5000
curl -i -X POST https://<host>/v1/quote \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -H 'X-Request-Id: demo-0001' \
  -d '{"orderId":"SO-20261027-001","tier":"Gold","lines":[{"sku":"A-100","quantity":10,"unitPrice":"5000"}]}'
# → 200  {"orderId":"SO-20261027-001","total":"41500.00","traceId":"demo-0001"}
```

---

## 3. 认证细节

- 算法固定 **HS256**（服务端显式指定 `algorithms=["HS256"]`，不接受 `alg: none` / 算法混淆）。
- 必须包含 `exp`（过期时间）与 `sub`（经销商标识），缺任一即 401。
- 密钥来自环境变量 `JWT_SECRET`，**仓库内不存在任何密钥字面量**。
- 测试 token 用 `uv run python scripts/make_token.py --sub dealer-001 --ttl 3600` 生成。
- 轮换：`JWT_SECRET` 支持 `JWT_SECRET_PREVIOUS` 并存态（新签用新钥、验签允许旧钥），轮换窗口内不打断存量 token <br>（实现见 RUNBOOK「token 轮换」）。

---

## 4. 审计

每次 **200** 成功报价写一行到 `quote_audit`：

| 列 | 类型 | 说明 |
|---|---|---|
| `order_id` | TEXT | 客户订单号 |
| `tier` | TEXT | 会员档 |
| `subtotal` | TEXT | 折扣前小计（字符串，避免 SQLite 的 REAL 亲和性） |
| `total` | TEXT | 应付金额 |
| `trace_id` | TEXT | 与响应头一致 |
| `created_at` | TEXT | UTC ISO-8601 |

**只记成功**。400/422 不入库 —— 它们没有可对账的金额；想追失败原因看日志（日志里同样带 `trace_id`）。
`(order_id, trace_id)` 不唯一：同一订单可被重复报价，审计表是流水，不是订单状态表。

---

## 5. 兼容性承诺

- 新增**可选**请求字段、新增响应字段 → 不算破坏性变更；
- 删除/重命名字段、改状态码语义、把字符串金额改成数字 → 破坏性变更，只允许出现在 `/v2`；
- 错误 `detail` 的具体措辞可能微调；**状态码**与**字段名**稳定。
