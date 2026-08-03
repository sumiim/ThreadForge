FROM python:3.12-slim AS builder

WORKDIR /build

COPY pico-legacy-runtime ./pico-legacy-runtime
COPY api-server ./api-server

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels \
    ./pico-legacy-runtime ./api-server


FROM python:3.12-slim

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
