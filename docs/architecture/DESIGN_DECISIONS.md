# Design Decisions

This document captures the rationale behind key architectural choices so
future maintainers understand *why* the system is shaped the way it is, not
just *how* it works.

Accepted decisions are also tracked as immutable ADRs in `docs/ADR/`:
- `0001-single-namespace-with-name-suffix.md`
- `0002-configmap-data-hash-restarts.md`
- `0003-debounce-configmap-restarts.md`
- `0004-lease-based-leader-election.md`
- `0005-force-pending-restarts-on-shutdown.md`

Historical note: ADR-0003 remains accepted for debounce/coalescing behavior,
but its earlier "pending restarts can be dropped on shutdown" consequence is
superseded-in-part by ADR-0005.

## 1. Single Namespace with `nameSuffix` (vs Separate Namespaces)

**Decision:** Deploy both `test` and `prod` environments in the same
`shipshape` namespace, differentiating resources with Kustomize `nameSuffix`
(`-test`, `-prod`) and `env` labels.

**Alternatives considered:**
- Separate namespaces (`shipshape-test`, `shipshape-prod`).
- Separate clusters.

**Rationale:**
- The design requires "the same cluster and the same namespace".
- `nameSuffix` is applied by Kustomize to *all* resource names (Deployment,
  Service, ConfigMap, Gateway, VirtualService, Certificate), preventing name
  collisions without duplicating base manifests.
- The `env` label on every resource enables label-scoped RBAC, NetworkPolicy,
  and monitoring queries.
- A single controller instance can watch both environments with one label
  selector (`app=helloworld`) and use the `env` label to scope restarts.

**Trade-offs:**
- Blast radius: a namespace-level misconfiguration affects both environments.
  In a real production setup, separate namespaces or clusters would provide
  stronger isolation.
- RBAC granularity: namespace-scoped Roles grant the controller access to
  *both* environments' ConfigMaps and Deployments.

## 2. Data Hashing (SHA-256) for Change Detection

**Decision:** Compute a SHA-256 hash of `ConfigMap.data` to detect meaningful
changes, rather than relying on `metadata.resourceVersion`.

**Rationale:**
- `resourceVersion` changes on *any* mutation to the object, including
  label/annotation edits, ownership updates, and `kubectl apply` no-ops.
  These metadata-only changes should not trigger application restarts.
- Hashing only the `data` field ensures restarts happen if and only if the
  application-visible configuration changes.
- The hash is computed from a deterministic JSON serialization
  (`json.dumps(data, sort_keys=True)`) so key ordering in the Kubernetes API
  response does not affect the result.

**Trade-off:**
- A small CPU cost per event for serialization and hashing, which is
  negligible for ConfigMap-sized payloads.

## 3. Debounce and Coalescing Strategy

**Decision:** Apply a configurable per-key debounce window (default 5 seconds)
that coalesces rapid ConfigMap changes into a single restart.

**Problem solved:**
- CI/CD pipelines or operators may update multiple ConfigMap keys in quick
  succession (e.g. `kubectl patch` followed by another `kubectl patch`).
  Without debouncing, each change triggers an independent rolling restart,
  creating unnecessary churn and potential availability impact.

**How it works:**
1. When a restart completes, the monotonic timestamp is recorded in
   `_last_restart[(env, configmap_name)]`.
2. On the next change event, `_debounce_remaining()` checks whether enough
   time has elapsed.  If not, the restart is deferred into
   `_pending_restarts` with a due-at timestamp.
3. The watch loop's timeout is shortened to wake up in time to drain pending
   restarts.
4. If additional changes arrive during the window, the due-at is pushed
   *forward* (never earlier), ensuring the final restart reflects all changes.

**Why monotonic time:**
- `time.monotonic()` is immune to NTP adjustments and wall-clock jumps,
  critical for correct debounce behaviour on nodes where `ntpd`/`chrony`
  may step the clock.

## 4. Leader Election via Lease API

**Decision:** Use `coordination.k8s.io/v1` Lease objects for leader election,
running two controller replicas with only the leader actively watching and
restarting.

**Rationale:**
- Multiple controllers patching the same Deployment simultaneously would
  create race conditions and amplify restart storms.
- The Lease API is the Kubernetes-native mechanism for leader election,
  avoiding external dependencies (etcd locks, Redis, etc.).
- A 15-second lease duration with a 2-second retry period balances fast
  failover (~15 s worst case) against API server load.

**Failover behaviour:**
1. Leader renews the lease every 2 seconds.
2. If the leader pod is evicted or partitioned, it stops renewing.
3. After `leaseDurationSeconds` (15 s) elapses past the last `renewTime`,
   the standby replica acquires the lease and starts the watch loop.
4. `on_stopped_leading` stops the old watch loop thread cleanly via
   `threading.Event`.

## 5. NetworkPolicy Design

**Decision:** Apply default-deny-style NetworkPolicies to both the helloworld
app pods and the controller pods.

**App pods (`k8s/base/networkpolicy.yaml`):**
- **Ingress:** From the Istio ingress gateway identity (namespace+pod selector)
  and monitoring namespace on port 8000.
- **Egress:** DNS + Istio control-plane ports (15010/15012) required by
  sidecars.

**Controller pods (`k8s/controller/networkpolicy-controller.yaml`):**
- **Ingress:** Only from the monitoring namespace on port 8080 (Prometheus
  scrapes).
- **Egress:** DNS + Kubernetes API server on port 443.

**Rationale:**
- Limits lateral movement if a pod is compromised.
- The app's narrow egress allow-list is particularly important because
  the container runs third-party Python dependencies.

## 6. Istio Host-Based Routing and TLS

**Decision:** Use separate Istio Gateways and VirtualServices per environment,
distinguished by hostname (`test.helloworld.shipshape.example.com` and
`prod.helloworld.shipshape.example.com`).

**Rationale:**
- Host-based routing provides clean URL separation without path-prefix
  complexity.
- Each environment gets its own TLS certificate (via cert-manager), so
  certificate rotation in test does not affect prod.
- `exportTo: ["."]` scopes VirtualServices to the local namespace, preventing
  route conflicts with other teams' services.
- Gateway `tls.httpsRedirect: true` on port 80 ensures no plaintext traffic
  reaches the app.

**Local development considerations:**
- Local clusters use self-signed certificates and `/etc/hosts` entries.
- The E2E scripts (`hack/e2e-kind.sh`, `hack/e2e-minikube.sh`) create
  short-lived TLS secrets to avoid cert-manager dependency during testing.

## 7. Python 3.14 Runtime Alignment

**Decision:** Standardize on Python 3.14 across runtime images, CI, lint/type
settings, and local dev shell.

**Rationale:**
- Avoids runtime-only bugs caused by version skew between CI and production.
- Keeps type-checking and lint target versions aligned with deployed code.
- Simplifies contributor setup and incident triage by using one interpreter
  version across environments.

**Trade-off:**
- Requires a nixpkgs channel that includes Python 3.14, which can introduce
  occasional package-version churn compared to older pinned channels.

## 8. FastAPI as the Application Framework

**Decision:** Use FastAPI for the helloworld HTTP service.

**Alternatives considered:**
- Flask — mature, but lacks built-in async support and type-driven validation.
- Starlette directly — lighter, but FastAPI adds OpenAPI docs and dependency
  injection with minimal overhead.
- aiohttp — more control over the event loop, but heavier boilerplate.

**Rationale:**
- FastAPI's automatic OpenAPI schema generation aligns with API-first design.
- Native async support pairs well with Uvicorn ASGI.
- Type hints + Pydantic validation reduce boilerplate for request/response
  handling (relevant if the service grows beyond a single endpoint).
- Strong community and documentation make onboarding faster.

**Trade-off:**
- Heavier dependency tree than plain Starlette for a single-endpoint service.

## 9. Prometheus + ServiceMonitor (vs Alternatives)

**Decision:** Use Prometheus with the Prometheus Operator's `ServiceMonitor`
and `PrometheusRule` CRDs for metrics collection and alerting.

**Alternatives considered:**
- Datadog / New Relic — SaaS observability platforms.
- OpenTelemetry Collector — vendor-neutral metrics pipeline.
- VictoriaMetrics — Prometheus-compatible, higher cardinality handling.

**Rationale:**
- Prometheus is the de facto standard in Kubernetes ecosystems.
- The Prometheus Operator's CRDs (`ServiceMonitor`, `PrometheusRule`) provide
  declarative, GitOps-friendly configuration.
- No external vendor dependency or licensing cost.
- PromQL is widely understood by platform teams.

**Trade-off:**
- Requires Prometheus Operator CRDs to be installed on the cluster.
- No built-in distributed tracing (addressed separately with OpenTelemetry).

## 10. Custom Controller (vs Stakater Reloader)

**Decision:** Build a custom Python controller rather than deploying an
off-the-shelf reloader like Stakater Reloader.

**Alternatives considered:**
- [Stakater Reloader](https://github.com/stakater/Reloader) — annotation-driven,
  supports ConfigMaps and Secrets.
- [configmap-reload sidecar](https://github.com/jimmidyson/configmap-reload) —
  volume-mount based, fires a webhook on change.

**Rationale:**
- The project includes a custom controller.
- A custom controller allows environment-scoped restarts (only restart
  deployments matching the same `env` label as the changed ConfigMap).
- Debounce/coalescing logic is tailored to the project's specific needs.
- Leader election ensures exactly-once semantics across replicas.

**Trade-off:**
- Higher maintenance burden than a well-maintained community project.
- In production, Stakater Reloader would be a reasonable choice if the
  custom scoping requirements can be met with annotations.

## 11. Observability: OpenTelemetry Is Available but Disabled by Default

**Decision:** Provide OpenTelemetry instrumentation behind an explicit
`OTEL_ENABLED=true` toggle, and keep tracing disabled by default.

**Rationale:**
- The current service remains simple and metrics-first operation is sufficient
  for most incidents.
- A toggle allows staged rollout (test first, then prod) without changing
  application code.
- The opt-in model avoids introducing tracing overhead in environments that do
  not yet run an OTLP collector and trace backend.

**Operational posture:**
- Enable tracing only where collector and backend readiness is validated.
- Keep trace data retention and sampling policies managed by the platform
  observability stack (collector/backend), not hard-coded in the app.

## 12. Alert Signal Dependencies Are Explicit Production Prerequisites

**Decision:** Treat alert metric sources as required production dependencies
and document validation checks before go-live.

**Required dependencies:**
- Prometheus Operator CRDs (`ServiceMonitor`, `PrometheusRule`)
- kube-state-metrics (for pod restart state metrics)
- cAdvisor/kubelet metrics pipeline (for container memory metrics)
- cert-manager metrics endpoint (for certificate expiry alerts)
- Istio telemetry (`istio_requests_total`) for ingress-level SLO alerts

**Rationale:**
- Alert rules are only trustworthy when all source metrics are present.
- Missing telemetry creates silent blind spots and false confidence during
  incidents.
- Explicit preflight validation reduces pager noise and missing-page failure
  modes after deployment.

## 13. Runbook URLs and Release Artifacts Must Be Production-Ready

**Decision:** Production manifests must not ship placeholders for
`runbook_url`, security contacts, or deployment image references.

**Hard requirements:**
- Every `PrometheusRule` alert uses a valid, reachable runbook URL in the
  canonical repository.
- Security reporting metadata points to a real policy and private reporting
  channel.
- Deployment images for production paths are digest-pinned (`image@sha256:...`)
  and enforced in CI.

**Rationale:**
- Broken runbook links increase MTTR during incidents.
- Placeholder vulnerability contacts block external disclosure workflows.
- Mutable tags weaken supply-chain integrity and rollback determinism.

## 14. Pending Restart Intents Must Survive Leadership Handoff

**Decision:** On shutdown or leadership handoff, force-process all pending
debounced restart intents instead of dropping not-yet-due entries.

**Rationale:**
- A queued restart represents a real observed ConfigMap data change.
- Dropping that intent on leader stop can leave workloads on stale config
  indefinitely if no subsequent ConfigMap mutation occurs.
- Executing early on shutdown is safer than silently losing reconciliation
  intent.

**Trade-off:**
- Rarely, a restart may occur earlier than the configured debounce interval
  when the active leader exits.

## 15. App Graceful Shutdown via Uvicorn's SIGTERM Handling

**Decision:** Rely on uvicorn's built-in SIGTERM handler for graceful shutdown
of the FastAPI app, rather than implementing custom signal handling.

**Rationale:**
- The app is a stateless HTTP service with no long-running background tasks,
  persistent connections, or in-flight work that requires explicit draining
  beyond what the ASGI server already provides.
- Uvicorn handles SIGTERM by stopping the event loop after in-flight requests
  complete, which is the correct behavior for a stateless service behind a
  load balancer.
- Kubernetes sends SIGTERM, waits `terminationGracePeriodSeconds` (default
  30 s), then sends SIGKILL. Uvicorn's shutdown is well within that window.
- The readiness probe (`/readyz`) fails once the process begins shutting down,
  causing the Service to remove the pod from endpoints before connections drain.

**Contrast with controller:**
- The controller requires explicit shutdown orchestration (signal the watch
  thread, flush pending restarts, release the leader lease) because it holds
  mutable state. The app does not.

## 16. Readiness Probe Reports Startup Config Without Re-Verification

**Decision:** The `/readyz` endpoint reports the configuration source detected
at startup (`env` or `configmap`) without re-querying the Kubernetes API to
verify ConfigMap availability on each probe.

**Rationale:**
- Environment variables injected from a ConfigMap via `envFrom` are immutable
  for the lifetime of a container. Once the container starts, the values cannot
  change — even if the source ConfigMap is deleted or modified. Re-checking the
  ConfigMap would report a false negative (unready) when the app is actually
  serving correctly with its existing configuration.
- The controller's watch loop is responsible for triggering a rolling restart
  when ConfigMap data changes, at which point new pods receive fresh env vars.
- Keeping the probe lightweight (no API calls, no I/O) ensures it responds
  within the `timeoutSeconds` budget and does not contribute to API server load
  under high replica counts.
