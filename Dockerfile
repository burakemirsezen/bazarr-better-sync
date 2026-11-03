FROM python:3.11-slim-bookworm

ARG INSTALL_ASR=0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BBS_DATA_DIR=/data \
    BBS_HOST=0.0.0.0 \
    BBS_PORT=8080 \
    HF_HOME=/data/models \
    OMP_NUM_THREADS=2

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 syncapp \
    && useradd --uid 10001 --gid 10001 --no-create-home syncapp \
    && mkdir /data && chown 10001:10001 /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN if [ "$INSTALL_ASR" = "1" ]; then pip install --no-cache-dir '.[asr]'; \
    else pip install --no-cache-dir .; fi

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1
ENTRYPOINT ["python", "-m", "bazarr_sync"]
