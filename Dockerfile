# syntax=docker/dockerfile:1
# Pinned base: python 3.12.8 slim bookworm (update digest when rebasing images).
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS builder

WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS runtime

RUN groupadd --gid 10001 concierge \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin concierge

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/concierge /usr/local/bin/concierge
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY examples ./examples

RUN chown -R concierge:concierge /app

USER concierge
EXPOSE 8765

# Liveness/readiness routes are provided by the gateway (see P1-6).
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz')" || exit 1

ENTRYPOINT ["concierge"]
CMD ["--config", "config/gateway.example.yaml"]
