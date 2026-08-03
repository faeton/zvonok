FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Flat, not a package: the worker is started as `python agent.py start`, so the
# modules it imports have to be importable by bare name from the working dir.
COPY agent.py answerer.py prompts.py report.py timing.py voice.py ./

# Non-root: this process talks to the internet and to a paid API.
RUN useradd -m -u 10001 zvonok && mkdir -p /data/transcripts && chown -R zvonok /data
USER zvonok

CMD ["python", "-u", "agent.py", "start"]
