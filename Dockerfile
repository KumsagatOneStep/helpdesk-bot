FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
# не root: при RCE через зависимость — без root в контейнере и на bind-mount ./data
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app
CMD ["sh", "-c", "alembic upgrade head && python -m helpdesk.main"]
