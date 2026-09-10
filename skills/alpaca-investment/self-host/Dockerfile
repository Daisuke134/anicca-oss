FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY apps/life-manager/scripts/install-alpaca-cli.sh /tmp/install-alpaca-cli.sh
RUN bash /tmp/install-alpaca-cli.sh /usr/local/bin/alpaca && rm /tmp/install-alpaca-cli.sh

COPY skills/alpaca-investment/ /app/skills/alpaca-investment/
COPY skills/_shared/telegram.py /app/skills/_shared/telegram.py
COPY skills/_shared/marketplace-core/scripts/telegram_outbox.py /app/skills/_shared/marketplace-core/scripts/telegram_outbox.py
COPY apps/life-manager/scripts/cloud-investment-agent-runner.js /app/cloud-investment-agent-runner.js
COPY skills/alpaca-investment/self-host/entrypoint.sh /usr/local/bin/investment-loop
RUN chmod 0755 /usr/local/bin/investment-loop \
    && useradd --create-home --uid 10001 investment \
    && mkdir -p /data/investment /run/investment \
    && chown -R investment:investment /data/investment /run/investment

USER investment
ENV ALPACA_CLI=/usr/local/bin/alpaca \
    LIFE_MANAGER_INVESTMENT_AGENT_RUNNER=/app/cloud-investment-agent-runner.js \
    LIFE_MANAGER_INVESTMENT_DEPLOYMENT=local

ENTRYPOINT ["investment-loop"]
