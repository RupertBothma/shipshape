# ADR 0005: Force Pending Restarts On Shutdown and Leadership Handoff

- Status: Accepted
- Date: 2026-02-08
- Deciders: Shipshape maintainers
- Supersedes: 0003-debounce-configmap-restarts.md (in part)

## Context
ConfigMap changes observed during a debounce window could previously remain queued
in `_pending_restarts` and be dropped when the leader stopped (SIGTERM or
leadership loss). A new leader starts with a fresh hash baseline and cannot
reconstruct the old leader's in-memory pending intent, causing possible silent
configuration drift.

## Decision
On shutdown/handoff, force-process all pending restarts immediately, even when
still inside the debounce window.

## Consequences
- Positive:
  - Eliminates silent loss of observed ConfigMap restart intents during handoff.
  - Preserves durability of controller decisions across leadership transitions.
- Negative:
  - Shutdown can trigger a restart slightly earlier than configured debounce.
  - Rare forced-flush failures are surfaced via `configmap_reload_dropped_restarts_total`.
- Follow-up work:
  - Monitor dropped restart metric and investigate any non-zero production trend.

## Alternatives Considered
1. Drop pending restarts and rely on next leader re-list
2. Persist pending restart queue to an external durable store
