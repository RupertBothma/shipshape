# Architecture Decision Records

This directory stores immutable architecture decision records (ADRs).

Use the template in `0000-template.md` and name new files as:
- `NNNN-short-title.md` (for example: `0001-controller-leader-election.md`)

Status values:
- Proposed
- Accepted
- Superseded
- Rejected

Accepted ADRs:
- `0001-single-namespace-with-name-suffix.md`
- `0002-configmap-data-hash-restarts.md`
- `0003-debounce-configmap-restarts.md` (superseded-in-part by `0005`)
- `0004-lease-based-leader-election.md`
- `0005-force-pending-restarts-on-shutdown.md`
