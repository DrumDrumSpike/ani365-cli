FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /data && chown bot:bot /data && chmod 700 /data
COPY ani365_bot/ ani365_bot/
COPY ani365-cli-master/LICENSE ./LICENSE
LABEL org.opencontainers.image.source="https://github.com/DrumDrumSpike/ani365-cli" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.title="ani365-bot"
USER bot
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "ani365_bot.control", "health"]
CMD ["python", "-m", "ani365_bot"]
