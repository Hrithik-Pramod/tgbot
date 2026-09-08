# syntax=docker/dockerfile:1
#
# Two stages so the build toolchain never reaches the runtime image: asyncpg
# needs a C compiler to build, and shipping gcc in a container that holds three
# bot tokens and a database password is not worth the convenience.

# ---------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

# Wheels are built once here and copied across, so the runtime image needs no
# compiler and no apt cache.
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# ---------------------------------------------------------------- runtime
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

# tzdata so log timestamps render in the operator's timezone;
# curl for the compose healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels requirements.txt

# Unprivileged. If the process is ever compromised it should not own the image.
RUN useradd --system --create-home --shell /usr/sbin/nologin settlement
WORKDIR /app

COPY --chown=settlement:settlement . /app

USER settlement

# The bot exposes no ports. It makes outbound connections only — Telegram and
# the TRON APIs — so there is nothing to publish and nothing to reach it on.

# Fails the container if the app cannot be imported, which catches a broken
# build or a missing dependency at start rather than in the logs an hour later.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import main" || exit 1

CMD ["python", "main.py"]
