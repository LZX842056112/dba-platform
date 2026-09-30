# 平台镜像（uv workspace 应用）。
# 多阶段构建：builder 安装依赖并打包 wheel，runtime 只带运行期产物。
FROM python:3.12-slim AS builder

# uv（固定版本，保证可复现）
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# 先复制清单以最大化缓存命中
COPY pyproject.toml uv.lock ./
COPY packages/agent-runtime/pyproject.toml packages/agent-runtime/
COPY apps/platform/pyproject.toml apps/platform/
COPY apps/worker/pyproject.toml apps/worker/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --all-packages --extra prod

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-packages --extra prod

FROM python:3.12-slim AS runtime

RUN useradd -m -u 10001 dba
WORKDIR /app

COPY --from=builder --chown=dba:dba /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER dba
EXPOSE 8000

# 存活探针由编排层调用 /health；此处仅默认启动命令
CMD ["uvicorn", "dba.main:app", "--host", "0.0.0.0", "--port", "8000"]
