FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY bruno ./bruno
COPY data ./data
RUN pip install ".[postgres]"

EXPOSE 8000
CMD ["python", "-m", "bruno"]
