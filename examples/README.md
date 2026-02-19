# Examples

- `api-contract.http` contains endpoint calls for local or cluster port-forward testing.
- `controller-apiserver-cidr-patch.yaml` is a merge patch example that replaces controller egress with DNS + explicit API CIDR allow-lists.
- `controller-egress/eks.patch.yaml`, `controller-egress/gke.patch.yaml`, and `controller-egress/aks.patch.yaml` are authoritative provider-specific policy replacements for strict API-server CIDR pinning.
- `networkpolicy-probe-allow/app-kubelet-probes.yaml` and `networkpolicy-probe-allow/controller-kubelet-probes.yaml` are optional additive policies for CNIs that require explicit node-origin health probe allow rules.
