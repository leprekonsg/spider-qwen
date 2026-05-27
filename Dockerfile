FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e ".[server]"

ENV SPIDER_QWEN_STATE_DIR=/tmp/spider_qwen
EXPOSE 8000

CMD ["uvicorn", "spider_qwen.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
