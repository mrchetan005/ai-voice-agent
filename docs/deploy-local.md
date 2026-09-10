# Deploy: local (docker-compose)

Bring up the whole platform — LiveKit + Redis + Postgres + API + worker — with one
`docker compose` command, then talk to the agent from your browser.

## Stack

`deploy/compose/docker-compose.yml` (compose project `voiceagent`). Inline configs
are shipped over the Docker API, so there are **no host bind mounts** — Docker
Desktop drive-sharing on Windows can't break the stack.

| service | image | ports (host) | notes |
|---------|-------|--------------|-------|
| `livekit` | `livekit/livekit-server:v1.13.6` | `7880` ws/http, `7881` rtc-tcp, `50100-50160/udp` | dev key `devkey` / `devsecret_devsecret_devsecret_123456`, `auto_create` rooms |
| `redis` | `redis:7-alpine` | — | shared bus (also egress/sip RPC) |
| `postgres` | `postgres:16-alpine` | — | user/pass/db all `voiceagent`; `init.sql` applied on first boot |
| `api` | built from `Dockerfile` (`voiceagent-api`) | `8080` | `/healthz`, browser demo at `/demo` |
| `worker` | built from `Dockerfile` (`voiceagent-worker start`) | `9100` metrics | `stop_grace_period: 610s` so live calls drain |

Redis and Postgres are **not** published to the host (only reachable inside the
compose network).

## Bring it up

```bash
cp .env.example .env          # repo root; the mock agent needs no provider keys
docker compose -f deploy/compose/docker-compose.yml up --build
```

`api`/`worker` wait for `livekit` + `postgres` healthchecks before starting. Both
load agents from `/app/examples/agents.yaml` (`mock-demo` requires no keys).

## Browser demo

The API serves `examples/browser-demo/` at `/demo` (static mount):

```
open http://localhost:8080/demo
```

Defaults in the form — API `http://localhost:8080`, token `devtoken`, agent
`mock-demo` — match the compose stack. Click **Start voice session**: the page
`POST`s `/v1/sessions`, joins the LiveKit room, publishes your mic, and plays the
agent audio. See [channels-browser](channels-browser.md).

> `LIVEKIT_PUBLIC_URL` is advertised as `ws://127.0.0.1:7880` (IPv4 literal, not
> `localhost`): Docker publishes on IPv4 only, and native RTC clients resolve
> `localhost` to IPv6 `::1` first and hang. Browsers dual-stack-fallback, so the
> demo works either way.

Or drive it headless with the load-test driver — see [load-testing](load-testing.md).

## Profiles

Recording (adds MinIO + LiveKit Egress) — needs **both** the env flag and the profile:

```bash
RECORDING_ENABLED=true \
  docker compose -f deploy/compose/docker-compose.yml --profile recording up --build
```

Adds `minio` (`9000` API, `9001` console; root `voiceagent` / `voiceagent-secret`),
a one-shot `minio-init` that creates the `recordings` bucket, and
`egress` (`livekit/egress:v1.14.1`). See [recording](recording.md).

SIP (adds the LiveKit SIP service, `livekit/sip:v1.14.0`):

```bash
docker compose -f deploy/compose/docker-compose.yml --profile sip up
```

Publishes `5060/udp`, `5060/tcp`, and RTP `10000-10010/udp` so a softphone on this
machine can reach it (dev uses bridge networking + published ports; prod/Linux uses
host networking for a wider RTP range). Provision a trunk with
`python -m voiceagent.channels.sip setup` — see [channels-sip](channels-sip.md).

## Windows-native alternative (no app container)

The compose stack already runs on Windows via Docker Desktop — it is tuned for it
(inline configs, IPv4 literal). But for a tight edit loop you can run the Python
`api`/`worker` on the host with `uv` and rebuild nothing:

```bash
uv sync --all-extras
uv run voiceagent-api            # serves :8080
uv run voiceagent-worker start   # registers with LiveKit, serves :9100
```

`.env.example` already targets `localhost` for LiveKit (`ws://localhost:7880`),
Postgres (`:5432`), and Redis (`:6379`), so point those at a LiveKit server, Redis,
and Postgres you provide (native installers, WSL2, or the compose infra with host
ports added — operator-supplied, since compose does not publish `redis`/`postgres`).
Apply the schema once with `psql -f deploy/compose/init.sql` against your Postgres.

## Next

- [quickstart](quickstart.md) · [configuration](configuration.md) · [providers](providers.md)
- Scale it out: [deploy-k8s](deploy-k8s.md)
- [observability](observability.md) · [recording](recording.md) · [load-testing](load-testing.md)
