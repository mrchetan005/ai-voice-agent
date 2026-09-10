# Kubernetes deployment

Cloud-neutral manifests for a self-hosted deployment. Four pieces, deployed in
this order:

1. **LiveKit server** — official chart, `livekit/values-livekit.yaml`
2. **LiveKit Egress** (recording, optional) — official chart, `livekit/values-egress.yaml`
3. **voiceagent** (API + worker fleet) — the `charts/voiceagent` chart
4. **SIP** (PSTN, optional) — custom manifests in `sip/`

We do not repackage what LiveKit ships: server and egress use the upstream
charts; we only supply values. Everything below is provider-agnostic — front it
with your own Ingress/Gateway or set `type: LoadBalancer` per cloud.

## Secrets (env-only — never in a chart)

All credentials come from Kubernetes Secrets you create out-of-band:

```bash
kubectl create namespace voiceagent

# voiceagent API + worker credentials
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
`voiceagent-secrets`, so deploy SIP in the `voiceagent` namespace (or copy the
secret).

## 1. LiveKit server + 2. Egress

```bash
helm repo add livekit https://helm.livekit.io && helm repo update
helm upgrade --install livekit livekit/livekit-server \
  -n livekit --create-namespace -f livekit/values-livekit.yaml
helm upgrade --install egress livekit/egress \
  -n livekit -f livekit/values-egress.yaml     # optional: recording
```

> **In-cluster URL:** the upstream chart names its Service
> `<release>-livekit-server`, so `helm install livekit` yields
> `livekit-livekit-server` in namespace `livekit`. The voiceagent chart, SIP,
> and egress default to `ws://livekit-livekit-server.livekit.svc.cluster.local:7880`.
> If you use a different release name or namespace, override `config.livekitUrl`
> (chart) and the `ws_url` in `sip/configmap.yaml` / `values-egress.yaml`. Confirm
> the actual name with `kubectl get svc -n livekit`.

## 3. voiceagent chart

The image must be built and pushed first (`docker build -t <repo>/voiceagent:<tag> .`).

```bash
helm upgrade --install voiceagent charts/voiceagent \
  -n voiceagent \
  --set image.repository=<repo>/voiceagent --set image.tag=<tag> \
  --set config.livekitPublicUrl=wss://livekit.example.com
```

- **api**: 2 replicas, HPA CPU 70% (2→10), PDB minAvailable 1, `/healthz`
  liveness + `/readyz` readiness, `/metrics` scraped.
- **worker**: HPA CPU 60% (2→20), PDB, `terminationGracePeriodSeconds: 660`
  (> the 600 s drain), livekit-agents health server probed on :8081, Prometheus
  on :9100. Models are baked into the image layer, so pods cold-start without
  downloading.
- **migration**: pre/post-upgrade Helm hook applies the Postgres schema
  (idempotent).

Override `agents` in your values to publish your own agent catalogue; it renders
into a ConfigMap mounted at `/config/agents.yaml`.

## 4. SIP (optional)

```bash
kubectl apply -n voiceagent -f sip/configmap.yaml -f sip/deployment.yaml -f sip/service.yaml
```

The SIP pod uses `hostNetwork` (to bind a wide RTP range on the node) with
`dnsPolicy: ClusterFirstWithHostNet` so in-cluster DNS still resolves. An
initContainer renders the config, prepending the API key/secret from the Secret
so credentials never live in the ConfigMap. External SIP traffic arrives on the
node's IP:5060. Register a trunk with `python -m voiceagent.channels.sip setup`.

## Autoscaling note (why CPU is the *secondary* scaler)

Each worker's `load_threshold` (0.7) is the real overload guard: a saturated
worker self-marks unavailable and LiveKit stops dispatching to it, so sessions
never land on a full pod. The CPU HPA (60%, below 0.7) only adds capacity behind
that backpressure. Size `maxReplicas` from `deploy/loadtest` measurements
(sessions-per-worker), not from CPU guesses. Upgrade path (deferred):
prometheus-adapter exposing the worker's `lk_agents_*` load as a custom HPA metric.
