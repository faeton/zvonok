# zvonok — phone-call tool for AI agents

Self-hosted service giving OpenClaw and Claude Code a `phone_call(number, goal, language)` tool via Zadarma SIP. Full spec: **BRIEF.md** (read it first — it is the source of truth for all architecture decisions). Registrations checklist: **ACCOUNTS.md** (git-ignored, local/de1 only — it holds real account data).

## Hard decisions (do not re-litigate without new evidence)

- Stack: LiveKit Server OSS + livekit-sip + LiveKit Agents (Python) on **de1**, NOT AVA/Pipecat/Vapi (see BRIEF §2).
- Hosting: **de1 only** for the realtime path; n5 is disqualified (no static IP, jittery 44–90 ms path) — measured, see BRIEF §3.
- Zadarma: IP-auth trunk, G.711 alaw only, caller ID per destination country via From/PAI.
- Default voice brain: Grok Voice realtime; cascade (Deepgram STT→LLM→TTS) as fallback profile.
- Agents never block on a call: async job API (`POST /v1/calls` → poll/webhook), MCP wraps it.

## Conventions

- Deploy target is de1 (`ssh de1`), Docker Compose. Never expose Redis/Postgres; 5060 scoped to Zadarma IPs.
- Secrets: env files on de1 only, never in this repo.
- Every call must disclose "automated assistant, call is transcribed" (BRIEF §8) — this is a product requirement, not decoration.
- Spend guardrails (destination allowlist, daily caps, idempotency) are mandatory from phase 2 on — this tool costs real money per invocation.
- Language is explicit per call (ru/en/es); no autodetect.
