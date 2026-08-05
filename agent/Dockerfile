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
# ⚠ EXPLICIT LIST, SO ADDING A MODULE MEANS ADDING IT HERE. `recorder.py` was
# written, imported and deployed without this line: the image built fine, the
# container "started", and agent.py died on the import in a restart loop that
# only showed up when a call went nowhere. A deploy that greps for "Started"
# does not catch it — check `docker logs` for a traceback.
COPY agent.py answerer.py prompts.py recorder.py report.py timing.py voice.py ./

# Non-root: this process talks to the internet and to a paid API.
RUN useradd -m -u 10001 zvonok && mkdir -p /data/transcripts && chown -R zvonok /data
USER zvonok

CMD ["python", "-u", "agent.py", "start"]
