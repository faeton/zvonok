# zvonok — a phone-call tool for AI agents

**Status:** phases 0, 1 and 2 complete and verified on live calls; phase 3 (RU/ES benchmarks, cascade profile, IVR/DTMF, retries, cost reconciliation) next (2026-07-27)
**Owner:** faeton
**Deploy target:** de1 (Hetzner dedicated, Debian, Docker) — see §3 for why
**Related docs:** [ACCOUNTS.md](./ACCOUNTS.md) (what to register where), [CLAUDE.md](./CLAUDE.md) (session conventions)

---

## 1. What we are building

A self-hosted service that gives AI agents (OpenClaw on de1, Claude Code anywhere) the ability to **make real phone calls to accomplish a task** and get the answer back:

```
phone_call(number, goal, language) → transcript + structured answers
```

Canonical use cases:

- "Call this hotel and ask whether they have parking and what it costs per night."
- "Call the restaurant and book a table for 2 at 20:00 tomorrow, name Alex."
- "Call this office and find out their opening hours in August."

Properties:

- **Outbound-first.** Single, goal-directed task calls triggered programmatically. Inbound handling is a later phase, not MVP.
- **Self-hosted core.** Call control, orchestration, transcripts, results live on our hardware. Cloud APIs (voice model, STT) are acceptable as swappable providers.
- **Async job model.** A phone call takes 30 s – 10 min; agents must never block a turn on it. Submit → get `call_id` → poll or webhook.
- **Carrier:** existing Zadarma account with several DIDs in different countries (incl. UK). Caller ID should match the callee's country when possible (raises answer rates).

### Non-goals (for now)

- No call-center features: queues, campaigns, human agent seats.
- No inbound IVR (phase 4 at the earliest).
- No bulk/marketing calling — ever. This is a personal assistant tool making individual legitimate calls.

---

## 2. Decision record

Three independent assessments (Claude, Codex/GPT, Grok, 2026-07-27) converged on the same stack.

### 2.1 Chosen stack

| Layer | Choice | Why |
|---|---|---|
| SIP/PSTN carrier | **Zadarma**, static-IP-auth trunk | Already have account + multi-country DIDs; official Asterisk/PBX docs; caller-ID selection via `From`/`P-Asserted-Identity` |
| Media + SIP bridge | **LiveKit Server OSS + livekit-sip + Redis** (Docker, self-hosted) | Outbound calls are first-class API objects (`CreateSIPParticipant`, `wait_until_answered`); huge active ecosystem (19k★ core, frequent releases) |
| Voice agent runtime | **LiveKit Agents (Python)** | Dispatch API passes per-call metadata `{number, goal, language, schema}` — exactly our pattern; official example `livekit-examples/outbound-caller-python` |
| Default voice brain | **xAI Grok Voice realtime** (`wss://api.x.ai/v1/realtime`) via official `livekit-plugins-xai` | Public S2S API, $0.05/min flat, tool calling in-session, OpenAI-Realtime-compatible protocol |
| Other S2S profiles | **Provider-pluggable via LiveKit `RealtimeModel`**: OpenAI gpt-realtime, Amazon Nova 2 Sonic, Gemini Live (each has an official LiveKit plugin) | Decision 2026-07-27: stay ready for every S2S provider with a LiveKit plugin; swapping is a one-line profile change. Priority to try: xAI → OpenAI → Nova Sonic. Benchmark on our own ru/en/es 8 kHz call matrix after MVP, not on public leaderboards |
| Fallback voice profile | **Deepgram streaming STT → text LLM → streaming TTS** (cascade) | On 8 kHz PSTN audio, telephony-tuned STT often beats S2S for reliability, esp. non-English; same job API, different agent profile |
| Answer extraction | Separate **text-model pass over the final transcript** against a JSON Schema | More reliable than trusting the voice model to emit JSON mid-call; also the injection boundary (see §8) |
| Orchestrator | Small **FastAPI "call-jobs" service + Postgres** | Job queue, state machine, transcripts, results, webhooks, audit |
| Agent interface | **MCP server** wrapping the REST API | Both Claude Code and OpenClaw consume MCP; tools: `phone_call`, `phone_call_result` |

### 2.2 Rejected / deferred alternatives

- **AVA (Asterisk-AI-Voice-Agent)** — impressive and active (v7.5.2 July 2026, native Grok Voice provider, 8 kHz-aware media path), but it is PBX-centric: inbound agents + CSV campaign scheduler. It has **no per-call dynamic REST API** ("originate one call with this goal, return transcript"), which is our whole product. Kept as **Plan B**: if LiveKit SIP ↔ Zadarma interop fails, insert a thin `asterisk-edge` container (Zadarma publishes exact PJSIP configs) between livekit-sip and Zadarma; or in the extreme, switch the whole media layer to AVA and bolt a job API onto ARI.
- **Pipecat** — strong pipeline framework, has `GrokRealtimeLLMService`, but no first-party generic SIP gateway; you end up deploying LiveKit (or a carrier bridge) anyway. Use LiveKit Agents directly.
- **Vapi / Retell (hosted)** — Zadarma officially documents BYO-SIP integration with both. Legitimate as a **1-day prompt-validation spike** before self-hosting, or as an emergency fallback. Not the base: recurring per-minute platform fees, call data leaves our infra, vendor lifecycle lock-in.
- **Local inference on n5** — rejected for the realtime path, see §3.

### 2.3 Codec reality (settled; re-verified 2026-07-27 by Claude + Codex + Grok independently)

Zadarma supports **G.711 alaw/ulaw only** in its recommended configs (their PJSIP template: `disallow=all; allow=alaw; allow=ulaw`). No Opus/G.722 anywhere in their docs. Re-check findings: softphone docs additionally list G.729/GSM/Speex (all worse for ASR — never enable); a 2025 r/VOIP thread quotes Zadarma support confirming G.722 works only between own PBX extensions while **external/PSTN calls are always converted to G.711**; their API exposes no codec/transcoding controls. So even if the trunk negotiated wideband, it would be transcoded before termination — G.711 is the real ceiling, and this is fine, not worth fighting:

- The PSTN last mile to a hotel desk phone is 8 kHz narrowband regardless of carrier. "HD voice to PSTN" does not exist for anyone.
- G.711 is **uncompressed** narrowband — the best-for-ASR option they offer. Never enable G.729/GSM/speex (bandwidth savers that damage ASR).
- Quality investment belongs on our side: correct 8 kHz resampling into the voice model, telephony-tuned STT in the fallback profile.
- If a specific destination ever justifies HD, add a second trunk (e.g. Telnyx — documented G.722/Opus/AMR-WB and a LiveKit HD-voice integration) behind the same orchestrator. Only after measuring an ASR problem on a concrete route AND verifying via SDP/RTP capture that wideband actually survives to that destination (mobile VoLTE/EVS terminations can in theory, classic PSTN cannot). Not now.
- DTMF: RFC2833/telephone-event (Zadarma requires it; codec-independent).

---

## 3. Hosting decision: de1, not n5

Measured 2026-07-27:

| From | → sip.zadarma.com | → api.x.ai (CF edge) | Notes |
|---|---|---|---|
| **de1** (Hetzner, DE, static IPv4) | **5.5 ms** avg, 0% loss, mdev 0.04 | 5.7 ms | datacenter uplink |
| **n5** (Spain, residential) | 11.1 ms avg | 11.1 ms | pings hit anycast edges; fine on the surface, but see below |
| de1 → n5 (Tailscale) | — | — | **44–90 ms RTT, mdev 18 ms** (jittery residential path) |

Decision: **everything realtime runs on de1.** The pings are not the reason — both look fine. The disqualifiers for n5:

1. **No static public IP.** n5 travels between households, LAN IP varies, DHCP everywhere. Zadarma IP-auth trunking is impossible; SIP registration + RTP through changing residential NATs (possibly CGNAT) is exactly the failure mode SIP is famous for.
2. **No clean port control.** livekit-sip needs 5060 + a 10000–20000/udp RTP range publicly reachable with a correctly advertised external IP. Not doable on other people's home routers.
3. **Jitter.** The 44–90 ms *jittery* path means n5 can't even serve as a low-latency inference backend for calls anchored on de1; each audio hop across it adds unstable delay.

Role for **n5**: none in the call path. Later, optionally: nightly sync of recordings/transcripts into the n5 archive (fits the data-catalog project), and offline batch work (re-transcription with local Whisper for private archival copies).

Note on api.x.ai: 5.7 ms is to the Cloudflare edge; actual realtime inference RTT will be higher (likely US-anchored) and is identical from either host. Not a siting factor.

---

## 4. Architecture

```
┌────────────────────────────┐
│ Claude Code / OpenClaw     │
│  MCP tools:                │
│   phone_call(...)          │
│   phone_call_result(id)    │
└──────────┬─────────────────┘
           │ HTTPS (Caddy, bearer token)
┌──────────▼─────────────────┐
│ call-api  (FastAPI)        │  jobs, state machine, transcripts,
│ + Postgres                 │  results, webhooks, audit, caps
└──────────┬─────────────────┘
           │ LiveKit control API (room + dispatch + SIP participant)
┌──────────▼─────────────────┐
│ livekit-server + Redis     │
│ livekit-sip  (host net)    │──── SIP (IP-auth) ───→ Zadarma ──→ PSTN
└──────────┬─────────────────┘
           │ WebRTC (local)
┌──────────▼─────────────────┐
│ voice-agent worker(s)      │
│  profile A: Grok Voice S2S │──── wss://api.x.ai/v1/realtime
│  profile B: Deepgram STT   │──── Deepgram / TTS / text LLM APIs
│    → text LLM → TTS        │
└──────────┬─────────────────┘
           │ final transcript
┌──────────▼─────────────────┐
│ extractor (text LLM pass)  │  answer_schema → answers JSON
└────────────────────────────┘
```

### Call lifecycle

1. `POST /v1/calls` — validate number (E.164), language, schema, caps; create job (`queued`).
2. Worker picks job: create LiveKit room, dispatch voice agent with metadata `{goal, language, max_duration, profile}` (`provisioning`).
3. Agent ready → `CreateSIPParticipant` toward Zadarma trunk with chosen caller ID (`dialing` → `ringing`).
4. Answered → conversation (`in_progress`). Agent has tools: `end_call`, `send_dtmf(digits)`, `mark_unreachable(reason)` (voicemail/IVR dead-end).
5. Hangup (either side) or `max_duration` cap → persist transcript + disposition (`post_processing`).
6. Extractor runs `answer_schema` over transcript → store `answers` + `summary` (`completed`).
7. Optional webhook (HMAC-signed, retried). Polling always available.

### State machine

```
queued → provisioning → dialing → ringing → in_progress → ending → post_processing → completed
```
Terminal alternatives from any pre-completed state: `busy`, `no_answer`, `rejected`, `voicemail`, `failed`, `canceled`, `timed_out`.

Keep **`call_status`** and **`processing_status`** separate: a successfully completed call whose extraction failed is *not* a failed call — re-run extraction without redialing. Retries create new attempt rows under the same job; never overwrite attempts.

---

## 5. Component specs

### 5.1 Zadarma trunk

- **Auth mode: static IP authorization** (not registration). de1 has a stable public IPv4; IP-auth avoids REGISTER lifecycle flapping under container restarts.
  - ⚠ Activating an IP trunk consumes the selected SIP login — that login can no longer take registered inbound. Use a dedicated SIP login for the trunk; keep another login free for future inbound.
- **Caller ID:** only numbers owned/verified in the Zadarma account. Select per call via `From:` (E.164) — priority mechanism is the `P-Asserted-Identity` header. Wrong/unowned PAI silently falls back to the default number (confusing to debug — test each DID explicitly).
  - Strategy: map destination country → our DID of that country; fallback to a designated default DID. Config table in call-api, exposed as optional `caller_id` override.
- **Codecs:** lock to `alaw` (primary) + `ulaw`. Nothing else.
- **DTMF:** RFC 2833 / `auto`. Verify early against a real IVR ("press 1 for reception") — task calls die on hotel auto-attendants far more often than on the LLM.
- **SIP endpoints** (from Zadarma's PJSIP `identify` block — all four are valid signalling sources):
  `sip.zadarma.com`, `sipurifr.zadarma.com`, `sipde.zadarma.com`, `sipuriny.zadarma.com` (+ `pbx.zadarma.com`).
- **Network block (resolved 2026-07-27, closes open question §10.2):** all five hostnames resolve inside **`185.45.152.0/22`** — a single routed prefix, AS199790 `IPTelecomBulgaria-AS` (Zadarma's carrier network). RDAP shows it split as `185.45.152.0–185.45.153.255` and `185.45.155.0/24`, same holder. Use `185.45.152.0/22` as the firewall scope for 5060 + RTP. Empirically derived, not published by Zadarma — still worth confirming with support before treating as authoritative, and re-check if SIP starts failing after a carrier renumber.
- **Reference config:** Zadarma's own PJSIP template (endpoint/aor/identify/transport) at their [IP-auth trunk guide](https://zadarma.com/en/support/instructions/asteriskpjsip/trunk/) — this is also the exact config to lift if we ever need the `asterisk-edge` Plan B (§2.2). Note their guide sets caller ID in `extensions.conf` via `Set(CALLERID(num)=…)`; our equivalent is the SIP `From` user, verified working in phase 0.
- **Account hygiene:** check outbound channel limit (standard logins ≈ 3 concurrent), enable auto top-up or balance alerts, disable premium-rate destinations account-side if possible.

### 5.2 LiveKit stack (Docker on de1)

| Service | Image | Network | Notes |
|---|---|---|---|
| `redis` | redis:7 | internal | LiveKit coordination + SIP session state |
| `livekit-server` | livekit/livekit-server | 7880 behind Caddy; ICE 7881/tcp + 7882/udp mux | keys in config; agents + SIP connect via ws |
| `livekit-sip` | livekit/sip | **host networking** (recommended by LiveKit) | `sip_port: 5060`, `rtp_port: 10000-20000`, `use_external_ip: true` (must advertise de1 public IP or → one-way audio) |
| `voice-agent` | our Python image (livekit-agents + livekit-plugins-xai + deepgram) | internal | worker registers with `agent_name: zvonok-caller` |
| `call-api` | our FastAPI image | behind Caddy | REST + webhook sender + extractor |
| `postgres` | postgres:16 | internal | jobs/attempts/transcripts/results/audit |
| `caddy` | caddy:2 | 80/443 | TLS for API + livekit ws endpoint |

**Firewall (de1):**

**Starting state (verified 2026-07-27): de1 already runs `nftables` (enabled+active, `/etc/nftables.conf`) with `table inet filter`, `policy drop` on both `input` and `forward`.** Note `ufw status` reports "inactive" — that is a red herring, ufw simply isn't the tool in use here; check `nft list ruleset`, not ufw.

Existing input chain: `established,related` accept · `lo` + `tailscale0` accept · `ct state invalid` drop · icmp/icmpv6 accept · `udp 41641` (tailscale) · `tcp {80,443,4443,5443}` · `tcp 52200` (SSH, per-source-IP meter 10/min burst 5, plus fail2ban). A `table ip raw` prerouting chain additionally hard-drops non-loopback traffic to the docker-proxy ports (8000, 8081, 8088, 5433, 12080, 12175). `forward` drops by default and accepts only bridge-originated traffic — which closes the classic "Docker publishes straight past your firewall" hole.

Consequence for us: **default-deny means 5060 is already closed**, and livekit-sip on host networking will be unreachable until we explicitly open it. So this is additive, not a policy rewrite:

- 5060/udp+tcp: **accept only from `185.45.152.0/22`** (Zadarma / AS199790 — derivation in §5.1); the drop policy handles everyone else. SIP scanners are constant and this trunk spends real money.
- 10000–20000/udp: same scope. Media comes from the same Zadarma block; widen only if RTP is observed arriving from outside it.
- ⚠ **Footgun: `/etc/nftables.conf` starts with `flush ruleset`.** A mid-session `systemctl restart nftables` wipes Docker's `iptables-nft` tables (nat/DOCKER chains) until Docker is also restarted. Harmless at boot (ordering works out), destructive at runtime. **Procedure: add rules live with `nft add rule …` AND persist the same lines into `/etc/nftables.conf` separately — never `restart nftables` to pick them up.** If you ever do restart it, restart Docker straight after.
- Unrelated tidy-up noted while surveying (not ours, not urgent): `0.0.0.0:3334` + `*:8130` (node) and `0.0.0.0:8090` (uwsgi) bind all interfaces and are saved only by the drop policy — no defence in depth if the ruleset ever fails to load. Binding them to loopback or the Tailscale IP would be tidier.
- 443/tcp: API + WS. 7881/tcp, 7882/udp: LiveKit ICE (only needed if non-local WebRTC participants ever join; agents are local).
- Redis/Postgres: never exposed.
- SIP over TLS (5061) + SRTP: try `allow` mode after MVP works plaintext; move to `require` once re-INVITEs/DTMF/hangups verified. (Encryption ends at the PSTN boundary anyway.)

**livekit-sip config** (verified against [docs](https://docs.livekit.io/transport/self-hosting/sip-server/) + [repo](https://github.com/livekit/sip), 2026-07-27). Passed as `SIP_CONFIG_BODY` env (full YAML inline) or `SIP_CONFIG_FILE`:

```yaml
api_key: <livekit api key>          # or LIVEKIT_API_KEY env
api_secret: <livekit api secret>    # or LIVEKIT_API_SECRET env
ws_url: ws://localhost:7880         # or LIVEKIT_WS_URL env
redis:
  address: localhost:6379           # must be the SAME redis as livekit-server
sip_port: 5060
rtp_port: 10000-20000
use_external_ip: true               # advertise public IP in SDP — omit → one-way audio
logging:
  level: debug
```
Optional keys: `health_port`, `prometheus_port`, `log_level`. Container **requires `network_mode: host`** (the 10k-port UDP range makes docker port mapping unusable) — LiveKit's own compose runs both `livekit-server` and `sip` with host networking.

**Outbound trunk object (LiveKit):** created once via `lk sip outbound create` with a JSON file:

```json
{ "trunk": { "name": "zadarma", "address": "sip.zadarma.com", "numbers": ["<UK-DID>"] } }
```
Fields on `SIPOutboundTrunkInfo`: `name`, `address`, `numbers[]`, `auth_username`, `auth_password`, `transport` (`SIP_TRANSPORT_AUTO|UDP|TCP|TLS`), `metadata`, `headers`, `headers_to_attributes`. Credentials, when used, go via CLI flags `--auth-user`/`--auth-pass`, **not** in the JSON.

⚠ **IP-auth nuance:** LiveKit's docs discourage IP authentication — but that caveat is about **LiveKit Cloud**, whose egress nodes have no static IP range. Self-hosted on de1 we have exactly one static IPv4 (<server-public-ip>), which is the entire premise of the Zadarma trunk. So: create the trunk with **no `auth_username`/`auth_password` at all**; Zadarma authorizes the source IP. `numbers[]` is what LiveKit puts in `From` — this is our caller-ID selector, and phase 0 proved Zadarma honours it (and bills on it — see §9 phase-0 findings on origin-based pricing).

### 5.3 Voice agent (Python, LiveKit Agents)

- Entry: dispatch metadata `{number, goal, language, caller_id, max_duration_seconds, profile, job_id}`.
- **Versions (checked 2026-07-27):** `livekit-agents` **1.6.7** (Python ≥3.10,<3.15), `livekit-plugins-xai` **1.6.7**, `livekit-api` 1.2.0, `livekit` (rtc SDK) 1.1.13. Install as `livekit-agents[xai]~=1.6`.
- **Profile A (default): Grok Voice** — `xai.realtime.RealtimeModel`, server VAD, session ≤ 30 min (xAI cap; ours is 5 min anyway).
  - **Access confirmed 2026-07-27**: our key opens `wss://api.x.ai/v1/realtime` and the server returns `session.created` with `model: "grok-voice-latest"`, `modalities: ["audio"]`, default `voice: "ara"` — OpenAI-Realtime-compatible event protocol, as assumed. Note the *plugin's* default model constant is `grok-voice-think-fast-1.0`; the bare endpoint resolves to `grok-voice-latest`. Voices: `ara`, `eve`, `leo`, `rex`, `sal`.
  - ⚠ **Gotcha — `instructions` silently dropped.** `livekit-plugins-xai`'s `RealtimeModel.__init__` did not accept/forward `instructions` to its `openai.realtime.RealtimeModel` parent, so **system prompts were ignored without error** ([agents#4305](https://github.com/livekit/agents/issues/4305), filed 2025-12-18, now closed). Our entire product *is* the system prompt (goal + §8 disclosure) — so **verify empirically at the pinned version** that the agent actually follows instructions before trusting any call. If it regresses, the fallback is `openai.realtime.RealtimeModel(base_url="https://api.x.ai/v1", api_key=XAI_API_KEY)` — protocol-identical, and the same swap that makes profile A′ cheap.
  - ⚠ **Gotcha — `noise_cancellation.BVCTelephony()`** appears in the canonical `outbound-caller-python` example but is **Krisp, a LiveKit Cloud-only feature**. Self-hosted it will not work — omit `noise_cancellation` from `RoomInputOptions`. G.711 from the PSTN is our only input conditioning.
- **Profiles A′ (alternate S2S): `openai-realtime`, `nova-sonic`, `gemini-live`** — same agent code, different LiveKit realtime plugin selected by `profile`. Keep API keys provisioned for at least OpenAI (protocol-identical to Grok, zero-risk swap) so an xAI outage or quality regression is a config change, not an incident. Compare all of them on the ru/en/es × {simple question, digit dictation, barge-in, noisy line, IVR} call matrix once profile A works.
- **Profile B (fallback): cascade** — Deepgram streaming STT (telephony model, per-language) → text LLM (Grok text or Claude) → streaming TTS (Cartesia or ElevenLabs). Selected per call via `profile`, or automatically for languages where A benchmarks poorly.
- **System prompt template** (per call, language-localized):
  > You are an assistant calling on behalf of {owner_name}. Your single goal: {goal}.
  > Open by saying you are an automated assistant calling on behalf of {owner_name} and that the call is transcribed to note the answer. If asked whether you are an AI, confirm honestly.
  > Be brief and polite. Confirm numbers, prices, and times by repeating them. If the person can't help, ask who can. When the goal is achieved or clearly unachievable, thank them and use end_call.
  > Facts to capture: {answer_schema field descriptions}.
- **In-call tools (keep minimal):** `end_call`, `send_dtmf`, `mark_unreachable(reason: voicemail|ivr_deadend|wrong_number|language_barrier|declined)`.

#### 5.3.1 Identity and the opening pattern (designed 2026-07-27, phase 1)

**Identity.** The agent is **Klava**, an **"AI assistant"** calling **on behalf of <owner>** (`<имя владельца>` on Russian calls). "AI assistant" is deliberate and load-bearing: it discharges the §8 disclosure inside the introduction itself instead of relying on the model to admit it when challenged. Never "automated assistant" (vaguer), never first-person-as-owner (that would be impersonation). Names are configured per language for **pronunciation** — `<имя владельца>`/`Клава` in Cyrillic on Russian calls, since the model mispronounces Latin spelling there. Env: `ZVONOK_{OWNER,ASSISTANT}_NAME[_RU]`.

**Never speak first.** A real person says "Hello?" and waits; talking over the pickup is the tell of a robocall. Whoever speaks first also reveals *which* of the four branches below applies. Only if the line stays silent do we open.

**Two utterances are fixed text, not model output** — both after the model demonstrably failed to produce them correctly on real calls:

- **The probe.** Asked for "one or two words like Hello?", the model instead said *"I'm listening — please go ahead"* — which sounds exactly like an IVR prompt and primes the callee to treat us as a machine. Now `session.say("Hello?")`, verbatim.
- **The disclosure.** On a real call the introduction was shredded by barge-in: `"This"` → `"This is Klava, an"` → `"on behalf of … This call is transcribed"`. The callee interrupted twice and **never once heard "Klava, an AI assistant"** — a §8 compliance failure, not a quality nit. A legal requirement cannot depend on a generative model winning a race against an impatient human and line noise. A `disclosure_guard` now checks what was *actually said* (a turn truncated by barge-in does not count) and, if no complete disclosure exists once a genuine two-way exchange is underway, speaks it verbatim with `allow_interruptions=False`. It waits for real dialogue precisely so it cannot fire at a call screener and break the four-words rule.

**`OPENING_SILENCE_SECONDS` is 3.0, not 1.5.** A callee's "hello" must cross the PSTN leg, the SIP media path and VAD before we see it; at 1.5 s we repeatedly concluded the line was silent when the person had already spoken, and talked over them.

**Four things can answer, and they need different behaviour:**

| Who | Cue | Behaviour |
|---|---|---|
| Person | short greeting, then pause | full intro in one breath (≤8 s), then straight to the question |
| **Screener** | "state your name after the tone", "who's calling?", "I'm screening this call" | **name ONLY** — "AI assistant Klava." Then silence. |
| Voicemail | "leave a message", "not available", beep | no message, `mark_unreachable("voicemail")` — decided 2026-07-27 |
| IVR | menu options, "press 1" | navigate by DTMF, ≤3 levels, no self-introduction to a machine |

⚠ **The screener rule is counter-intuitive and was learned the hard way.** A screener is not a listener — it records a short label and announces it to the human as *"You have a call from ___"*. A full sentence gets truncated or garbled there, so the human hears nonsense and declines. Give it four words. Everything else — the owner's name, the transcription notice, the question — is wasted at that stage, because **the human hears none of the screener exchange**. When they do come on the line, the agent must **introduce itself again in full**.

**Three distinct silences, three timers.** Conflating them made the agent simultaneously jumpy and slow:

1. `OPENING_SILENCE_SECONDS = 1.5` — nobody spoke after pickup → we open.
2. `END_OF_TURN_SILENCE_MS = 800` — they spoke and stopped → how long before assuming the turn ended. OpenAI's `server_vad` default of 500 ms is too eager on the phone: a screener says "Hello?" and only *then* "please state your name after the tone", so an eager agent answers the greeting and misses the actual request. Passed via `turn_detection=TurnDetection(type="server_vad", …)` — the xAI plugin does accept it (and `base_url`, despite the docs page omitting both).
3. `SILENCE_NUDGE_SECONDS = 6.0`, `MAX_SILENCE_NUDGES = 2` — mid-call dead air → nudge, then give up (~28 s from answer to hangup).
4. `NO_RESPONSE_BUDGET_SECONDS = 20.0` — absolute cap for a line where the callee has **never** made a sound, measured from answer. A distracted person trips timer 3 and gets the full humane cycle; a line that answered with no media has nobody to nudge, so it is cut without playing an introduction and two nudges into the void.

**Two ways a call must end that aren't `end_call`:**

- **Answered with no media at all.** A carrier can return `200 OK` and never deliver audio — observed 2026-07-27. Without a guard the job sat open for the full `max_duration`, burning a paid PSTN leg and realtime-model minutes on pure silence. After the nudges are exhausted → hang up, disposition `no_audio` (never spoke) or `abandoned` (went quiet mid-call).
- **Callee hangs up.** The SIP participant leaving does **not** close the room — the agent is still in it. Needs an explicit `participant_disconnected` handler, or the job idles to `max_duration`. Disposition `callee_hangup`.

Operational: `deploy/lkctl.sh rooms` lists live calls, `hangup [room|all]` force-ends one. Written after a stuck job had to be killed by hand.
- **Language:** always **explicit per call** (`ru`/`en`/`es` to start). No autodetect — it wrecks STT model choice, voice choice, and number/date formatting.
  - **Refined 2026-07-27 (S2S only).** The rule above exists because autodetect breaks *STT model selection*. A speech-to-speech model has no separate STT, so that reason does not apply to profile A: the call still **opens** in the requested language, but if the callee is plainly speaking another language we support, the agent switches and stays there. For the cascade profile B the original rule stands unchanged — hard-locked, never switch.
- **Audio discipline:** 8 kHz G.711 in/out; resample properly to the model's expected rate; test barge-in on a real phone (target: stop speaking ≤ 300 ms after callee starts; require a short confirmation window to avoid false barge-ins from line noise; commit to history only words actually played).
- **Answering machines/IVR:** do **not** enable aggressive AMD initially (hotel greetings look like voicemail → false positives). Instead: transcript-pattern recognition + bounded IVR phase (max N menu levels / DTMF presses) before the goal conversation.
- On session end: write full timestamped transcript (turns with speaker + t_start/t_end) + disposition to call-api; optionally save mixed-audio recording (flag per call, default off until §9 review).
- **Dial pattern** (from [`livekit-examples/outbound-caller-python`](https://github.com/livekit-examples/outbound-caller-python), the shape we follow):
  `ctx.api.sip.create_sip_participant(api.CreateSIPParticipantRequest(room_name, sip_trunk_id, sip_call_to, participant_identity, wait_until_answered=True))`.
  Order matters: **start the `AgentSession` before dialling** (as an `asyncio.Task`) so the agent doesn't miss the callee's first words on pickup; then `await` the dial, then `ctx.wait_for_participant(identity=…)`.
  Failures raise **`api.TwirpError`**, carrying the real carrier verdict in `e.metadata["sip_status_code"]` / `["sip_status"]` — this is the direct source for our `busy`/`no_answer`/`rejected` terminal states (§4) and for `wait_seconds` fast-fail (§5.5). Log it on every attempt row.
  Hangup = delete the room: `ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))`.
  In-call tools are plain `@function_tool()` methods on the `Agent` subclass; `end_call` should `await ctx.session.current_speech.wait_for_playout()` first so the agent's goodbye actually reaches the callee before the line drops.

### 5.4 call-api (FastAPI + Postgres)

**Endpoints:**

```
POST /v1/calls                     → 202 {call_id, status}
GET  /v1/calls/{id}                → status + result summary
GET  /v1/calls/{id}/transcript    → turns[]
GET  /v1/calls/{id}/result        → answers + summary + disposition
POST /v1/calls/{id}/cancel
POST /v1/calls/{id}/reextract     → re-run extraction over a transcript we have
GET  /v1/calls?since=…             → history (audit/debug)
GET  /healthz
```

`reextract` is not a convenience. Without it the separation of `call_status`
from `processing_status` is decorative: a completed call whose extraction failed
has no route back to an answer except placing the call again — spending money on
the phone network, and disturbing a stranger a second time, to fix a text-model
problem. It proved itself within an hour of shipping (§9 phase 2).

**Request:**

```json
{
  "number": "+34911234567",
  "goal": "Ask whether hotel guests can park onsite and the nightly price.",
  "language": "es",
  "caller_id": "+34910000000",
  "answer_schema": {
    "type": "object",
    "properties": {
      "parking_available": {"type": ["boolean","null"]},
      "price_per_night":  {"type": ["number","null"]},
      "currency":         {"type": ["string","null"]},
      "reservation_required": {"type": ["boolean","null"]},
      "notes": {"type": "string"}
    },
    "required": ["parking_available","price_per_night","currency","reservation_required","notes"],
    "additionalProperties": false
  },
  "max_duration_seconds": 300,
  "profile": "grok-voice",
  "callback_url": "https://…/hooks/zvonok",
  "idempotency_key": "uuid"
}
```

**Result:**

```json
{
  "call_id": "c_01J…",
  "call_status": "completed",
  "disposition": "goal_achieved",
  "duration_seconds": 142,
  "answers": {"parking_available": true, "price_per_night": 15, "currency": "EUR", "reservation_required": false, "notes": "Underground garage, book at check-in."},
  "summary": "Hotel confirms onsite parking at €15/night, no reservation needed.",
  "transcript_url": "/v1/calls/c_01J…/transcript",
  "costs": {"telephony_min": 2.4, "model_min": 2.4, "est_usd": 0.19}
}
```

**Tables (sketch):** `jobs` (request, status pair, caps, idempotency), `attempts` (per dial attempt: sip codes, timings), `turns` (speaker, text, t0, t1, attempt_id), `results` (answers, summary, extractor model+prompt hash), `events` (append-only audit), `spend` (per-day counters).

**Webhooks:** optional; HMAC-SHA256 signature + timestamp header; retries with exponential backoff; idempotent by `call_id`+`event`.

**Extractor:** separate text-model call (Claude via existing key, or grok text model) with the transcript + `answer_schema`; constrained JSON output; stores nulls where unknown; never re-dials on its own.

### 5.5 MCP server

Thin adapter over call-api (stdio for Claude Code; SSE/HTTP for OpenClaw if preferred). Tools:

```
phone_call(
  number: string (E.164),
  goal: string,
  language: "ru"|"en"|"es",
  answer_schema?: object,
  caller_id?: string,
  max_duration_seconds?: int = 300,
  wait_seconds?: int = 0..30      # catches instant failures (busy/invalid); then returns call_id
) → {call_id, status, result?}

phone_call_result(
  call_id: string,
  include_transcript?: bool
) → {call_status, disposition, answers?, summary?, transcript?}

phone_call_reextract(call_id: string)   # costs a text-model call, dials nobody
```

**Idempotency is generated client-side, and not by hashing a clock.** The obvious
implementation — `sha256(10-minute bucket | number | goal | language)` — has a
hole exactly where it matters: a request at 12:09:59 whose response is lost and
is retried at 12:10:01 falls in a different bucket, gets a fresh key, and dials
the person again. The failure is likeliest precisely when the boundary is
nearest, because that is when the client is still waiting. The MCP server
therefore *remembers* the key it issued for a request fingerprint (30-minute
sliding TTL) instead of computing it from wall-clock time, so a repeat has no
boundary to fall across. The fingerprint covers everything that shapes the call —
schema, caller ID, disclosure level — because a corrected schema is a different
request, and silently returning the old job would hand back a result in the wrong
shape. Across an MCP restart the key does change; call-api's same-number-in-flight
refusal covers that window, so the two mechanisms cover each other.

Client config: Claude Code `.mcp.json` (Mac) over Tailscale with a bearer token; OpenClaw gets its **own** API identity/token (per-agent identity → per-agent caps + audit).

**OpenClaw consumes it as a skill, not over MCP** (installed 2026-07-27). `openclaw/phone-call/`
in this repo is a skill directory — `SKILL.md` plus three shell scripts hitting the
REST API — installed to `/home/debian/.openclaw/skills/phone-call` by `openclaw/install.sh`.
Reasons: it matches the convention already used by OpenClaw's own skills on de1
(`flight-search` does exactly this), it needs no extra long-running process, and
call-api enforces every guardrail regardless of which door a client comes through.
Note OpenClaw runs as user **`debian`**, not the repo owner's user, so the installer needs root
and validates the JSON before swapping `openclaw.json` — that file drives a live
gateway, where a half-written write is an outage rather than a failed script.

The skill's real content is judgment, not plumbing: when a phone call is the right
move at all, that placing it is not finishing it, that `unreliable_fields` must be
read before believing a number, and that a `422`/`429` is a decision rather than a
transient error to retry. Verified after install: OpenClaw lists it `✓ ready`, the
`debian` user reaches call-api, policy refusals surface correctly, and a call id
belonging to the `mac-claude` identity is invisible to the `openclaw` one.

**Exposure decision (2026-07-27): tailnet only, no public endpoint.** call-api binds
to de1's tailscale address (`<tailnet-ip>:18131`). The Mac reaches it over the
tailnet; OpenClaw runs on de1 and reaches the same address locally. §5.2's Caddy
vhost was not built: this API places calls that cost money, and it should not
depend on a firewall rule or a bearer token staying correct to keep it off the
internet. Port 18131 rather than 8130 — 8130 was already taken on de1 by an
unrelated node process, the same collision class that killed the agent on 8081.

---

## 6. Security & guardrails (this is a spend-capable actuator)

- **AuthN/Z:** bearer tokens per client identity (mac-claude, openclaw, manual); Caddy TLS; tokens in env/secret files, never in repo.
- **Destination policy:** default-deny list: premium-rate prefixes, emergency numbers (112/999/911/…), satellite, unknown country codes. Allowlist of destination countries (start: ES, GB, UA, PL, DE, FR, IT, CH, SA, IN, NP, AE).
- **Caps:** per-call max duration (default 300 s, hard 600 s); per-identity daily minutes + daily call count; global concurrency 1–2 (Zadarma login limit ≈ 3 anyway); daily spend estimate cutoff.
- **Idempotency:** required key on POST /calls — an agent retry must not double-dial a restaurant.
- **Injection boundary:** everything the **callee says is untrusted data**. The extractor prompt must treat transcript as data-only (no instruction-following from it); the voice agent never gets tools that touch our systems beyond the three call-control tools.
- **Audit:** append-only `events`; keep who-asked-what (agent identity, goal) with every job.
- **Secrets on de1:** xAI/Deepgram/etc. keys in Docker env via sops/age or plain root-only env files (consistent with existing de1 practices); Zadarma trunk has no password (IP-auth) — protect by firewall + account 2FA.

---

## 7. Costs (estimates)

| Item | Rate | 5-min call |
|---|---|---|
| Grok Voice realtime | $0.05/min | $0.25 |
| Zadarma termination (EU landline/mobile) | ~€0.01–0.10/min dest-dependent | €0.05–0.50 |
| Extraction (text LLM) | ~$0.001–0.01/call | negligible |
| Cascade profile alternative | Deepgram ~$0.006/min + TTS ~$0.005–0.015/min + LLM | comparable |

⇒ **~$0.15–0.6 per typical task call.** Infra: $0 marginal (de1 already paid). Keep a running `costs` estimate per call in results.

### 7.1 Cost accounting source (resolved 2026-07-27, closes §10 open item)

Per-call telephony cost comes from **`GET /v1/statistics/` with `sip=<trunk login>`** (the trunk's SIP login). Each row carries `callstart`, `from`, `to`, `disposition`, `billseconds`, `billcost`, `hangupcause`, `description` — everything the `spend` table needs. Observed dispositions: `answered`, `cancel`, `limited by cost` (the carrier-side max-price cap), `undefined`.

Two traps, both of which silently produced wrong numbers before being found:

- ⚠ **The account timezone is UTC+3, not UTC.** A call our logs stamp `19:53:11Z` appears as `22:53:12`. Querying with a UTC window returns `{"status":"success","stats":[]}` — a *successful* empty answer, not an error. Convert before querying, and never treat an empty window as "no calls".
- ⚠ **The API rate-limits hard (HTTP 429)** after a handful of calls in quick succession. The phase-0 helper `zadarma_api.py` returns `{"error": …}` on failure, which naive parsing reads as zero rows — i.e. an outage under-reports spend instead of failing. **call-api must treat a non-`success` status as an error and retry with backoff, never as an empty result.** Poll well after the call ends; rows are not instant.

First real measurement (2026-07-27, 13 rows on our trunk): total **€0.0462**. The padel call to a Spanish mobile — 87 billed seconds — cost **€0.0319**; every US call billed **€0**, confirming the tariff. Two rows are the phase-1 bugs made visible: **235 s** (the `wait_for_playout` crash left the leg open) and **159 s** (the answered-with-no-media call that had to be killed by hand). Free on US, but the same 159 s on a UA destination is €0.50 — which is the whole argument for the `no_audio` budget and the `participant_disconnected` handler in §5.3.1.

---

## 8. Legal & etiquette (EU/UK, not legal advice)

- Recording/transcribing calls = processing personal data (GDPR); UK ICO expects disclosure of recording and purpose.
- Baseline built into the agent prompt: **discloses it's an AI assistant, on whose behalf, and that the answer is being kept** — at call start, every call. If the callee objects: apologize, end, and **discard the transcript** (implemented: `declined` writes a minimal audit record with no turns).

#### 8.1 Two disclosure levels (decided 2026-07-27)

**"I am an AI" and "your words are being kept" are different disclosures, and the first does not imply the second.** Knowing you are talking to a machine tells you nothing about retention — a caller can reasonably assume an AI handles the call transiently, like a voice menu. We retain transcripts, so the storage fact must be stated. What is negotiable is the *register*, not the content:

| Level | Wording | When |
|---|---|---|
| **`light`** (default) | *"This is Klava, an AI assistant calling for <owner>. I'll note down your answer so nothing gets lost."* | Ordinary information-gathering calls |
| **`full`** | *"… This call is transcribed and stored. If you would rather it were not, say so and I'll end the call."* | Two-party-consent jurisdictions; anything that books or commits on someone's behalf |

Rationale for `light` being the default: "this call is recorded" is call-centre boilerplate that people hear as surveillance, and it frightens them into hanging up before the goal is reached. It is also **less accurate than it sounds** — we keep *text*, not audio (§5.3 has audio recording off by default), so "I'll note down your answer" describes what actually happens and is what a human receptionist would say. Both levels state the storage fact; `full` adds explicit transcribe-and-store wording plus an offer to stop.

Selected per call via `disclosure_level` in the dispatch metadata (and, from phase 2, by a **policy table keyed on destination country and task type** — the same table §8 already calls for). Unknown values fall back to `light`.

Enforcement is not left to the model: `disclosure_delivered()` requires the assistant name, the owner, an AI self-identification **and** a storage word to appear in a single uninterrupted turn. A turn shredded by barge-in does not count, and neither does an introduction that names the AI but omits the retention. Failing that, `disclosure_guard` speaks the canonical text verbatim with `allow_interruptions=False`.

⚠ **That "uninterrupted" requirement was claimed before it was true.** The function
documented it and never checked the flag — it matched substrings on any assistant
turn, so a shredded introduction still counted as delivered, which is precisely
the phase-1 failure the guard exists to prevent. Fixed once turns began carrying
`interrupted` (phase 2); there is now a test for it.

#### 8.2 `declined` is evidence-gated, not model-asserted (learned 2026-07-27, phase-2 acceptance)

Honouring "don't record me" means discarding the transcript — the only
irreversible action in the system, and one that also destroys the evidence of
whether it was warranted. On the first phase-2 acceptance call the callee said
**"шо"** — one Russian syllable meaning roughly "what?" — on an English-language
call. The model classified it as a refusal, discarded everything, and hung up
without explaining itself. A confused noise is not consent withdrawal.

The gate is principled rather than another word list: **you cannot decline
something you were never told about.** `mark_unreachable("declined")` is refused
once if `disclosure_delivered()` is false — if the callee has not actually heard
what we are doing, whatever they just made a sound about, it was not our
retention of their answer. The model is told to repeat the introduction and ask.
Bounded to a single challenge, like the `end_call` gate: a person who genuinely
objects must never be trapped on the line arguing with a machine.

The same lesson in the prompt: a sound you did not catch, a word in another
language, a bad line — none of these are answers *or* refusals, and the call must
never be classified on one syllable. When the callee answers in another language
we speak, switch and re-introduce in full rather than treating it as a failure.
On the re-run this worked verbatim: "Hola" → Spanish intro → "why in Spanish?
Please, English?" → complete English re-introduction with the full disclosure.

Not legal advice — worth a real review before calling businesses across many jurisdictions.
- Always honestly confirm being an AI if asked.
- Audio recording storage default **off**; transcripts retained with a retention limit (start: 180 days, revisit).
- Per-country nuances (two-party-consent style rules) → maintain a small policy table before adding new destination countries.
- No unsolicited marketing, no robocall patterns, ever. One task = one call (+ small bounded retry on busy/no-answer with backoff).

---

## 9. Rollout plan

### Phase 0 — trunk validation (~1 hour, no code)
Softphone or bare `pjsua`/Asterisk container on de1 → Zadarma IP-auth trunk → call own mobile.
**Verify:** outbound works, chosen caller-ID displays correctly per DID, two-way audio, DTMF to a real IVR, clean hangup both directions.
**Gate:** this is the only real experiment in the project; everything above it is conventional app code. If interop is ugly → deploy `asterisk-edge` per Zadarma's PJSIP manual and put LiveKit behind it.

**RESULT (2026-07-27): PASSED** — via baresip on de1 (config in `~/.baresip`, artifacts in `~/zvonok-p0/`). Findings:

- Trunk "de1" (SIP login <redacted>), IP-auth for <server-public-ip> confirmed (activation = call `8888@sip.zadarma.com` from that IP). Outbound to real PSTN works: ES mobile rang, answered, 39 s call, clean hangup both directions. G.711 alaw negotiated as planned.
- **Caller ID via SIP `From` works** and is accepted for billing (undocumented by Zadarma — verified empirically). Set From user to any E.164 number owned/verified in the account.
- **Origin-based pricing is a first-order cost factor (×20–34)**: EU/UK destinations are cheap only with EU/UK-group caller ID. Measured: ES mobile €0.022 (UK CID) vs €0.44 (UA CID/none); UK mobile €0.018 vs €0.50. UA destinations: flat €0.19 regardless of CID. ⇒ caller-ID-per-country table (§5.1) is a **cost** feature, not just answer-rate; cost estimator must call `GET /v1/info/price/` **with `caller_id`**.
- The "expensive destination" block = user-configurable **max price/min in the Zadarma profile** (was €0.16, raised to €0.20). Blocked calls "answer" instantly, play a ~4 s announcement, then drop — looks confusingly like a completed call in SIP. Keep the cap as a carrier-side guardrail.
- Anti-spoof: calls where A-number = B-number are killed silently (4 s, no audio). Own account DIDs are invalid test destinations (both <UA-DID-2> "UKR-7037" and <UK-DID> are Zadarma DIDs on SIP login <redacted>, NOT external SIMs).
- DTMF: RFC 2833 events sent correctly by baresip (`send DTMF digit` + payload switch visible in RTP). Far-end menu reaction still to be confirmed on a real IVR in phase 1 (test target 00000 turned out to be a live-human support queue).
- Zadarma API works (keys on de1: `~/zvonok-p0/zadarma-api.env`, helper `zadarma_api.py` — balance, price, DIDs, tariff). Note: API keys require e-mail confirmation link on creation. Tariff "Standard" `is_active:false` is harmless (period activates on any top-up, 30-day window); Standard (per-second) chosen over Economy (per-minute) — right call for 1–3 min agent calls.
- Zadarma's own "AI voice agent" (2026) is inbound-only, PBX-bound, no outbound-call API — confirms this product gap; irrelevant to our stack (maybe phase-4 inbound secretary).

### Phase 1 — first AI call (a weekend)
Compose up redis + livekit-server + livekit-sip; create the Zadarma outbound trunk; minimal Python agent (Grok Voice, hardcoded goal); trigger by dispatching with metadata.
**Acceptance:** my phone rings from my UK DID, agent converses toward a hardcoded goal in English, transcript lands in a file.

**RESULT (2026-07-27): PASSED.** Code in `agent/` + `deploy/`, deployed to `~/zvonok` on de1. Acceptance dial to a US mobile from the UK DID: agent opened with the §8 disclosure, held a two-way conversation on 8 kHz G.711, pursued the goal, and hung up on its own. Transcripts in `deploy/transcripts/`.

Verbatim opening from the accepted run:
> "Hello, I'm an automated assistant calling on behalf of the owner, and this call is being transcribed so your answer can be noted. How does the audio quality sound on your end, and can you hear me clearly?"

Findings:

- **`instructions` DO reach Grok Voice** — closes §10.6. The #4305 regression is not present at `livekit-agents[xai]==1.6.7`; the goal, the language, and the disclosure all landed. Everything in phase 2 can rely on the system prompt.
- **Cost: €0.00.** US (prefix 1) is included in the Standard tariff at €0/min for *every* caller ID — balance was €31.3504 before and after three calls. So the ×20–34 origin-pricing effect measured in phase 0 is EU/UK-specific, not universal; the per-country caller-ID table must be priced per destination, not assumed. Also means the €0.20/min profile cap poses no risk on US routes.
- **Call screening is a real disposition.** The very first dial was answered by the callee's phone-screening bot, which reads as a normal `answered` in SIP — indistinguishable from a human at the signalling layer. This is the same class of problem as voicemail (§5.3) and reinforces the decision to detect it from transcript patterns rather than AMD.
- Latency/barge-in not yet measured — the transcript's `t` values include ring time, so they can't be used for this. Needs a proper measurement in phase 3 against the ≤300 ms barge-in target.

Bugs found and fixed during the acceptance run (all ours, none in the stack):

1. **`JobContext.wait_for_shutdown()` does not exist** in 1.6.7. The job outlives the entrypoint — the framework keeps the process alive until the room closes. Transcripts must be written from `ctx.add_shutdown_callback(...)`.
2. **Transcript dir permissions.** The agent container runs as uid 10001; the bind-mounted `transcripts/` was host-owned, so writes failed with EPERM *after* the call — the worst possible moment. `deploy.sh` now chowns it, and `write_transcript` logs the payload inline rather than raising if the disk is unavailable.
3. **`SpeechHandle.wait_for_playout()` cannot be awaited from inside the function tool that owns the handle** — livekit-agents raises `RuntimeError` on the circular wait. The canonical `outbound-caller-python` example still shows this pattern; it is no longer valid. Consequence was severe and silent-ish: the agent announced "I'll end the call now" and then *didn't*, leaving the callee to hang up on a live paid line. Fixed with `hangup_after_goodbye()`, which detaches a task, awaits `session.wait_for_idle()` (bounded at 20 s), then deletes the room.

- Running: `zvonok-redis`, `zvonok-livekit` (v1.13.4), `zvonok-sip` (v1.8.0), `zvonok-agent` (livekit-agents 1.6.7). All on **host networking**; livekit-server binds `127.0.0.1:7880` so the control plane is not exposed at all.
- livekit-sip validated its external IP as `<server-public-ip>` via STUN and listens on 5060/udp+tcp. Agent reports `registered worker`.
- Outbound trunk **`<trunk-id>`** ("zadarma", `sip.zadarma.com`, UDP), created with **no credentials** (IP-auth) and all four caller IDs in `numbers[]`: `<UK-DID>`, `<UA-DID-2>`, `<CY-DID>`, `<UA-DID>`.
- Firewall: 5060 + 10000-20000/udp opened **only** to `185.45.152.0/22`, applied live and persisted to `/etc/nftables.conf` without restarting the service (see `deploy/firewall.sh` and the footgun note in §5.2). Docker's nat chains verified intact afterwards.
- **Gotcha found in deployment:** the LiveKit Agents worker binds a health/debug HTTP server on `0.0.0.0:8081` by default, which collides with matomo on de1 and kills the worker at startup — an artefact of host networking on a shared box. Pinned to `127.0.0.1:18130` via `WorkerOptions(host=…, port=…)`.
- Tooling note: de1 has no `lk` CLI and doesn't need one — `deploy/lkctl.py` drives trunk creation and dispatch through the `livekit-api` SDK already present in the agent image (`deploy/lkctl.sh` runs it in that image). Prefer the non-deprecated SDK names `list_outbound_trunk` / `create_outbound_trunk`.
- Still unproven until the first dial: whether `instructions` actually reach Grok Voice (§10.6), DTMF against a real IVR, and barge-in behaviour.

#### Phase-1 hardening pass (2026-07-27, after independent Codex + Grok reviews)

Two independent reviews of the phase-1 code and design. Both were worth it: between them they found one design flaw that explained an observed bug better than our own hypothesis, three P0 lifecycle/money defects, and one implementation that contradicted a promise made out loud to a callee.

**Design (Grok).** The four-branch answerer model was sound, but *the silence-open path contradicted it*: on 1.5 s of silence the code played the full introduction, which hard-codes the "a person answered" branch before anything has been heard. Answering machines and PBXs routinely insert 0.5–3 s of silence before their greeting — so we introduced ourselves over a recording and the model, already committed to a conversation, never called `mark_unreachable`. **The observed voicemail bug was designed in, not a model failure.** Fixed: on silence we now emit a two-word probe and explicitly withhold the introduction until we know who answered.

Grok also rejected "tell the model to repeat numbers back" as a control: a conversational model optimises for progress, not auditability, which is exactly what we saw when it accepted a spoken `17:00` unconfirmed. Confirmation is now **structural** — a `record_answer` tool, and `end_call` refuses once until something is confirmed. And barge-in was misdiagnosed by us as an end-of-turn problem: it is interrupt hysteresis, so `threshold` went 0.5 → 0.65 (8 kHz line noise was tripping false interrupts).

**Code (Codex).** Defects fixed:

| Sev | Defect | Consequence |
|---|---|---|
| P0 | Shutdown callback + disconnect listener registered *after* dialling | An exception after answer (realtime session failing to init) left an answered, billing leg with no teardown and no transcript. Both now registered **before** the dial; setup wrapped in try/except that hangs up. |
| P0 | `hangup()` treated **every** exception as "room already closed" | A transient control-plane error silently disarmed the hard duration cap and could leave a conversation open indefinitely. Now: bounded retry, `not_found` distinguished from transient. |
| P0 | Lost-wakeup race — callee disconnecting during `await session_started` was never observed | `wait_for_participant` could wait forever. Listener now registered pre-dial; participant wait bounded at 20 s. |
| P0 | §6 concurrency cap not implemented | LiveKit admission control is CPU-based, so one worker would happily run many paid calls. Now a `load_fnc` counting `active_jobs` against `MAX_CONCURRENT_CALLS`. Phase 2's call-api must enforce it too — a second worker cannot see this one. |
| P1 | **A callee who declined transcription was still fully transcribed to disk** | Directly contradicted what the agent says out loud, and §8. Now `declined` discards turns and keeps only a minimal audit record. |
| P1 | Terminal actions raced | `end_call` set `goal_achieved`, then the duration guard overwrote it with `timed_out` mid-farewell. All termination now funnels through a one-shot `Terminator`; first cause wins. |
| P1 | Detached tasks swallowed exceptions | A watchdog dying on a closing session removed silence/no-audio protection for the rest of the call, silently. All background tasks now report failures and are cancelled on shutdown. |
| P1 | `latest` tags on media services | An upstream release could change SIP behaviour on the next pull. Pinned: `livekit-server:v1.13.4`, `livekit/sip@sha256:ad8dafc…` (no v1.8.0 tag is published). |
| P1 | No Docker log rotation, SIP at `debug` | Unbounded logs on a shared host. Now `max-size 10m / max-file 5` on every service and SIP at `info`. |
| P2 | `firewall.sh` edited `/etc/nftables.conf` before validating | An invalid append would leave the host with a config that fails at next boot — no firewall — while live rules still looked fine. Now: build candidate → `nft -c` → swap. |

**Second independent fuse.** `CreateSIPParticipantRequest` carries server-side `max_call_duration` and `ringing_timeout`, which we were not using. Both are now set. They do not depend on our process being alive or on the room delete succeeding, which is precisely the failure mode that produced the 235 s and 159 s billed calls in §7.1.

**Not adopted (deliberately).** Grok argued for a hard `classify_answerer` gate before any goal speech. Deferred: the probe-not-introduce fix removes the mechanism that caused the bug, and a mandatory extra tool round-trip on every call costs latency on the common path. Revisit if voicemail slips through again.

### Phase 2 — the product spine
call-api (endpoints, state machine, Postgres), extractor pass, MCP server, caps/allowlists, idempotency, `wait_seconds` fast-fail.
**Acceptance:** from a Claude Code session: `phone_call(my number, test goal)` → poll → structured answers. OpenClaw gets its token.

**Entry conditions and known traps** (from the phase-1 review, ranked by how much damage they do if ignored):

1. **Decide who owns the dial.** Today the *agent* creates the SIP participant and owns hangup and the duration guard; §4 says the call-api worker dials. Half-migrating this is the worst outcome — double-dials and orphan rooms, which breaks idempotency at the root. Pick one owner explicitly before writing the state machine. Recommendation: leave dialling in the agent (it is what makes "start the session before dialling" possible) and have call-api own only job state, so the room is the single source of truth for "a call is in flight".
2. **Idempotency is about not dialling twice, not about returning the same id.** An `idempotency_key` on `POST /v1/calls` is necessary but insufficient: an agent crash after answer, and the busy/no-answer retry path, both need the live room/SIP session consulted before a redial. `lkctl.sh rooms` is the manual version of that check.
3. **One normalised disposition enum.** The agent currently emits `goal_achieved`, `unreachable`(+reason), `no_audio`, `abandoned`, `callee_hangup`, `setup_failed`, `timed_out`; §4 names `busy`, `no_answer`, `rejected`, `voicemail`, `failed`, `canceled`, `timed_out`. `call_status_for()` in the agent is the current mapping — call-api must own the canonical enum and treat the agent's as an input dialect.
4. **Transcript delivery must be at-least-once, keyed by `job_id`.** Callee hangs up → room deleted → shutdown callback → HTTP POST to call-api, which can fail. Accept late finalisation; never let a lost POST look like a call that never happened.
5. **Keep `call_status` and `processing_status` genuinely separate** (already in §4). A completed call whose extraction failed must never be redialled — that spends money to fix a text-model problem.
6. **Caps belong in call-api, before dispatch.** The agent's `MAX_CONCURRENT_CALLS` and `max_duration` are per-worker safety nets, not spend control. Destination allowlist, daily minutes and daily spend must gate at the API, or a second worker silently doubles the limit.
7. **`wait_seconds` in the MCP tool is a footgun.** Clients — OpenClaw especially — will read `phone_call` returning as "the call is done". Document hard that it only catches instant SIP failures, and make the return shape say so (`status: "dialing"`, not a result object).
8. **The answer schema should drive both ends.** Feed the schema's field descriptions into the voice prompt's "facts to capture" *and* the extractor, not just the extractor — otherwise the agent never asks for a field the schema requires. `record_answer` (§5.3.1) is the bridge: it lets the extractor distinguish "the callee said 17:00" from "17:00 was read back and agreed", and disagreement between the two is the signal that a value is unreliable.
9. **Transcript fidelity is load-bearing.** Barge-in truncation means the stored assistant turns can contain words the human never heard. Until interrupted turns are flagged, an extractor can "confirm" something that was never actually said aloud.

**RESULT (2026-07-27): PASSED.** Code in `api/` (call-api), `mcp/` (MCP server), agent
changes in `agent/agent.py` + `agent/report.py`. Acceptance from a Claude Code
session: `POST /v1/calls` → dial → conversation → transcript → extraction →
structured answers, over the tailnet, with caps and audit. Two live calls to an ES
mobile, €0.09 total.

Verbatim from the accepted run — note the language switch and the *complete*
re-introduction, both designed in §5.3.1 and §8.2:

> `15.6s user:` Hola
> `22.8s assistant [INTERRUPTED]:` Hola, soy Klava, una asistente de IA que llama en nombre de <owner>. Voy a anotar su
> `26.5s user:` Um, why in Spanish? Please, English?
> `38.9s assistant:` I'm happy to switch to English. This is Klava, an AI assistant calling for <owner>. I'll note down your answer so nothing gets lost. What time tomorrow would suit you for a short follow-up call?

Decisions taken at this phase:

- **Extractor = xAI `grok-latest`** (resolves to `grok-4.3`) on the same key as the voice brain — no new account, no new secret. Strict `json_schema` verified working. Closes ACCOUNTS.md §4.
- **Dialling stays in the agent** (trap 1, as recommended). call-api owns job state only.
- **Tailnet-only exposure**, no Caddy vhost — see §5.5.

How the traps actually landed:

| Trap | Outcome |
|---|---|
| 1 dial ownership | Agent dials; call-api never does. No double-dial observed. |
| 2 idempotency ≠ same id | Key always returns the existing job; plus a same-number-in-flight 409. MCP key is remembered, not clock-hashed (§5.5). |
| 3 one disposition enum | `states.normalise()` owns it; the agent's is an input dialect. Unknown input degrades to `failed`, never to `completed`. |
| 4 at-least-once transcripts | Agent writes the file **then** POSTs; janitor recovers from the shared volume. Verified: `final report delivered`. |
| 5 statuses separate | Proved in anger — see the extraction bug below. |
| 6 caps before dispatch | In call-api, inside one admission transaction. |
| 7 `wait_seconds` footgun | Returns `call_status: in_progress`, never a result object; MCP note says so explicitly. |
| 8 schema drives both ends | `facts_block()` feeds descriptions into the voice prompt. Worked: the agent asked both questions unprompted. |
| 9 transcript fidelity | `interrupted` + `transcript_confidence` stored and rendered to the extractor. The interrupted Spanish intro above is a real instance. |

**Two independent reviews (Codex on code, Grok on design) before deploying.** Both
converged on the same defects, which is the signal worth trusting. Fixed:

| Sev | Defect | Consequence |
|---|---|---|
| P0 | Admission was check-then-act | Two simultaneous POSTs both read zero open jobs and an unspent budget, both passed, both dialled. Every cap in §6 was walk-through-able. Now one advisory-locked transaction, with the job INSERT inside it so the row itself is the mutex. |
| P0 | Cost estimated by destination only | Phase 0 measured a ×20–34 origin swing, and the estimator ignored it — under-reporting by an order of magnitude exactly when a client overrode `caller_id`. Now keyed `(country, origin group)`; an unknown caller ID prices pessimistically. |
| P0 | `record_final` overwrote terminal state | A late agent report turned an operator's `canceled` into `completed`, and both delivery routes could bill the same call twice. Now an atomic claim (`claim_finalisation`); first writer owns the verdict and the money, later ones may only refresh the transcript. |
| P0 | Cancel didn't revoke the dispatch | Between `create_dispatch` and the worker joining there is no room to delete, so cancelling in that window marked the job terminal and then let the agent dial anyway. Now revokes the dispatch first. |
| P1 | Redelivery re-ran extraction | A retried final reset `processing_status` to `pending`, re-billing the extractor and overwriting a good result. |
| P1 | `disclosure_delivered` ignored `interrupted` | §8 compliance bug — see §8.1. |
| P1 | `captured` labelled "trusted" to the extractor | It is the *least* trustworthy structured channel: the same model, talked to by the callee. Demoted to "claims to check against the transcript". |
| P1 | Fixed transcript delimiters | A callee can say "END UNTRUSTED TRANSCRIPT" out loud. Now a per-call nonce they cannot guess. |
| P1 | `record_answer` logged values | A callee who later declined had their words already written to Docker's logs, which redaction cannot reach. Logs the field name only. |
| P1 | Dead worker starved concurrency | Unclaimed jobs held a slot for `max_duration + grace` (~6.5 min), blocking every other call. Now failed after 45 s — nothing was dialled or billed. |
| P2 | `_strictify` forced invented values | Promoting an optional property to `required` without admitting null makes the model fill a field it has no evidence for — a fabricated fact about a real phone call. Now widens the type as it makes it required. |
| P2 | `profile` accepted but ignored | Recorded a job as `nova-sonic` and placed a Grok Voice call: an audit trail that lies. Restricted to the profile the worker implements. |

Also found by the tests, not by review: the NANP area-code slice read `number[1:4]`
when `+1` is two characters, so **every US premium (`+1900`) and Caribbean
(`+1809`) number passed the allowlist**.

Bugs found by the live calls themselves (neither review caught either):

1. **"шо" read as a refusal** — §8.2. The single irreversible action in the system,
   triggered by one syllable.
2. **The extractor nulled a value the callee actually said.** Asked for noon, it
   returned `callback_time: null` because nobody confirmed it — conflating "not
   stated" with "not confirmed", which is exactly what `unreliable_fields` exists
   to distinguish. Nulling throws away the answer the call was made to get; the
   caller can act on a shaky value and can do nothing with a null. The rules now
   separate the two explicitly, and `goal_achieved` tracks whether the
   information was *obtained*, not whether it met the confirmation bar.
   **Fixed via `reextract` — no redial, nobody disturbed twice, €0 spent.** That
   is the entire argument for keeping `call_status` and `processing_status` apart.

Still unproven / deliberately deferred: `record_answer` is not reliably called
(on the accepted run the model *said* "I'll note that down" in response to the
`end_call` refusal, instead of calling the tool — the gate is bounded to one
challenge by design, and the extractor caught the unconfirmed values anyway, so
it degraded safely). Webhook delivery, the `retention_days` purge, and busy/no-answer
retries are written or configured but not exercised.

### Phase 3 — reality hardening
RU/ES language profiles benchmarked on real 8 kHz calls; cascade profile B wired and selectable; IVR bounded-navigation + DTMF; retry policy (busy/no-answer); webhooks; cost accounting; SIP-TLS/SRTP `require`; caller-ID-per-country table.

### Phase 4 — optional/later
Inbound (separate SIP login + dispatch rules → "secretary" agent), human-transfer to my mobile, recordings (post §8 review), archival sync of transcripts to n5, second trunk (Telnyx) for HD/SIP-diversity, admin mini-UI.

---

## 10. Open questions

1. Grok Voice quality for **Russian** and **Spanish** over 8 kHz — benchmark early (phase 3 gate for profile default per language).
2. ~~Zadarma IP ranges for firewall scoping~~ — **resolved 2026-07-27: `185.45.152.0/22` (AS199790)**, derived by DNS+RDAP (§5.1). Confirm with support opportunistically; not a blocker.
3. Exact Zadarma per-login concurrent-channel limit on our tariff. (DIDs report 2 channels each; trunk login unknown.)
4. ~~Whether OpenClaw consumes MCP directly or prefers a plain REST skill~~ — **resolved 2026-07-27: both exist, OpenClaw picks.** The MCP server is a thin adapter holding no state and enforcing nothing; policy, caps, idempotency and audit all live in call-api, so a client that prefers plain REST bypasses no guardrail. OpenClaw has its own identity/token either way.
5. ~~xAI realtime access~~ — **resolved 2026-07-27: confirmed working** on our key (`grok-voice-latest`, audio modality, §5.3). Still unverified: concurrency limit and per-minute billing rate on our tier — the $0.05/min figure in §7 is from public docs, not from our invoice. Check after the first real calls.
6. **New:** does the pinned `livekit-plugins-xai` actually honour `instructions`? (§5.3 gotcha.) Must be proven on call #1 — everything downstream assumes the system prompt lands.

## 11. In-house prior art (found 2026-07-27)

- **onova** (`~/Sites/onova`) contains a **working LiveKit Agents voice agent** — TypeScript, `@livekit/agents` + `agents-plugin-deepgram` (nova-3 STT) + OpenAI LLM/TTS: a voice *interview* agent with an `llm.tool` `save_answer` pattern (per-question summary + raw transcript persisted via session userData), core-api issuing LiveKit tokens, dashboard room component, compose wiring for `LIVEKIT_URL/API_KEY/SECRET` (server was external — likely LiveKit Cloud; no self-hosted livekit-server in its compose). This validates familiarity with the framework and is directly reusable for zvonok's agent structure: the tool-calling + structured-persistence pattern is exactly our transcript/answers flow, minus SIP.
- **raspisnoy** roadmap lists "Voice/video chat via LiveKit" as a future item (idea only, no code).
- Language note: onova's agent is Node/TS; zvonok's brief assumes Python (richer plugin coverage incl. `livekit-plugins-xai`). **Decided at phase 1 (2026-07-27): Python.** A first-party `livekit-plugins-xai` exists for both runtimes, but the canonical outbound-call example we're cloning is Python, the extractor/call-api spine (§5.4) is FastAPI anyway, and every S2S profile in §5.3 has a Python plugin. Node stays viable as a fallback (Grok Voice speaks the OpenAI-Realtime protocol, so the openai plugin with an x.ai base URL works there too) but there's no reason to split runtimes.

## 12. References

**LiveKit — self-hosting & SIP**
- SIP server self-hosting (config.yaml reference): https://docs.livekit.io/transport/self-hosting/sip-server/
- Ports & firewall: https://docs.livekit.io/transport/self-hosting/ports-firewall/
- livekit/sip repo (config keys, `SIP_CONFIG_BODY`, host-networking requirement): https://github.com/livekit/sip · reference compose: https://github.com/livekit/sip/blob/main/docker-compose.yaml
- Outbound trunk spec (`SIPOutboundTrunkInfo`, `lk sip outbound create`): https://docs.livekit.io/sip/trunk-outbound/
- Outbound calls / `CreateSIPParticipant`: https://docs.livekit.io/telephony/making-calls/outbound-calls/
- Canonical outbound agent example (our template): https://github.com/livekit-examples/outbound-caller-python

**LiveKit — Agents (Python 1.6.7)**
- xAI Grok Voice plugin: https://docs.livekit.io/agents/models/realtime/plugins/xai/
- `livekit.plugins.xai.realtime` API reference: https://docs.livekit.io/reference/python/v1/livekit/plugins/xai/realtime/index.html
- `instructions`-dropped bug (must re-verify, §5.3): https://github.com/livekit/agents/issues/4305

**xAI**
- Voice docs: https://docs.x.ai/developers/model-capabilities/audio/voice (realtime `wss://api.x.ai/v1/realtime`, $0.05/min)
- Confirmed live on our key 2026-07-27 → `grok-voice-latest`, voices `ara|eve|leo|rex|sal`
- Zadarma Asterisk PJSIP (codecs/config reference): https://zadarma.com/en/support/instructions/asteriskpjsip/ · IP-auth trunk: https://zadarma.com/en/support/instructions/asteriskpjsip/trunk/ · SIP trunk overview: https://zadarma.com/en/services/calls/sip-trunk/
- AVA (Plan B): https://github.com/hkjarral/Asterisk-AI-Voice-Agent
- Prior art for MCP surface: Retell MCP server (`create_phone_call`/`get_call` tool shape)
