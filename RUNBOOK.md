# RUNBOOK · 报价服务

> 本文件面向"凌晨三点被叫起来的人"：先看第 1 节判断是不是自己的问题，再按第 5、6、7 节处置。

---

## 1. 现状与验证边界（先读这一段）

| 事项 | 状态 |
|---|---|
| 计价等价性（20 条） | ✅ 本地实测通过 |
| HTTP 契约（22 条测试） | ✅ 本地实测通过 |
| ruff + mypy strict | ✅ 本地实测通过（42 测试） |
| `docker build` 与镜像体积 | ✅ **本地实测通过**：`192MB`（183MiB，预算内） |
| 公网部署 | ⚠️ **未执行**（需要你的云账号授权） |
| GitHub Actions 运行 | ✅ **已跑绿**：[run 40646262880](https://github.com/backboneLiu/WebServerTest/actions/runs/40646262880)（lint / mypy / pytest / build / 200MB 门禁 / 容器冒烟） |

**结论**：代码、测试、镜像构建、容器冒烟、CI 全部实测通过；只剩“公网可访问”一项，需要按第 5 节选一个平台部署。

---

## 2. 本地跑起来（3 条命令）

```powershell
# 1) 装依赖（国内建议先设镜像）
$env:UV_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"
uv sync

# 2) 设密钥（≥32 字符，仓库里不存任何密钥）
$env:JWT_SECRET = "dev-secret-change-me-0123456789abcdef"
$env:DATABASE_URL = "sqlite:///./data/pricing.db"
$env:LOG_LEVEL = "INFO"

# 3) 启动（用项目自带入口，不要用 uvicorn CLI，原因见下方说明）
uv run python -m pricing_api.serve
# 监听端口读 PORT 环境变量，不设就是 8000
```

> **为什么不写 `uv run uvicorn pricing_api.main:app`？** 两条都是实测出来的：
> 1. uvicorn CLI 会自己 `dictConfig` 一次 logging，把 JSON formatter 冲掉；更麻烦的是 `--no-access-log` **关不掉**那条 access 记录——它用的是 `logging.getLogger("uvicorn.access").hasHandlers()` 判断，只要 root 挂了 handler 它就认为「该打」，于是每次请求多出一行 `trace_id: "-"` 的孤儿日志，一次请求变成两行、其中一行永远查不到。
> 2. PaaS 通过 `PORT` 环境变量注入监听端口，CLI 参数没法继承，部署后健康检查会直接打不通。

另开一个终端：

```powershell
$TOKEN = uv run python scripts/make_token.py --sub dealer-001 --ttl 3600
curl.exe http://127.0.0.1:8000/health
curl.exe -i -X POST http://127.0.0.1:8000/v1/quote `
  -H "authorization: Bearer $TOKEN" `
  -H 'content-type: application/json' `
  -H 'X-Request-Id: smoke-0001' `
  -d '{\"orderId\":\"SO-1\",\"tier\":\"Gold\",\"lines\":[{\"sku\":\"A\",\"quantity\":10,\"unitPrice\":\"5000\"}]}'
# 期望 200：{"orderId":"SO-1","total":"41500.00","traceId":"smoke-0001"}
```

> `curl.exe` 显式带 `.exe`：PowerShell 里 `curl` 是 `Invoke-WebRequest` 的别名，参数语法完全不同，这是最常见的"命令明明抄对了却报错"。

---

## 3. 环境变量（配置只走这里）

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `JWT_SECRET` | ✅ | — | HS256 密钥，**< 32 字符拒绝启动** |
| `JWT_SECRET_PREVIOUS` | ❌ | 空 | 轮换窗口内的旧密钥，只验签不签发 |
| `DATABASE_URL` | ❌ | `sqlite:///./data/pricing.db` | 仅支持 `sqlite:///<path>`；`postgresql://` 会明确报错（W2 支持） |
| `LOG_LEVEL` | ❌ | `INFO` | DEBUG/INFO/WARNING/ERROR，非法值拒绝启动 |
| `PORT` | ❌ | `8000` | 监听端口；Render / Fly / Railway 会自动注入，本地不用设 |
| `APP_VERSION` | ❌ | `0.2.0` | 会出现在 `/health` 与 `OpenAPI` 里 |

**启动失败是设计行为**：配置缺失就崩，编排系统会保留上一个健康版本，比"带病上线"安全。

---

## 4. Docker（本地验证）

```powershell
docker build -t pricing-api:local .
docker images pricing-api:local
# 实测：192MB（183MiB），距 200MB 红线还有 8MB
```

**一键容器冒烟**（比只看构建成功靠谱得多：它会验非 root、金额、日志、审计落盘）：

```powershell
pwsh -File scripts/docker-smoke.ps1
```
期望最后一行是 `CONTAINER SMOKE OK`，中途关键输出：`RUN AS: appuser (uid=10001)`、`QUOTE: {"total":"41500.00"}`、`ACCESS-LOG LEAK: 0`。

运行：

```powershell
docker run --rm -p 8000:8000 `
  -e JWT_SECRET="dev-secret-change-me-0123456789abcdef" `
  -e DATABASE_URL="sqlite:////data/pricing.db" `
  -e LOG_LEVEL="INFO" `
  -v "${PWD}/data:/data" `
  pricing-api:local
```

**体积超了怎么查**（别猜，逐层看）：

```powershell
docker history --no-trunc --format "table {{.Size}}\t{{.CreatedBy}}" pricing-api:local | Select-Object -First 20
# 或装 dive：dive pricing-api:local
```

三个最常见的超限原因：① builder 阶段的 `pip` 缓存被带进 runtime；② 装了 `uvicorn[standard]`（多出 uvloop/httptools）；③ 把 `tests/`、`.venv/` 拷进了镜像。

---

## 5. 部署到公网

### 方案 A：Render（有 Dockerfile 就能用，最省事）

1. 把仓库推到 GitHub；
2. Render → New → Blueprint → 选中仓库（仓库根有 `render.yaml`）；
3. 在 Render 控制台填 `JWT_SECRET`（或让它自动生成）。

> **⚠️ 必须知道**：Render 免费实例**没有持久磁盘**，SQLite 文件会随重启/重部署清空 → 审计记录丢失。
> 演示可接受；要保留审计，必须升到支持 Disk 的计划并挂载 `/var/data`（`render.yaml` 里已写好，切换 `plan` 即可）。

### 方案 B：Fly.io（需要 volume 才能保住 SQLite）

```bash
fly launch --no-deploy            # 生成 fly.toml（dockerfile 构建）
fly volumes create pricing_data --size 1 --region nrt
fly secrets set JWT_SECRET="<32+ 字符随机串>"
fly deploy
curl https://<app>.fly.dev/health
```

`fly.toml` 里需要把 volume 挂到 `/data`，并把 `DATABASE_URL` 设为 `sqlite:////data/pricing.db`。

### 方案 C：任意 VPS

```bash
docker run -d --name pricing-api --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -e JWT_SECRET='<32+ 字符>' \
  -e DATABASE_URL='sqlite:////data/pricing.db' \
  -v /srv/pricing/data:/data \
  pricing-api:local
```

前面套 Nginx/Caddy 做 HTTPS（客户要求公网 HTTPS）。**不要**把 8000 直接暴露到公网。

---

## 6. 回滚

镜像/容器方式：

```bash
# 1) 找到上一个健康版本（建议每次发布都打 git sha tag）
docker images pricing-api
# 2) 用旧 tag 重启，30 秒内回到旧版本
docker rm -f pricing-api
docker run -d --name pricing-api --restart unless-stopped \
  -p 127.0.0.1:8000:8000 -e JWT_SECRET=... \
  -v /srv/pricing/data:/data pricing-api:sha-<旧提交>
```

Render / Fly 都是**一键回滚到上一个部署**（控制台 Redeploy 旧版本 / `fly releases` + `fly deploy --image <旧镜像>`）。

代码层回滚：

```bash
git revert <坏提交> && git push    # CI 绿后再部署
```

数据库层：**本服务不改表结构、不删数据**，回滚代码不需要回滚数据库。这也是"只追加审计行"的设计好处——回滚风险最低。

---

## 7. 日志在哪看

- 本地：直接看终端（JSON 行）；
- 容器：`docker logs -f pricing-api`；
- 云端：Render/Fly 的 Logs 面板；自建可用 Loki/阿里云 SLS 采集 stdout。

**排查一条请求的标准动作**：

```bash
# 客户给你响应头里的 X-Request-Id
grep '"trace_id": "dealer-corr-0001"' app.log
# → 能看到 http.request / quote.calculated / quote.rejected 等该请求的全部行

# 只有订单号时，查审计表
sqlite3 data/pricing.db "SELECT order_id, tier, subtotal, total, trace_id, created_at \
  FROM quote_audit WHERE order_id = 'SO-20261027-001' ORDER BY id DESC;"
```

一条成功请求至少出现两行（`http.request` + `quote.calculated`），两行 `trace_id` 必须相同。若不同 → 说明 trace 上下文串了（回到 `src/pricing_api/trace.py` 检查是否有人又用了全局变量）。

**日志对不上、或者「改了代码行为没变」，先查是不是有僵尸进程占着端口**（本项目真实踩过）：

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object OwningProcess
Get-CimInstance Win32_Process -Filter "ProcessId=<上面那个 pid>" | Select-Object CommandLine
```

`uv run` 的进程树是 `uv.exe -> python.exe -> python.exe`，`Stop-Process -Id <最外层>` 只杀最外层，子进程会变成孤儿继续监听端口。Windows 上**允许重复绑定同一端口**，所以新进程不会报错，请求会被分给跑旧代码的那个进程。Linux 用 `ss -ltnp | grep 8000`。

---

## 8. Token 轮换（不打断存量客户）

1. 生成新密钥：`openssl rand -base64 48`（或 `python -c "import secrets;print(secrets.token_urlsafe(48))"`）；
2. 把**当前**密钥挪到 `JWT_SECRET_PREVIOUS`，新密钥设成 `JWT_SECRET`：

```bash
fly secrets set JWT_SECRET_PREVIOUS="<旧密钥>" JWT_SECRET="<新密钥>"
```

3. 重启生效后：新签发的 token 用新钥，旧 token 仍能验通；
4. 等存量 token 全部过期（最长 = 你签发的 TTL，默认 1 小时）后，**移除 `JWT_SECRET_PREVIOUS`**；
5. 记录操作时间与版本，写进变更单。

**密钥泄漏时不要走轮换流程，走吊销**：直接换 `JWT_SECRET`、删除 `JWT_SECRET_PREVIOUS`，所有 token 立即失效（此时不接受"平滑"）。

---

## 9. 验收清单自查（对着 TASK-02 第三节逐条）

```bash
curl -i https://<host>/health                                   # 200 + version
curl -i -X POST https://<host>/v1/quote -d '...'                # 401
# 带 token，Gold 10×5000                                       # "41500.00"
# 空 lines                                                     # 400 + detail
# 自定义 X-Request-Id → 日志里 grep 该 id                       # 全部行都在
docker images | grep pricing                                    # < 200MB
grep -rIn "JWT_SECRET *= *['\"]" src/ scripts/ .github/         # 应无输出
```

最后一条（仓库里 grep 不到硬编码密钥）建议在本地跑一次再交付。

前 5 条可以用一条命令自测：`pwsh -File scripts/smoke.ps1`——它会起一个真实进程、发真实 HTTP 请求，并自动校验日志里没有 `uvicorn.access` 孤儿行、请求期的行全都带 `trace_id`。

---

## 10. 已知限制 / W2 计划

| 限制 | 影响 | W2 计划 |
|---|---|---|
| SQLite + 单实例 | 多副本部署时审计会分裂成多个库 | 换 Postgres + 连接池 |
| 无速率限制 | 单个经销商可打满服务 | 加 token bucket（按 sub 维度） |
| 无幂等键 | 客户端重试会产生多条审计行 | 引入 `Idempotency-Key` |
| `/health` 不查依赖 | 数据库挂了仍返回 ok | 另加 `/ready`（查 DB） |
| 同步 SQLite 写 | 高并发下 `to_thread` 线程池可能排队 | 换异步驱动 |
