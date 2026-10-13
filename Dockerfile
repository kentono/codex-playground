# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=custom_ca \
    if [ -f /run/secrets/custom_ca ]; then export PIP_CERT=/run/secrets/custom_ca; fi; \
    pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home appuser && mkdir -p /data/uploads && chown -R appuser:appuser /data
COPY --chown=appuser:appuser app ./app
USER appuser
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
