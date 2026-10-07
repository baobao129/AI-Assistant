FROM python:3.12.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv==0.11.16
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend ./backend
# Demo initialization and acceptance scripts are run inside the container
# (`docker compose exec backend python -m scripts.seed_demo`).
COPY scripts ./scripts
COPY demo ./demo
RUN useradd --create-home agent && mkdir /app/runtime && chown agent:agent /app/runtime
USER agent
ENV PATH="/opt/venv/bin:$PATH"
CMD ["uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
