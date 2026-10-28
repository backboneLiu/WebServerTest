# notes.md · W1-1.2 C# → Python 计价迁移

实现：`src/pricing/core.py`；测试：`tests/test_pricing.py`（20 条：README 表格 16 条 + 4 条补充/固化用例）。
所有期望值都是**先按 C# 语义手算、再写进断言**，没有一条是「跑一遍看输出多少」抄下来的。

---

## 一、C# 习惯在 Python 里别扭的地方

1. **`decimal` 在 C# 是语言关键字，在 Python 只是个库类型。**
   C# 里 `0.5m` 天生精确，写不出错；Python 里 `Decimal(0.5)` 拿到的是 `0.5000000000000000277...`，因为字面量 `0.5` 先生成了 float。
   所以本项目所有金额一律用字符串构造：`Decimal("0.5")`。这条不是洁癖——`0.335` 这种值走 float 再进 Decimal，就是带着误差进入计价链。

2. **`MidpointRounding.AwayFromZero` 对应的是 `ROUND_HALF_UP`，不是内置 `round()`。**
   Python 3 的 `round(2.5) == 2`（银行家舍入），`round(Decimal("1.005"), 2)` 同样是 banker's，直接得到 `1.00`。
   必须写 `value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`——注意 decimal 的 `ROUND_HALF_UP` 是「中点**远离 0**」，恰好等于 C# 的 AwayFromZero，名字有误导性。
   这条正是 README 第 12 条分水岭：`3 × 0.335 = 1.005 → 1.01`，用 `round()` 会静默变成 `1.00`，差一分钱。

3. **类型安全不再兜底：`int * Decimal` 是对的，`Decimal * float` 也能过 mypy。**
   C# 里 `line.Quantity * line.UnitPrice` 的类型在编译期就钉死了；Python 里 `Decimal * float` **不会报错**，typeshed 允许它，运行期也照算，只是结果精度不再可控。
   mypy strict 拦不住这类事故，只能靠「构造款额只用字符串、全程 Decimal」这个团队约定 + 测试守住。

4. **`decimal` 的运算精度是隐式的全局状态，C# 里根本没有这个概念。**
   C# 的 decimal 精度固定 28~29 位有效数字、固定银行家舍入；Python 的 `Decimal` 行为受当前线程 `decimal context` 的 `prec` / `rounding` 影响，任何第三方库（或一段 `getcontext().prec = 6`）都能把它改掉，进而让计价结果漂移。
   所以 `calculate_total` 里用 `with localcontext() as ctx` 把 `prec=28`、`ROUND_HALF_EVEN` 显式钉住，函数退出即还原。

5. **C# 的 `catch ... when` 过滤器，在 Python 里只能「先捕获、再决定要不要重新抛」。**
   原代码 `catch (Exception ex) when (attempt < maxRetry - 1)`：过滤不成立时异常等于「没被捕获」，直接冒泡。
   Python 没有这个语法，只能 `except Exception:` 里判断 `if attempt >= max_retry - 1: raise` 手动还原语义。
   更要注意的是 **except 的书写顺序即匹配优先级**：`PricingError` 必须写在 `Exception` 前面，否则业务校验失败也会被当成「基础设施抖动」去重试 3 次——这种 bug 在 C# 里由编译器帮你避免，在 Python 里靠人看。

6. **可空值 `decimal?` 的语义要自己维护。**
   C# 的 `decimal? Total` 明确区分「没算出来」和「算出来是 0」，Python 侧就是 `Decimal | None`，运行时没有任何保护：一旦有人写成 `total=Decimal("0")` 兜底，`BatchResult` 的语义就被悄悄改掉了，而且测试里 `== Decimal("0")` 依然是合法的。

7. **`decimal` 的相等与表示是两件事。**
   `Decimal("100") == Decimal("100.00")` 为 `True`，但 `str()` 分别是 `"100"` 和 `"100.00"`。C# 的 decimal 会保留 scale，做 `ToString` / 四舍五入时差异明显。
   测试里因此同时断言「值相等」和（关键用例）「`str()` 是 2 位小数」，否则金额在展示层仍可能变成 `100` 这种不合规格式。

---

## 二、对 `MaxDiscountRate = 0.30` 的判断

先算证据，不猜：

- 会员档最高：Platinum `0.12`
- 阶梯档最高：`subtotal >= 50000` → `0.10`
- 两者叠加上限 = `0.22`
- `0.22 < 0.30`，所以 `Math.Min(tierRate + volumeRate, 0.30m)` **在当前规则下永远取不到右值**。

结论：**这是死代码，但我不改、也不删。** 理由三条：
1. 原注释、变量名、常量值都没有可判定「废」的证据，只能判定「当前不可达」；
2. 它更像**防御性夹子**——将来商务侧把 Platinum 提到 20%，或新增 `>= 100000` 的 12% 档，上限立刻就有意义；删掉它，届时超额折扣会静默生效，财务才炸；
3. FDE 的纪律是「先记录、再求证」，不是「顺手优化」。真有 30% 的特殊合同，那也应该是**客户口头确认后我们主动加测试**，而不是我从代码里删掉一行。

为了让这个判断不会被后人遗忘，我把它写成了测试 `test_case17_max_discount_rate_is_currently_unreachable`：
一旦有人调高会员/阶梯档位让叠加率触及 30%，这条会先红，逼人重新核算口径，而不是让 `Math.Min` 静默吃掉差额。

---

## 三、附加题：连续 3 次非 `PricingError` 异常，批量方法会怎样？

我没有靠读代码下结论，写了 `test_case19_batch_third_non_pricing_failure_aborts_whole_batch` 跑出来。

**结论**：整个 `calculate_batch` 会因为该异常**直接抛出**，已经算好的结果一起丢掉。

实测输出（断言固化）：

- `bad_attempts == 3`：`maxRetry=3` 的实际含义是「**总共尝试 3 次**」（1 次首发 + 2 次重试），第 3 次 `attempt == 2` 时过滤器 `attempt < maxRetry - 1` 不成立，异常原样冒泡；不是很多人以为的「重试 3 次 = 共 4 次」；
- 期间睡了 `200ms + 400ms`（`200 * attempt` 线性退避，不是指数）；
- `notified == ["OK-1"]`：前面 `OK-1` 已经计价成功、也回调过了，但它的 `BatchResult` 随异常一起消失；后面的 `OK-2` 根本没被处理。

**这符合直觉吗？不符合，而且这是原代码真正的坑：**

1. 方法注释写的是「一条失败不影响其余」，但那只对 `PricingException` 成立；基础设施类异常（网关超时、DB 抖动）反而会**拖垮整批**——这是最反直觉的地方：越是运维故障，越该继续处理其它订单，实际却最不宽容。
2. `CalculateBatchAsync` 的 `results` 是局部变量，异常逃逸后调用方拿不到任何已完成结果，等于「剩下的订单白算」。生产上应该 `yield` / 增量回调 / 或把异常也转成 `BatchResult.Error`。
3. 重试包住的是 `CalculateTotal` **和** `notify` 两段：`notify` 已经发出去了才抛（比如下游超时但实际已落库），重试会导致**重复通知**，需要幂等键才能安全。

按 README 要求，这些只记录、不在本次练习里改行为——Python 侧 1:1 复刻了「异常冒泡、结果丢弃」的原始语义，并用测试钉住，方便以后和客户确认后再迭代。

---

## 四、一处配置改动（需要知会）

`pyproject.toml` 的 ruff 配置里新增了 `allowed-confusables`（只放行中文全角标点 `，。：；（）「」× →` 等）。

原因：`select` 里含 `RUF`，而脚手架自带的 `src/pricing/core.py` 第 1/24/50 行的中文 docstring 就会触发 `RUF001/002/003`（「文档串含形近字符」），本项目全部注释都是中文，不白名单则 `.\check.ps1` 永远不可能绿。
这是**收窄误报**而非放宽检查：真正危险的形近字母（西里尔 `а` 冒充 `a` 之类）仍然会被报出来。没动 `select`，没动 `uv.lock`。

---

## 五、其他记录

- C# 对 `order == null` 抛 `ArgumentNullException`，Python 侧没有对应防线：`calculate_total` 的类型标注是 `Order`，mypy strict 下写 `if order is None` 反而会被 `warn_unreachable` 判为不可达。调用方必须在边界（API 层）保证非空，这是 C# → Python 时**校验责任从语言转移到了框架/调用方**的典型例子。
- `notify` 的签名是 `Callable[[Order], Awaitable[None]]`，用 `asyncio.run` 驱动，测试不依赖 `pytest-asyncio`，避免为了运行测试而改依赖。

---

## 六、服务化和库不一样的地方（TASK-02 新增 3 条）

1. **「错了」在库里是一个异常，在服务里是一套状态码契约。**
   库里只需抛 `PricingError`，怎么处置由调用方决定；服务对外却要区分：没有/过期 token → `401`，请求体字段缺失或类型错 → `422`，计价规则不满足 → `400`。
   最反直觉的地方在**校验该放哪一层**：如果为了「防御式编程」在 Pydantic 模型上给 `unitPrice` 加 `ge=0`、给 `lines` 加 `min_length=1`，那么「负价」「空单」会先被模型拦成 `422`，客户看到的是「字段不合法」，而契约写的是「400 + 为什么算不出钱」。
   所以模型层**只校验类型和形状，业务规则一律留在 `pricing` 包**——`models.py` 里明明能加校验却故意不加，这是设计选择而不是遗漏。副作用是个好处：`400` 只可能来自计价逻辑，`422` 只可能来自协议层，看状态码就知道该查谁。

2. **日志从「写给人看」变成「给机器 join」，所以必须先有请求上下文。**
   库里一条日志可以独立存在；服务里日志的契约是「同一个 `trace_id` 的行 = 同一次请求」。实现是 `contextvars` + 中间件在请求边界 `set`/`reset`，formatter 只负责读——用全局变量存 `trace_id` 一定串单，而且只在并发时暴露（所以测试是 8 个并发请求 + 断言 DB 里 `order_id ↔ trace_id` 一一对应，而不是单请求点一下）。
   踩到的第二件事：**uvicorn 自带的 access log 是一条没有上下文的孤儿行**（`trace_id` 恒为 `-`），一次请求变成两行、其中一行永远查不到。而且 `--no-access-log` 关不掉它——读源码发现 uvicorn 是用 `logging.getLogger("uvicorn.access").hasHandlers()` 判断要不要输出的，只要 root 挂了 handler，它就算「有 handler」。
   最终只能在自己代码里 `disabled=True`，并用 `uvicorn.run(log_config=None, access_log=False)` 自己启动（这就是 `serve.py` 存在的原因）。教训：**「每行都必须带 trace_id」这条要求，等于宣布不能再让任何第三方库自由打日志。**

3. **「算对了」不等于「成功了」：库是无状态纯函数，服务有副作用。**
   `calculate_breakdown` 算完返回 `Decimal` 就算成功；服务里一次报价的成功是「算对 + 审计落库 + 返回响应」三件事都完成，任何一环失败都不该回 `200`。
   所以审计写失败必须 `500`，而不是「记个 warning 继续返回」——否则客户拿着 `200` 的报价单，我们事后查不到这一单，等于审计造假。这就是 `app.py` 里 `insert` 同步等待、`except sqlite3.Error → 500` 的原因。
   随之而来的是库里根本不存在的问题：状态。SQLite 文件是进程内唯一的共享可写资源，于是每请求新建连接（连接对象不能跨线程复用）、开 WAL、把阻塞写放 `asyncio.to_thread`。**库的接口是纯函数，服务的接口还得管生命周期。**
