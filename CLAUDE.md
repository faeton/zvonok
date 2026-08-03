# zvonok — phone-call tool for AI agents

Self-hosted service giving OpenClaw and Claude Code a `phone_call(number, goal, language)` tool via Zadarma SIP. Full spec: **BRIEF.md** (read it first — it is the source of truth for all architecture decisions). Registrations checklist: **ACCOUNTS.md** (git-ignored, local/de1 only — it holds real account data).

## Hard decisions (do not re-litigate without new evidence)

- Stack: LiveKit Server OSS + livekit-sip + LiveKit Agents (Python) on **de1**, NOT AVA/Pipecat/Vapi (see BRIEF §2).
- Hosting: **de1 only** for the realtime path; n5 is disqualified (no static IP, jittery 44–90 ms path) — measured, see BRIEF §3.
- Zadarma: IP-auth trunk, G.711 alaw only, caller ID per destination country via From/PAI.
- Default voice brain: Grok Voice realtime; cascade (Deepgram STT→LLM→TTS) as fallback profile.
- Agents never block on a call: async job API (`POST /v1/calls` → poll/webhook), MCP wraps it.
- Multi-tenant = **one billing account per tenant, one worker per tenant, routed by `agent_name`** (BRIEF §5.7). Account-identifying config never inherits between tenants; a misconfiguration must fail startup, never produce a working call billed to the wrong person. Two identities may share a tenant — caps are per identity, trunk/DIDs/keys are per tenant.

## Conventions

- Deploy target is de1 (`ssh de1`), Docker Compose. Never expose Redis/Postgres; 5060 scoped to Zadarma IPs.
- Secrets: env files on de1 only, never in this repo. The live one is `~/.config/zvonok/env`; `deploy/.env` is a symlink to it (`deploy.sh` relinks if missing).
- **Never `rsync --delete` into `~/zvonok` on de1.** The repo is gitignored-heavy, so --delete removes exactly the files that exist only on de1 — it wiped `deploy/.env` once and nothing warned, because the running containers kept the stack alive. Sync with `--exclude '.env'` or without `--delete`.
- Every call must disclose "automated assistant, call is transcribed" (BRIEF §8) — this is a product requirement, not decoration.
- Spend guardrails (destination allowlist, daily caps, idempotency) are mandatory from phase 2 on — this tool costs real money per invocation.
- Language is explicit per call (ru/en/es/pl); no autodetect. The ASR hint uses **xAI's** field names — `language_hint` / `keyterms`, not OpenAI's `language` / `keywords` (BRIEF §5.3.2). The realtime server echoes unknown keys back unchanged, so a wrong name looks confirmed and does nothing.
- The voice agent is split by concern: `prompts.py` (what it says), `answerer.py` (who picked up), `timing.py` (every clock), `voice.py` (the model), `agent.py` (lifecycle only). The first three import nothing heavy and are covered by `api/tests/test_policy.py` — keep it that way.
