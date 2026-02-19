## Summary

<!-- What changed and why? -->

## Validation

- [ ] `make check` passed locally
- [ ] `make check-ci-core` passed locally (recommended for CI parity)
- [ ] `python3 hack/validate_manifests.py --overlay test --overlay prod --controller-egress-patch examples/controller-apiserver-cidr-patch.yaml --controller-egress-patch examples/controller-egress/eks.patch.yaml --controller-egress-patch examples/controller-egress/gke.patch.yaml --controller-egress-patch examples/controller-egress/aks.patch.yaml` passed
- [ ] If manifests changed, verified `kustomize build k8s/overlays/test`, `kustomize build k8s/overlays/prod`, and `kustomize build k8s/monitoring`
- [ ] If controller changed, verified leader/readiness behavior

## Security Checklist

- [ ] No new privileged container settings introduced
- [ ] NetworkPolicy / AuthorizationPolicy impact reviewed (if applicable)
- [ ] No secrets or credentials added to repo

## Deployment Notes

<!-- Any ordering, migration, rollback, or operator notes -->
