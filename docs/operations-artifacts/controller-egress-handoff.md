# Controller API Egress Handoff Evidence

Track the environment-specific NetworkPolicy patch that enables controller access
to the Kubernetes API endpoint. Keep this artifact current so release handoff
does not rely on rediscovering control-plane CIDRs during incidents.

## Current Status
`PENDING_PLATFORM_VALIDATION`

Set to `PASS`/`APPROVED` only after the target environment row below has:
- placeholder-free API CIDR evidence,
- `Smoke check result` of `PASS`/`APPROVED`,
- reviewer sign-off.

## Validation Matrix

| Date (UTC) | Environment | Cluster | Applied patch file | API endpoint source | Verified API CIDRs | Render/validation command | Smoke check result | Reviewer |
|---|---|---|---|---|---|---|---|---|
| 2026-02-09 | prod | `<managed-cluster-name>` | `examples/controller-egress/<provider>.patch.yaml` | `kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'` | `<cidr-1>, <cidr-2>` | `python3 hack/validate_manifests.py --overlay test --overlay prod --controller-egress-patch examples/controller-egress/<provider>.patch.yaml` | `<PASS/BLOCKED>` | `<name>` |

## Required Evidence

1. Patch file used for the target cluster (`examples/controller-egress/*.patch.yaml`).
2. Command output showing resolved control-plane endpoint/IPs.
3. Render-time validation output proving placeholder CIDR is removed.
4. Controller-to-API smoke check output (`https://kubernetes.default.svc/version`).
5. Date + reviewer sign-off that CIDRs are still current.
