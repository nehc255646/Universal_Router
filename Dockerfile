FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# 配置与日志目录（可挂载卷持久化）
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
