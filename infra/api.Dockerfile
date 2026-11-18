FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY gateway gateway
COPY services services
COPY shared shared
COPY control_plane control_plane
COPY alembic.ini ./
COPY db db
ENV PATH="/app/.venv/bin:$PATH"
USER 10001
CMD ["uvicorn", "gateway.app:create_app", "--factory", "--host", "0.0.0.0"]
