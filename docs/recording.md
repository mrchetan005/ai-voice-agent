# Recording

Opt-in audio recording via LiveKit Egress: an audio-only room-composite egress writes one OGG file per session to S3-compatible storage (MinIO or AWS S3), fire-and-forget so a failed start never touches the realtime path.

## How it works

When a session is created with `record: true` **and** `RECORDING_ENABLED=true`, the worker starts a `RoomCompositeEgressRequest` with `audio_only=True`, encoding to `EncodedFileType.OGG` uploaded via `S3Upload` (with `force_path_style=True`, which MinIO and most S3-compatibles require). Egress stops automatically when the room closes. The resulting object key is persisted to the session row as `recording_url`.

A failed start logs a warning and returns `None` — recording is best-effort and never blocks or fails a call. Both `RECORDING_ENABLED` and the per-session `record` flag must be set; either alone records nothing.

Object key layout:

```
{RECORDING_PREFIX}/{tenant_id}/{agent_id}/{session_id}.ogg
```

## Configuration

| env var | default | meaning |
|---------|---------|---------|
| `RECORDING_ENABLED` | `false` | master switch on the worker |
| `RECORDING_S3_ENDPOINT` | *(empty)* | S3 endpoint URL (MinIO: `http://minio:9000`; empty = AWS default) |
| `RECORDING_BUCKET` | *(empty)* | target bucket (**required** when enabled) |
| `RECORDING_ACCESS_KEY` | *(empty)* | access key (**required** when enabled) |
| `RECORDING_SECRET_KEY` | *(empty)* | secret key (**required** when enabled) |
| `RECORDING_REGION` | *(empty)* | bucket region, e.g. `us-east-1` |
| `RECORDING_PREFIX` | `recordings` | key prefix |

Secrets are env-only. When `RECORDING_ENABLED=true`, startup fails fast if `RECORDING_BUCKET`, `RECORDING_ACCESS_KEY`, or `RECORDING_SECRET_KEY` is missing.

Egress is a separate LiveKit service — the worker only *requests* egress; a running `livekit/egress` (sharing the LiveKit server's Redis bus) does the capture and upload.

## Enable it

Per session, ask for a recording when creating the session:

```bash
curl -s -X POST localhost:8080/v1/sessions \
  -H "Authorization: Bearer $PLATFORM_API_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"agent_id":"mock-demo","record":true}'
```

## Local (compose + MinIO)

The compose stack ships a `recording` profile that adds MinIO, a one-shot bucket-init, and the Egress service:

```bash
RECORDING_ENABLED=true docker compose \
  -f deploy/compose/docker-compose.yml --profile recording up --build
```

That profile wires the worker to MinIO with dev credentials (`RECORDING_S3_ENDPOINT=http://minio:9000`, bucket `recordings`). MinIO console is on `:9001`; the bucket is created and set to public-download read by `minio-init`.

List what landed after a recorded call:

```bash
docker compose -f deploy/compose/docker-compose.yml --profile recording \
  exec minio mc ls -r local/recordings
```

(Or browse the MinIO console at `http://localhost:9001`, user `voiceagent`.)

## Production (S3)

Point the worker at real S3 and drop the endpoint override (or set your regional endpoint):

```bash
RECORDING_ENABLED=true
RECORDING_S3_ENDPOINT=          # empty = AWS default
RECORDING_BUCKET=my-call-recordings
RECORDING_ACCESS_KEY=AKIA...
RECORDING_SECRET_KEY=...
RECORDING_REGION=us-east-1
```

You still need an Egress deployment alongside LiveKit; see the [deploy-k8s](deploy-k8s.md) / [deploy-eks](deploy-eks.md) guides.

## See also

- [configuration](configuration.md) — full env reference
- [observability](observability.md) — where `recording_url` and cost are recorded per session
- [deploy-local](deploy-local.md), [deploy-k8s](deploy-k8s.md), [deploy-eks](deploy-eks.md)
