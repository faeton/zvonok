# zvonok call-api — reference

Base URL and token come from `ZVONOK_API_URL` / `ZVONOK_API_TOKEN`. The service
runs on de1's tailnet address; there is no public endpoint.

The token identifies **this agent** (`openclaw`), and caps are per identity — so
spending here does not eat the Mac's budget, and every job records who asked.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/calls` | Place a call → `202 {call_id, call_status, …}` |
| `GET` | `/v1/calls/{id}` | Status + summary |
| `GET` | `/v1/calls/{id}/result` | Answers, summary, `unreliable_fields` |
| `GET` | `/v1/calls/{id}/transcript` | Turn-by-turn |
| `POST` | `/v1/calls/{id}/reextract` | Re-read answers, no redial |
| `POST` | `/v1/calls/{id}/cancel` | Kill a call in flight |
| `GET` | `/v1/calls?since=…&limit=…` | Recent history |
| `GET` | `/healthz` | Liveness + what is in flight (no auth) |

## POST /v1/calls

| Field | Required | Notes |
|---|---|---|
| `number` | yes | E.164, e.g. `+34911234567` |
| `goal` | yes | Written for the assistant, not read aloud |
| `language` | yes | `ru`, `en`, `es` or `pl` |
| `answer_schema` | no | JSON Schema; **descriptions drive what gets asked** |
| `max_duration_seconds` | no | Default 300, range 30–600 |
| `caller_id` | no | Leave unset — see below |
| `disclosure_level` | no | `brief` / `light` / `full`; leave unset except for a one-question canvass, which wants `brief` |
| `introduce_as` | no | Who the call is said to be for; default "a potential client" |
| `keywords` | no | Proper nouns the call turns on — a drug, a brand, a part number. Biases speech recognition; ≤16 |
| `callback_url` | no | HMAC-signed webhook on completion |
| `idempotency_key` | no | Reusing one always returns the original job |
| `wait_seconds` | no | 0–30. Catches instant failures only |

### Leave `caller_id` alone

Caller ID is chosen from the destination country, and it is a **cost** lever, not
cosmetics: the same Spanish mobile costs about €0.022/min dialled from the UK
number and €0.44/min from the Ukrainian one — a ×20 swing, measured. Overriding
it with a mismatched origin is the easiest way to make a cheap call expensive.

### Leave `disclosure_level` alone

It is chosen from the destination country and whether the goal commits to
something on the owner's behalf. Germany, Switzerland and Poland always get the
explicit transcribed-and-stored wording; so does any goal that books, orders,
reserves or cancels. Overriding it down is a legal decision, not a tuning knob —
and `brief` on a goal that commits to something is refused outright, upgraded to
`full` rather than honoured.

### `keywords` — say the words the line will destroy

An 8 kHz phone line mangles proper nouns first, and a canvass usually turns on
exactly one of them. Passing `["Ozempic", "semaglutyd"]` biases the recogniser
towards hearing them; without it the call can turn on a word the transcript never
contains. Give native spellings, keep the list short — a long one makes
recognition worse rather than better.

### `introduce_as` — the framing of who is calling

The assistant always discloses that it is an AI and that answers are noted
down; `introduce_as` only changes whose assistant it says it is. Unset, it says
"a potential client". Set it to `"your regular customer"` where that is true,
or to the owner's first name when the callee personally knows them. Russian
values must arrive in the genitive ("вашего постоянного клиента", "Ивана") —
the phrase is spliced verbatim into the spoken introduction. The owner's actual
name and callback number live in server-side env and are only used mid-call
when a booking needs them.

## Statuses

`call_status` — where the call got to: `queued`, `provisioning`, `dialing`,
`ringing`, `in_progress`, `ending`, `post_processing`, `completed`, or terminal
`busy`, `no_answer`, `rejected`, `voicemail`, `failed`, `canceled`, `timed_out`,
`invalid_number`.

`processing_status` — the answer-reading pass, separately: `pending`,
`extracting`, `completed`, `failed`, `skipped`.

**These two are deliberately independent.** A `completed` call with `failed`
processing is a successful call whose answers could not be read — fix it with
`reextract`, never with another call.

## Destination policy

Default-deny. Allowed countries: AE, CH, CY, DE, ES, FR, GB, IN, IT, NP, PL, SA,
UA, US. Refused regardless: premium-rate ranges (UK `09`/`087x`, ES `80x`/`90x`,
US `900`/`976`, DE `0900`, …), Caribbean `+1` area codes that bill as premium,
satellite prefixes, and anything too short to be a real international number —
which is what keeps emergency numbers unreachable.

A refusal is `422` with a message naming the reason. It is a decision, not a
transient error: never retry it.

## Caps

Per identity per day: 40 calls, 60 minutes, $10 estimated spend. Globally: 2
concurrent calls. Exhausting one gives `429`. In-flight calls count against the
minute and spend caps at their full duration cap, so the budget cannot be
oversubscribed in the seconds before the first call ends.

## curl, if you need it directly

```bash
curl -sS -X POST "$ZVONOK_API_URL/v1/calls" \
  -H "Authorization: Bearer $ZVONOK_API_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"number":"+34911234567","language":"es",
       "goal":"Ask whether guests can park onsite and the nightly price.",
       "wait_seconds":12}'

curl -sS -H "Authorization: Bearer $ZVONOK_API_TOKEN" \
  "$ZVONOK_API_URL/v1/calls/<id>/result"
```

## Where this lives

Source of truth is the `zvonok` repo (`~/Sites/zvonok` on the Mac, `~/zvonok` on
de1); `BRIEF.md` is the design record. This skill directory is installed from
`openclaw/phone-call/` in that repo — edit it there, not in place, or the next
install will overwrite your changes.
