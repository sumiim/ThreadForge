FROM python:3.12-slim AS builder

WORKDIR /build

COPY pico-legacy-runtime ./pico-legacy-runtime
COPY agent-orchestrator ./agent-orchestrator
COPY api-server ./api-server

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels \
    ./pico-legacy-runtime ./agent-orchestrator ./api-server


FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN addgroup --system threadforge && adduser --system --no-create-home --ingroup threadforge threadforge

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels \
    && mkdir -p /data /config \
    && chown -R threadforge:threadforge /data /config /app

USER threadforge:threadforge

EXPOSE 8000

ENTRYPOINT ["python", "-m", "threadforge_api"]
