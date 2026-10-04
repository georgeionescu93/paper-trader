# Paper Trader - web build, containerised so it can run for months unattended.
#
# The server layer (app_web.py) needs only the standard library; pandas and
# websocket-client are for the market-data layer and the candle engine.
#
# Build:  docker build -t paper-trader .
# Run:    docker run -d --name paper-trader --restart unless-stopped \
#             -p 127.0.0.1:8080:8080 \
#             -v paper_trader_data:/data \
#             -e PAPER_TRADER_DB=/data/paper_trading_app.db \
#             paper-trader
#
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PAPER_TRADER_DB=/data/paper_trading_app.db \
    PAPER_TRADER_CONFIG=/data/web_config.json \
    TZ=UTC

# tzdata is needed by tv_data.py (zoneinfo is used for US market hours) and
# ca-certificates by the HTTPS calls to TradingView / DuckDuckGo / the Ollama
# API. Nothing else is required: there is no C extension to compile.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# OPTIONAL: the full 61-pattern TA-Lib engine.
#
#   docker build --build-arg WITH_TALIB=1 -t paper-trader .
#
# Without it the app uses its built-in pure-Python pattern engine: the risk
# engine, sizing, guard rails and the shared scan are all identical, so this is
# a quality upgrade rather than a requirement. Building from source keeps the
# image working on ARM (Oracle's Ampere A1 shapes) as well as x86_64.
# ---------------------------------------------------------------------------
ARG WITH_TALIB=0
RUN if [ "$WITH_TALIB" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends build-essential curl \
      && curl -fsSL -o /tmp/talib.tgz \
         https://downloads.sourceforge.net/project/ta-lib/ta-lib/0.4.0/ta-lib-0.4.0-src.tar.gz \
      && tar -xzf /tmp/talib.tgz -C /tmp \
      && cd /tmp/ta-lib \
      && ./configure --prefix=/usr >/dev/null \
      && make -j"$(nproc)" >/dev/null \
      && make install >/dev/null \
      && pip install --no-cache-dir TA-Lib \
      && cd / \
      && apt-get purge -y build-essential \
      && apt-get autoremove -y \
      && rm -rf /tmp/ta-lib /tmp/talib.tgz /var/lib/apt/lists/*; \
    fi

WORKDIR /app

# Dependencies first, so a code edit does not re-resolve them.
COPY requirements.txt requirements-postgres.txt ./
# psycopg2 is always installed, even for a plain SQLite deployment: it is a
# small wheel and costs nothing at runtime. It used to be conditional on a
# WITH_POSTGRES build argument, but platforms that build from a blueprint
# (Render) cannot pass build arguments at all, so the conditional would have
# produced an image that silently could not reach PostgreSQL.
RUN pip install --no-cache-dir -r requirements.txt -r requirements-postgres.txt

# Application code. tv_data.py and engine.py must sit next to
# paper_trading_app_TV.py - app_web.py imports all three. accounts.py and
# cloud_llm.py are the multi-user layer and the optional cloud AI client.
# pg_compat.py / shared_log.py / run_cycle_job.py are the hosted-database
# backend, the shared activity feed and the external one-shot cycle runner.
COPY app_web.py accounts.py cloud_llm.py engine.py paper_trading_app_TV.py tv_data.py ./
COPY pg_compat.py shared_log.py run_cycle_job.py migrate_to_postgres.py ./
COPY web/ ./web/
COPY deploy/ ./deploy/

# The database and web_config.json (password hash + session secret) live on a
# volume, NOT in the image: recreating the container must never lose the
# account, the ledger or the password.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# /api/health is auth-free on purpose, so an orchestrator can probe it. The port
# is read from the environment because hosts like Render inject their own PORT.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import os, sys, urllib.request; \
port = os.environ.get('PORT', '8080'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + port + '/api/health', timeout=8).status == 200 else 1)"

# --host 0.0.0.0 inside the container; publish it to the host loopback only
# and put a TLS reverse proxy in front of it (see deploy/nginx.conf). PORT is
# honoured when the platform provides one (Render does), otherwise 8080.
CMD ["sh", "-c", "python app_web.py --host 0.0.0.0 --port ${PORT:-8080}"]
