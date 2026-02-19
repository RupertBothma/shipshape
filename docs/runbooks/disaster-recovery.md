# Cluster Disaster Recovery Runbook

## Scope
This runbook covers full-cluster recovery scenarios that are not solved by
namespace re-apply alone:
- etcd corruption or data loss
- control-plane node loss
- API server unavailability

Use `docs/operations.md` for namespace/config-only incidents.

## Objectives
- **RTO:** 15 minutes for service restoration after cluster control-plane recovery.
- **RPO:** <= 15 minutes for Kubernetes object state (bounded by snapshot cadence).

## Ownership
- Platform/SRE team owns etcd/control-plane backup and restore.
- Application team owns post-restore verification of `shipshape` workloads.

## Backup Policy
1. Snapshot etcd every 15 minutes (or tighter if required by RPO).
2. Retain at least:
   - 24 hours of 15-minute snapshots
   - 7 days of hourly snapshots
   - 30 days of daily snapshots
3. Store snapshots encrypted in off-cluster storage.
4. Record checksum and snapshot metadata per backup artifact.

## etcd Snapshot (Self-Managed Control Plane)
Run on a control-plane node with etcd client certs available:

```bash
export ETCDCTL_API=3
export ETCDCTL_CACERT=/etc/kubernetes/pki/etcd/ca.crt
export ETCDCTL_CERT=/etc/kubernetes/pki/etcd/server.crt
export ETCDCTL_KEY=/etc/kubernetes/pki/etcd/server.key

etcdctl --endpoints=https://127.0.0.1:2379 \
  snapshot save /var/backups/etcd/etcd-$(date -u +%Y%m%dT%H%M%SZ).db

etcdctl snapshot status /var/backups/etcd/etcd-<timestamp>.db -w table
sha256sum /var/backups/etcd/etcd-<timestamp>.db
```

## etcd Restore (Self-Managed Control Plane)
1. Stop API server/controller-manager/scheduler and etcd on the affected node(s).
2. Restore snapshot to a clean data directory:

```bash
export ETCDCTL_API=3
etcdctl snapshot restore /var/backups/etcd/etcd-<timestamp>.db \
  --name <member-name> \
  --initial-cluster <member-name>=https://<member-ip>:2380 \
  --initial-advertise-peer-urls https://<member-ip>:2380 \
  --data-dir /var/lib/etcd-restored
```

3. Update etcd manifest (`/etc/kubernetes/manifests/etcd.yaml`) to point to restored data dir.
4. Start etcd and control-plane components.
5. Validate API health:

```bash
kubectl get --raw='/readyz?verbose'
kubectl get nodes
```

## Managed Control Planes (EKS/GKE/AKS)
etcd is provider-managed. Use provider recovery primitives:
- Restore cluster from provider backup/snapshot options.
- Reconcile control-plane endpoint and IAM/OIDC integrations.
- Re-apply GitOps state after control-plane recovery.

### EKS Control-Plane Recovery
```bash
aws eks describe-cluster --name <cluster-name> --region <region> --query 'cluster.status'
aws eks update-kubeconfig --name <cluster-name> --region <region>
kubectl get --raw='/readyz?verbose'
```

If using AWS Backup for cluster resources:
```bash
aws backup list-recovery-points-by-backup-vault --backup-vault-name <vault-name>
aws backup start-restore-job \
  --recovery-point-arn <recovery-point-arn> \
  --metadata file://eks-restore-metadata.json \
  --iam-role-arn <restore-role-arn>
```

### GKE Control-Plane Recovery
```bash
gcloud container clusters describe <cluster-name> --region <region> --format='value(status)'
gcloud container clusters get-credentials <cluster-name> --region <region> --project <project-id>
kubectl get --raw='/readyz?verbose'
```

If using Backup for GKE:
```bash
gcloud container backup-restore backups list --location <region> --project <project-id>
gcloud container backup-restore restores create <restore-name> \
  --location <region> \
  --backup-plan <backup-plan-name> \
  --backup <backup-name> \
  --cluster <cluster-resource>
```

### AKS Control-Plane Recovery
```bash
az aks show --resource-group <resource-group> --name <cluster-name> --query 'powerState.code'
az aks get-credentials --resource-group <resource-group> --name <cluster-name> --overwrite-existing
kubectl get --raw='/readyz?verbose'
```

If using Azure Backup/extension-based backups, restore via the latest protected recovery point and then run the post-restore workload recovery steps below.

## Post-Restore Workload Recovery
After control plane is healthy:

```bash
kubectl apply -k k8s/namespace
kubectl apply -k k8s/istio-ingress
kubectl apply -k k8s/overlays/test
kubectl apply -k k8s/overlays/prod
kubectl apply -k k8s/monitoring
kubectl apply -k k8s/controller
```

Canonical apply order cross-check (keep aligned with `docs/operations.md`):
```bash
cat <<'EOF'
k8s/namespace
k8s/istio-ingress
k8s/overlays/test
k8s/overlays/prod
k8s/monitoring
k8s/controller
EOF
```

Then verify:
```bash
kubectl -n shipshape rollout status deployment/helloworld-test --timeout=180s
kubectl -n shipshape rollout status deployment/helloworld-prod --timeout=180s
kubectl -n shipshape rollout status deployment/helloworld-controller --timeout=180s
kubectl -n shipshape get servicemonitor,prometheusrule
```

## Control-Plane Failure Recovery Checklist
1. Declare incident and freeze non-essential deploys.
2. Recover control plane (provider workflow or etcd restore).
3. Reconcile RBAC/service accounts/secrets/certs.
4. Reapply platform and app manifests.
5. Run ingress and alerting smoke checks from `docs/operations.md`.
6. Close incident only after SLO metrics stabilize.

## RTO/RPO Drill Procedure
Run quarterly in staging:
1. Take a fresh etcd snapshot.
2. Simulate control-plane outage (or use isolated restore environment).
3. Restore snapshot and recover API server.
4. Reapply `shipshape` manifests.
5. Measure:
   - time to API readiness
   - time to app/controller rollouts healthy
   - data staleness against snapshot timestamp
6. Record actual RTO/RPO and remediation items in:
   - `docs/operations-artifacts/dr-drill-YYYYMMDD.md`
   - Keep the latest report linked from `docs/operations.md`.
