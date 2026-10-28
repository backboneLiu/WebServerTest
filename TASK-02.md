# TASK-02 · 把计价库变成能上线的服务

| | |
|---|---|
| **对应任务** | W1-1.3（服务与可观测）+ W1-1.4（容器化与部署） |
| **限时** | 7 小时 |
| **前置** | TASK-01 已完成（20 条测试全绿） |
| **产出** | 一个公网可访问的 HTTPS 服务 + CI 绿 |

---

## 一、背景

客户验收了你的 Python 计价库，现在提新需求：

> 「我们 200 家经销商要接你们的报价能力，走接口。审计要留痕，出了问题要能查到哪一单、哪个请求。下周一能上吗？」

**这就是 W1 到 W2 的分界**：库是给自己用的，服务是给别人用的。别人用你的东西时，会关心五件事——**能连上、有权限、出错会说人话、出事能查、能回滚**。

---

## 二、做五件事

### 1. 加一层 HTTP

在现有仓库里加 `src/pricing_api/`（**不要重写 `pricing` 包，直接 import 它**）。

| 接口 | 说明 |
|---|---|
| `GET /health` | 200，返回 `{"status":"ok","version":"x.y.z"}`，**不需要认证** |
| `POST /v1/quote` | 需要 JWT，算一笔报价并落库 |

请求体：

```json
{
  "orderId": "SO-20261027-001",
  "tier": "Gold",
  "lines": [
    {"sku": "A-100", "quantity": 10, "unitPrice": "5000", "isPromotional": false}
  ]
}
```

响应 200：

```json
{"orderId": "SO-20261027-001", "total": "41500.00", "traceId": "..."}
```

错误：

| 场景 | 状态码 |
|---|---|
| 没有 / 过期 token | `401` |
| 请求体字段缺失或类型错 | `422` |
| 计价规则不满足（空单、数量 ≤ 0、负价） | `400`，body 里带 `detail` 说明 |

### 2. 认证

JWT（HS256），密钥从**环境变量**读，代码里不许出现任何字面量密钥。给一个 `scripts/make_token.py` 生成测试 token，别手搓。

### 3. 审计落库

SQLite 就够（W2 再换 Postgres）。每次成功报价存一行：`order_id / tier / subtotal / total / trace_id / created_at`。

### 4. 可观测

- 结构化日志（JSON 行），每行必须带 `trace_id`
- `trace_id` 来源优先级：请求头 `X-Request-Id` → 没有就生成 UUID
- 响应头回显 `X-Request-Id`（客户排查时要能对上）
- `/metrics` 可选，不做不扣分

### 5. 容器化 + 上线

- 多阶段 Dockerfile，**镜像 < 200MB**
- 非 root 用户运行
- 配置全部走环境变量（`DATABASE_URL`、`JWT_SECRET`、`LOG_LEVEL`）
- 部署到任意公网环境：Railway / Fly.io / Render / 阿里云函数计算 / 腾讯云轻量任选
- GitHub Actions：`lint → test → build`

---

## 三、验收清单（我会逐条查）

- [ ] `curl https://<你的域名>/health` 返回 200
- [ ] 不带 token 调 `/v1/quote` → 401
- [ ] 带 token 算 **README 第 8 条**（Gold，10 × 5000）→ `"41500.00"`，与本地结果一致
- [ ] 传空 lines → 400，`detail` 里能看懂为什么
- [ ] 拿响应里的 `X-Request-Id`，在日志里能捞到这**一次**请求的全部日志行
- [ ] `docker images` 里那个镜像 < 200MB
- [ ] 全仓库 grep 不到硬编码密钥
- [ ] CI 是绿的（贴个链接或截图）
- [ ] 原有 20 条测试仍然全绿

---

## 四、三个坑（提前说，因为都会踩）

**坑 1：金额过 JSON 会再碎一次。**
你本地 `Decimal("41500.00")` 是对的，但序列化成 JSON 数字 `41500.0` 再反序列化，前端的 `float` 就把它变成 `41500`。要求：**金额在 API 层一律字符串**，响应体里也是 `"41500.00"` 而不是 `41500.00`。这是金融类接口的行业惯例，不是洁癖。

**坑 2：`trace_id` 别用全局变量。**
FastAPI 是异步的，全局变量会被并发请求互相覆盖。用 `contextvars.ContextVar` + 中间件，日志 formatter 里读它。写错了你会看到日志串单——而且只在压测时出现。

**坑 3：多阶段构建不是把 Dockerfile 写长一点。**
builder 阶段装编译依赖，runtime 阶段只拷 wheel 和源码，`--no-install-recommends`、`python:slim`、删缓存。做不到 200MB 以内，说明你把 builder 的东西带进 runtime 了。

---

## 五、时间盒

| 时段 | 做什么 |
|---|---|
| 0:00–0:30 | 定接口契约（写 OpenAPI 或 README），先不写代码 |
| 0:30–2:30 | FastAPI + JWT + 落库，本地 curl 通 |
| 2:30–4:00 | 日志 + trace_id，用两个并发请求验证不串单 |
| 4:00–5:30 | Dockerfile，把镜像压到 200MB 以内 |
| 5:30–7:00 | 部署 + CI + 写 runbook |

> **这次先写契约再写代码是硬要求。** 因为接口一旦发给经销商，改一次要陪笑脸三天。

---

## 六、交什么

1. 公网 URL（能调通）
2. `docker images` 输出
3. CI 运行链接
4. 一份 `RUNBOOK.md`：怎么部署、怎么回滚、日志在哪看、token 怎么轮换
5. 更新后的 `notes.md`：这次新增的 3 条「服务化和库不一样」的地方

---

## 七、加分项（不强制）

把 `/v1/quote` 的 p95 延迟压到 50ms 以内，并说明你怎么测的。
