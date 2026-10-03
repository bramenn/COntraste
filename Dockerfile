FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data HF_HOME=/opt/models PLAYWRIGHT_BROWSERS_PATH=/opt/pw
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl poppler-utils \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home app && mkdir -p /data /opt/models && chown app /data /opt/models

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

# Bake the models into the image: embeddings (deduplication) and Whisper (transcription).
ARG WHISPER_MODEL=small
USER app
RUN python -c "from huggingface_hub import hf_hub_download as d; r='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'; d(r,'tokenizer.json'); d(r,'onnx/model_quint8_avx2.onnx')" \
    && python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

COPY --chown=app . .
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fs http://localhost:8080/healthz || exit 1
# Open progress streams are closed after 30 s on shutdown (browsers reconnect to another replica).
# WEB_WORKERS processes share the port, so every core can serve pages; each runs its own job worker too
# (MAX_CONCURRENT_CHECKS is per worker).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers ${WEB_WORKERS:-4} --timeout-graceful-shutdown 30"]
