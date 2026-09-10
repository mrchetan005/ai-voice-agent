# Deploy: Kubernetes

Cloud-neutral deployment: the upstream LiveKit charts for server + egress, the
custom `voiceagent` Helm chart for the API and worker fleet, and plain manifests
for SIP. This mirrors `deploy/k8s/README.md` — the canonical source.

Four pieces, deployed in order. `voiceagent`/API/worker/SIP live in namespace
`voiceagent`; LiveKit server + egress live in namespace `livekit`. Front media with
your own Ingress/Gateway or `type: LoadBalancer` per cloud — see
[deploy-gke](deploy-gke.md) / [deploy-eks](deploy-eks.md).

## Build and push the image

```bash
docker build -t <repo>/voiceagent:<tag> .   # from repo root
docker push <repo>/voiceagent:<tag>
```

The `Dockerfile` bakes the livekit-agents turn-detector models into an image layer
(`livekit.agents download-files`), so worker pods cold-start without downloading.

## Secrets (env-only — never in a chart)

Create out-of-band. Keys are read verbatim (`envFrom: secretRef`):

```bash
kubectl create namespace voiceagent

kubectl create secret generic voiceagent-secrets -n voiceagent \
  --from-literal=LIVEKIT_API_KEY=<key> \
  --from-literal=LIVEKIT_API_SECRET=<secret-min-32-chars> \
  --from-literal=PLATFORM_API_TOKEN=<bearer-token> \
  --from-literal=PLATFORM_DATABASE_URL=postgresql://user:pass@host:5432/voiceagent \
  --from-literal=PLATFORM_REDIS_URL=redis://host:6379/0 \
  --from-literal=DEEPGRAM_API_KEY=<...> \
  --from-literal=CARTESIA_API_KEY=<...> \
  --from-literal=GOOGLE_API_KEY=<...>
  # add OPENAI_API_KEY / ELEVEN_API_KEY / RECORDING_* as needed

# LiveKit server/egress key file (KEY: SECRET)
kubectl create secret generic livekit-keys -n livekit \
  --from-literal=<key>=<secret-min-32-chars>
```

The `sip/` manifests read `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` from
`voiceagent-secrets`, so deploy SIP in `voiceagent` (or copy the secret).

## 1 + 2. LiveKit server + egress (upstream charts)

We don't repackage what LiveKit ships — we only supply values
(`deploy/k8s/livekit/`).

```bash
helm repo add livekit https://helm.livekit.io && helm repo update
helm upgrade --install livekit livekit/livekit-server \
  -n livekit --create-namespace -f livekit/values-livekit.yaml
helm upgrade --install egress livekit/egress \
  -n livekit -f livekit/values-egress.yaml     # optional: recording
```

`values-livekit.yaml`: 2 replicas, `keysFrom: livekit-keys`, RTC `use_external_ip:
true`, UDP media `50000-50200` (grow it), TURN off, `loadBalancer.type: disable`
(front it yourself). `values-egress.yaml` points at the same server + Redis; S3
target is operator-supplied — see [recording](recording.md).

> **In-cluster URL.** The upstream chart names its Service `<release>-livekit-server`,
> so `helm install livekit` yields **`livekit-livekit-server`** in ns `livekit`.
> The voiceagent chart, SIP, and egress all default to
> `ws://livekit-livekit-server.livekit.svc.cluster.local:7880` — a bare
> `ws://livekit:7880` will **not** resolve cross-namespace. If you change the
> release name/namespace, override `config.livekitUrl` (chart) and `ws_url` in
> `sip/configmap.yaml` / `values-egress.yaml`. Confirm with
> `kubectl get svc -n livekit`.

## 3. voiceagent chart (API + worker)

```bash
cd deploy/k8s
helm upgrade --install voiceagent charts/voiceagent \
  -n voiceagent \
  --set image.repository=<repo>/voiceagent --set image.tag=<tag> \
  --set config.livekitPublicUrl=wss://livekit.example.com
```

What it renders (defaults in `charts/voiceagent/values.yaml`):

- **api** — 2 replicas, HPA CPU 70% (2→10), PDB `minAvailable: 1`, `/readyz`
  readiness + `/healthz` liveness on `:8080`, `/metrics` scraped; `ClusterIP`
  Service on port 80 → `http`.
- **worker** — HPA CPU 60% (2→20), PDB `minAvailable: 1`,
  `terminationGracePeriodSeconds: 660` (> the 600 s `WORKER_DRAIN_TIMEOUT_S` drain,
  so no mid-call SIGKILL), health server probed on `:8081`, Prometheus on `:9100`,
  HPA `scaleDown` stabilization 300 s.
- **migration** — pre-install/upgrade ConfigMap + post-install/upgrade Helm-hook
  `Job` (`postgres:16-alpine`) that applies the schema via `psql`. Idempotent
  (`CREATE ... IF NOT EXISTS`).
- **agents** — `.Values.agents` rendered into a ConfigMap mounted at
  `/config/agents.yaml`. Override to publish your own catalogue (default is a
  `mock-demo` agent needing no keys). See [prompts](prompts.md) / [tools](tools.md).

Secrets come from `existingSecret` (default `voiceagent-secrets`).
`config.livekitPublicUrl` empty = same as `config.livekitUrl`.

> **Resource names.** Helm names resources `<release>-<chart>`, so release
> `voiceagent` yields `voiceagent-voiceagent-api`, `voiceagent-voiceagent-worker`,
> etc. Confirm with `kubectl get pods,svc -n voiceagent`, or pass
> `--set fullnameOverride=voiceagent` to drop the doubled prefix.

Smoke test:

```bash
kubectl get pods,svc -n voiceagent
kubectl port-forward -n voiceagent svc/voiceagent-voiceagent-api 8080:80
curl localhost:8080/readyz
curl -XPOST localhost:8080/v1/sessions -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"agent_id":"mock-demo"}'
```

## 4. SIP (optional)

```bash
kubectl apply -n voiceagent -f sip/configmap.yaml -f sip/deployment.yaml -f sip/service.yaml
```

The pod uses `hostNetwork: true` (to bind RTP `10000-20000` on the node) with
`dnsPolicy: ClusterFirstWithHostNet` so in-cluster DNS still resolves. An
initContainer prepends `api_key`/`api_secret` from `voiceagent-secrets` onto the
non-secret ConfigMap, so creds never live in the ConfigMap. External SIP arrives on
the **node's IP:5060**; the `Service` is for in-cluster discovery only. Register a
trunk: `python -m voiceagent.channels.sip setup`. See [channels-sip](channels-sip.md).

## Autoscaling model

CPU is the *secondary* scaler. Each worker's `load_threshold` (0.7) is the real
overload guard: a saturated worker self-marks unavailable and LiveKit stops
dispatching to it, so sessions never land on a full pod. The CPU HPA (60%, below
0.7) only adds capacity behind that backpressure. Size `worker.autoscaling.maxReplicas`
from measured **sessions-per-worker** (`deploy/loadtest`), not CPU guesses — see
[load-testing](load-testing.md). Upgrade path (deferred): prometheus-adapter
exposing `lk_agents_*` load as a custom HPA metric.

## Next

- Cloud deltas: [deploy-gke](deploy-gke.md) · [deploy-eks](deploy-eks.md)
- [observability](observability.md) · [recording](recording.md) · [configuration](configuration.md)
