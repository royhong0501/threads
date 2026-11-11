FROM python:3.12-slim-bookworm

# Google Chrome（程式以 channel="chrome" 使用系統的 Chrome）、中日韓字型，
# 以及登入時用來顯示瀏覽器視窗的虛擬螢幕 + noVNC
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    google-chrome-stable libatomic1 fonts-noto-cjk xvfb x11vnc novnc websockify \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright pillow

WORKDIR /app
COPY logtail.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    LOGTAIL_CMD="docker compose run --rm --service-ports logtail" \
    LOGTAIL_CHROME_ARGS="--no-sandbox --disable-dev-shm-usage" \
    LOGTAIL_LOGIN_AUTOCLOSE=1

VOLUME /app/.profile
EXPOSE 6080
ENTRYPOINT ["/app/docker-entrypoint.sh"]
