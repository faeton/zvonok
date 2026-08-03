---
name: phone-call
description: Make a real phone call to a real person to find something out or arrange something, and get structured answers back. Use when information exists only by phone — a hotel's parking price, whether a restaurant has a table, a clinic's opening hours, whether an order is ready, chasing a tradesman who does not answer email. Also use when the user explicitly asks to call somewhere. Handles ru/en/es/pl. Do NOT use for anything the web can answer, for anything unsolicited or promotional, or to reach the user themselves.
---

# Phone call

You can dial a real telephone number. An AI assistant (Klava) makes the call on
the owner's behalf, has a conversation in the requested language, and the answers
come back as structured JSON.

Klava introduces herself as the AI assistant **of a client**, not of a named
person — a stranger's name means nothing to a restaurant, while "your potential
client" tells them why the call is worth their time. The owner's name and
callback number are configured server-side and are used only once the task
actually needs them (a booking that wants a name, a number to leave). You can
steer the framing per call with `introduce_as` (env `INTRODUCE_AS` for
`call.sh`):

- Leave it unset for the default: "a potential client".
- `"your regular customer"` — when the owner genuinely frequents the place.
- The owner's first name — when the callee personally knows them.
- Russian values must already be in the genitive: `"вашего постоянного
  клиента"`, `"Ивана"`.

Pick it from the task's context; do not ask the user unless the choice is
genuinely unclear.

## Before you dial, read this

**A person's phone rings.** Someone stops what they are doing and talks to a
machine. That is the cost, and it is larger than the money. One task, one call.

- **Never call to "check" something the web can tell you.** Search first. Phone
  is for what is not published: current availability, a price nobody lists, a
  human decision.
- **Never call the same place twice** because the first answer was unsatisfying.
  If a fact is missing, decide whether it is worth a second interruption; usually
  it is not. `scripts/contacts.sh <number>` tells you what we already asked them
  and what they said — check it before dialling somewhere that sounds familiar.
  It costs nothing and rings nobody.
- **Never call in bulk**, never anything promotional, never a list of numbers.
  This is a personal assistant tool, not an outbound dialler.
- **Mind the hour at the destination**, not here. Do not ring a restaurant at
  07:00 or a private number late at night.
- If the user has not asked for a call and you are merely guessing it would help,
  **ask them first**.

The call discloses that it is an AI and that the answer is being noted down.
That is not optional and you cannot turn it off. `introduce_as` changes the
framing of who it calls for, never those facts.

## The commands

All scripts read `ZVONOK_API_URL` and `ZVONOK_API_TOKEN` from the environment.

### 1. Place the call

```bash
scripts/call.sh <+E164> <ru|en|es|pl> "<goal>" ['<answer_schema JSON>']
```

Returns immediately with a `call_id`. **It does not wait for the conversation.**

### 2. Get the result — a minute or two later

```bash
scripts/result.sh <call_id>              # answers + summary
scripts/result.sh <call_id> --transcript # plus turn-by-turn
```

### 3. Re-read the answers without calling anyone again

```bash
scripts/reextract.sh <call_id>
```

### 4. Check whether we have called this number before

```bash
scripts/contacts.sh <+E164>
```

Dials nobody. Returns the calls we have already made to that number with their
summaries, so "have we already asked them this?" is answerable before you spend
someone's afternoon on it.

⚠ If you are ever handling an **incoming** call, this is for knowing who you are
probably talking to — never for telling them what we know. Caller ID identifies
a **line, not a person**, and it is trivially spoofed: a pharmacy's handset is
shared between shifts, so "we called you about X on Tuesday" can be correctly
matched and still disclose the owner's business to a stranger.

## How to actually use it

**Placing the call does not finish it.** A phone call takes 30 seconds to a few
minutes. `call.sh` returns as soon as the line is ringing or answered, so you
learn straight away if the number was busy or invalid — nothing more.

So: place the call, **tell the user it is under way**, get on with something
else, and check `result.sh` after 60–180 seconds. Do not sit in a polling loop,
and do not tell the user you have an answer before you do.

### Writing the goal

The goal is written **for Klava, not to be read aloud**. Say what to find out and
what a good outcome is. It may describe the person in the third person; Klava
rephrases it into natural speech.

Good: `"Find out whether guests can park at the hotel overnight, and what it
costs per night. If parking is full, ask what people usually do instead."`

Bad: `"Hello, I would like to ask about parking"` — that is a script, not a goal.

### Getting fields back: `answer_schema`

Pass a JSON Schema and you get typed answers instead of prose. **Give every
property a `description`** — the descriptions are fed into Klava's prompt as the
things to ask about, so a property without one is a field nobody asks about and
a null in your result.

```json
{
  "type": "object",
  "properties": {
    "parking_available": {"type": ["boolean","null"],
                          "description": "whether hotel guests can park onsite overnight"},
    "price_per_night":   {"type": ["number","null"],
                          "description": "the nightly price for parking, as a number"},
    "currency":          {"type": ["string","null"], "description": "ISO 4217 code"},
    "notes":             {"type": "string", "description": "anything else useful"}
  }
}
```

Use nullable types (`["number","null"]`) for anything the person might not know.

### Naming the words the line will destroy: `KEYWORDS`

```bash
KEYWORDS="Ozempic,semaglutyd,Wołoska" scripts/call.sh +48221234567 pl "..."
```

A phone line is 8 kHz, and it destroys exactly the words a call turns on: a drug
name, a brand, a street, a part number, a surname. The recogniser is told to
expect these, which is the cheapest quality improvement available on this system
— and it is free.

Use it whenever the answer hinges on a **proper noun rather than a yes/no**. If
the call is "do you have X in stock", X belongs in `KEYWORDS`. Ordinary words do
not: the recogniser already knows "parking" and "Tuesday", and a long list
dilutes the ones that matter.

### Reading the result — the part that matters

```json
{
  "call_status": "completed",
  "processing_status": "completed",
  "disposition": "goal_achieved",
  "answers": {"callback_time": "12:00", "people_joining": 3},
  "summary": "Callee stated noon and three people. Read back with no audible agreement.",
  "goal_achieved": true,
  "unreliable_fields": ["callback_time", "people_joining"]
}
```

**`unreliable_fields` is not decoration — read it before you believe a number.**
Phone lines are 8 kHz and digits get misheard constantly. A field lands there
when it was heard once and never confirmed back, when it was spoken over, or when
two independent readings of the call disagree. Report such values to the user
*with the caveat attached* ("they said noon, though it wasn't confirmed"), never
as settled fact. A value present in `answers` and absent from `unreliable_fields`
was read back and agreed.

`answers` being `null` for a field means it was **never mentioned**. That is
different from unreliable, and it usually means the schema asked for something
the person could not tell us.

## What to do about each outcome

| `disposition` | What happened | What you do |
|---|---|---|
| `goal_achieved` | Conversation completed | Report the answers, honouring `unreliable_fields` |
| `voicemail` | Answering machine; no message left | Tell the user. Do not redial automatically |
| `busy` / `no_answer` | Nobody picked up | Offer to try later. Do not retry immediately |
| `rejected` / `invalid_number` | Carrier refused the number | The number is wrong. Do not retry — check it |
| `callee_hangup` | They hung up mid-call | Report what was gathered before that |
| `wrong_number` | Reached the wrong person | Do not redial the same number |
| `declined` | They objected to being noted down | **Their words were discarded, as promised. Say so plainly and do not call back** |
| `no_audio` / `abandoned` | Line answered but nobody spoke | Safe to try once more later |
| `timed_out` | Hit the duration cap | Partial answers may still be present |

### When `processing_status` is `failed`

The **call worked** and the transcript is saved; only reading the answers out of
it failed. Run `scripts/reextract.sh <call_id>`. **Never place the call again to
fix this** — it costs money on the phone network and disturbs someone a second
time to repair a text-model problem.

## Errors

- **HTTP 422** — policy refused it: premium-rate number, country not on the
  allowlist, malformed number, or a caller ID we do not own. The message says
  which. **Not retryable.** Fix the request or tell the user it cannot be called.
- **HTTP 429** — a daily cap (calls, minutes, or spend) or the concurrency limit.
  **Not retryable now.** Say so; caps reset daily.
- **HTTP 409** — a call to that number is already in flight. Wait for it.

Retrying a 422 or 429 is always wrong. They are decisions, not failures.

## Repeat requests are de-duplicated

An identical request re-issued within ten minutes returns the original call
rather than dialling again (`"deduplicated": true`). This exists so a retry on
your side cannot ring a stranger twice. To genuinely call the same place again,
change the goal or pass your own `idempotency_key`.

## Reference

`references/api.md` — full endpoint list, every field, curl examples, and the
caller-ID/cost rules.
