# Deploy: GKE

GKE-specific deltas on top of [deploy-k8s](deploy-k8s.md) — do that first. This
only covers the image registry, external reach, node pools, and storage. Anything
not in the repo is marked **operator-supplied** (generic cloud practice).

## Image registry (Artifact Registry)

```bash
gcloud artifacts repositories create voiceagent \
  --repository-format=docker --location=<region>
gcloud auth configure-docker <region>-docker.pkg.dev

REPO=<region>-docker.pkg.dev/<project>/voiceagent
docker build -t $REPO/voiceagent:<tag> .
docker push $REPO/voiceagent:<tag>
```

Then `--set image.repository=$REPO/voiceagent --set image.tag=<tag>` on the
`voiceagent` chart. If the repo is private, add an `imagePullSecrets` entry (chart
value `imagePullSecrets`) or grant the node pool's service account
`roles/artifactregistry.reader`.

## External reach (LoadBalancer / Ingress)

LiveKit needs public reach for **WebRTC media (UDP)**, which HTTP(S) Ingress does
not carry. Options (operator-supplied):

- **Signaling (wss):** front `livekit-livekit-server` with a GKE Ingress /
  Gateway + managed cert, and set `config.livekitPublicUrl=wss://livekit.example.com`
  on the voiceagent chart.
- **Media (UDP):** set the upstream chart's `loadBalancer` to a service type your
  cluster exposes, or run LiveKit with `hostNetwork` on a dedicated pool. Open a
  **VPC firewall rule** for the RTC UDP range (`50000-50200` in
  `values-livekit.yaml`) to the nodes.
- **SIP:** the pod uses `hostNetwork`, so calls land on the node IP:5060 — open a
  firewall rule for `5060` + the RTP range (`10000-20000`).

## Node pools

- **Standard cluster** (not Autopilot) is recommended: the SIP pod needs
  `hostNetwork` and the media paths need wide UDP ranges — operator-supplied
  judgement.
- Put the **workers** on their own pool (chart requests `cpu: 1 / mem: 2Gi`, limits
  `2 / 4Gi`; scales 2→20) via `worker` `nodeSelector` / `tolerations` /
  `affinity` (chart values `nodeSelector`, `tolerations`, `affinity`).
- Keep **egress** off the worker nodes — it is CPU-heavy even audio-only (see the
  comment in `values-egress.yaml`); give it a separate pool.

## Storage

- Recording targets S3-compatible storage. For GCS use its S3-interoperability
  endpoint + HMAC keys in the egress chart's S3 config, or point at a self-hosted
  MinIO — operator-supplied. See [recording](recording.md).
- If you self-host Postgres/Redis in-cluster, use a `pd-ssd`/`pd-balanced`
  StorageClass; otherwise prefer Cloud SQL / Memorystore and pass their URLs via
  `PLATFORM_DATABASE_URL` / `PLATFORM_REDIS_URL` in `voiceagent-secrets`.
