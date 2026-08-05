# zvonok

**Give your AI agent a telephone.** Self-hosted `phone_call(number, goal, language)` tool: a realtime voice model places a real outbound call over a SIP trunk, holds a goal-directed conversation, and returns a transcript plus structured JSON answers.

Built as a personal-assistant actuator — Claude Code (via MCP) or OpenClaw (via a skill) can book a table, ask a hotel about parking, check whether an order is ready. One task, one call.

```
your agent ──MCP/REST──► call-api ──dispatch──► voice agent ──SIP──► PSTN ──► a real phone
                          │  policy, caps,       │  Grok Voice realtime,
                          │  idempotency,        │  disclosure, DTMF,
                          │  audit, extractor    │  voicemail detection
                          ▼                      ▼
                    Postgres state          transcript + confirmed facts
```

## Layout

| Directory | What it is |
|---|---|
| `agent/` | The voice agent — LiveKit Agents + Grok Voice realtime, G.711 telephony |
| `api/` | call-api — FastAPI + Postgres: job state machine, destination policy, spend caps, transcript→JSON extractor |
| `mcp/` | MCP server exposing `phone_call` / `phone_call_result` to Claude Code etc. |
| `openclaw/` | The `phone-call` skill for OpenClaw, installed from this repo |
| `deploy/` | Docker Compose stack (redis, livekit-server, livekit-sip, agent, postgres, call-api), firewall, tooling |

`BRIEF.md` is the full design record — architecture, measured findings, and every trap already hit, so you don't hit it again.

## What makes it careful

This tool spends real money and rings real strangers, so the guardrails are structural, not vibes:

- **Disclosure is mandatory and code-enforced.** Every call says it is an AI assistant and that answers are noted down; a watchdog re-delivers the disclosure uninterruptibly if barge-in shredded it. If the callee objects, their words are discarded — literally.
- **Default-deny destinations.** Country allowlist, premium-rate/satellite ranges refused, emergency numbers structurally unreachable.
- **Spend guardrails.** Per-identity daily caps (calls / minutes / USD), global concurrency cap, cost estimated *before* dialling with origin-aware pricing.
- **Idempotency everywhere.** A retried request cannot ring the same person twice.
- **Numbers are confirmed, not just heard.** 8 kHz lines mishear digits; unconfirmed values are flagged `unreliable_fields`, and the agent is refused hangup until it has read the key value back.
- **No personal data in this repo.** The owner's name, phone numbers and account identifiers live in env on the deploy host only.

## Setting it up

**[SETUP.md](SETUP.md)** is written so that an AI agent (Claude Code, OpenClaw, or similar) can set the whole thing up for its human end-to-end — including the exact list of things to ask the human for. Short version:

1. A Linux server with a static IP and Docker (low latency to your call regions matters — measure it).
2. A Zadarma account (or any SIP trunk with IP-auth and G.711 alaw) with a DID or two.
3. An xAI API key (voice brain + extraction model).
4. Tailscale (call-api binds to the tailnet address only — no public API surface).
5. `cp deploy/.env.example deploy/.env`, fill it in, `./deploy.sh`, create the trunk, then `deploy/echotest.sh` (Zadarma's free echo test — proves the media path with no human involved) before placing a real test call to yourself.

## Operating it

`deploy/README.md` covers day-to-day operation, health checks, and the gotchas already paid for. `mcp/README.md` wires Claude Code up. `openclaw/phone-call/SKILL.md` is what an agent reads before dialling — the etiquette section applies to humans too.

## License

MIT — see [LICENSE](LICENSE).
