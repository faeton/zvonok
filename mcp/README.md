# zvonok MCP server

Gives Claude Code (and anything else speaking MCP) two tools: `phone_call` and `phone_call_result`.

This is a **thin adapter**. It holds no state and enforces nothing — destination policy, spend caps, idempotency and audit all live in call-api, so a second client cannot bypass them by talking to a different adapter.

## Reaching call-api

call-api binds to the server's **tailnet address only** (`<tailnet-ip>:18131`). There is no public endpoint: this API places calls that cost money, and it should not depend on a firewall rule staying correct.

- **Mac / Claude Code** — over Tailscale. Check with `curl -s -H "Authorization: Bearer $TOKEN" http://<tailnet-ip>:18131/healthz`.
- **OpenClaw** — runs on de1 itself, so the same address is a local interface. It gets its **own** token (`openclaw:…` in `ZVONOK_API_TOKENS`), which is what makes per-agent caps and per-agent audit possible.

## Claude Code setup

Add to `.mcp.json` in the project you want to be able to make calls from, or to `~/.claude.json` for all projects:

```json
{
  "mcpServers": {
    "zvonok": {
      "command": "uv",
      "args": [
        "run", "--quiet", "--with", "mcp<2", "--with", "httpx",
        "python", "/absolute/path/to/zvonok/mcp/server.py"
      ],
      "env": {
        "ZVONOK_API_URL": "http://<tailnet-ip>:18131",
        "ZVONOK_API_TOKEN": "<the mac-claude token from de1's deploy/.env>"
      }
    }
  }
}
```

The token is the `mac-claude:` half of `ZVONOK_API_TOKENS` on de1. It never enters this repo.

## Using it

```
phone_call(
  number="+34600123456",
  goal="Find out whether guests can park onsite and what it costs per night.",
  language="es",
  answer_schema={
    "type": "object",
    "properties": {
      "parking_available": {"type": ["boolean","null"],
                            "description": "whether guests can park at the hotel"},
      "price_per_night":   {"type": ["number","null"],
                            "description": "nightly price for parking, as a number"},
      "currency":          {"type": ["string","null"], "description": "ISO 4217 code"},
      "notes":             {"type": "string", "description": "anything else useful"}
    }
  }
)
→ {"call_id": "c_01K…", "call_status": "in_progress", "note": "…NOT produced an answer yet…"}

phone_call_result("c_01K…")            # ~60–180 s later
→ {"answers": {...}, "summary": "…", "goal_achieved": true, "unreliable_fields": []}
```

### Things worth knowing before you trust the output

- **`phone_call` returning is not the call finishing.** It returns once the line is ringing or answered, so an invalid or busy number surfaces immediately. The conversation takes minutes. Poll `phone_call_result`.
- **Give every schema property a `description`.** It is fed into the *voice prompt* as well as the extractor. Without it the agent may never think to ask about a field the schema requires, and you get a well-formed object full of nulls.
- **Read `unreliable_fields` before believing a number.** Phone lines are 8 kHz and digits get misheard. A field lands there when it was heard once and never read back for confirmation, or when the extractor's reading disagrees with what the agent confirmed out loud.
- **Retries won't double-dial.** An idempotency key is generated from the request plus a 10-minute bucket, so re-issuing the same tool call collapses onto the original job. Pass `idempotency_key` explicitly to control that yourself.
- **A refusal is not a transient error.** `422` means policy declined the destination (premium rate, non-allowlisted country, malformed number); `429` means a cap is exhausted. Neither is worth retrying.
