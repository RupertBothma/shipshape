# Deployment Considerations

> Detailed guide for adapting this Kustomize deployment to your cluster.
> All items below must be reviewed before first apply.

---

## Table of Contents

- [1. Container Registry & Image Access](#1-container-registry-image-access)
- [2. DNS & Hostname Configuration](#2-dns-hostname-configuration)
- [3. TLS & Certificate Management](#3-tls-certificate-management)
- [4. Istio Integration Points](#4-istio-integration-points)
- [5. Controller API Server Egress](#5-controller-api-server-egress)
- [6. Monitoring Stack Dependencies](#6-monitoring-stack-dependencies)
- [7. Namespace Ownership & Resource Quotas](#7-namespace-ownership-resource-quotas)
- [8. Resource Sizing](#8-resource-sizing)
- [9. Cluster Compatibility Checklist](#9-cluster-compatibility-checklist)
- [10. Adaptation Steps](#10-adaptation-steps)
- [11. Validation & Rollback](#11-validation-rollback)

---

## 1. Container Registry & Image Access

The manifests reference two container images:

| Image | Base Reference | Overlay Mechanism | Controller Kustomization |
|---|---|---|---|
| **App** | `shipshape/helloworld` | Injected dynamically by `scripts/manage-images.sh` (mutable tag for test, immutable digest for prod) | — |
| **Controller** | `shipshape/controller` | — | Digest-pinned in `k8s/controller/kustomization.yaml` (currently `sha256:aaf418…`) |

> [!NOTE]
> The test and prod overlay kustomizations do **not** contain static `images:` blocks. Image references are injected at build/deploy time by `scripts/manage-images.sh`. All images are built locally and pushed to a local registry (e.g. `localhost:5000`).

### What to verify

- **Local registry access:** Confirm images are pullable from your cluster nodes:
  ```bash
  docker pull localhost:5000/shipshape-helloworld:latest
  docker pull localhost:5000/shipshape-controller:latest
  ```
- **Private registry:** If images are private, create an `imagePullSecret` and attach it to the ServiceAccounts or Deployments:
  ```bash
  kubectl create secret docker-registry private-registry-secret \
    -n shipshape \
    --docker-server=my-private-registry.com \
    --docker-username=<user> \
    --docker-password=<PAT>
  ```

### Local development

For `Kind` / `Minikube` local clusters, images are loaded directly:
```bash
docker build -f app/Dockerfile -t shipshape/helloworld:dev .
kind load docker-image shipshape/helloworld:dev
```

No remote push or `imagePullSecrets` are needed locally.

---

## 2. DNS & Hostname Configuration

The deployment uses placeholder hostnames that **must be replaced** for any real cluster:

| Current Placeholder | Used By |
|---|---|
| `test.helloworld.shipshape.example.com` | Test Gateway, VirtualService, Certificate |
| `prod.helloworld.shipshape.example.com` | Prod Gateway, VirtualService, Certificate |

### Files requiring hostname changes

> [!CAUTION]
> There are **4 files** that reference these hostnames. Missing even one creates silent routing or security failures.

| # | File | What to change |
|---|---|---|
| 1 | `k8s/overlays/test/app-vars.yaml` | `data.HOSTNAME` — propagated via Kustomize Replacements into Gateway, VirtualService, and Certificate |
| 2 | `k8s/overlays/prod/app-vars.yaml` | `data.HOSTNAME` — same as above for prod |
| 3 | `k8s/istio-ingress/ratelimit-envoyfilter.yaml` | `vhost.name` entries (must include `:443` suffix) |
| 4 | `k8s/monitoring/prometheusrule.yaml` | `request_host` in PromQL alert queries |

> [!WARNING]
> The EnvoyFilter vhost names **must include the `:443` suffix** and exactly match the Gateway hostnames (e.g. `prod.example.com:443`). Changing Gateway hostnames without updating the EnvoyFilter **silently disables rate limiting**.

### Local development

For local clusters, use one of:
- `/etc/hosts` entries pointing to `127.0.0.1`
- `nip.io` wildcard domains (e.g. `test.helloworld.127.0.0.1.nip.io`)
- The E2E scripts (`hack/e2e-kind.sh`) handle this automatically via `curl --resolve`

---

## 3. TLS & Certificate Management

### Required ClusterIssuers

The Certificate resources reference two `ClusterIssuer` names that **must exist** in the target cluster:

| Environment | ClusterIssuer Name | File |
|---|---|---|
| Test | `letsencrypt-staging` | `k8s/overlays/test/app-vars.yaml` (`data.CLUSTER_ISSUER`) |
| Prod | `letsencrypt-prod` | `k8s/overlays/prod/app-vars.yaml` (`data.CLUSTER_ISSUER`) |

```bash
# Verify issuers exist
kubectl get clusterissuer letsencrypt-staging letsencrypt-prod
```

If your cluster uses different issuer names (e.g. `cloudflare-issuer`, `internal-ca`), update the `CLUSTER_ISSUER` value in each overlay's `app-vars.yaml`.

### ACME Challenge Solver

The manifests do not prescribe a specific challenge solver. Your ClusterIssuer must support issuance for the configured `dnsNames`. Common configurations:
- **HTTP01** — requires the ingress to be publicly reachable on port 80
- **DNS01** — requires DNS provider credentials (works behind firewalls)

### Certificate Secret Namespace

> [!IMPORTANT]
> The Certificate resources are created in the `shipshape` namespace, but the Istio Gateway's `credentialName` (`helloworld-test-tls`, `helloworld-prod-tls`) requires the TLS Secret to be accessible by the ingress gateway in `istio-system`.

Depending on your Istio configuration, you may need one of:
1. **Istio SDS cross-namespace** — recent Istio versions support reading secrets from the Certificate's namespace
2. **reflector/replicator** — automatically copy secrets to `istio-system`
3. **Relocate Certificates** — create Certificate resources directly in `istio-system`

### Local development

The E2E scripts (`hack/e2e-kind.sh`) create short-lived self-signed TLS secrets directly, bypassing cert-manager entirely.

---

## 4. Istio Integration Points

### 4.1 Gateway Selector

The base Gateway (`k8s/components/istio-routing/gateway.yaml`) uses:
```yaml
selector:
  istio: ingressgateway
```
This is the **default label** for the standard Istio `istio-ingressgateway` deployment. If your cluster uses a custom gateway deployment (e.g. Istio Gateway API, or a differently labeled gateway), update this selector.

### 4.2 AuthorizationPolicy Principals

The `AuthorizationPolicy` (`k8s/components/istio-routing/authorizationpolicy.yaml`) hardcodes two mTLS principals:

```yaml
# Ingress gateway identity:
principals:
  - "cluster.local/ns/istio-system/sa/istio-ingressgateway-service-account"

# Prometheus scraper identity:
principals:
  - "cluster.local/ns/monitoring/sa/prometheus-k8s"
```

**You must verify** these match your cluster:

```bash
# Check Istio trust domain (default: cluster.local)
kubectl -n istio-system get cm istio -o jsonpath='{.data.mesh}' | grep trustDomain

# Check ingress gateway service account name
kubectl -n istio-system get sa -l istio=ingressgateway

# Check Prometheus service account (if using AuthZ for metrics)
kubectl -n monitoring get sa | grep prometheus
```

If any of these differ (different trust domain, namespace, or SA name), update the `principals` list accordingly.

### 4.3 PeerAuthentication — Namespace-Wide STRICT mTLS

> [!CAUTION]
> The `PeerAuthentication` resource (`k8s/components/istio-routing/peerauthentication.yaml`) enforces **STRICT mTLS for the entire `shipshape` namespace** — not just helloworld pods.

This means:
- ✅ All service-to-service traffic within the namespace is encrypted and authenticated
- ❌ **Any existing non-mesh workloads** in the `shipshape` namespace that receive plaintext traffic will **break immediately**
- ❌ Any external clients connecting directly to pods (bypassing the mesh) will be rejected

If you need to co-locate non-mesh workloads, either:
- Use `PERMISSIVE` mode initially and migrate to `STRICT`
- Add a pod-level `PeerAuthentication` override for specific workloads

### 4.4 EnvoyFilter Rate Limiting

The EnvoyFilter (`k8s/istio-ingress/ratelimit-envoyfilter.yaml`) is deployed to the `istio-system` namespace and applies to all pods with label `istio: ingressgateway`. It:
- Inserts a local rate limit HTTP filter **on all gateway listeners**
- Scopes rate limit enforcement to specific vhost names (must match Gateway hostnames with `:443`)
- Default: 120 requests/minute per host per gateway pod

### 4.5 VirtualService Export Scope

VirtualServices are configured with:
```yaml
exportTo:
  - "."            # current namespace
  - "istio-system" # ingress gateway namespace
```
This limits route visibility and prevents conflicts with services in other namespaces.

---

## 5. Controller API Server Egress

> [!IMPORTANT]
> **This is the most critical configuration item.** The controller's `NetworkPolicy` uses a deliberately unreachable placeholder CIDR that **blocks all API server access by default**.

### The problem

In `k8s/controller/networkpolicy-controller.yaml`:
```yaml
egress:
  - to:
      - ipBlock:
          cidr: 127.255.255.255/32  # ← intentional deny-by-default placeholder
    ports:
      - protocol: TCP
        port: 443
```

**The controller cannot reach the Kubernetes API server until this is replaced.**

### The solution

1. **Find your API server endpoint:**
   ```bash
   kubectl cluster-info | grep "Kubernetes control plane"
   # or:
   kubectl get endpoints kubernetes -o jsonpath='{.subsets[0].addresses[0].ip}'
   ```

2. **Choose a provider-specific patch** from `examples/controller-egress/`:

   | Provider | Patch File |
   |---|---|
   | Amazon EKS | `examples/controller-egress/eks.patch.yaml` |
   | Google GKE | `examples/controller-egress/gke.patch.yaml` |
   | Azure AKS | `examples/controller-egress/aks.patch.yaml` |
   | Generic | `examples/controller-apiserver-cidr-patch.yaml` |

3. **Edit the chosen patch** to include your real API server CIDRs:
   ```yaml
   - to:
       - ipBlock:
           cidr: <YOUR-API-SERVER-IP>/32
     ports:
       - protocol: TCP
         port: 443
   ```

4. **Apply as a Kustomize patch** or directly via `kubectl apply -f`.

### Local development

The Kind E2E script and Tilt workflows handle this automatically — the local API server is accessible without the restrictive NetworkPolicy because Kind's CNI does not enforce NetworkPolicy by default.

---

## 6. Monitoring Stack Dependencies

### Required CRDs

The deployment includes `ServiceMonitor` and `PrometheusRule` resources that require the **Prometheus Operator** CRDs:

| CRD | Used In |
|---|---|
| `servicemonitors.monitoring.coreos.com` | `k8s/monitoring/servicemonitor.yaml`, `k8s/controller/servicemonitor.yaml` |
| `prometheusrules.monitoring.coreos.com` | `k8s/monitoring/prometheusrule.yaml`, `k8s/controller/prometheusrule.yaml` |

```bash
# Verify CRDs exist
kubectl get crd servicemonitors.monitoring.coreos.com
kubectl get crd prometheusrules.monitoring.coreos.com
```

### If CRDs are not available

Remove the monitoring resources from the kustomization files:

**`k8s/monitoring/kustomization.yaml`** — remove `servicemonitor.yaml` and `prometheusrule.yaml` from `resources:`

**`k8s/controller/kustomization.yaml`** — remove `servicemonitor.yaml` and `prometheusrule.yaml` from `resources:`

Then skip `kubectl apply -k k8s/monitoring` during deployment.

### Prometheus label selectors

Ensure your Prometheus instance is configured to discover `ServiceMonitor` resources in the `shipshape` namespace. This typically requires matching `serviceMonitorNamespaceSelector` and/or `serviceMonitorSelector` labels on the Prometheus CR.

### Hardcoded assumptions in alert rules

The `PrometheusRule` resources contain PromQL queries with hardcoded values:

| Hardcoded Value | Used In | Purpose |
|---|---|---|
| `namespace="shipshape"` | All infrastructure alerts | Scopes to this namespace |
| `horizontalpodautoscaler="helloworld-prod"` | HPA near-max alert | Matches suffixed HPA name |
| `poddisruptionbudget="helloworld-prod"` | PDB disruption alerts | Matches suffixed PDB name |
| `request_host="prod.helloworld.shipshape.example.com"` | Istio gateway alerts | Must match actual prod hostname |

---

## 7. Namespace Ownership & Resource Quotas

### Namespace creation

The namespace manifest (`k8s/namespace/namespace.yaml`) creates:
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: shipshape
  labels:
    app: helloworld
    istio-injection: enabled
```

- `kubectl apply` performs a **merge** — existing labels are preserved, these labels are added.
- The `istio-injection: enabled` label enables **automatic sidecar injection** for all pods in the namespace.

### ResourceQuota impact

> [!WARNING]
> The ResourceQuota is **namespace-wide** and shared across all workloads in `shipshape`.

| Resource | Limit |
|---|---|
| `requests.cpu` | 4 cores |
| `requests.memory` | 4Gi |
| `limits.cpu` | 8 cores |
| `limits.memory` | 8Gi |
| `pods` | 30 |

**This deployment alone consumes** (at baseline):

| Component | Pods | CPU Requests | Memory Requests |
|---|---|---|---|
| App test | 1 | 50m | 96Mi |
| App prod | 3 | 450m | 576Mi |
| Controller | 2 | 100m | 192Mi |
| **Total baseline** | **6** | **600m** | **864Mi** |
| **With HPA max (prod=10)** | **13** | **1650m** | **2208Mi** |

If other workloads share this namespace, verify sufficient headroom:
```bash
kubectl -n shipshape describe resourcequota shipshape-quota
```

### LimitRange defaults

The `LimitRange` sets default resource requests/limits for containers that don't specify their own:
- Default request: `100m CPU / 128Mi memory`
- Default limit: `500m CPU / 512Mi memory`

This applies to **all containers** in the namespace, including **Istio sidecars**. The deployment manifests explicitly set sidecar proxy resources via pod annotations to prevent the LimitRange from applying heavy defaults:
```yaml
sidecar.istio.io/proxyCPU: "50m"
sidecar.istio.io/proxyMemory: "64Mi"
```

---

## 8. Resource Sizing

| Component | Test Overlay | Prod Overlay | Notes |
|---|---|---|---|
| **App replicas** | 1 | 3 (HPA: 2–10) | Test uses `ScheduleAnyway`; prod uses `DoNotSchedule` topology spread |
| **App CPU** | 50m–200m | 150m–500m | |
| **App Memory** | 96Mi–192Mi | 192Mi–384Mi | |
| **Controller replicas** | 2 | 2 | Leader election ensures only one is active |
| **Controller CPU** | 50m–200m | 50m–200m | No separate prod overlay |
| **Controller Memory** | 96Mi–256Mi | 96Mi–256Mi | |

### HPA prerequisite

The prod HPA uses `Resource` metrics (`cpu` and `memory` utilization), which requires **metrics-server** to be installed:
```bash
kubectl get apiservice v1beta1.metrics.k8s.io
```

---

## 9. Cluster Compatibility Checklist

Run this checklist before first deployment:

### Infrastructure prerequisites

- [ ] **Kubernetes** ≥ 1.26
- [ ] **Istio** installed with sidecar injection support
  - [ ] Ingress gateway deployed with label `istio: ingressgateway` in `istio-system`
  - [ ] CRDs: `gateways`, `virtualservices`, `destinationrules`, `peerauthentications`, `authorizationpolicies`, `envoyfilters`
- [ ] **cert-manager** installed
  - [ ] CRD: `certificates.cert-manager.io`
  - [ ] `ClusterIssuer` `letsencrypt-staging` exists (or equivalent for test)
  - [ ] `ClusterIssuer` `letsencrypt-prod` exists (or equivalent for prod)
- [ ] **metrics-server** installed (required for prod HPA)
- [ ] **Prometheus Operator** CRDs installed (optional — can be removed)
  - [ ] `servicemonitors.monitoring.coreos.com`
  - [ ] `prometheusrules.monitoring.coreos.com`

### Configuration prerequisites

- [ ] DNS records configured for test and prod hostnames
- [ ] Container images accessible from cluster nodes
- [ ] API server CIDR identified for controller NetworkPolicy
- [ ] AuthorizationPolicy principals match cluster's Istio trust domain and SA names
- [ ] No conflicting PeerAuthentication for the `shipshape` namespace
- [ ] No existing workloads in `shipshape` that require plaintext traffic

### Automated validation

```bash
# Verify all required CRDs
for crd in gateways.networking.istio.io virtualservices.networking.istio.io \
  destinationrules.networking.istio.io certificates.cert-manager.io \
  peerauthentications.security.istio.io authorizationpolicies.security.istio.io \
  envoyfilters.networking.istio.io servicemonitors.monitoring.coreos.com \
  prometheusrules.monitoring.coreos.com; do
  kubectl get crd "$crd" -o name 2>/dev/null \
    && echo "✅ $crd" \
    || echo "❌ $crd MISSING"
done

# Verify ClusterIssuers
kubectl get clusterissuer letsencrypt-staging letsencrypt-prod 2>/dev/null

# Verify Istio ingress gateway
kubectl -n istio-system get deploy -l istio=ingressgateway

# Verify metrics-server
kubectl get apiservice v1beta1.metrics.k8s.io

# Check namespace state (if pre-existing)
kubectl get ns shipshape 2>/dev/null && \
  echo "⚠️  Namespace exists — review resources before applying" || \
  echo "✅ Namespace does not exist"
```

---

## 10. Adaptation Steps

### Step-by-step for a new cluster deployment

**1. Replace hostnames** in the `app-vars.yaml` files and additional files listed in [Section 2](#2-dns-hostname-configuration).

**2. Set ClusterIssuer names** in:
- `k8s/overlays/test/app-vars.yaml` → `data.CLUSTER_ISSUER`
- `k8s/overlays/prod/app-vars.yaml` → `data.CLUSTER_ISSUER`

**3. Patch controller egress** — choose and customize a patch from `examples/controller-egress/`:
```bash
# Find API server IP
kubectl get endpoints kubernetes -o jsonpath='{.subsets[0].addresses[0].ip}'

# Edit chosen patch with real CIDR, then apply
kubectl apply -f examples/controller-egress/eks.patch.yaml
```

**4. Update AuthorizationPolicy principals** in `k8s/components/istio-routing/authorizationpolicy.yaml` if:
- Trust domain ≠ `cluster.local`
- Ingress gateway namespace ≠ `istio-system`
- Ingress gateway SA ≠ `istio-ingressgateway-service-account`
- Monitoring namespace ≠ `monitoring`
- Prometheus SA ≠ `prometheus-k8s`

**5. Update monitoring alert queries** if hostnames changed (see `k8s/monitoring/prometheusrule.yaml`).

**6. Remove monitoring resources** if Prometheus Operator CRDs are not available:
- Remove `servicemonitor.yaml` and `prometheusrule.yaml` from `k8s/monitoring/kustomization.yaml`
- Remove `servicemonitor.yaml` and `prometheusrule.yaml` from `k8s/controller/kustomization.yaml`
- Skip `kubectl apply -k k8s/monitoring` during deployment

**7. Configure imagePullSecrets** if images are in a private registry.

**8. Adjust ResourceQuota** in `k8s/namespace/resourcequota.yaml` if namespace is shared.

---

## 11. Validation & Rollback

### Pre-apply dry run

```bash
kubectl apply -k k8s/namespace          --dry-run=server
kubectl apply -k k8s/istio-ingress      --dry-run=server
kubectl apply -k k8s/overlays/test      --dry-run=server
kubectl apply -k k8s/overlays/prod      --dry-run=server
kubectl apply -k k8s/monitoring         --dry-run=server
kubectl apply -k k8s/controller         --dry-run=server
```

### Deployment order

Apply in this order (dependencies first):
```bash
kubectl apply -k k8s/namespace
kubectl apply -k k8s/istio-ingress
kubectl apply -k k8s/overlays/test
kubectl apply -k k8s/overlays/prod
kubectl apply -k k8s/monitoring
kubectl apply -k k8s/controller
```

### Post-deploy verification

```bash
# Verify all pods are running
kubectl -n shipshape get pods

# Verify services and labels
kubectl -n shipshape get deploy,svc,cm -l app=helloworld --show-labels

# Test app response (via port-forward)
kubectl -n shipshape port-forward svc/helloworld-test 8000:80 &
curl -s http://localhost:8000/

# Verify controller is watching
kubectl -n shipshape logs -l app=helloworld-controller --tail=20
```

### Rollback procedures

```bash
# Rollback a specific deployment
kubectl -n shipshape rollout undo deployment/helloworld-test
kubectl -n shipshape rollout undo deployment/helloworld-prod
kubectl -n shipshape rollout undo deployment/helloworld-controller

# Full teardown (reverse order)
kubectl delete -k k8s/controller
kubectl delete -k k8s/monitoring
kubectl delete -k k8s/overlays/prod
kubectl delete -k k8s/overlays/test
kubectl delete -k k8s/istio-ingress
kubectl delete -k k8s/namespace   # ⚠️ deletes namespace and ALL contents
```

> [!CAUTION]
> Deleting the namespace removes **everything** inside it, including any resources not managed by this repository.
