# SIP / PSTN channel

Take and place real phone calls: point a SIP provider at the LiveKit SIP service, provision an inbound trunk + dispatch rule once with the setup CLI, and dial out at runtime via `POST /v1/calls/sip`.

Inbound and outbound both reuse the homogeneous worker fleet — there is no SIP-specific
worker. LiveKit's SIP service handles the media leg; the framework only provisions the
trunk/rule (inbound) or dispatches an agent then dials (outbound).

## The LiveKit SIP service

SIP media/signaling ride LiveKit's `livekit/sip` service, not the API or worker.

- **Local** ([deploy-local](deploy-local.md)): the compose `sip` profile.
  ```bash
  docker compose -f deploy/compose/docker-compose.yml --profile sip up
  ```
  Publishes `5060/udp`, `5060/tcp`, and RTP `10000-10010/udp` so a softphone on the same
  machine can reach it.
- **Kubernetes** ([deploy-k8s](deploy-k8s.md)): `deploy/k8s/sip/` runs `livekit/sip` with
  `hostNetwork: true` (RTP range `10000-20000`) so it binds the wide media range on the
  node; external SIP traffic reaches it on the node IP:5060.

Point your SIP provider (Twilio, Telnyx, a self-hosted softswitch, …) at that endpoint.

## Inbound: one-time provisioning

Inbound needs two objects created once against LiveKit: an **inbound trunk** that accepts
calls to your DID(s), and a **dispatch rule** that drops each caller into their own room
and dispatches the agent fleet with `channel="sip"` metadata.

```bash
python -m voiceagent.channels.sip setup \
    --numbers +15551234567,+15557654321 \
    --agent sales-pipeline
```

Flags:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--numbers` | required | comma-separated DIDs to accept |
| `--agent` | required | `agent_id` that serves inbound calls (validated against loaded agents) |
| `--room-prefix` | `sip-` | prefix for the per-caller rooms |
| `--trunk-name` | `voiceagent-inbound` | name of the created inbound trunk |
| `--rule-name` | `voiceagent-inbound-rule` | name of the created dispatch rule |
| `--allowed-addresses` | `""` (any) | comma-separated CIDRs/IPs allowed to send INVITEs |
| `--auth-username` | `""` | optional digest-auth username on the trunk |
| `--auth-password` | `""` | optional digest-auth password |

It prints the created ids:

```
inbound trunk:  ST_xxxxxxxx
dispatch rule:  SDR_xxxxxxxx
agent:          sales-pipeline  (channel=sip)
```

The rule is an *individual* dispatch rule (one room per caller). Only `agent_id` and
`channel="sip"` are known at provisioning time; the worker fills `session_id` and the room
from the job at call time. Requires `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`.

## Outbound: POST /v1/calls/sip

Dispatches the agent into a fresh room, then dials the callee through a trunk and drops
them into that room. Bearer auth.

Request (`SipCallIn`):

```jsonc
{
  "agent_id": "sales-pipeline",  // required; 404 if not loaded
  "to_number": "+15551234567",   // required; E.164
  "trunk_id": null,              // optional; falls back to PLATFORM_SIP_TRUNK_ID
  "user_id": null,              // defaults to to_number
  "prompt_vars": {},
  "record": false
}
```

Response `201`:

```json
{ "session_id": "9a8b...", "room": "sip-out-9a8b7c6d", "sip_participant": "PA_xxxx" }
```

- Room is `sip-out-{session_id[:8]}`; the callee joins as identity `phone-{to_number}`.
- `trunk_id` falls back to `PLATFORM_SIP_TRUNK_ID`; with neither set the call is
  `400 no SIP trunk configured`.

```bash
curl -sX POST http://localhost:8080/v1/calls/sip \
  -H "Authorization: Bearer $PLATFORM_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"sales-pipeline","to_number":"+15551234567"}'
```

Track and end the call with the shared session routes — `GET /v1/sessions/{id}` and
`DELETE /v1/sessions/{id}` (see [channels-browser](channels-browser.md)).

> Outbound also needs an *outbound* trunk configured with your provider; only the inbound
> trunk is created by the setup CLI. Set its id as `PLATFORM_SIP_TRUNK_ID` or pass
> `trunk_id` per call.

## See also

- [configuration](configuration.md) (`PLATFORM_SIP_TRUNK_ID`) · [deploy-local](deploy-local.md) · [deploy-k8s](deploy-k8s.md)
- [channels-browser](channels-browser.md) · [channels-whatsapp](channels-whatsapp.md) · [adding-a-channel](adding-a-channel.md)
