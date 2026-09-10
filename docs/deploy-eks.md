# Deploy: EKS

EKS-specific deltas on top of [deploy-k8s](deploy-k8s.md) — do that first. This
only covers the image registry, external reach, node groups, and storage. Anything
not in the repo is marked **operator-supplied** (generic cloud practice).

## Image registry (ECR)

```bash
aws ecr create-repository --repository-name voiceagent
aws ecr get-login-password --region <region> \
  | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com

REPO=<acct>.dkr.ecr.<region>.amazonaws.com
docker build -t $REPO/voiceagent:<tag> .
docker push $REPO/voiceagent:<tag>
```

Then `--set image.repository=$REPO/voiceagent --set image.tag=<tag>` on the
`voiceagent` chart. Node pull access comes from the node group's instance role
(`AmazonEC2ContainerRegistryReadOnly`) — no `imagePullSecrets` needed for same-account
ECR.

## External reach (LoadBalancer / Ingress)

LiveKit needs public reach for **WebRTC media (UDP)**, which an ALB does not carry.
Options (operator-supplied, via the AWS Load Balancer Controller):

- **Signaling (wss):** front `livekit-livekit-server` with an ALB Ingress + ACM
  cert, and set `config.livekitPublicUrl=wss://livekit.example.com` on the
  voiceagent chart.
- **Media (UDP):** expose LiveKit via an **NLB** (UDP-capable) — set the upstream
  chart's `loadBalancer` to a `Service type: LoadBalancer` with NLB annotations, or
  run LiveKit with `hostNetwork` on a dedicated group. Open the node **security
  group** for the RTC UDP range (`50000-50200` in `values-livekit.yaml`).
- **SIP:** the pod uses `hostNetwork`, so calls land on the node IP:5060 — open the
  security group for `5060` + the RTP range (`10000-20000`).

## Node groups

- Put the **workers** on their own managed node group (compute-optimized; chart
  requests `cpu: 1 / mem: 2Gi`, limits `2 / 4Gi`; scales 2→20) via `worker`
  `nodeSelector` / `tolerations` / `affinity` (chart values `nodeSelector`,
  `tolerations`, `affinity`).
- Keep **egress** off the worker nodes — it is CPU-heavy even audio-only (see the
  comment in `values-egress.yaml`); give it a separate group.
- Cluster Autoscaler / Karpenter provisioning of those groups is operator-supplied.

## Storage

- Recording targets S3-compatible storage — on AWS use **native S3**: leave the
  egress chart's S3 `endpoint` empty (`values-egress.yaml` notes this) and grant the
  egress pod access via **IRSA** rather than static keys (operator-supplied). See
  [recording](recording.md).
- If you self-host Postgres/Redis in-cluster, install the EBS CSI driver and use a
  `gp3` StorageClass; otherwise prefer RDS / ElastiCache and pass their URLs via
  `PLATFORM_DATABASE_URL` / `PLATFORM_REDIS_URL` in `voiceagent-secrets`.
