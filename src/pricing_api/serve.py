"""生产启动入口：`python -m pricing_api.serve`。

为什么不用 `uvicorn pricing_api.main:app` 这个 CLI？

1. CLI 会自己 `dictConfig` 一次 logging，把应用装好的 JSON formatter 冲掉；
   更麻烦的是 `--no-access-log` 挡不住那条纯文本 access 记录（实测仍会输出，
   而且它的 `trace_id` 是空的——因为那条记录落在请求上下文之外）。
   结果就是"一次请求两行日志，其中一行查不到对应请求"，直接违反验收清单第 5 条。
2. PaaS（Render / Fly / Railway）通过 `PORT` 环境变量注入监听端口，
   CLI 参数没法从环境变量继承，部署后健康检查会打不通。

所以这里显式调用 `uvicorn.run`：
- `log_config=None`  不接管 logging，保留 `pricing_api` 的 JSON 行格式；
- `access_log=False` 不要 uvicorn 的访问日志（由 trace_middleware 带 trace_id 输出）；
- 端口读 `PORT`，默认 8000。
"""

import logging
import os

import uvicorn

from pricing_api.settings import Settings


def main() -> None:
    # 提前校验配置：少一个 JWT_SECRET 就让进程立刻退出，
    # 而不是等第一个请求进来才发现所有 token 都验不过。
    settings = Settings.from_env()

    port = int(os.environ.get("PORT") or "8000")

    logging.getLogger("pricing_api").info(
        "serve.starting", extra={"port": port, "version": settings.app_version}
    )

    uvicorn.run(
        "pricing_api.main:app",
        host="0.0.0.0",
        port=port,
        log_config=None,
        access_log=False,
        # 站在 Nginx / PaaS 反代后面时，用 X-Forwarded-For 还原真实客户端 IP。
        # 注意：若把服务直接暴露公网，这个头可被伪造（只影响日志里的 IP，不影响鉴权）。
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
