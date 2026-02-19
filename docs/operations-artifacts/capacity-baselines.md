# Capacity Baselines

Record load-test evidence and scaling decisions here so capacity planning remains auditable.

| Date (UTC) | Cluster | Ingress path | Image digest | Load profile | Requests/s | p95 latency | p99 latency | 5xx error rate | HPA behavior | Alert outcomes | Promotion policy result | Notes |
|---|---|---|---|---|---:|---:|---:|---:|---|---|---|---|
| 2026-02-08 | local-dev (uvicorn + dockerized k6) | `BASE_URL=http://host.docker.internal:18000` | n/a (working tree) | `k6 VUS=20 DURATION=60s` | 96.88 | 12.55ms | n/a | n/a | n/a (no cluster HPA) | n/a (Prometheus alerts not wired in local run) | `BLOCKED` | Baseline is for developer performance regression only. |
| 2026-02-08 | kind-lab (`hack/e2e-kind.sh`) | `Host: test.helloworld.shipshape.example.com` via kind ingress gateway | `ghcr.io/<your-org>/shipshape-helloworld@sha256:4375a921cb986dcb1077376e3b40ad8e992642a1c96b2da529ec601fed085483` | smoke-only verification (`hack/e2e-kind.sh`) | n/a | n/a | n/a | n/a | `helloworld-prod` HPA object present; no scale event during smoke test window | No alert rules fired during smoke window | `BLOCKED` | Cluster-backed smoke evidence only; run a timed k6 ingress load test and replace `n/a` throughput/latency before production capacity sign-off. |

## Promotion Policy Thresholds

Managed-cluster production signoff requires all thresholds below in a single baseline window:
1. Sustained throughput `>= 120 requests/s` for at least `5m`.
2. Latency `p95 <= 250ms` and `p99 <= 500ms`.
3. Error rate `5xx <= 1%`.
4. HPA stability: no more than `2` scale-direction changes in any `10m` steady-state window.
5. No critical production alerts firing during the load window.

## Production Approval Gate

At least one non-local managed-cluster baseline is required for production
approval. Local-dev and kind rows above do not satisfy this gate.

Current gate status (2026-02-08): `BLOCKED` (managed-cluster baseline missing).

Required managed-cluster entry fields:

| Date (UTC) | Cluster | Ingress path | Image digest | Load profile | Requests/s | p95 latency | p99 latency | 5xx error rate | HPA behavior | Alert outcomes | Promotion policy result | Notes |
|---|---|---|---|---|---:|---:|---:|---:|---|---|---|---|
| YYYY-MM-DD | managed-cluster-name | `Host: prod.helloworld.shipshape.example.com` via `<INGRESS_IP>` | `ghcr.io/<owner>/shipshape-helloworld@sha256:<digest>` | `k6 VUS=<n> DURATION=<m>` | <value> | <value> | <value> | <value> | include scale-up/down events and time-to-scale | include `HelloworldHighLatencyP95`, `HelloworldHighErrorRate`, `HelloworldProdHPANearMaxReplicas`, `HelloworldProdPDBDisruptionsExhausted`, `HelloworldProdPDBInsufficientHealthyPods` | `PASS` or `BLOCKED` against thresholds above | Include test window start/end and command reference (`hack/load-test-k6.js`). |

Update this file after each planned capacity test and each significant
production scaling incident.
