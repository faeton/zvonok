# Setting up zvonok

This guide is written for an **AI agent setting zvonok up on behalf of a human**
(Claude Code, OpenClaw, or similar), though a human can follow it too. It tells
you what to collect from your human, what to provision, in what order, and how
to verify each step before spending money on the next one.

Read `BRIEF.md` §9 first if anything goes wrong — it records every trap this
project already hit, with the fix.

## 0. What you are building

A Docker Compose stack on one server: LiveKit (media) + livekit-sip (SIP↔room
bridge) + a Python voice agent (Grok Voice realtime) + Postgres + call-api
(FastAPI). Your human's other agents then call it over MCP or REST. All state,
all secrets, and all personal data live on that server — nothing personal is in
this repository, and it must stay that way.

## 1. Ask your human for these

Collect everything up front; each maps to a `deploy/.env` value.

| What to ask for | Why | Goes into |
|---|---|---|
| A Linux server (Debian/Ubuntu, Docker, root) with a **static IP** | SIP IP-auth and RTP need it; a VPS near the call destinations keeps latency sane (target <50 ms to the SIP provider's POP — measure with `ping`/`mtr` before committing) | — |
| Permission to open **UDP 5060 + 10000-20000** to the SIP provider's ranges only | telephony signalling + media | `deploy/firewall.sh` |
| A **Zadarma account** (or another SIP trunk provider that does IP-auth and G.711 alaw), topped up, with at least one DID | the actual telephony | trunk setup, `ZVONOK_OWNED_CALLER_IDS`, `ZVONOK_DEFAULT_CALLER_ID` |
| An **xAI API key** | Grok Voice realtime (the voice) + a Grok text model (the extractor) | `XAI_API_KEY` |
| **Tailscale** on the server and on every machine that will place calls | call-api binds to the tailnet address only; there is deliberately no public endpoint | `ZVONOK_BIND_HOST` |
| First name (Latin + Cyrillic if Russian calls are wanted, incl. genitive), full name, a booking/callback phone, a messenger phone | what the agent may hand out mid-call when a booking needs it | the `ZVONOK_OWNER_*` / `*_PHONE*` block |
| Which countries they actually want to call | keep the allowlist tight | `api/zvonok_api/policy.py` `_PREFIXES` |
| Daily budget (calls / minutes / USD) | spend caps are enforced before dialling | `ZVONOK_DAILY_*` |

Ask before assuming: **which languages** (ru/en/es/pl supported), and whether any
destination country has two-party-consent rules they care about (then the
`full` disclosure wording applies — see BRIEF §8).

## 2. Provision the trunk (Zadarma specifics)

1. Register, top up, buy/verify DIDs. Prefer a DID in the **origin group that
   is cheap for your destinations** — measured here: EU/UK destinations were
   ×20–34 cheaper from a UK DID than from a UA one. Verify your own pricing
   with the provider's calculator before trusting any defaults.
2. Create a SIP trunk with **IP authorization** for the server's static IP
   (no username/password) and **G.711 alaw** only.
3. API keys (optional, for spend reconciliation) need an e-mail confirmation
   link on creation.

## 3. Deploy

On the server:

```bash
git clone https://github.com/faeton/zvonok && cd zvonok/deploy

# Secrets live OUTSIDE the checkout, with deploy/.env as a symlink to them.
# The checkout gets rsync'd and pulled over; the secrets must survive that.
mkdir -p ~/.config/zvonok && chmod 700 ~/.config/zvonok
cp .env.example ~/.config/zvonok/env && chmod 600 ~/.config/zvonok/env
ln -s ~/.config/zvonok/env .env
# fill in EVERY section of ~/.config/zvonok/env — each variable is documented inline

./deploy.sh                  # renders configs, builds, starts the stack
sudo ./firewall.sh           # scopes 5060+RTP to the SIP provider's ranges
./lkctl.sh create-trunk      # prints SIP_OUTBOUND_TRUNK_ID → put it in .env
docker compose up -d agent   # picks up the trunk id
```

Generate secrets as documented in `.env.example` (`openssl rand …`). Never
reuse a token between two client identities — per-identity tokens are what
make per-agent caps and audit work.

⚠ **Do not `rsync --delete` into the checkout.** Everything that only exists on
the server is gitignored, which is precisely what `--delete` removes: it wiped
`deploy/.env` once — every key, token and DID — and nothing warned, because the
running containers held their environment and the stack stayed up. The symlink
above means the worst case is now a broken link that `deploy.sh` repairs on its
next run. Sync with `--exclude '.env'`, or without `--delete`.

Verify: `docker compose ps` all healthy; `docker compose logs --tail 5 sip`
shows port 5060 listening; `./lkctl.sh trunks` shows your trunk.

## 4. First call — to your human, not to a stranger

```bash
./call.sh <your-human's-number> "Confirm you reached the right person, ask how the audio sounds, then end the call." en
docker compose logs -f agent
```

Checklist on that call:

- [ ] Audio both ways, no half-duplex, agent audible and loud enough
      (`ZVONOK_OUTPUT_GAIN`, default 1.4 ≈ +3 dB, if not)
- [ ] The agent disclosed being an AI and that answers are noted down
- [ ] It hung up on its own
- [ ] A transcript landed in `deploy/transcripts/` and
      `GET /v1/calls/<id>/result` returns it

Do not point other agents at the tool until this passes.

## 5. Connect the callers

- **Claude Code** → `mcp/README.md` (MCP server config with the tailnet URL and
  a `mac-claude` token).
- **OpenClaw** → `openclaw/install.sh` installs the `phone-call` skill;
  it uses REST with its own token.

Then have the calling agent read `openclaw/phone-call/SKILL.md` — it is the
etiquette contract (one task one call, no unsolicited calls, mind the hour,
read `unreliable_fields` before believing a number).

## 6. Personalization your human will thank you for

All optional, all env, all in `deploy/.env.example` with examples:

- `ZVONOK_ASSISTANT_NAME[_RU]` — what the assistant calls itself.
- `ZVONOK_OWNER_*` — first name for bookings, full name only if a venue
  insists. Unset values make the agent promise "the client will confirm"
  rather than invent details.
- `ZVONOK_BOOKING_PHONE` — normally the DID you call from (callable, SMS-able).
- `ZVONOK_MESSENGER_PHONE` + `_NOTE` — handed out only when a messenger is
  specifically needed; the note is a dictation hint ("five zeros in a row").
- Per call, callers can set `introduce_as` — "a potential client" (default),
  "your regular customer", or the owner's first name for callees who know
  them. The AI + note-taking disclosure itself is not configurable.

## 7. A second person on the same box

Only if they have their **own Zadarma account and their own xAI key**. Sharing
your trunk is not a lighter version of this — their calls would present your
number, bill your balance, and route every callback to you.

Each billing account is a **tenant**: its own worker container, its own trunk,
its own DIDs, its own extraction key. Each bearer token is an **identity**, with
its own caps and audit trail; several identities can belong to one tenant (your
mac-claude and openclaw both belong to yours).

1. They create a Zadarma trunk with **SIP credential auth**, not IP auth — yours
   is authorised by de1's IP, and two accounts claiming one source IP makes
   billing ambiguous. `./lkctl.sh create-trunk` with their credentials.
2. Fill in the second-tenant block at the bottom of `.env` (every variable is
   documented inline), including `COMPOSE_PROFILES="tenant2"`.
3. Set a concurrency share for **both** of you — otherwise either can fill the
   box alone. call-api warns at startup if you forget.
4. `./deploy.sh`, then check the startup log: it prints one line per tenant with
   the worker name, caller IDs and identities it resolved. If a tenant is
   missing its own key or worker name, call-api refuses to start rather than
   quietly falling back to yours.

What is still shared and cannot be split without a second LiveKit and a second
Postgres: the LiveKit API key (room admin), the internal token, the jobs
database, and the transcript directory. Neither of you can read the other's
calls through the API, but their callees' voice data lives on your disk under
your retention policy. Fine between people who know each other; not a
substitute for per-tenant infrastructure.

## 8. Ongoing

- Caps reset daily; check spend against the provider's statistics API rather
  than trusting the estimator forever (BRIEF §7).
- `docker compose logs` are size-capped; transcripts are the durable record.
- Retention: `ZVONOK_RETENTION_DAYS` (default 180).
