# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY . .
RUN pip install --no-cache-dir .

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user
RUN useradd --create-home appuser && \
    mkdir -p /home/appuser/.tradingagents/cache \
             /home/appuser/.tradingagents/logs \
             /home/appuser/.tradingagents/memory && \
    chown -R appuser:appuser /home/appuser/.tradingagents

WORKDIR /home/appuser/app
COPY --from=builder --chown=appuser:appuser /build .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Streamlit config: disable file watcher & telemetry
RUN mkdir -p /home/appuser/.streamlit && \
    echo '[server]\n\
enableCORS = false\n\
enableXsrfProtection = false\n\
headless = true\n\
\n\
[browser]\n\
gatherUsageStats = false\n\
\n\
[runner]\n\
magicEnabled = false\n' > /home/appuser/.streamlit/config.toml && \
    chown -R appuser:appuser /home/appuser/.streamlit

ENV STREAMLIT_SERVER_PORT=8501 \
    HOME=/home/appuser

EXPOSE 8501

# Run entrypoint as root to fix volume permissions, then drop to appuser
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "streamlit", "run", "web/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
