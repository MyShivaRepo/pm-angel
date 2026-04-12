FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml .
RUN uv pip install --system --no-cache -r pyproject.toml

COPY src/ src/

ENV PYTHONPATH=/app/src

RUN mkdir -p /app/data

EXPOSE 8888

CMD ["uvicorn", "pm_angel.main:app", "--host", "0.0.0.0", "--port", "8888"]
