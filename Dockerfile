FROM python:3.11.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DEBIAN_FRONTEND=noninteractive
# libgl/glib: OpenCV · libreoffice-writer: permit request PDF · fonts: Arabic rendering in PDF
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 curl libreoffice-writer fonts-dejavu fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY pyproject.toml README.md requirements.lock ./
RUN mkdir -p app && pip install -r requirements.lock && pip install --no-deps -e .
COPY app ./app
COPY config ./config
COPY ui ./ui
# P0-06: run as an unprivileged user; data and the OCR model cache are the only writable paths
RUN useradd -r -u 10001 -m -d /home/app app && mkdir -p /srv/data /home/app/.EasyOCR && chown -R app:app /srv /home/app
USER app
ENV HOME=/home/app
EXPOSE 8000 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD curl -sf http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
