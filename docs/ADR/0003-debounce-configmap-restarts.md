# ADR 0003: Debounce and Coalesce ConfigMap-Triggered Restarts

- Status: Accepted
- Date: 2026-02-08
- Deciders: Shipshape maintainers
- Supersedes: none
- Superseded-in-part-by: 0005-force-pending-restarts-on-shutdown.md

## Context
Rapid sequential ConfigMap updates can create rollout storms, increasing pod churn and risk during deployments.

## Decision
Apply a per-ConfigMap-key debounce window (`DEBOUNCE_SECONDS`) and coalesce pending restart requests until the debounce window elapses.

## Consequences
- Positive:
  - Reduces restart amplification during noisy config update bursts.
  - Preserves final state by deferring to the latest change.
- Negative:
  - Introduces bounded propagation delay for restart-triggered config updates.
  - Original design allowed pending restarts to be dropped on shutdown;
    superseded-in-part by ADR-0005.
- Follow-up work:
  - Tune debounce defaults using production rollout and alert telemetry.

## Alternatives Considered
1. Restart immediately on each meaningful change
2. Fixed periodic reconciliation loop without event-driven triggers
