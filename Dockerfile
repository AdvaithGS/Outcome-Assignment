FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tesseract: fallback OCR; libgl1 + libglib2.0-0: required by OpenCV, which RapidOCR uses
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# everything stateful lives on the /data volume: job DB, uploads, results, caches, spend ledger
ENV DATA_DIR=/data \
    LLM_CACHE_PATH=/data/cache/llm_cache.sqlite \
    ARTIFACT_CACHE_DIR=/data/cache/artifacts \
    LLM_LEDGER_PATH=/data/ledger/spend.jsonl

EXPOSE 8080
CMD ["uvicorn", "app.service.api:factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
