FROM python:3.12.11-alpine3.22

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY deploy/astraquote-mcp-oauth/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-compile -r /app/requirements.txt \
    && addgroup -g 10001 oauth \
    && adduser -D -u 10001 -G oauth oauth \
    && mkdir -p /data \
    && chown 10001:10001 /data

COPY deploy/astraquote-mcp-oauth/app.py /app/app.py

USER 10001:10001
EXPOSE 8001

HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/readyz', timeout=3).read()"]

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log", "--no-server-header"]
