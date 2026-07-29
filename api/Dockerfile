FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schema.sql .
COPY zvonok_api ./zvonok_api

# Same uid as the agent container, so the transcript directory bind-mounted into
# both is readable here without a second chown.
RUN useradd -m -u 10001 zvonok
USER zvonok

CMD ["uvicorn", "zvonok_api.main:app", "--host", "0.0.0.0", "--port", "8130"]
