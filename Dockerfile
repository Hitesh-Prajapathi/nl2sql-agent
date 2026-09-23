FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml .
COPY src/ src/

RUN uv pip install --system -e ".[dev]"

ENV PYTHONPATH=/app/src

EXPOSE 8000
CMD ["uvicorn", "nl2sql.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
