# ADR 0004: Lease-Based Leader Election For Controller Replicas

- Status: Accepted
- Date: 2026-02-08
- Deciders: Shipshape maintainers
- Supersedes: none

## Context
Running multiple controller replicas without coordination can cause duplicate deployment patching and restart storms.

## Decision
Use Kubernetes `coordination.k8s.io/v1` Lease leader election. Only the active leader runs the watch loop; standby replicas wait and take over after lease expiry.

## Consequences
- Positive:
  - Prevents concurrent restart actions from multiple replicas.
  - Uses native Kubernetes primitives and integrates cleanly with RBAC.
  - Supports graceful leadership handoff on shutdown.
- Negative:
  - Requires lease timing tuning for high-latency clusters.
  - Adds dependency on Lease API availability and API-server health.
- Follow-up work:
  - Monitor leader flapping and adjust lease/renew/retry settings when needed.

## Alternatives Considered
1. Single controller replica (no HA)
2. External lock service (Redis/etcd outside Kubernetes)
