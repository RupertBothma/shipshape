# ADR 0002: ConfigMap Data Hash For Restart Decisions

- Status: Accepted
- Date: 2026-02-08
- Deciders: Shipshape maintainers
- Supersedes: none

## Context
`resourceVersion` changes on metadata-only updates and replay events, which can trigger unnecessary restarts if used directly as change signal.

## Decision
Compute a SHA-256 hash of normalized `ConfigMap.data` and restart workloads only when this hash changes.

## Consequences
- Positive:
  - Avoids false-positive rollouts from label/annotation updates.
  - Preserves idempotency during watch reconnects and relists.
- Negative:
  - Adds small CPU overhead per watch event for serialization + hashing.
- Follow-up work:
  - Keep normalization logic aligned with any future binary-data handling.

## Alternatives Considered
1. Use `metadata.resourceVersion` only
2. Restart on every `MODIFIED` event
