# Chaos Drills

Use these drills in a non-production cluster to validate resilience claims before production sign-off.

## Safety Rules
- Run drills in `test` first.
- Keep a terminal with `kubectl get pods -w` open.
- Set a hard stop timer (15 minutes) and rollback immediately if exceeded.
- Capture timestamps for start, impact observed, mitigation started, and recovery complete.

## Drill 1: Controller API Server Unavailable

### Goal
Validate controller behavior when Kubernetes API access is interrupted.

### Inject
Apply a temporary NetworkPolicy in `shipshape` that blocks controller egress to TCP/443.

### Observe
- `configmap_reload_watch_errors_total` increases.
- Controller `/readyz` may remain `200` briefly while the leader is still within
  `LEADER_ELECTION_RENEW_DEADLINE_SECONDS`, then returns `503` after leadership loss.
- `configmap_reload_leader_transitions_total` does not spike uncontrollably.

### Success Criteria
- Controller resumes watch without restart storms after policy rollback.
- No unexpected restarts in unaffected environment.

### Rollback
Delete the temporary blocking NetworkPolicy and verify:
```bash
kubectl -n shipshape get pods -l app=helloworld-controller
kubectl -n shipshape logs deploy/helloworld-controller --tail=100
```

## Drill 2: Pod Eviction Storm (App)

### Goal
Validate availability and rollout behavior during rapid pod evictions.

### Inject
Force-delete app pods in one environment repeatedly:
```bash
kubectl -n shipshape delete pod -l app=helloworld,env=test --grace-period=0 --force
```

### Observe
- Service remains reachable (`/healthz` and `/` succeed via ingress host).
- P95 latency may spike briefly but returns below SLO threshold.
- HPA/PDB behavior aligns with configured min availability.

### Success Criteria
- No prolonged outage (>2 minutes) for the tested host.
- New pods become ready and traffic stabilizes.

### Rollback
Stop pod deletion and wait for:
```bash
kubectl -n shipshape rollout status deploy/helloworld-test
```

## Drill 3: Ingress-to-App Partition

### Goal
Validate behavior when ingress cannot reach backend workloads.

### Inject
Apply a temporary deny ingress policy for traffic from `istio-system` to app pods.

### Observe
- `IstioGateway5xxRate` alert expression rises.
- Ingress requests return 5xx.
- Application pod health checks remain healthy (problem is path, not pod crash).

### Success Criteria
- Alert fires within expected window.
- Recovery is immediate after policy rollback.

### Rollback
Remove temporary deny policy and verify host checks:
```bash
curl -H "Host: test.helloworld.shipshape.example.com" https://<INGRESS_IP>/
curl -H "Host: prod.helloworld.shipshape.example.com" https://<INGRESS_IP>/
```

## Evidence Template
Record each run with:
- Cluster and environment
- Drill name
- Start/end timestamps
- Expected signals observed (yes/no)
- Recovery duration
- Follow-up actions
