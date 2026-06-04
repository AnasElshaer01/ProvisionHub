FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen

# Copy source
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini .

# Run migrations and start app
CMD ["sh", "-c", "alembic upgrade head && uvicorn provisionhub.main:app --host 0.0.0.0 --port 8000"]
