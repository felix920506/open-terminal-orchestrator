# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY oto ./oto
RUN python -m build --wheel --outdir /wheels \
    && pip wheel --no-cache-dir --wheel-dir /wheels /wheels/*.whl

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home /data oto \
    && mkdir -p /data && chown oto /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels open-terminal-orchestrator \
    && rm -rf /wheels
USER oto
WORKDIR /data
ENV ORCH_DATABASE_URL=sqlite+aiosqlite:////data/oto.db \
    ORCH_STAGING_DIR=/data/staging \
    ORCH_LISTEN_HOST=0.0.0.0 \
    ORCH_LISTEN_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
ENTRYPOINT ["oto", "serve"]
