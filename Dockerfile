FROM python:3.12-slim

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files first (for layer caching)
COPY pyproject.toml uv.lock ./

# Install production dependencies only (no dev)
RUN uv pip install --system .

# Copy source code
COPY archolith_proxy/ archolith_proxy/

# Create logs directory
RUN mkdir -p logs

# Default env vars (override at runtime)
ENV PROXY_PORT=9800
ENV SESSION_NEO4J_URI=bolt://neo4j:7687
ENV SESSION_NEO4J_DATABASE=neo4j
ENV SESSION_NEO4J_USER=neo4j

EXPOSE 9800

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://localhost:9800/live'); assert r.status_code == 200" || exit 1

CMD ["uvicorn", "archolith_proxy.main:app", "--host", "0.0.0.0", "--port", "9800", "--log-level", "info"]
