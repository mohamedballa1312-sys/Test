FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DEBIAN_FRONTEND=noninteractive
# libgl/glib: OpenCV · libreoffice-writer: permit request PDF · fonts: Arabic rendering in PDF
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 curl libreoffice-writer fonts-dejavu fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY pyproject.toml README.md ./
RUN mkdir -p app && pip install -e ".[dev]" && pip install --ignore-installed "cryptography>=42" easyocr python-docx
COPY app ./app
COPY config ./config
COPY ui ./ui
RUN mkdir -p /srv/data
EXPOSE 8000 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD curl -sf http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
