FROM ghcr.io/astral-sh/uv:0.12.19 AS uv
FROM python:3.12-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY manage.py ./
COPY payments/ ./payments/
COPY testproject/ ./testproject/
COPY tests/ ./tests/
RUN useradd --create-home app
USER app

CMD ["sh", "-c", "python manage.py migrate --noinput && exec python manage.py runserver 0.0.0.0:8000 --noreload --insecure"]
