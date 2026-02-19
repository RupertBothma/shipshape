# Operations Runbook

## Namespace Ownership
- Namespace manifests are owned only by `k8s/namespace`.
- `k8s/overlays/test` and `k8s/overlays/prod` intentionally do not emit `Namespace` resources.
- App monitoring ownership is centralized in `k8s/monitoring` (single source).
- Apply `k8s/namespace` first, then ingress policy, overlays, monitoring, and controller.

## Monitoring Prerequisites
- Deploy app monitoring from `k8s/monitoring` only. Do not add app
  ServiceMonitor/PrometheusRule resources back into environment overlays.
- App and controller metrics are scraped via ServiceMonitors in the `shipshape` namespace.
- App ServiceMonitor propagates the service `env` label into metric series
  (`spec.targetLabels: ["env"]`) so app SLO queries can be environment-scoped.
- App SLO/paging reliability alerts in `k8s/monitoring/prometheusrule.yaml` are
  scoped to `env="prod"`.
- Test-only app alerts are intentionally non-paging (`severity: warning`,
  `paging: "false"`).
- Istio ingress reliability alerts are scoped to
  `request_host="prod.helloworld.shipshape.example.com"`.
- App HTTP metrics normalize unknown routes to `path=\"other\"` to prevent label-cardinality explosion.
- Under strict mTLS + AuthorizationPolicy, scraping requires Prometheus mesh identity:
  - `cluster.local/ns/monitoring/sa/prometheus-k8s`
- Public ingress access to app `/metrics` is denied by AuthorizationPolicy; only
  monitoring principal traffic is allowed for that path.
- If your monitoring stack uses a different namespace/service account/trust domain, update:
  - `k8s/istio/authorizationpolicy.yaml`
  - `k8s/base/networkpolicy.yaml`

## Configuration Reference
- Use `docs/reference/configuration.md` as the single source of truth for app/controller environment variables, defaults, validation constraints, and incident impact notes.

Required metric sources for all shipped alerts:
- Prometheus Operator CRDs (`ServiceMonitor`, `PrometheusRule`)
- kube-state-metrics:
  - `kube_pod_container_status_restarts_total`
  - `kube_horizontalpodautoscaler_status_desired_replicas`
  - `kube_horizontalpodautoscaler_spec_max_replicas`
  - `kube_poddisruptionbudget_status_pod_disruptions_allowed`
  - `kube_poddisruptionbudget_status_current_healthy`
  - `kube_poddisruptionbudget_status_desired_healthy`
- cAdvisor/kubelet container metrics (`container_memory_working_set_bytes`)
- cert-manager metrics (`certmanager_certificate_expiration_timestamp_seconds`)
- Istio telemetry (`istio_requests_total`)

Validation:
```bash
kubectl get crd servicemonitors.monitoring.coreos.com prometheusrules.monitoring.coreos.com
kubectl -n monitoring get deploy -l app.kubernetes.io/name=kube-state-metrics
kubectl -n cert-manager get deploy
kubectl -n istio-system get deploy istiod
```

Prometheus query spot checks:
```bash
kubectl -n monitoring port-forward svc/prometheus-k8s 9090:9090
curl -s 'http://127.0.0.1:9090/api/v1/query?query=kube_pod_container_status_restarts_total'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=kube_horizontalpodautoscaler_status_desired_replicas'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=kube_horizontalpodautoscaler_spec_max_replicas'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=kube_poddisruptionbudget_status_pod_disruptions_allowed'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=kube_poddisruptionbudget_status_current_healthy'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=container_memory_working_set_bytes'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=certmanager_certificate_expiration_timestamp_seconds'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=istio_requests_total'
```

Alert routing ownership matrix:

| Severity | Alertmanager route | Target channel | Ack SLA | Routing owner |
|---|---|---|---|---|
| warning | `shipshape-warning` | `#shipshape-oncall` (Slack) | 15 minutes | Platform primary on-call |
| critical | `shipshape-critical` | PagerDuty service `shipshape-prod` + `#shipshape-incidents` | 5 minutes | Incident commander on-call |

AlertmanagerConfig objects and PagerDuty/Slack routing integrations are
cluster-external dependencies for this repository. Track owning team and last
validation date in your platform inventory so routing drift is visible during
audits.

Alertmanager route validation:
```bash
kubectl -n monitoring get secret alertmanager-main -o name
kubectl -n monitoring get alertmanagerconfig -A | rg shipshape
kubectl -n monitoring port-forward svc/alertmanager-main 9093:9093
curl -s http://127.0.0.1:9093/api/v2/status | jq '.configYAML'
```

## Pre-Deploy Validation
Run render-time invariants before deployment:
```bash
python3 hack/validate_manifests.py \
  --overlay test \
  --overlay prod \
  --controller-egress-patch examples/controller-apiserver-cidr-patch.yaml \
  --controller-egress-patch examples/controller-egress/eks.patch.yaml \
  --controller-egress-patch examples/controller-egress/gke.patch.yaml \
  --controller-egress-patch examples/controller-egress/aks.patch.yaml
python3 hack/check_immutable_images.py
python3 hack/check_doc_links.py
python3 hack/validate_release_metadata.py
python3 hack/validate_deployment_order.py
python3 hack/validate_trivyignore.py
kustomize build k8s/monitoring >/dev/null
```

`hack/validate_manifests.py` enforces selector-preservation invariants for
NetworkPolicies (ingress gateway and DNS peer selectors) and fails fast if
kustomize label transforms mutate these external selectors. It also validates
controller egress patch renders and fails if the deny placeholder CIDR
(`127.255.255.255/32`) remains after applying your patch source.

## Strict Production Evidence Gate
Before approving a production release tag, validate operational evidence
artifacts are complete:
```bash
python3 hack/validate_production_evidence.py --environment prod
```

This strict gate fails when any required artifact is still blocked or pending:
- `docs/operations-artifacts/capacity-baselines.md`
- latest `docs/operations-artifacts/dr-drill-YYYYMMDD.md`
- `docs/operations-artifacts/security-controls-validation.md`
- `docs/operations-artifacts/controller-egress-handoff.md` for the target environment

Accepted gate statuses are:
- capacity gate status: `APPROVED`/`PASS`/`PASSED`/`READY`/`COMPLETED`
- latest DR drill status: `COMPLETED`
- security matrix result: `APPROVED`/`PASS`/`PASSED`/`READY`/`COMPLETED` with no placeholders
- controller egress current status: `APPROVED`/`PASS`/`PASSED`/`READY`/`COMPLETED`
- controller egress smoke result for target environment row: `APPROVED`/`PASS`/`PASSED`/`READY`/`COMPLETED`

## Deployment Order
1. Apply namespace kustomization.
2. Deploy ingress-gateway policy resources (`istio-system` namespace).
3. Deploy `test` overlay.
4. Deploy `prod` overlay.
5. Deploy app monitoring resources.
6. Deploy controller manifests.

Commands:
```bash
kubectl apply -k k8s/namespace
kubectl apply -k k8s/istio-ingress
kubectl apply -k k8s/overlays/test
kubectl apply -k k8s/overlays/prod
kubectl apply -k k8s/monitoring
kubectl apply -k k8s/controller
```

### Deployment Order Drift Check
Keep this exact apply order synchronized across all runbooks and release artifacts:
```bash
python3 hack/validate_deployment_order.py
```

Canonical order reference:
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

## Post-Deploy Health Checks
```bash
kubectl -n shipshape get deployments
kubectl -n shipshape get pods -l app=helloworld
kubectl -n shipshape get pods -l app=helloworld-controller
kubectl -n shipshape get networkpolicy
kubectl -n shipshape get gateway,virtualservice,destinationrule,authorizationpolicy,peerauthentication
```

## Validate Metrics and Alert Objects
```bash
kubectl -n shipshape get servicemonitor,prometheusrule
kubectl -n shipshape get authorizationpolicy helloworld-allow-ingress-only-test -o yaml
kubectl -n shipshape get authorizationpolicy helloworld-allow-ingress-only-prod -o yaml
```

## Validate Probe Reachability Under NetworkPolicy
Some CNIs require explicit node-origin ingress allow rules for kubelet health probes,
even when service traffic policies are otherwise correct.

Controller probe semantics:
- Kubernetes startup/liveness/readiness probes intentionally target `/healthz`
  for `helloworld-controller` so active and standby replicas can remain Ready.
- `/readyz` is leadership-aware (`503` on followers) and should be treated as
  an operator diagnostic endpoint, not the pod readiness probe target.

Probe compatibility check:
```bash
kubectl -n shipshape get pods -l app=helloworld
kubectl -n shipshape describe pods -l app=helloworld | rg -n "Liveness|Readiness|probe|Unhealthy"
kubectl -n shipshape describe pods -l app=helloworld-controller | rg -n "Liveness|Readiness|probe|Unhealthy"
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}'
```

If probes fail only under default-deny policies:
1. Determine the exact node InternalIP CIDR range used by kubelet probes.
2. Apply additive probe allow policies from:
   - `examples/networkpolicy-probe-allow/app-kubelet-probes.yaml`
   - `examples/networkpolicy-probe-allow/controller-kubelet-probes.yaml`
3. Replace placeholder CIDR `10.0.0.0/16` with your node CIDR before applying.

Example apply flow:
```bash
NODE_CIDR=10.0.0.0/16
sed "s|10.0.0.0/16|${NODE_CIDR}|g" examples/networkpolicy-probe-allow/app-kubelet-probes.yaml | kubectl apply -f -
sed "s|10.0.0.0/16|${NODE_CIDR}|g" examples/networkpolicy-probe-allow/controller-kubelet-probes.yaml | kubectl apply -f -
```

## Validate Controller API Egress Policy
The shipped controller policy is strict by default:
1. DNS egress to CoreDNS/NodeLocalDNS is allowed.
2. API egress on TCP/443 is `ipBlock` allow-list only.
3. Base manifest includes a non-routable placeholder CIDR and must be replaced per environment.

Resolve your API endpoint and prepare exact CIDR allow-list entries:
```bash
kubectl -n default get svc kubernetes -o jsonpath='{.spec.clusterIP}{"\n"}'
kubectl -n default get endpoints kubernetes -o jsonpath='{.subsets[*].addresses[*].ip}{"\n"}'
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}{"\n"}'
```

Platform-specific patch starters:
- `examples/controller-egress/eks.patch.yaml`
- `examples/controller-egress/gke.patch.yaml`
- `examples/controller-egress/aks.patch.yaml`
- Generic starter: `examples/controller-apiserver-cidr-patch.yaml`

Apply the provider patch that matches your cluster. These patch files are
authoritative egress replacements (DNS + API CIDRs):
```bash
kubectl -n shipshape patch networkpolicy helloworld-controller --type merge --patch-file examples/controller-egress/eks.patch.yaml
```

Render-time guard (fails if placeholder CIDR remains in patched output):
```bash
python3 hack/validate_manifests.py \
  --overlay test \
  --overlay prod \
  --controller-egress-patch examples/controller-egress/eks.patch.yaml
```

Record resolved CIDRs and the selected patch source in:
- `docs/operations-artifacts/controller-egress-handoff.md`

Do not approve production rollout unless the latest row in that artifact is
dated, reviewer-signed, and references the patch file used by the target
cluster.

Controller-to-API smoke check:
```bash
kubectl -n shipshape exec -i deploy/helloworld-controller -- python - <<'PY'
import ssl
import urllib.request

token = open("/var/run/secrets/kubernetes.io/serviceaccount/token", "r", encoding="utf-8").read().strip()
req = urllib.request.Request(
    "https://kubernetes.default.svc/version",
    headers={"Authorization": f"Bearer {token}"},
)
ctx = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
    print(resp.status)
PY
```

After the smoke check succeeds, update
`docs/operations-artifacts/controller-egress-handoff.md` with:
- resolved API endpoint source details,
- validated CIDR list,
- exact patch path applied,
- validation command output link or transcript,
- reviewer/date sign-off.

## HPA Prerequisites and Preflight
`helloworld-prod` autoscaling depends on the Kubernetes resource-metrics API
(`metrics-server`, APIService `v1beta1.metrics.k8s.io`).

Preflight checks:
```bash
kubectl get apiservice v1beta1.metrics.k8s.io -o wide
kubectl top nodes
kubectl -n shipshape top pods
kubectl -n shipshape get hpa helloworld-prod -o wide
kubectl -n shipshape describe hpa helloworld-prod | rg -n 'AbleToScale|ScalingActive|ValidMetricFound'
```

Fallback behavior if metrics API is unavailable:
1. HPA target values can show `<unknown>` and no autoscaling decisions are made.
2. Treat scale as manual until metrics API health is restored.
3. Temporarily scale directly:
   - `kubectl -n shipshape scale deployment/helloworld-prod --replicas=<n>`
4. Do not close production scaling incidents until HPA conditions return healthy.

## Validate ConfigMap-Driven Restart Semantics
Initial `ADDED` events should not restart workloads when data is unchanged.

Trigger a data change:
```bash
kubectl -n shipshape patch configmap helloworld-config-test --type merge \
  -p '{"data":{"MESSAGE":"new test message"}}'
```

Watch rollout:
```bash
kubectl -n shipshape rollout status deployment/helloworld-test
kubectl -n shipshape logs deployment/helloworld-controller --tail=100
```

## Istio Routing Verification
Confirm rendered cross-resource wiring:
```bash
kustomize build k8s/overlays/test | rg 'helloworld-gateway-test|test\.helloworld\.shipshape\.example\.com'
kustomize build k8s/overlays/prod | rg 'helloworld-gateway-prod|prod\.helloworld\.shipshape\.example\.com'
```

Confirm cluster resources:
```bash
kubectl -n shipshape get gateway,virtualservice,destinationrule,certificate
```

Run ingress host smoke checks (replace ingress address):
```bash
curl -H "Host: test.helloworld.shipshape.example.com" https://<INGRESS_IP>/
curl -H "Host: prod.helloworld.shipshape.example.com" https://<INGRESS_IP>/
```

Expected response mapping:
- `test.helloworld.shipshape.example.com` -> test message from `helloworld-config-test`
- `prod.helloworld.shipshape.example.com` -> prod message from `helloworld-config-prod`

## Validate Ingress Rate Limiting
Rate limiting is enforced by `k8s/istio-ingress/ratelimit-envoyfilter.yaml`.

> **Important:** The EnvoyFilter matches routes by vhost name with a `:443` suffix
> (e.g. `prod.helloworld.shipshape.example.com:443`). If you change hostnames in
> the Gateway/VirtualService resources, you must also update the corresponding
> `routeConfiguration.vhost.name` entries in the EnvoyFilter — otherwise rate
> limiting is silently disabled for the affected host.

Defaults:
- Token bucket per host/per ingress-gateway pod: `120` requests per `60s`.
- A limited response includes header `x-shipshape-ratelimited: true`.

Validation:
```bash
kubectl -n istio-system get envoyfilter helloworld-ingress-ratelimit -o yaml
```

Burst test (expect some `429` responses):
```bash
for i in $(seq 1 300); do
  curl -sk -o /dev/null -w '%{http_code}\n' \
    -H 'Host: test.helloworld.shipshape.example.com' \
    https://<INGRESS_IP>/
done | sort | uniq -c
```

## Immutable Release Flow
1. Build and publish images.
2. Resolve and verify immutable digests.
3. Update deployment manifests with `image@sha256:...` values.
4. Deploy using standard order.
5. Verify image digests in running workloads.

Verification:
```bash
kubectl -n shipshape get deploy helloworld-test -o jsonpath='{.spec.template.spec.containers[*].image}'
kubectl -n shipshape get deploy helloworld-prod -o jsonpath='{.spec.template.spec.containers[*].image}'
kubectl -n shipshape get deploy helloworld-controller -o jsonpath='{.spec.template.spec.containers[*].image}'
```

## Emergency Rollback Procedure
1. Re-apply manifests pinned to a previous known-good digest.
2. Wait for rollouts to complete.
3. Re-run manifest and ingress verification checks.
4. Validate alert noise has returned to baseline.

Commands:
```bash
kubectl apply -k k8s/istio-ingress
kubectl apply -k k8s/overlays/test
kubectl apply -k k8s/overlays/prod
kubectl apply -k k8s/monitoring
kubectl apply -k k8s/controller
kubectl -n shipshape rollout status deployment/helloworld-test
kubectl -n shipshape rollout status deployment/helloworld-prod
kubectl -n shipshape rollout status deployment/helloworld-controller
```

## Disaster Recovery

### DR: Namespace Accidentally Deleted
1. Recreate namespace resources:
```bash
kubectl apply -k k8s/namespace
```
2. Recreate app + controller resources:
```bash
kubectl apply -k k8s/istio-ingress
kubectl apply -k k8s/overlays/test
kubectl apply -k k8s/overlays/prod
kubectl apply -k k8s/monitoring
kubectl apply -k k8s/controller
```
3. Re-verify certificates, gateways, and rollouts.
4. Run host-based smoke checks.

### DR: ConfigMap Corruption / Bad Config Push
1. Identify previous value from Git history or backup artifact.
2. For a single-key correction, patch directly:
```bash
kubectl -n shipshape patch configmap helloworld-config-test --type merge -p '{"data":{"MESSAGE":"<previous>"}}'
kubectl -n shipshape patch configmap helloworld-config-prod --type merge -p '{"data":{"MESSAGE":"<previous>"}}'
```
3. For full object restore from backup artifacts, use deterministic restore flow:
```bash
./scripts/backup-configmaps.sh restore <backup-dir>
```
4. Confirm controller-triggered rollout completes.

### Backup Procedure (Config)
Take periodic metadata-sanitized snapshots:
```bash
./scripts/backup-configmaps.sh backup
```
Store in encrypted, versioned backup storage.

Restore from backup with server-side validation:
```bash
./scripts/backup-configmaps.sh restore <backup-dir>
```

Post-restore verification:
```bash
kubectl -n shipshape get configmap -l app=helloworld -o name
kubectl -n shipshape rollout status deployment/helloworld-test --timeout=180s
kubectl -n shipshape rollout status deployment/helloworld-prod --timeout=180s
```

### DR: Full Cluster Failure (Control Plane / etcd)
Cluster-level disaster recovery (etcd snapshots, control-plane rebuild, and RTO/RPO drills) is documented in:
- `docs/runbooks/disaster-recovery.md`

Minimum operational policy:
1. Snapshot etcd on a fixed cadence and store encrypted off-cluster copies.
2. Test restore to a staging control plane at least quarterly.
3. Reapply manifests in canonical order:
   - `k8s/namespace`
   - `k8s/istio-ingress`
   - `k8s/overlays/test`
   - `k8s/overlays/prod`
   - `k8s/monitoring`
   - `k8s/controller`
4. Re-run ingress, metrics, and alert smoke checks before declaring recovery complete.

Provider command quick links:
- EKS: `docs/runbooks/disaster-recovery.md#eks-control-plane-recovery`
- GKE: `docs/runbooks/disaster-recovery.md#gke-control-plane-recovery`
- AKS: `docs/runbooks/disaster-recovery.md#aks-control-plane-recovery`

Latest drill evidence:
- `docs/operations-artifacts/dr-drill-20260209.md`

## Chaos Drills

Run resilience drills at least quarterly using:
- `docs/runbooks/chaos-drills.md`

Minimum scenarios:
1. Controller API-server connectivity loss.
2. Pod eviction storm in one app environment.
3. Ingress-to-app network partition.

## Multi-Cluster / Multi-Region Strategy

Recommended baseline for production expansion:
1. Active/passive across two regions (`primary`, `secondary`) with independent Kubernetes clusters.
2. Replicate manifests and image digests to both clusters from the same Git revision.
3. Keep DNS failover TTL <= 60 seconds for ingress hostnames.
4. Run quarterly failover drills that include controller leader election, ConfigMap patch/restart behavior, and alert routing validation.

Minimum artifacts to maintain:
- Region inventory (cluster names, API endpoints, and ingress addresses).
- Per-region controller API egress CIDR patch files.
- Last successful failover drill report with measured RTO/RPO.

## Upgrade Procedures

### Kubernetes Cluster Upgrade
1. Upgrade non-prod cluster first.
2. Run `make check-ci-core` and `./hack/e2e-kind.sh` on the candidate branch.
3. Upgrade control plane, then worker nodes.
4. Run post-upgrade smoke checks and rollback if health checks fail.

### Istio Upgrade
1. Review target version release notes.
2. Upgrade in test/staging first.
3. Verify `PeerAuthentication`, `AuthorizationPolicy`, `Gateway`, and `VirtualService` behavior:
```bash
kubectl -n shipshape get peerauthentication,authorizationpolicy,gateway,virtualservice,destinationrule
kubectl -n istio-system get envoyfilter helloworld-ingress-ratelimit
```
4. Roll out to prod after sustained healthy metrics.

### cert-manager Upgrade
1. Upgrade cert-manager controller and CRDs per release notes.
2. Confirm certificate renewals are healthy:
```bash
kubectl -n shipshape get certificate
kubectl -n shipshape describe certificate helloworld-cert-test
kubectl -n shipshape describe certificate helloworld-cert-prod
```

### Python Dependency Upgrades
1. Review Dependabot PRs weekly.
2. Run:
```bash
make check-ci-core
```
3. Build and scan images in CI.
4. Roll out to test first, then prod.

## Alert Runbooks

## Runbook: App High Error Rate
Triggered by `HelloworldHighErrorRate`.
1. Check recent deploys and config changes.
2. Inspect app logs and ingress metrics.
3. Validate upstream dependencies and Istio routing.
4. Roll back to last known-good digest if error rate persists.

## Runbook: App High Latency P95
Triggered by `HelloworldHighLatencyP95`.
1. Check pod CPU/memory saturation.
2. Check upstream network/Istio retries.
3. Validate HPA status and pod scheduling.
4. Scale up temporarily or roll back recent risky changes.

## Runbook: App SLO Burn Rate (Fast)
Triggered by `HelloworldErrorBudgetBurnFast`.
1. Treat as active user impact and open incident bridge immediately.
2. Compare current and previous deploy/image digest in prod.
3. Roll back to last known-good digest if regression is deployment-related.
4. Verify error budget burn drops below threshold in both 5m and 1h windows.

## Runbook: App SLO Burn Rate (Slow)
Triggered by `HelloworldErrorBudgetBurnSlow`.
1. Investigate sustained degradation sources (dependency latency, retries, upstream 5xx).
2. Check whether warning-level burn is drifting toward the fast-burn threshold.
3. Apply mitigations (capacity increase, traffic shaping, rollback) before fast-burn escalation.
4. Track burn trend for at least one full 6h window after mitigation.

## Runbook: Controller Restart Errors
Triggered by `ConfigMapReloadErrors`.
1. Check controller logs.
2. Check retry pressure:
```bash
kubectl -n shipshape port-forward deployment/helloworld-controller 8080:8080
curl -s http://127.0.0.1:8080/metrics | rg 'configmap_reload_errors_total|configmap_reload_retry_total'
```
3. Validate controller RBAC:
```bash
kubectl -n shipshape auth can-i patch deployments --as system:serviceaccount:shipshape:helloworld-controller
```
4. Verify target deployment selectors and labels (`app=helloworld`, `env=<test|prod>`).

## Runbook: Controller Watch Down
Triggered by `ConfigMapReloadWatchDown`.
1. Check Kubernetes API availability.
2. Validate controller pod connectivity to API server.
3. Inspect controller logs for repeated watch reconnect failures or auth errors.
4. Interpret readiness correctly during transient outages:
   - `/readyz` can remain `200` briefly while the current leader is within `LEADER_ELECTION_RENEW_DEADLINE_SECONDS`.
   - `/readyz` returns `503` once leadership is lost or startup list/readiness is not satisfied.
5. Restart controller deployment if needed after root cause is fixed.

## Runbook: Controller Metrics Absent
Triggered by `ConfigMapReloadMetricsAbsent`.
1. Verify controller pods are running and reachable:
   - `kubectl -n shipshape get pods -l app=helloworld-controller`
2. Confirm ServiceMonitor and scrape endpoints are healthy:
   - `kubectl -n shipshape get servicemonitor helloworld-controller -o yaml`
   - `kubectl -n shipshape get endpoints helloworld-controller -o yaml`
3. Check for monitoring-plane auth/policy regressions:
   - `kubectl -n shipshape get authorizationpolicy helloworld-allow-ingress-only-test -o yaml`
   - `kubectl -n shipshape get authorizationpolicy helloworld-allow-ingress-only-prod -o yaml`
4. Inspect controller logs for startup/list/watch failures that may coincide with metric loss.
5. Restore scrape path first, then verify `configmap_reload_leader_state` returns before resolving the alert.

## Runbook: Controller High Restart Rate
Triggered by `ConfigMapReloadHighRestartRate`.
1. Identify source of frequent ConfigMap updates.
2. Validate debounce settings (`DEBOUNCE_SECONDS`).
3. Pause noisy automation/pipeline and stabilize config churn.

## Runbook: Controller Dropped Restarts
Triggered by `ConfigMapReloadDroppedRestarts`.
1. Check controller logs around shutdown/leadership transitions.
2. This alert should be rare: pending restarts are force-processed on shutdown.
3. Treat any increment as a forced-flush failure path and inspect controller exceptions.
4. If drift persists, patch the ConfigMap again to force a clean restart trigger.

## Runbook: Controller Leader Flapping
Triggered by `ConfigMapReloadLeaderFlapping`.
1. Check `configmap_reload_leader_transitions_total` and `configmap_reload_leader_acquire_latency_seconds` trends.
2. Inspect controller and API server connectivity for intermittent packet loss or auth issues.
3. Verify lease timings (`LEADER_ELECTION_LEASE_DURATION_SECONDS`, `LEADER_ELECTION_RENEW_DEADLINE_SECONDS`, `LEADER_ELECTION_RETRY_PERIOD_SECONDS`) are compatible with cluster latency.
4. Stabilize networking or tune lease timings, then confirm transitions return to baseline.

## Runbook: Controller No Active Leader
Triggered by `ConfigMapReloadNoActiveLeader`.
1. Confirm both controller pods are running: `kubectl -n shipshape get pods -l app=helloworld-controller`.
2. Query leader endpoint on each pod (`/leadz`) to verify whether election is stalled.
3. Inspect Lease object freshness and holder:
```bash
kubectl -n shipshape get lease helloworld-controller-leader -o yaml
```
4. Check controller logs for lease renewal failures, RBAC denials, or clock skew symptoms.
5. If no pod can renew, restore Kubernetes API connectivity and restart controller deployment once root cause is fixed.

## Runbook: Controller Pending Restart Queue Stuck
Triggered by `ConfigMapReloadPendingRestartsStuck`.
1. Check queue depth and restart counters:
```bash
kubectl -n shipshape port-forward deployment/helloworld-controller 8080:8080
curl -s http://127.0.0.1:8080/metrics | rg 'configmap_reload_pending_restarts|configmap_reload_restarts_total'
```
2. Inspect controller logs for repeated watch errors or restart patch failures.
3. Verify target deployments still match selector labels (`app=helloworld`, `env=<test|prod>`).
4. If queue remains non-zero while restart counters do not advance, correlate with
   `configmap_reload_retry_total` to determine if retries are active vs. stalled.
5. If queue stays stuck without retry progress, restart the controller after fixing the underlying API or RBAC issue.

## Runbook: Istio Gateway 5xx
Triggered by `IstioGateway5xxRate`.
1. Check ingress gateway logs and recent Envoy/Istio config pushes for host `prod.helloworld.shipshape.example.com`.
2. Compare app 5xx (`http_requests_total{env="prod"}`) with ingress 5xx (`istio_requests_total{request_host="prod.helloworld.shipshape.example.com"}`) to isolate source.
3. Validate `Gateway`, `VirtualService`, and `DestinationRule` objects for routing drift.
4. Roll back the most recent risky networking/deployment change if errors persist.

## Runbook: Ingress Rate-Limit Saturation
Triggered by `IstioGateway429Saturation` or sustained `429` responses at the ingress layer.
1. Confirm `x-shipshape-ratelimited: true` is present on throttled responses.
2. Inspect ingress-gateway load and request patterns by host.
3. If traffic is legitimate and sustained, raise token bucket values in `k8s/istio-ingress/ratelimit-envoyfilter.yaml`.
4. If traffic is abusive, keep limits and apply upstream controls (WAF, bot filtering, block rules).

## Logging

### Structured Logging
Both the app and controller emit structured JSON logs to stdout. Each log line is a single JSON object with keys: `ts`, `level`, `logger`, `msg`, and optionally `error`.

Configure log level via the `LOG_LEVEL` environment variable (default: `INFO`). Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.

### Redaction Policy
Before serialization, runtime formatters redact common credential patterns from
log message and exception text:
- key/value patterns for `token`, `password`, `secret`, `api_key`, `authorization`
- bearer tokens (`Bearer <token>`)
- URL query credential fields (`access_token`, `token`, `api_key`, `password`)

Do not log raw service-account tokens, kubeconfigs, API keys, or user secrets.
Treat any detected unredacted secret in logs as a security incident and rotate
the exposed credential immediately.

### Log Aggregation & Retention
For production, ship container stdout to a centralized log system (e.g. Loki, Elasticsearch, CloudWatch Logs). Recommended retention:
- **Hot storage:** 7 days (for incident triage)
- **Warm/archive:** 30–90 days (for compliance and post-incident review)

Example Loki label config for pod logs:
```yaml
- source_labels: [__meta_kubernetes_namespace]
  target_label: namespace
- source_labels: [__meta_kubernetes_pod_label_app]
  target_label: app
- source_labels: [__meta_kubernetes_pod_label_env]
  target_label: env
```

### Querying Logs
```bash
# Follow live logs
kubectl -n shipshape logs -f deployment/helloworld-test
kubectl -n shipshape logs -f deployment/helloworld-controller

# Filter by log level (jq)
kubectl -n shipshape logs deployment/helloworld-controller | jq 'select(.level == "ERROR")'
```

## Distributed Tracing (Current Scope)
Tracing support in this repository is currently app-only and opt-in
(`OTEL_ENABLED=true` in `app/src/main.py`).
Controller tracing is intentionally not implemented in the current production
baseline; controller observability is metrics + structured logs.

OpenTelemetry packages are optional and not present in default runtime images.
If omitted, the app logs a warning and continues without tracing.

Recommended staged enablement:
1. Enable tracing in `test` first.
2. Set exporter endpoint to your collector (OTLP/HTTP):
   - `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.monitoring.svc:4318`
3. Set service identity (optional):
   - `OTEL_SERVICE_NAME=helloworld`
   - `OTEL_SERVICE_NAMESPACE=shipshape`
4. Validate traces in Jaeger/Tempo before enabling in prod.

## RTO/RPO Targets

- **RTO (Recovery Time Objective):** 15 minutes for app service restoration (redeploy from Git + controller restart).
- **RPO (Recovery Point Objective):** Zero data loss for configuration (ConfigMaps are stored in etcd and backed by Git). Application is stateless.
- These targets assume: functioning cluster, accessible container registry, and valid Git state.

## SLOs

- **App availability:** 99.9% measured over a rolling 30-day window.
- **App p95 latency (prod):** < 500ms (aligned with `HelloworldHighLatencyP95` alert threshold, scoped to `env="prod"`).
- **App error rate (prod):** < 5% 5xx (aligned with `HelloworldHighErrorRate` alert threshold, scoped to `env="prod"`).
- **Controller restart latency:** ConfigMap change detected and restart triggered within `DEBOUNCE_SECONDS + 30s` (watch timeout).

## Capacity Baseline & HPA Validation

Use `hack/load-test-k6.js` to produce repeatable throughput/latency baselines and tune HPA targets with evidence.

Managed-cluster production promotion thresholds (all required):
1. Sustained throughput: `>= 120 requests/s` for at least `5m`.
2. Latency: `p95 <= 250ms` and `p99 <= 500ms` for the same test window.
3. Error budget pressure: `5xx <= 1%` during the baseline window.
4. HPA stability: no more than `2` scale-direction changes in any `10m` steady-state window.
5. Alert posture: no critical production alerts firing during the baseline window:
   - `HelloworldHighLatencyP95`
   - `HelloworldHighErrorRate`
   - `IstioGateway5xxRate`
   - `HelloworldProdHPANearMaxReplicas`
   - `HelloworldProdPDBDisruptionsExhausted`
   - `HelloworldProdPDBInsufficientHealthyPods`

Example run:
```bash
k6 run \
  -e BASE_URL=https://<INGRESS_IP> \
  -e HOST_HEADER=prod.helloworld.shipshape.example.com \
  -e VUS=30 \
  -e DURATION=5m \
  hack/load-test-k6.js
```

If `k6` is not installed locally, run from Docker:
```bash
docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  -v "$PWD:/work" -w /work \
  grafana/k6 run \
  -e BASE_URL=https://<INGRESS_IP> \
  -e HOST_HEADER=prod.helloworld.shipshape.example.com \
  -e VUS=30 \
  -e DURATION=5m \
  hack/load-test-k6.js
```

Record after each baseline run:
1. Requests/s, p95, p99, and 5xx error rate from k6 output.
2. `kubectl -n shipshape get hpa helloworld-prod -o yaml` current target/observed utilization.
3. Whether any production scaling alerts fired:
   - `HelloworldHighLatencyP95`
   - `HelloworldHighErrorRate`
   - `IstioGateway5xxRate`
   - `IstioGateway429Saturation`
4. Save the run summary in `docs/operations-artifacts/capacity-baselines.md` with:
   - date and cluster identifier,
   - ingress path used (`HOST_HEADER` and gateway address),
   - deployed image digests,
   - HPA observed behavior (scale-up/down events),
   - alert outcomes (fired/not fired),
   - promotion policy result (`PASS`/`BLOCKED`) with failed threshold notes.
5. Keep at least one non-local cluster baseline entry current before production rollout approval.

## Secrets Management & Rotation

### TLS Certificates
cert-manager handles automatic issuance and renewal. Monitor certificate health:
```bash
kubectl -n shipshape get certificate
kubectl -n shipshape describe certificate helloworld-cert-test
```

The `CertManagerCertExpiringSoon` alert fires when a certificate has < 7 days until expiry. If cert-manager is not renewing:
1. Check cert-manager controller logs: `kubectl -n cert-manager logs -l app=cert-manager`
2. Verify ClusterIssuer is healthy: `kubectl get clusterissuer -o yaml`
3. Check ACME challenge solver DNS/HTTP01 configuration.

### Kubernetes Secrets
- TLS secrets are created by cert-manager and stored as `kubernetes.io/tls` Secrets.
- For external secret management (e.g. AWS Secrets Manager, HashiCorp Vault), consider deploying the External Secrets Operator and replacing direct Secret references with `ExternalSecret` resources.
- Rotate secrets by updating the source and triggering cert-manager renewal or ExternalSecret sync.

### Audit Logging
Enable Kubernetes API audit logging at the cluster level to track who modified ConfigMaps, Secrets, and RBAC objects. Configure audit policy to log `configmaps`, `secrets`, and `deployments` in the `shipshape` namespace at the `RequestResponse` level.

Minimum validation checklist:
```bash
# Verify API server is started with an audit policy file.
kubectl -n kube-system get pods -l component=kube-apiserver -o yaml | rg -- '--audit-policy-file|--audit-log-path'

# Validate recent ConfigMap mutation events are present in your log backend.
# Replace with your provider-specific audit sink query.
```

Record dated, environment-specific evidence for encryption-at-rest and
audit-log sink validation in:
- `docs/operations-artifacts/security-controls-validation.md`

## Compliance Scope Notes

- **Data classification assumption:** application payloads are non-sensitive by default; if sensitive data is routed through this stack, external governance controls must be added.
- **Data residency:** this repository does not enforce region pinning; residency controls depend on cluster, registry, and logging backend configuration.
- **Regulatory posture:** GDPR/SOC2/HIPAA outcomes depend on external identity, logging retention, incident response, and vendor controls not versioned in this repository.

## Release & Tagging Process

1. Update `CHANGELOG.md` with the new version entry.
2. Update `version` in `pyproject.toml`.
3. Run metadata gate locally: `python3 hack/validate_release_metadata.py`
4. Create and push a signed tag: `git tag -s v<VERSION> -m "Release v<VERSION>" && git push origin v<VERSION>`
5. Tag push triggers `.github/workflows/release.yml`, which:
   - verifies the pushed tag is an annotated tag object with a valid cryptographic signature recognized by GitHub,
   - validates changelog/version/tag/runtime-constant consistency,
   - validates manifest invariants, immutable image references, and security/runbook links,
   - enforces strict production evidence gate for `--environment prod` (including controller egress handoff evidence),
   - builds and pushes app/controller images (`v<VERSION>` + `sha-<commit>` tags),
   - scans images with Trivy,
   - signs images with Cosign (keyless OIDC),
   - publishes provenance attestations and BuildKit SBOM/provenance artifacts,
   - runs a post-publish Kind smoke deployment using the released image digests,
   - uploads `release-manifests-v<VERSION>` artifact containing pre-rendered manifests with released digests and controller egress handoff templates,
   - publishes/updates the GitHub Release object for the tag with generated notes from the tagged changelog section and manifest-bundle run link.
6. Use the release manifest artifact for deployment (preferred), or update repository manifests with released `image@sha256:...` digests.
7. Include the environment-selected controller egress patch evidence in `docs/operations-artifacts/controller-egress-handoff.md` before production handoff.
8. Deploy using the standard deployment order.

## Scheduled Base-Image Rebuild & Rescan
- Weekly non-blocking refresh is run by `base-image-refresh` job in `.github/workflows/ci.yml` on the Monday scheduled CI run.
- The job rebuilds app/controller images with `--pull --no-cache`, then performs Trivy scans.
- Reports are uploaded as artifact `base-image-refresh-reports`.
- Review the latest report artifact before weekly production promotion windows to catch base-image CVEs that appeared after the last code change.

## On-Call Escalation
| Severity | Initial responder | Escalate after | Escalation target | Primary channel | Secondary channel |
|---|---|---|---|---|---|
| warning | Platform primary on-call | 30 minutes unresolved | Platform secondary on-call | `#shipshape-oncall` | Jira incident ticket |
| critical | Incident commander (IC) on-call | 10 minutes unresolved | Engineering manager + SRE lead | PagerDuty `shipshape-prod` | `#shipshape-incidents` bridge |

Escalation procedure:
1. Acknowledge page within route SLA.
2. Open/attach incident timeline within 5 minutes for `critical` alerts.
3. Record owner for mitigation and owner for communications.
4. Publish post-incident review within 2 business days for all `critical` incidents.

## Runbook: Pod Crash-Looping
Triggered by `KubePodCrashLooping`.
1. Identify the crash-looping pod: `kubectl -n shipshape get pods --field-selector=status.phase!=Running`
2. Check pod events: `kubectl -n shipshape describe pod <pod-name>`
3. Check container logs: `kubectl -n shipshape logs <pod-name> --previous`
4. Common causes: missing ConfigMap/Secret, OOMKill, bad image, failing readiness probe.
5. Fix the root cause and let Kubernetes reschedule, or delete the pod to force a fresh restart.

## Runbook: Pod High Memory
Triggered by `KubePodHighMemory`.
1. Check current memory usage: `kubectl -n shipshape top pods`
2. Check for memory leaks in application logs.
3. Consider increasing memory limits in the deployment patch, or investigate the workload for inefficiencies.
4. If HPA is enabled, verify scaling behavior: `kubectl -n shipshape get hpa`

## Runbook: Prod HPA Near Max Replicas
Triggered by `HelloworldProdHPANearMaxReplicas`.
1. Confirm desired/max pressure:
   - `kubectl -n shipshape describe hpa helloworld-prod`
   - `kubectl -n shipshape get deploy helloworld-prod`
2. Determine whether saturation is expected traffic growth or regression.
3. If demand is legitimate, increase `maxReplicas` and/or reduce per-pod saturation bottlenecks (CPU/memory limits, upstream latency).
4. If demand is abnormal, mitigate with ingress controls (rate-limit, WAF, traffic shaping) and roll back risky recent changes.
5. Keep incident open until desired replicas drop below 90% of max for at least one evaluation window.

## Runbook: Prod PDB Disruptions Exhausted
Triggered by `HelloworldProdPDBDisruptionsExhausted`.
1. Confirm allowed disruptions and healthy count:
   - `kubectl -n shipshape get pdb helloworld-prod -o yaml`
2. Pause voluntary disruptions (node drain/autoscaler evictions) for affected nodes.
3. Restore headroom by stabilizing unhealthy pods or increasing replica count.
4. Resume node maintenance only after `disruptionsAllowed` is consistently above 0.

## Runbook: Prod PDB Insufficient Healthy Pods
Triggered by `HelloworldProdPDBInsufficientHealthyPods`.
1. Treat as availability-impacting and check current pod readiness:
   - `kubectl -n shipshape get pods -l app=helloworld,env=prod`
2. Identify root cause (crash loops, readiness failures, scheduling constraints, node disruption).
3. Restore healthy capacity first (rollback, fix config/image, or scale up).
4. Validate `currentHealthy >= desiredHealthy` in the PDB before closing incident.

## Runbook: Certificate Expiring
Triggered by `CertManagerCertExpiringSoon`.
1. Check certificate status: `kubectl -n shipshape describe certificate <cert-name>`
2. Verify cert-manager controller is running: `kubectl -n cert-manager get pods`
3. Check cert-manager logs for renewal failures.
4. Verify ClusterIssuer/Issuer is healthy and ACME challenge solver is configured.
5. If auto-renewal is stuck, delete the CertificateRequest and let cert-manager recreate it.

## Troubleshooting
1. Controller not restarting deployments:
```bash
kubectl -n shipshape auth can-i patch deployments --as system:serviceaccount:shipshape:helloworld-controller
kubectl -n shipshape logs deployment/helloworld-controller
```

2. Istio route mismatch:
```bash
kubectl -n shipshape get virtualservice helloworld-virtualservice-test -o yaml
kubectl -n shipshape get virtualservice helloworld-virtualservice-prod -o yaml
kubectl -n shipshape get gateway helloworld-gateway-test -o yaml
kubectl -n shipshape get gateway helloworld-gateway-prod -o yaml
kubectl -n shipshape get destinationrule helloworld-destinationrule-test -o yaml
kubectl -n shipshape get destinationrule helloworld-destinationrule-prod -o yaml
```

3. TLS certificate not issuing:
```bash
kubectl -n shipshape describe certificate helloworld-cert-test
kubectl -n shipshape describe certificate helloworld-cert-prod
```

4. Unexpected ingress `429` responses:
```bash
kubectl -n istio-system get envoyfilter helloworld-ingress-ratelimit -o yaml
kubectl -n istio-system logs deploy/istio-ingressgateway --tail=200
```
