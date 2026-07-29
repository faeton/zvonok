# zvonok — deploy

Runs on **de1 only** (BRIEF §3). Deployed at `~/zvonok`; secrets live in `deploy/.env` (mode 600, never committed).

## Layout

| File | Purpose |
|---|---|
| `docker-compose.yml` | redis + livekit-server + livekit-sip + voice-agent + postgres + call-api, all host-networked |
| `livekit.yaml` | livekit-server config, **placeholder keys** — safe to commit |
| `deploy.sh` | renders `livekit.rendered.yaml` from `.env`, then `compose up -d --build` |
| `firewall.sh` | opens 5060 + RTP to Zadarma's `185.45.152.0/22` only |
| `lkctl.py` / `lkctl.sh` | trunk creation + call dispatch via the livekit-api SDK |
| `call.sh` | thin wrapper: place one test call, bypassing call-api |

Everything is on **host networking**, so each service binds narrowly itself rather than relying on docker port mapping: redis and postgres on `127.0.0.1`, livekit-server on `127.0.0.1:7880`, call-api on the **tailnet address only**.

## First-time setup

```bash
cp .env.example .env && chmod 600 .env   # fill in — every variable is documented inline
./deploy.sh
sudo ./firewall.sh
./lkctl.sh create-trunk                  # put the printed ST_… into .env
docker compose up -d agent               # restart agent so it picks up the trunk id
```

Secrets to generate for phase 2:

```bash
openssl rand -hex 24                                        # POSTGRES_PASSWORD
openssl rand -hex 32                                        # ZVONOK_INTERNAL_TOKEN, ZVONOK_WEBHOOK_SECRET
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'  # one per client identity
```

## Placing a call

Through the product spine (what agents use — enforces policy, caps, idempotency, and runs the extractor):

```bash
curl -s -X POST http://$ZVONOK_BIND_HOST:$ZVONOK_API_PORT/v1/calls \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"number":"+34600123456","language":"es",
       "goal":"Ask whether guests can park onsite and the nightly price.",
       "wait_seconds":10}'
curl -s -H "Authorization: Bearer $TOKEN" .../v1/calls/<id>/result
```

Or bypassing it entirely, for debugging the media path (no caps, no record, no extraction):

```bash
./call.sh +34600123456 "Ask whether they have parking and the nightly price." en
docker compose logs -f agent
ls -t transcripts/ | head -1
```

Caller ID is chosen per destination country and is a **cost** lever: EU/UK destinations cost ×20–34 less with a UK caller ID than a UA one (BRIEF §9). `call.sh` defaults to the UK DID; call-api picks it from the country.

## Gotchas that already bit us

- **Agent health port.** LiveKit Agents binds `0.0.0.0:8081` by default; matomo owns 8081 on de1 and host networking makes that fatal. Pinned to `127.0.0.1:18130`.
- **`/etc/nftables.conf` starts with `flush ruleset`.** Never `systemctl restart nftables` mid-session — it wipes Docker's chains until Docker restarts too. `firewall.sh` deliberately applies rules live *and* persists them separately, without reloading.
- **No `noise_cancellation`.** `BVCTelephony` is LiveKit Cloud-only and silently does nothing self-hosted.
- **Trunk uses no credentials.** Zadarma authorizes de1's static IP. LiveKit's docs discourage IP auth, but that warning is about LiveKit Cloud's non-static egress.

## Health check

```bash
docker compose ps
docker compose logs --tail 5 sip      # expect: sip signaling listening on ... 5060
docker compose logs --tail 5 agent    # expect: registered worker
./lkctl.sh trunks
sudo nft list chain inet filter input | grep zvonok
```
