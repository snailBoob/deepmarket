FROM python:3.13-slim-bookworm
ARG REVISION=source
ENV DEEP_MARKET_REVISION=$REVISION

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir data \
    && echo '[]' > data/tabs.json \
    && chown -R app:app data
COPY deep_market/ ./deep_market/
COPY web/ ./web/
COPY docs/deepmarket.png ./docs/deepmarket.png
COPY compose.yaml README.md LICENSE THIRD_PARTY_NOTICES.md CONTRIBUTING.md ./
LABEL org.opencontainers.image.source="https://github.com/snailBoob/deepmarket" \
      org.opencontainers.image.description="Live crypto perpetual orderflow heatmap"
USER 10001:10001
EXPOSE 8740
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8740/api/settings', timeout=3).read()"
ENTRYPOINT ["python", "-m", "deep_market.server"]
