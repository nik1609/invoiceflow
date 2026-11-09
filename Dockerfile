FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && mkdir -p /data && chown -R app /app /data
USER app
ENV PYTHONUNBUFFERED=1 IF_DATA_DIR=/data
# Sample invoices, the procurement register and the opening ledger are generated on first start.
# Cloud Run supplies PORT; other hosts use 7860.
EXPOSE 7860
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
