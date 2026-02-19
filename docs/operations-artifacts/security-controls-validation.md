# Security Controls Validation Evidence

This artifact is the canonical audit trail for environment-specific validation
of:
- Kubernetes data encryption-at-rest controls.
- Kubernetes API audit-log sink configuration and delivery.

Update this file after each quarterly control review and after any control-plane
or logging-platform migration.

## Validation Matrix

| Date (UTC) | Environment | Cluster | Encryption-at-rest check | Audit-log sink check | Result | Evidence link(s) | Operator |
|---|---|---|---|---|---|---|---|
| 2026-02-09 | test | `<cluster-name>` | `PENDING_PLATFORM_VALIDATION` | `PENDING_PLATFORM_VALIDATION` | `BLOCKED` | `<ticket-or-doc-link>` | `<name>` |
| 2026-02-09 | prod | `<cluster-name>` | `PENDING_PLATFORM_VALIDATION` | `PENDING_PLATFORM_VALIDATION` | `BLOCKED` | `<ticket-or-doc-link>` | `<name>` |

## Checklist

### Encryption-at-Rest

1. Confirm etcd/control-plane data encryption is enabled for the cluster.
2. Confirm key-management source (provider-managed or customer-managed KMS key).
3. Record key identifier and rotation policy reference.
4. Capture command output or control-plane settings evidence link.

Provider check starters:

- EKS:
  ```bash
  aws eks describe-cluster --name <cluster-name> --region <region> \
    --query 'cluster.encryptionConfig'
  ```
- GKE:
  ```bash
  gcloud container clusters describe <cluster-name> --region <region> \
    --format='yaml(databaseEncryption,state)'
  ```
- AKS:
  ```bash
  az aks show --resource-group <rg> --name <cluster-name> \
    --query '{securityProfile:securityProfile, diskEncryptionSetID:agentPoolProfiles[0].osDiskType}'
  ```

### Audit-Log Sink

1. Confirm API audit logging is enabled with policy coverage for:
   - `configmaps`
   - `secrets`
   - `deployments`
   in namespace `shipshape`.
2. Confirm sink target (CloudWatch/Loki/Elastic/SIEM) and retention policy.
3. Validate at least one recent ConfigMap mutation appears in the sink.
4. Record query URI or dashboard/saved-search link as evidence.

Minimum in-cluster verification:

```bash
kubectl -n kube-system get pods -l component=kube-apiserver -o yaml | \
  rg -- '--audit-policy-file|--audit-log-path'
kubectl -n shipshape get events --field-selector involvedObject.kind=ConfigMap
```

## Sign-off

| Date (UTC) | Environment | Reviewer | Status | Notes |
|---|---|---|---|---|
| `<YYYY-MM-DD>` | `<test/prod>` | `<name>` | `<approved/blocked>` | `<notes>` |
