FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY db ./db
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /nonexistent --no-create-home app
USER app
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=4s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/v1/users/health/ready', timeout=3)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
