# syntax=docker/dockerfile:1
#
# 多阶段构建的思路（TASK-02 坑 3）：
#   builder  只干一件事：把依赖和项目装进一个干净目录 /opt/app-libs
#   runtime  只拷这个目录，不带 pip 缓存、不带编译器、不带 .pyc、不带测试
#
# 体积预算（python:3.13-slim 未压缩约 130MB）：
#   + site-packages（fastapi/pydantic/pyjwt/uvicorn 等）约 40-50MB
#   - 删除 pip/setuptools/标准库测试 约 -25MB
#   ≈ 145-175MB，目标 < 200MB
# 实测方法见 RUNBOOK.md「镜像体积」。

# ---------------------------------------------------------------- builder
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 只拷打包所需的文件。tests/ legacy/ docs/ 一律不进构建上下文（见 .dockerignore）
COPY pyproject.toml ./
COPY src ./src

# --target 装成"纯目录"，而不是 venv：
#   venv 会顺手带上 pip/setuptools（约 15MB），而 runtime 完全不需要它们。
# --no-compile 不生成 .pyc：首次请求会多一点编译开销，但省下十几 MB。
RUN python -m pip install --no-cache-dir --no-compile --target=/opt/app-libs .

# 清掉包里不该进生产的东西
RUN find /opt/app-libs -name "__pycache__" -type d -prune -exec rm -rf {} + \
 && find /opt/app-libs -name "*.pyi" -delete \
 && rm -rf /opt/app-libs/pip /opt/app-libs/setuptools /opt/app-libs/wheel

# ---------------------------------------------------------------- runtime
FROM python:3.13-slim AS runtime

# 配置全部走环境变量（TASK-02 第 5 条要求）。
# 注意 DATABASE_URL 用四个斜杠：sqlite:////data/pricing.db 表示绝对路径 /data/pricing.db
ENV PYTHONPATH=/opt/app-libs \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:////data/pricing.db \
    LOG_LEVEL=INFO \
    PORT=8000

# 非 root 运行：容器逃逸时攻击者拿到的也只是普通用户。
# /data 必须可写（SQLite 要建库建 WAL 文件），所以先把属主给 appuser。
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /data \
 && chown appuser:appuser /data

COPY --from=builder /opt/app-libs /opt/app-libs

# 体积优化：runtime 不再安装任何东西，pip/setuptools 与标准库测试可以整块删掉。
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* \
           /usr/local/lib/python3.13/site-packages/setuptools* \
           /usr/local/lib/python3.13/site-packages/wheel* \
           /usr/local/lib/python3.13/test \
           /usr/local/lib/python3.13/idlelib \
           /usr/local/lib/python3.13/tkinter \
           /root/.cache

USER appuser
WORKDIR /app

EXPOSE 8000

# 探针只打 /health（不查数据库）：数据库抖动不该导致容器被反复重启。
# 镜像里没有 curl，用标准库 urllib 就够，省一层依赖。
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; raise SystemExit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# 用 pricing_api.serve 而不是 uvicorn CLI：
# CLI 会自己 dictConfig 一次 logging，冲掉 JSON formatter，并且挡不住那条
# 没有 trace_id 的 access 日志；同时 serve 会读 PORT 环境变量（PaaS 必需）。
CMD ["python", "-m", "pricing_api.serve"]
