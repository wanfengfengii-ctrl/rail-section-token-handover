FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 多 worker 也安全：所有移交事务由 SQLite 的 BEGIN IMMEDIATE 串行化。
# 用 1 个 worker 让锁竞争保持在 busy_timeout 可覆盖的范围内。
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
