# zvonok — a phone-call tool for AI agents

**Status (2026-07-31):** phases 0, 1 and 2 complete and verified on live calls. Phase 3 in progress: Polish added, canvassing mode shipped, **first multi-number canvass ran and returned nothing usable — post-mortem in §9.4**. Inbound trunk and dispatch rule created but nothing answers yet (§5.6).
**Owner:** faeton
**Deploy target:** de1 (Hetzner dedicated, Debian, Docker) — see §3 for why
**Related docs:** [ACCOUNTS.md](./ACCOUNTS.md) (what to register where), [CLAUDE.md](./CLAUDE.md) (session conventions)

> **How to read this document.** §§1–8 state what is true *now*; where an earlier
> belief was wrong, the statement is simply corrected and the wrong version is not
> preserved inline. §9 is the log — what was tried, what it cost, and what it
> taught — and that is the only place archaeology belongs. Corrections layered on
> top of corrections in the spec is not a style problem: it is how the ASR bug in
> §9.4 survived for a whole canvass, because §5.3 described a fix the code was not
> actually making and the description was easier to read than the wire.

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
- **Network block — Zadarma's published list, not our derived one (corrected 2026-07-31).** The six ranges below are what Zadarma states in the *External server (SIP URI)* dialog under a number's settings:

  `185.45.152.0/24` · `185.45.154.0/24` · `185.45.155.0/24` · `195.122.19.0/27` · `31.31.222.192/27` · `15.235.128.64/28`

  We previously used **`185.45.152.0/22`**, derived by resolving their five SIP hostnames and taking the enclosing prefix (AS199790 `IPTelecomBulgaria-AS`). That derivation was not wrong so much as incomplete **in the direction that matters**: outbound only ever talks to the hosts we resolved, so it worked perfectly, while **three of the six ranges above sit outside that /22 entirely** — and inbound is delivered from them. A call to our DID would have been dropped by a default-deny firewall with no log, no carrier-side error and nothing to debug from. The number would simply not ring, which is precisely the symptom we had already spent time chasing.

  Derived-from-DNS is a reasonable way to start and a bad way to stay. Kept in two places that must not drift: `deploy/firewall.sh` (does a packet reach the box) and `ZADARMA_SIP_NETS` in `deploy/lkctl.py` (does livekit-sip believe it). A call needs both.

  **Cross-checked against Zadarma's second, per-IP list** (their Twilio integration guide publishes twelve `/32`s by hostname). All twelve fall inside the six ranges above — so the ranges are a correct superset, not a guess. The same check against the old `185.45.152.0/22` fails on four of them: `pbxlv1/2` (195.122.19.x), `pbxsg1` (15.235.128.70) and `pbxal1` (31.31.222.201).

  For our mode — External Server / SIP URI, no PBX — only **two** of those twelve ever originate an INVITE: `sipurifr.zadarma.com` (185.45.152.216) and `sipuriny.zadarma.com` (185.45.155.33). The other ten are PBX nodes we do not use. Tightening to those two `/32`s would be materially stricter; it is not done because a carrier adding a node would then present as a number that silently stops ringing, which is the failure mode this whole section exists to avoid.
- **Reference config:** Zadarma's own PJSIP template (endpoint/aor/identify/transport) at their [IP-auth trunk guide](https://zadarma.com/en/support/instructions/asteriskpjsip/trunk/) — this is also the exact config to lift if we ever need the `asterisk-edge` Plan B (§2.2). Note their guide sets caller ID in `extensions.conf` via `Set(CALLERID(num)=…)`; our equivalent is the SIP `From` user, verified working in phase 0.
- **Before a DID is used for anything, check what kind of number it is.** Added after buying one that could not be dialled. Zadarma's own API answers this in one call — `GET /v1/info/price/?number=<DID>&caller_id=<ours>` returns a `description`, and anything reading **VAS**, *personal number*, *special services*, *premium*, *shared-cost* or *freephone* disqualifies the number for our purposes. Acceptance gate, all four:
  1. **Geographic range**, per the national regulator's plan — not a service range. A number that is cheap for us to own and expensive for a stranger to dial is the wrong way round: on a callback line, the *caller* pays.
  2. **Dialable from an ordinary foreign mobile**, tested from outside the country before it is trusted. Carriers decline international service ranges at their own discretion, so "our carrier prices a route to it" is not evidence anyone can reach it.
  3. **Under our own per-minute cost guard**, or we cannot even test it — see §9.5.
  4. **Rings through to us**, verified as an INVITE in `livekit-sip` logs, *before* it goes into `ZVONOK_OWNED_CALLER_IDS`.

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

- 5060/udp+tcp: **accept only from Zadarma's six published ranges** (listed in §5.1); the drop policy handles everyone else. SIP scanners are constant and this trunk spends real money.
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

**Four distinct silences, and they are not interchangeable.** Conflating them made the agent simultaneously jumpy and slow. Canonical values live in `agent/timing.py`, which is the file to read before changing any of them — they interact.

1. `OPENING_SILENCE_SECONDS = 3.0` — nobody spoke after pickup → we **probe**, we do not introduce (§5.3.1). Not 1.5: a callee's "hello" has to cross the PSTN leg, the SIP media path and VAD before we see it, and at 1.5 s we kept talking over people who had already spoken.
2. `END_OF_TURN_SILENCE_MS = 800` — they spoke and stopped → how long before assuming the turn ended. OpenAI's `server_vad` default of 500 ms is too eager on the phone: a screener says "Hello?" and only *then* "please state your name after the tone", so an eager agent answers the greeting and misses the actual request. Passed via `turn_detection=TurnDetection(type="server_vad", …)`; the xAI plugin accepts it (and `base_url`, despite the docs page omitting both). **This is also the latency floor** — the callee's experienced pause is this plus time-to-first-audio plus egress.
3. `SILENCE_NUDGE_SECONDS = 6.0`, `MAX_SILENCE_NUDGES = 2` — mid-call dead air → nudge, then give up (~28 s from answer to hangup).
4. `QUEUE_PATIENCE_SECONDS = 60.0` / `QUEUE_PATIENCE_TOTAL = 90.0` — a menu has told us to hold, so quiet means *queued*, not *abandoned*: neither nudge nor hang up. The first is refreshed by every menu turn (a switchboard that repeats its announcement keeps the grace alive); the second is measured from when we first started waiting, because otherwise a looping announcement extends the wait forever and only `max_duration` ends the call. One call burned its whole budget that way.

Two absolute budgets bound the call regardless of which silence it is in:

- `NO_RESPONSE_BUDGET_SECONDS = 20.0` — the callee has **never** made a sound, measured from answer. A distracted person trips timer 3 and gets the full humane cycle; a line that answered with no media has nobody to nudge.
- `NO_SPEECH_BUDGET_SECONDS = 75.0` — nothing **intelligible** has come from the far end. Distinct from the above and necessary because VAD is energy: hold music, a fax tone and a noisy open line satisfy it indefinitely while saying nothing. Only a turn a recogniser was willing to call words counts here.

**Two ways a call must end that aren't `end_call`:**

- **Answered with no media at all.** A carrier can return `200 OK` and never deliver audio — observed 2026-07-27. Without a guard the job sat open for the full `max_duration`, burning a paid PSTN leg and realtime-model minutes on pure silence. After the nudges are exhausted → hang up, disposition `no_audio` (never spoke) or `abandoned` (went quiet mid-call).
- **Callee hangs up.** The SIP participant leaving does **not** close the room — the agent is still in it. Needs an explicit `participant_disconnected` handler, or the job idles to `max_duration`. Disposition `callee_hangup`.

Operational: `deploy/lkctl.sh rooms` lists live calls, `hangup [room|all]` force-ends one. Written after a stuck job had to be killed by hand.

- **Audio discipline:** 8 kHz G.711 in/out; resample properly to the model's expected rate; test barge-in on a real phone (target: stop speaking ≤ 300 ms after callee starts; require a short confirmation window to avoid false barge-ins from line noise; commit to history only words actually played).
- **Answering machines/IVR:** do **not** enable aggressive AMD initially (hotel greetings look like voicemail → false positives). Instead: transcript-pattern recognition + bounded IVR phase (max N menu levels / DTMF presses) before the goal conversation.
- On session end: write full timestamped transcript (turns with speaker + t_start/t_end) + disposition to call-api; optionally save mixed-audio recording (flag per call, default off until §9 review).
- **Dial pattern** (from [`livekit-examples/outbound-caller-python`](https://github.com/livekit-examples/outbound-caller-python), the shape we follow):
  `ctx.api.sip.create_sip_participant(api.CreateSIPParticipantRequest(room_name, sip_trunk_id, sip_call_to, participant_identity, wait_until_answered=True))`.
  Order matters: **start the `AgentSession` before dialling** (as an `asyncio.Task`) so the agent doesn't miss the callee's first words on pickup; then `await` the dial, then `ctx.wait_for_participant(identity=…)`.
  Failures raise **`api.TwirpError`**, carrying the real carrier verdict in `e.metadata["sip_status_code"]` / `["sip_status"]` — this is the direct source for our `busy`/`no_answer`/`rejected` terminal states (§4) and for `wait_seconds` fast-fail (§5.5). Log it on every attempt row.
  Hangup = delete the room: `ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))`.
  In-call tools are plain `@function_tool()` methods on the `Agent` subclass. ⚠ The canonical example has `end_call` `await ctx.session.current_speech.wait_for_playout()` first — **that pattern is invalid in 1.6** and raises on the circular wait (the speech handle waits for the tool to return while the tool waits for playout). Detach a task instead, `await session.wait_for_idle()` bounded, then delete the room: `hangup_after_goodbye()`.

#### 5.3.2 Language and the ASR hint

**Language is always explicit per call** (`ru`/`en`/`es`/`pl`). Never autodetected at the API boundary: it decides the voice, the disclosure wording, number and date formatting, and — see below — what the recogniser is told to expect.

**Grok Voice runs a transcription pass, and it must be configured.** A speech-to-speech model is not "STT-free": there is an ASR in there producing the transcript we store, extract from, and detect voicemail on. `livekit-plugins-xai` hardcodes `AudioTranscription()` — every field `None`, i.e. **automatic language detection** — and exposes no way to set it, so it is set on `_opts` after construction (`agent/voice.py`).

**The field names are xAI's, not OpenAI's.** This is the part worth reading twice:

| What we want | OpenAI SDK field | **What xAI actually reads** |
|---|---|---|
| bias ASR to a language | `language` | **`language_hint`** (BCP-47) |
| bias ASR to proper nouns | `keywords` | **`keyterms`** (max 100 terms, 50 chars each) |

Grok Voice speaks the OpenAI realtime *protocol*, which is not the same as accepting the OpenAI realtime *schema*. `openai.types.realtime.AudioTranscription` is declared `extra="allow"`, so it carries either spelling without complaint, and the realtime server **echoes back whatever keys you send it** — verified against a live session with deliberate nonsense. The wrong names therefore produce no error, no warning, and a `session.updated` frame that reads exactly like confirmation.

⇒ **An echo is not an acknowledgement.** A config field the server never validates has to be checked against behaviour, not against the server's reply. §9.4 is what that cost.

`keyterms` is fed from the `keywords` field on `CallRequest` (the public name stays OpenAI-ish because it is the one clients guess) and biases the recogniser toward the proper nouns a canvass turns on — a drug, a brand, a part number, which is exactly what 8 kHz destroys first.

**`model` is deliberately left unset.** Setting `audio.input.transcription.model` to `grok-transcribe` switches the server to emitting `conversation.item.input_audio_transcription.updated`, which the xAI plugin does not handle — it handles `.completed`, distinguishing partials by a `status` field. Enabling it would silently drop every user transcript: a worse failure than the one it might fix.

**Language switching mid-call is allowed on S2S only.** The call opens in the requested language; if the callee is plainly speaking another language we support, the agent switches and stays there — the commonest thing that happens when a number reaches a real person. Cascade profile B picks a whole STT model from the language and cannot switch mid-call, so there it stays hard-locked.

**Voice model pinned, not aliased.** The plugin default is still `grok-voice-think-fast-1.0`. We run **`grok-voice-think-fast-2.0`** (announced 2026-07-29: 1.5–2x better transcription WER over 24 languages, and a larger claimed margin in noisy telephony — our entire operating condition). `grok-voice-latest` only flips to 2.0 on **2026-08-05**, and an alias that changes under a running deployment is not something to discover from a transcript. `ZVONOK_VOICE_MODEL` overrides.


#### 5.3.3 Canvassing mode

A canvass is fifteen numbers, one question, and an answer that is often a single
word. It is the shape the conversational agent is worst at, and the difference is
not cosmetic — it is a **different prompt**, selected by `disclosure_level:
"brief"` (§8.1), not the long prompt with sections switched off.

Why a separate template rather than flags:

- **Prompt length is a latency cost here in a way it is not elsewhere.** A long
  system prompt normally slows only the first turn, because later turns ride the
  provider's KV cache. **A canvass is all first turns**: fifteen calls, fifteen
  cold starts, and the callee sits in the silence for every one of them. The
  conversational prompt is ~1700 words and carries branches (screeners, bookings,
  dictating a number back, steering through small talk) a thirty-second stock
  check never reaches. The canvass prompt is ~560 words.
- **"Ask everything in the schema" is wrong here.** With five fields it turns a
  fifteen-second stock check into an interrogation and the callee hangs up
  mid-way with nothing captured. The **first** schema field is the call; the rest
  are a bonus. `facts_block(brief=True)` says so explicitly, and the runner
  reports on the first field.
- **A short "no" is a complete, successful call.** The agent is told this in as
  many words, because otherwise it keeps probing after the answer it was sent for.
- **The first turn is fixed.** Disclosure plus the question, in one breath,
  whatever the model thinks it heard — the single highest-value use of a fixed
  utterance on this kind of call.

What is **not** trimmed at any length: the §8 disclosure, admitting to being an AI
when asked, and honouring a refusal to be transcribed.

`tools/canvass.py` drives a list: stdlib-only, resumable (the idempotency key
covers the playbook's *content*, so editing the goal and re-running is a new
request rather than a cached answer), backs off on the concurrency cap rather
than hammering it, and stops after five consecutive dial failures because that is
a trunk or balance problem and burning the rest of the list against it helps
nobody. Playbooks live in `playbooks/`.

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
  language: "ru"|"en"|"es"|"pl",
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

### 5.6 Inbound (transport built, nothing answers yet)

Outbound calls create callbacks. Once we have rung fifteen pharmacies, some of
them ring back — and today that call reaches a number nobody is listening on,
which is worse than not calling from it at all.

**Built and verified as configuration:**

- A LiveKit **inbound trunk** (`ST_TkZ9U3DYWho4`) scoped to the same six Zadarma
  ranges the firewall trusts (§5.1). Scoped, not open: 5060 receives constant
  scanner traffic and an unscoped inbound trunk is an invitation. Refresh it with
  `lkctl.sh sync-inbound-trunk` — note the SDK's update REPLACES the trunk, so
  that command copies the existing one and changes a single field rather than
  silently dropping `numbers` and both server-side fuses.
- A **dispatch rule** (`SDR_nTzooGvMwHAA`), individual room per call.
- Zadarma side: the DID sits on the trunk's SIP login. Note the §5.1 warning —
  activating an IP trunk consumes that login, which is why the Lithuanian DID
  appeared not to ring at all. **The gap was ours, not the carrier's:** there was
  no inbound trunk for the INVITE to land on.

`deploy/lkctl.py` gained `inbound-trunks`, `create-inbound-trunk`,
`dispatch-rules` and `create-inbound-rule` for this.

**Not built, and deliberately not started until the questions below are answered:**
a `zvonok-secretary` worker, and a `GET /v1/contacts/{number}` history endpoint
that would tell it who is calling and what we called *them* about.

⚠⚠ **`<lt-did>` (a `+370 700` national number) is the wrong kind of number, and everything below about it is
plumbing pointed at a dead end.** The routing, the widened firewall, the trunk and
the dispatch rule are all correct and all stay — they are just aimed at a number
nobody can dial. Evidence, three independent sources:

- **Zadarma's tariff calls it `Lithuania VAS - mobile`, €0.52/min** to dial.
- **The Lithuanian regulator classifies `700xxxxx` as a non-geographic *personal
  number* service** (RRT / ITU +370 plan). Not geographic, not mobile.
  Shared-cost is `808`, freephone is `800`. Foreign carriers route it at their
  own discretion and at service rates — Cyta lists `3707` as "Special Services,
  calls via operator" only; an Austrian carrier prices `+370 7` at €1.25/min.
- **`GET /v1/statistics/incoming-calls/` shows zero incoming calls all day**, a
  properly-formed empty result with the window echoed back. The owner's attempts
  never reached Zadarma, so nothing on our side was ever involved.

And the detail that settles it: **our own outbound test call to it was refused by
our own spend guard** — Zadarma returned `disposition: "limited by cost"`, the
account's €0.20/min ceiling declining a €0.52/min destination. We cannot dial our
own callback number. (It "answers" instantly and drops with no audio, which is
the phase-0 blocked-call signature in §9.1 — easy to misread as a completed call.)

A callback line where **the caller pays a premium** and many carriers refuse to
connect is the exact inverse of the requirement. Replace with ordinary
**geographic** DIDs — Warsaw `+48 22`, Madrid `+34 91`, London `+44 20` — one per
market we call into, all routed to the same SIP endpoint. The existing UK mobile
already works for outbound, so the gap is PL and ES.

**Why it did not ring, and what fixed it.** The DID was assigned to **SIP login
408146** — *the IP-auth trunk login*. Per §5.1, activating an IP trunk consumes
that login for registered inbound: nothing is registered against it and nothing
ever will be, so the call arrived somewhere that structurally could not accept
it. Not the carrier, and not our trunk.

The fix is Zadarma's **External server (SIP URI)** mode on the number:

```
sip: <lt-did>@<de1 public IP>      # confirmed via GET /v1/direct_numbers/
```

Zadarma then INVITEs de1 directly and livekit-sip's inbound trunk answers — the
same shape as the outbound trunk, in the other direction. Settable in the panel
(*number → External server*), or via `PUT /v1/direct_numbers/set_sip_id/`, whose
`sip_id` takes either a SIP login or a SIP URI.

⚠ Note the response key is **`info`**, not `numbers` — reading the wrong key
returns an empty list under `"status": "success"`, which looks exactly like an
account with no DIDs on it. Same trap as the statistics window in §7.1.

The alternative — attach the DID to the virtual PBX (`pbx.zadarma.com`,
extension 101) and forward from an incoming-call scenario — is what Zadarma's
support suggests and it does work, but it puts their PBX in the signalling and
media path for no benefit we need. **The PBX extension is not part of this
design; nothing in zvonok registers against it.** It is also why ten of the
twelve source IPs in §5.1 are irrelevant to us: they are PBX nodes.

**`CALLED_DID` is the header that makes a multi-DID secretary possible.** Zadarma
puts the virtual number that received the call in a proprietary `CALLED_DID` SIP
header. That is precisely the key a callback needs — *which of our numbers did
they dial* — and it is what lets one inbound endpoint serve a Warsaw, a Madrid
and a London number and still know which campaign the caller is answering.
Without it, the Request-URI user is the only clue.

That dialog is also where §5.1's corrected network list came from, and enabling
SIP-URI mode without widening the firewall to match would have produced the same
non-ringing number for a completely different reason.

⚠ **Still no answerer, and that is a product defect the moment anyone has this
number.** The dispatch rule names `zvonok-secretary`; no such worker exists.
livekit-sip answers the INVITE regardless, so an inbound call today reaches
connected silence — the worst of the options, worse than a dead number and worse
than carrier voicemail, because "ring, connect, nothing" reads as spam or a
broken line rather than as a service that is not ready. The trunk's
`max_call_duration` is pinned to **60 s**, which caps the cost and the rudeness
without fixing either.

**Why it is nonetheless safe to leave routed right now, and exactly when it stops
being safe.** Every call we have ever placed went out from the UK DID —
`SELECT caller_id, count(*) FROM jobs` is 19/19 `<uk-did>`. Nobody has the
Lithuanian number; it appears in no call log anywhere, so the callback rate is
not low, it is zero. It is also not in `ZVONOK_OWNED_CALLER_IDS`, so no agent can
select it.

That changes the day it is used as a caller ID — which is the plan. **The rule,
therefore: a DID goes into `ZVONOK_OWNED_CALLER_IDS` only once something answers
it.** The people who ring an unanswered number are precisely the ones we just
interrupted, and the silence attaches to the number we intend to keep dialling
them from. That is answer-rate that cannot be bought back, and the cost lands on
the exact experiment §9.4 exists to re-run.

If inbound must stay unanswered for long, the honest posture is to point the DID
at carrier voicemail or a real phone rather than at us: a beep is a language
every adult already speaks.

**The message-taker's minimum bar** (below it, ship carrier voicemail instead —
a chatty half-agent is not progress): answer promptly and speak; disclose that it
is an AI and that the message is kept — inbound needs its own §8 wording, not the
outbound text, because someone returning a missed call did not knowingly dial a
machine; take a message and a callback number, because caller ID is withheld,
shared or spoofed often enough to be useless as the only route back; say plainly
what happens next and do not promise a call back with an answer unless a human
will actually make it; and **deliver the message somewhere a person will see it**.
A transcript nobody reads is strictly worse than a voicemail box on a phone that
buzzes.

**Scope note for when it is built:** `GET /v1/contacts/{number}` is
identity-scoped, which is right for an agent asking "did *I* already ring this
pharmacy". It is the wrong boundary for a secretary: a callback lands on a shared
DID, so an identity-scoped lookup would miss the outbound call that caused it and
route at random. The secretary needs tenant- or DID-scoped history — a deliberate
widening, decided once, and still never recited to the caller.

Three constraints that must be settled before any of that is written, because
each is a way to build the wrong thing convincingly:

1. **Caller ID is a hint, not authentication.** It is trivially spoofable. The
   history endpoint therefore must never let an inbound caller hear another
   person's call history — the failure mode of "we called you about X" when the
   number was spoofed is disclosing our outbound activity to a stranger. Match on
   caller ID to *route*, never to *authorise*.
2. **Unknown callers need a defined behaviour**, and it is the common case: most
   numbers that ring us will never have been dialled by us.
3. **Disclosure on inbound is a different question from outbound.** §8's wording
   assumes we initiated the call and the callee did not choose to speak to a
   machine. Someone who rings a number back has arguably chosen differently — but
   not knowingly, and that distinction has to be made deliberately rather than by
   reusing the outbound text because it exists.

### 5.7 Tenancy model (added 2026-07-31, branch `multi-tenant` — rationale in §9.7)

One box, more than one billing account. Two scopes, and conflating them is the
mistake the whole design exists to prevent:

| | **Tenant** | **Identity** |
|---|---|---|
| Is | a billing account | one bearer token = one client agent |
| Owns | SIP trunk, verified DIDs, xAI key, its own agent worker | daily caps, audit trail, idempotency scope |
| Env suffix | `_<TENANT>` — `XAI_API_KEY_TENANT2` | `_<IDENTITY>` — `ZVONOK_DAILY_USD_FRIEND` |
| Cardinality | 1..n identities belong to it | belongs to exactly one tenant |

`mac-claude` and `openclaw` are two identities of one tenant: separate budgets,
one Zadarma account. An unsuffixed variable *is* the default tenant's, so a
single-tenant deployment is a one-tenant deployment with nothing to configure.

**Routing.** Each tenant runs its own voice worker registered under its own
`agent_name`; `dispatch_call` selects it. That name is the *only* tenant-specific
value crossing into LiveKit — deliberately, because dispatch metadata is
persisted in Redis and appears in logs, so trunk ids and API keys must never
travel that way. The worker's own env holds those. Consequence worth stating
plainly: **`agent_name` is the whole isolation mechanism for placing a call**,
which is why a duplicate is fatal at startup rather than a warning (§9.7 trap 1).

**Inheritance is not uniform, and the split is load-bearing.** Account-identifying
values (`XAI_API_KEY`, `ZVONOK_AGENT_NAME`, the three caller-ID variables) do
**not** fall back to the unsuffixed variable for an added tenant — `config._own`.
Operational ones (base URL, extractor model, caps) do — `config._scoped`. A
forgotten key that quietly resolved to the *other* tenant's would bill the wrong
person; forgotten DIDs would let one tenant dial out as the other. Both failures
are silent — the call connects and sounds normal — so they are made unreachable
by configuration error rather than merely discouraged.

**What is isolated:** caller ID and trunk (per tenant), extraction key (per
tenant), spend caps and audit (per identity), API reads (per identity — `_job_or_404`
404s across identities), the same-number-in-flight 409 (per tenant: two
identities of one account are one caller to the callee; across accounts it would
be a false conflict *and* would disclose the other tenant's target), and the
worker callbacks — `ZVONOK_INTERNAL_TOKEN` is **per tenant** and does not
inherit, so the internal endpoints know not merely that *a* worker is reporting
but *whose*.

**A job's tenant is stored, not re-derived.** `jobs.tenant` is written at
admission and read by everything downstream (`Settings.tenant_of`). Dispatch
already fixes the trunk and the caller ID when the call is placed; this makes
the accounting equally immutable. Without it, editing `ZVONOK_TENANT_<IDENTITY>`
— or dropping the mapping while jobs were still unextracted — moved extraction,
`/reextract` and the janitor's disk recovery onto the *new* mapping, so one
tenant's transcript was read by, and billed to, another tenant's xAI key. The
already-created LiveKit dispatch keeps its original agent name, so no in-flight
PSTN leg reroutes: it is the money and the transcript that move, silently. Rows
written before the column fall back to the derived answer, which is what they
had anyway — the identity→tenant mapping lives in env, so SQL cannot backfill
them.

**What is not, and cannot be without a second LiveKit and a second Postgres:**
`LIVEKIT_API_KEY` (room admin — a tenant's worker can in principle join another's
room, and trunk ids are not secret to anything holding it), the jobs database,
the transcript directory. This is **co-located
multi-account with a soft trust boundary**, not tenant isolation: correct for
people who know each other, not for strangers. Hosting a third party also makes
the operator a processor for their callees' voice data under their own retention
policy.

---

## 6. Security & guardrails (this is a spend-capable actuator)

- **AuthN/Z:** bearer tokens per client identity (mac-claude, openclaw, manual); Caddy TLS; tokens in env/secret files, never in repo.
- **Tenancy (§5.7):** an identity belongs to a billing account. Caller IDs, SIP trunk and extraction key are per *tenant*; caps and audit per *identity*. Account-identifying config never inherits — a forgotten variable fails startup rather than resolving to another account's key or numbers. What stays shared (LiveKit room admin, Postgres, transcripts) makes this a soft boundary: safe for people who know each other, not for strangers.
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
- Baseline built into the agent prompt, at call start, on every call: **it is an AI assistant, and the answer is being kept.** Those two facts are non-negotiable at every level. *On whose behalf* is stated at `light` and `full` and deliberately omitted at `brief` — see §8.1. If the callee objects: apologise, end, and **discard the transcript** (implemented: `declined` writes a minimal audit record with no turns).

#### 8.1 Three disclosure levels

**"I am an AI" and "your words are being kept" are different disclosures, and the first does not imply the second.** Knowing you are talking to a machine tells you nothing about retention — a caller can reasonably assume an AI handles the call transiently, like a voice menu. We retain transcripts, so the storage fact must be stated. What is negotiable is the *register*, not the content:

| Level | Wording | When |
|---|---|---|
| **`brief`** | *"Hello, this is an AI assistant, I'll note the answer down. <question>"* | One-question canvassing: ringing 15 pharmacies for a drug, shops for a part |
| **`light`** (default) | *"This is Klava, an AI assistant calling for <owner>. I'll note down your answer so nothing gets lost."* | Ordinary information-gathering calls |
| **`full`** | *"… This call is transcribed and stored. If you would rather it were not, say so and I'll end the call."* | Two-party-consent jurisdictions; anything that books or commits on someone's behalf |

**`brief` is a conversation mode, not just a shorter sentence.** The same knob — how much ceremony this call can afford — moves both, so it is one field rather than two. See §5.3.3 for what else it changes.

**`brief` is a genuine thinning of §8, not merely a change of register.** State it plainly: it discloses the AI fact and the retention fact, **and nothing else — no principal, no personal name.** Both omissions were learned on the first Polish canvass. The owner framing ("calling for a potential client") costs three seconds in front of the question and means nothing to a counter clerk who has never heard of them; a name nobody recognises invites *"Klawa? which Klawa?"* instead of an answer.

Two consequences follow, and both are load-bearing:

- `disclosure_delivered()` drops its **name** requirement at this level. Leaving it in would have `disclosure_guard` conclude the disclosure never landed and cut across the callee to repeat a line they had already heard — the exact failure the guard exists to prevent.
- `brief` is **never selected by the heuristic**, only by an explicit `disclosure_level`, *and* it is refused where the goal commits something. Shortening a disclosure must be a decision made by an agent that knows the call is a one-question canvass; the country table knows only where the call is going. And `policy.disclosure_level_for()` upgrades `brief` to `full` when the goal contains commitment words — nobody books, orders or cancels in someone's name behind the shortest disclosure we have. The asymmetry is deliberate: a caller may always ask for **more** disclosure than we would choose, never less than the goal warrants.

Rationale for `light` being the default: "this call is recorded" is call-centre boilerplate that people hear as surveillance, and it frightens them into hanging up before the goal is reached. It is also **less accurate than it sounds** — we keep *text*, not audio (audio recording is off by default), so "I'll note down your answer" describes what actually happens and is what a human receptionist would say.

Selected per call via `disclosure_level` in the dispatch metadata, or by a **policy table keyed on destination country and task type**. Unknown values fall back to `light`.

**Enforcement is not left to the model.** `disclosure_delivered()` requires an AI self-identification **and** a storage word in a **single uninterrupted turn**, plus — at `light` and `full` only — the assistant's name. A turn shredded by barge-in does not count, and neither does an introduction that names the AI but omits the retention. Failing that, `disclosure_guard` speaks the canonical text verbatim with `allow_interruptions=False`.

The `interrupted` check is the part that makes that claim true rather than merely documented: for a while the function described the requirement and never checked the flag, matching substrings on any assistant turn — so a shredded introduction still counted as delivered, which is precisely the phase-1 failure the guard exists to prevent. There is now a test for it (`test_policy.py`).


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

## 9. Rollout and findings log

This is the archaeology. Everything above states what is true now; this states what was tried, what it cost, and what it taught.

### 9.1 Phase 0 — trunk validation (~1 hour, no code)
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

### 9.2 Phase 1 — first AI call (a weekend)
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

#### 9.2.1 Phase-1 hardening pass (2026-07-27, after independent Codex + Grok reviews)

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

### 9.3 Phase 2 — the product spine
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

### 9.4 The first canvass (2026-07-31) — post-mortem

**What was run.** 15 pharmacies in Warsaw's Mokotów district, Polish, `brief`
disclosure, one question ("do you have Ozempic 2 mg in stock?") with a fallback
about dispensing 1 mg against a 2 mg prescription. Two concurrent, ~120 s cap,
**~$1.64 total**.

**What came back: nothing usable.** Zero answers about the drug.

**The SIP layer's own tally**, which is worth more than our transcripts because
it is independent of the agent that was mishearing everything:

| | |
|---|---|
| dialled | 15 |
| never answered (`480 Temporarily unavailable`, after ringing) | 6 |
| answered | 9 |
| … of those, ended by the far end (`BYE`) | 6 |
| … of those, ended by us (room deleted: voicemail, IVR dead-end, cap) | 3 |

Ring time before pickup ranges from **1.4 s to 22.5 s**, and that spread is
itself a signal: the three numbers answering in under 1.5 s are an auto-attendant
picking up instantly, not a person reaching for a handset. Nine answers, six of
which the other end terminated, is the entire evidential basis for anything said
about how businesses react — and six of the fifteen numbers never even rang
through to a human on a weekday afternoon.

**The experiment was invalid, and it took two reviews and a protocol probe to see
why.** Ranked by how much each one could have produced this result on its own:

1. **The ASR was never told what language it was listening to.** The fix believed
   to be in place was sending `language`/`keywords`; xAI reads
   `language_hint`/`keyterms` (§5.3.2). The server echoed the wrong keys back, so
   every log and every `session.updated` frame agreed the pin was applied. It was
   not. **All fifteen calls ran on automatic language detection**, which is
   weakest in exactly the first second — the second that decides the call. And
   `Ozempic` / `semaglutyd`, the words the entire canvass turned on, never reached
   the recogniser at all.
2. **A greeting misheard is not a transcript defect — it redirects the call.** Two
   observed instances: `"Apteka X, słucham?"` came back as the English *"I think I
   look to the."*, and `"How to take a pressure?"` — which the model then
   **answered**, giving a pharmacy instructions on using a blood-pressure cuff
   instead of asking about the drug.
3. **That same hallucination was then classified as an IVR menu**, because
   `"press" in text` also matches `"pressure"`, and it bought sixty seconds of
   patient silence on a line where nobody had said anything of the sort.
4. **We rang Polish pharmacies from a UK mobile number.** Both reviewers
   independently named this the single largest untested variable, and it is the
   only one we have not eliminated.
5. **The model was upgraded 1.0 → 2.0 in the same change as the language pin.** So
   the transcription improvement that was observed is attributable *entirely* to
   the model — the pin was doing nothing. Two variables, one measurement, and the
   conclusion drawn from it was wrong.

**A wrong diagnosis worth recording, because it wasted a day.** The agent was
reported as taking nine seconds to reply. It was not: the claim came from
transcript `t` values, which are stamped when a turn is **transcribed**, not when
it was heard or spoken. The real local gap is on the order of 3.5–4 s. There is
now a direct measurement (`reply gap`, callee-stops-talking → our audio starts,
logged alongside model `ttft`), and its docstring says exactly what it excludes,
because the previous version of that log claimed to include the end-of-turn wait
and did not.

**What this does NOT license concluding.** That businesses hang up on an AI
caller. **n = 6** under broken conditions is not a finding, and there is a
competing explanation for every one of those six. It also does not license the
opposite: a shortage drug is a plausible thing to be short with any caller about.

**The clean re-run, when it happens:** the same fifteen numbers, the ASR fix in
place, **a Polish caller ID**, and nothing else changed. Two variables have
already been confounded once in this experiment; a third would make the result
uninterpretable again. The metric is *"a human stayed on the line as far as the
product question"*, not *"the call connected"* — the latter is what made this run
look like a product verdict when it was an instrumentation failure.

### 9.5 Phase 3 — reality hardening (in progress)
RU/ES language profiles benchmarked on real 8 kHz calls; cascade profile B wired and selectable; IVR bounded-navigation + DTMF; retry policy (busy/no-answer); webhooks; cost accounting; SIP-TLS/SRTP `require`; caller-ID-per-country table.

### 9.6 Phase 4 — optional/later
Inbound (separate SIP login + dispatch rules → "secretary" agent), human-transfer to my mobile, recordings (post §8 review), archival sync of transcripts to n5, second trunk (Telnyx) for HD/SIP-diversity, admin mini-UI.

### 9.7 Phase 5 — multi-tenancy (2026-07-31, branch `multi-tenant`)

**Trigger.** "Can my friend's calls run on my box?" The answer that mattered was
the follow-up: *he has his own Zadarma account and his own xAI key*. That turns
the problem from multi-tenant SaaS — which needs per-tenant trunks, billing, KYC
and an abuse process, and makes the operator a telecom reseller — into
**two isolated accounts co-located on one host**, which the existing design was
already most of the way toward. Spec in §5.7.

**What was already true and did not need building.** `identity` was a first-class
column from phase 2, `_job_or_404` already enforced read isolation across
identities, idempotency was already scoped per identity, and spend counters were
already keyed `(day, identity)`. The gap was never the data model; it was that
everything *account-shaped* — caller IDs, the xAI key, the trunk — was
module-level or process-level global.

**Decisions taken:**

- **One worker per tenant, routed by `agent_name`.** Rejected: passing per-call
  credentials through dispatch metadata (it is persisted in Redis and logged —
  the rule `dispatch.py` already stated), and a per-call trunk lookup in the
  agent (would put every tenant's trunk in every tenant's process). A separate
  worker gets trunk, voice key and spoken identity from its own env for free.
- **Env suffixes rather than a config file.** Keeps "secrets in env on de1 only"
  (§6) intact and makes a single-tenant deployment configure to exactly what it
  was. Cost: two suffix namespaces to keep aligned; both reviewers called this a
  poor 1am interface and they are right — §10 item 12.
- **Caller-ID policy became a value (`policy.CallerIds`), not module state.**
  The module-level `OWNED_CALLER_IDS` frozenset was the single largest blocker:
  one process cannot serve two accounts while the verified-DID list is global.
- **Compose: `env_file` for call-api only.** It is the one service that
  legitimately needs every tenant's config; enumerating arbitrary suffixes would
  fail *silently* when a line is forgotten. Workers deliberately do not get it.

**Traps, and how they landed.** Items 1–3 were found by writing the tests, 4–8 by
Codex and Grok reviewing the branch. Six of the eight are the same shape: a
misconfiguration that produces a working call billed to the wrong person.

| # | Trap | Outcome |
|---|---|---|
| 1 | **Account values inheriting from the unsuffixed variable.** A forgotten `XAI_API_KEY_TENANT2` resolved to the default tenant's key; forgotten DIDs let the added tenant present the default tenant's numbers — i.e. exactly what the change existed to prevent. | `_own` vs `_scoped` split; fatal at startup. Caught by the config test, not by review. |
| 2 | **Empty ≠ absent.** docker-compose passes an unconfigured variable as `""`, which `os.getenv(name, default)` treats as *present*. | Empty-as-absent at both lookup levels. Also fixed pre-existing `_int`/`_float`, where `ZVONOK_MAX_CONCURRENT_CALLS=""` crashed call-api at import. |
| 3 | **A nameless worker accepts every tenant's jobs.** Same empty-string path, but in the agent: an empty `agent_name` disables explicit dispatch entirely. | `SystemExit` on set-but-empty; unset still means the single-tenant default. Same guard for an empty trunk id. |
| 4 | **Duplicate `agent_name` was only a warning.** LiveKit hands each dispatch to whichever worker is free, so ~half of one tenant's calls leave on the other's trunk, with the other's caller ID, billed to the other's balance. Nothing looks wrong. | Fatal in `require()`. This was inconsistent with a missing key already being fatal — the more expensive error was the more forgiving one. |
| 5 | **Whitespace defeated that fix.** The API compared names verbatim; the worker `.strip()`s before registering. `" zvonok-caller "` and `"zvonok-caller"` pass the uniqueness check and collide at LiveKit. | Config strips to match the worker. |
| 6 | **`_suffix()` is not injective.** It collapses punctuation runs, so identities `friend-a` and `friend_a` read the same `ZVONOK_TENANT_*` and the same caps — separate in `ZVONOK_API_TOKENS`, one budget and one account in effect. | Canonical suffix collisions are fatal, for identities and tenants. |
| 7 | **Caller-ID config was fail-open**, while `.env.example` claimed it failed startup. Empty DIDs → the trunk default is presented and everything prices as the expensive origin group. | Fatal for added tenants, plus a check that a tenant's default DID is on its own verified list. Default tenant's requirements untouched, so existing deploys are unaffected. |
| 8 | **`/healthz` was unauthenticated and listed every in-flight job id.** A bare count is still activity: polled on a timer it says when the other tenant is on the phone and for how long. | Liveness and static config only. |

**Defence in depth.** The worker re-checks any caller ID the API hands it against
its own `ZVONOK_OWNED_CALLER_IDS` and falls back to the trunk default rather than
failing the call — the number is wrong, not the request, and the trunk's own
default is by definition a DID of the right account. Reaching that branch means
call-api routed a job to the wrong worker, so it logs loudly.

**Verified.** `docker compose config` renders identically with no profile (an
existing `.env` needs no edits); the profile adds exactly one service.
`api/tests/test_policy.py` covers tenant isolation and config resolution — 23
assertions in the config test alone. Not verified: a real second Zadarma account
placing a real call. **This has not been run with two live tenants.**

**Both reviewers' findings are now closed** (2026-08-03): the internal token is
per tenant and `jobs.tenant` is stored at admission — see §5.7. Both were
pre-existing designs that multi-tenancy made exploitable rather than new
defects, which is why they were worth fixing before a second account exists
rather than after. What remains open is the ergonomics of the env-suffix
interface, §10 item 10.

**Operational note for the second trunk:** it must use **SIP credential auth**,
not IP auth. Ours is authorised by de1's static IP; if a second Zadarma account
whitelists the same IP, two accounts claim one source and which one is billed is
not a thing to learn from an invoice.

---

## 10. Open questions

Resolved items are not kept here — they have been folded into the sections above.

**Blocking the next real experiment:**

1. **Geographic DIDs for the markets we call: Warsaw `+48 22`, Madrid `+34 91`.**
   Two purchases, and they unblock two separate things at once — §9.4's re-run
   needs a Polish caller ID as its only remaining uncontrolled variable, and §5.6
   needs a number that ordinary phones can actually ring back. Caller ID is also
   a *cost* lever, not merely an answer-rate one (§9.1 measured a x20–34 origin
   swing on EU routes). Both must pass the §5.1 acceptance gate before use.

   ⚠ The claim that a matching-country caller ID **raises answer rates** is
   assumed, not measured. Phase 0 measured *pricing*, never pickup. Buy them for
   the cost and the callback path, and treat any answer-rate improvement as a
   hypothesis the re-run is testing rather than a reason it will work.
2. **Does `language_hint` measurably change what comes back?** The plumbing is
   settled — the options the plugin builds serialise to
   `session.audio.input.transcription = {"language_hint": "pl", "keyterms": [...]}`,
   dumped from the real object inside the deployed container, so it is on the
   wire in xAI's field names. What is *not* settled is whether the server acts on
   it, and it cannot be settled from the protocol: the schema is not validated
   server-side and unknown keys are echoed back unchanged. The only proof is
   behavioural — the same Polish audio, hint on vs hint off, transcripts
   compared. Cheap, and it is exactly the check whose absence caused §9.4.

   **Measured 2026-07-31, and the answer so far is "no effect we can detect."**
   `tools/asr_probe.py` streams a WAV straight at the realtime API, bypassing
   LiveKit so nothing in between can introduce or hide a difference. Twenty arms
   over real 8 kHz PSTN audio (the Spanish voicemail leg): clip lengths 60 s /
   2 s / 1 s, SNR clean / 5 / 0 / −6 dB, hints `off` / `es` / `pl` / `ru`, plus
   the OpenAI spelling `language` as a negative control. **Every arm returned an
   identical, correct Spanish transcript** — including a deliberately wrong
   Polish hint on one second of speech buried under noise louder than the
   signal.

   This does **not** license "xAI ignores `language_hint`", and both reviewers
   said so independently: a hint is a soft prior, and a recogniser that honours
   it can still be overridden by decisive acoustic evidence, so `hint=pl` ==
   `hint=off` is the expected result either way. What it does license is
   refusing to *assume* the opposite. §9.4 ranked "the ASR was never told what
   language it was listening to" as root cause #1 of the failed canvass; that
   ranking now rests on nothing measured, and the untested `keyterms` half of
   the same fix is the more product-critical one anyway — a canvass turns on
   whether `Ozempic` and `semaglutyd` survive 8 kHz, not on language ID.

   ⇒ **Next is `keyterms`, not more `language_hint`.** It is the sharper probe
   because it is near-binary: a rare proper noun either appears in the
   transcript or it does not, where a language hint has to out-argue the
   acoustics. Needs audio containing a low-prior proper noun that the unhinted
   arm gets wrong — which we do not have, because audio recording is off by
   default and no Polish call was ever captured. Recording one leg of the
   re-run, once, is what unblocks this.

   Two probe bugs found while building it, both of which manufacture a
   convincing null and are worth knowing about for any future protocol probe:
   streaming audio before `session.updated` arrives (the decisive first second
   decodes under defaults), and ending a clip by *stopping transmission* rather
   than sending trailing silence — server VAD never sees an end-of-turn, never
   commits the segment, and the arm scores zero as if the recogniser heard
   nothing. Nine arms scored zero that way before it was caught.
3. **What the reply gap actually is.** Instrumentation is deployed and has never
   run against a live call. Everything said about this agent's latency so far has
   been inferred from the wrong timestamps.
4. **Does the inbound 60 s fuse actually fire on the wire?** It is configured on
   the trunk and has never been observed ending a call. If it does not fire, the
   silence is unbounded rather than merely rude. One test call answers it — and
   the same call is the only proof that inbound transport works at all.

**Derived rather than confirmed, and load-bearing.** §5.1's firewall scope was
exactly this shape — outbound-proven, inbound-wrong, silent when it failed — so
the class is worth listing rather than trusting:

- **RTP source ranges are assumed to equal the six SIP ranges.** Never observed
  under real answered inbound media; if media comes from elsewhere the symptom is
  one-way audio, not an error.
- **Per-login concurrent channel limit** is still unknown (§10.7). Inbound makes
  exhaustion a product bug rather than a footnote: a callback sitting in silence
  can occupy a channel an outbound call needed.
- **Inbound tariff.** Outbound origin pricing was measured carefully (§9.1);
  what our own DIDs cost to *receive* was never established.
- **Two allowlists, one fact.** `deploy/firewall.sh` and `ZADARMA_SIP_NETS` in
  `deploy/lkctl.py` must agree and are maintained separately. Drift fails closed
  (a number stops ringing) unless someone widens 5060 while debugging, which
  fails open.

**Design decisions owed before code is written:**

4. **Inbound behaviour** — the three constraints in §5.6 (caller ID is not
   authentication; unknown callers are the common case; inbound disclosure is a
   different question from outbound).
5. **Question-first opener for canvassing.** Proposed: *"Dzień dobry, czy mają
   Państwo Ozempic 2 mg? Tu asystent AI — tylko zanotuję odpowiedź."* Both §8
   facts still land in the same breath, before the callee answers, but the first
   thing they hear is an ordinary question about stock rather than the word
   "robot". This is a deliberate compliance trade and needs an explicit decision,
   not a quiet prompt edit.

**Standing / not blocking:**

6. Grok Voice quality for **Russian** and **Spanish** over 8 kHz — benchmark
   before making either the per-language default.
7. Exact Zadarma per-login concurrent-channel limit on our tariff. (DIDs report 2
   channels each; the trunk login is unknown.)
8. xAI **concurrency limit and actual per-minute billing** on our tier. The
   $0.05/min in §7 is a public list price, not a figure from our invoice.
9. `mcp>=2.0` renames `mcp.server.fastmcp` to `mcp.server.mcpserver`, which breaks
   `mcp/server.py`. Pinned to `<2` for now; migrate deliberately rather than
   discovering it on a client upgrade.

**Owed before a second tenant places a real call:**

10. **The env-suffix interface is poor for humans.** Two suffix namespaces
    (`_<TENANT>` for accounts, `_<IDENTITY>` for caps) plus a compose profile
    name that must stay aligned with the tenant name. Both reviewers said the
    same thing unprompted. Options: a tenants file with env for secrets only, or
    generate the per-tenant compose service. Not urgent at two tenants; it is the
    thing that will bite at three.

11. **Nothing binds a job's tenant to the trunk that actually dials it.** Named
    by Grok as the next *working call, wrong bill*, and it is the honest limit of
    what §5.7 currently guarantees. Placement isolation is still only
    `agent_name` → whichever process registered that name → that process's
    `SIP_OUTBOUND_TRUNK_ID`. The API never records the trunk on the job, and
    nothing at dial time asserts that the worker's trunk matches `jobs.tenant`.
    Three ways to reach it, all silent: worker env drift, a name registered at
    LiveKit that differs from the configured one (`require()` checks *config*,
    not who actually registered), and an identity whose `ZVONOK_TENANT_<IDENTITY>`
    is simply unset — it becomes the default tenant, so every carefully set
    `_FRIEND` variable sits unused while their calls leave on our trunk. Fix:
    record the trunk id on the job at dispatch and have the worker refuse a job
    whose trunk is not its own.

12. **The janitor's disk recovery bypasses the internal token entirely.** It
    reads the shared transcript directory and finalises by job id, so it is the
    one remaining path that can settle a call and trigger extraction without
    presenting a tenant's token at all. Lower priority than 11 because it
    misattributes rather than dials, but it is the surviving hole in the fix
    above and should not be rediscovered later as a surprise.

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
