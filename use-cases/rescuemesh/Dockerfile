FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    RESCUEMESH_LEDGER_PATH=/data/ledger.json

RUN pip install --no-cache-dir uv==0.8.15 \
    && useradd --create-home --uid 10001 rescuemesh \
    && mkdir -p /app /data \
    && chown -R rescuemesh:rescuemesh /app /data

WORKDIR /app
COPY --chown=rescuemesh:rescuemesh pyproject.toml uv.lock ./
USER rescuemesh
RUN uv sync --frozen --no-dev

COPY --chown=rescuemesh:rescuemesh . .
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "python", "command_center.py"]
