# Grafana Dashboards

This directory includes importable Grafana dashboard exports plus the PromQL
queries used to build them:
- `app-overview.json`
- `controller-overview.json`

Import in Grafana: **Dashboards -> New -> Import -> Upload JSON file**.

## App Metrics (scraped via `k8s/monitoring/servicemonitor.yaml`)

`app-overview.json` is intentionally production-scoped and includes
`env="prod"` on all app PromQL selectors (the app ServiceMonitor propagates
this from Service labels).

### Request Rate (per second)

```promql
sum(rate(http_requests_total{env="prod",path!="other"}[5m])) by (path, method, status)
```

### Error Rate (5xx ratio)

```promql
sum(rate(http_requests_total{env="prod",status=~"5.."}[5m]))
/
sum(rate(http_requests_total{env="prod"}[5m]))
```

### Latency Percentiles (p50 / p95 / p99)

```promql
histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket{env="prod"}[5m])) by (le))
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{env="prod"}[5m])) by (le))
histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{env="prod"}[5m])) by (le))
```

### Latency by Endpoint

```promql
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{env="prod"}[5m])) by (le, path))
```

### In-Flight Requests

```promql
max(http_in_flight_requests{env="prod"})
```

### Config Load Timestamp

```promql
max(app_config_loaded_timestamp_seconds{env="prod"})
```

### Config Metadata (source + fingerprint + version)

```promql
max by (source, config_fingerprint, app_version) (app_config_loaded_info{env="prod"})
```

### Error Budget Burn Rate

```promql
(sum(rate(http_requests_total{env="prod",status=~"5.."}[5m])) / clamp_min(sum(rate(http_requests_total{env="prod"}[5m])), 0.001)) / 0.001
(sum(rate(http_requests_total{env="prod",status=~"5.."}[1h])) / clamp_min(sum(rate(http_requests_total{env="prod"}[1h])), 0.001)) / 0.001
```

## Controller Metrics (scraped via `k8s/controller/servicemonitor.yaml`)

### Restart Rate by Environment

```promql
sum(rate(configmap_reload_restarts_total[5m])) by (env)
```

### Restart Error Rate by Environment

```promql
sum(rate(configmap_reload_errors_total[5m])) by (env)
```

### Debounced Events by Environment

```promql
sum(rate(configmap_reload_debounced_total[5m])) by (env)
```

### Watch Stream Health

```promql
rate(configmap_reload_watch_errors_total[5m])
rate(configmap_reload_watch_reconnects_total[5m])
```

### Leadership Transitions

```promql
sum(increase(configmap_reload_leader_transitions_total[5m])) by (transition)
```

### Leadership Acquisition Latency (p95)

```promql
histogram_quantile(0.95, sum(rate(configmap_reload_leader_acquire_latency_seconds_bucket[5m])) by (le))
```

### Current Leader State

```promql
max(configmap_reload_leader_state)
```

### Pending Restart Queue Depth

```promql
configmap_reload_pending_restarts
```

### Retry Attempts by Environment

```promql
sum(rate(configmap_reload_retry_total[5m])) by (env)
```

### Leader State Over Time

```promql
max(configmap_reload_leader_state)
```

### Pending Restart Queue Trend

```promql
max(configmap_reload_pending_restarts)
max_over_time(configmap_reload_pending_restarts[15m])
```

## Suggested Dashboard Layout

1. **Row: App Overview** — Request rate, error rate, p95 latency (stat + time series)
2. **Row: App Detail** — Latency by endpoint, status code distribution (time series + pie)
3. **Row: App Saturation & Config** — in-flight requests, config load timestamp, config metadata
4. **Row: Controller** — Restart rate, error rate, debounced events (time series per env)
5. **Row: Controller Health** — Watch errors, reconnects, pending queue depth (time series + stat)
6. **Row: Leadership** — Transition rate, acquisition latency p95, current leader state

## Importing Alerts

The PrometheusRule resources already define alert thresholds:
- `k8s/monitoring/prometheusrule.yaml` — `HelloworldHighErrorRate`, `HelloworldHighLatencyP95`, `HelloworldErrorBudgetBurnFast`, `HelloworldErrorBudgetBurnSlow`, `KubePodCrashLooping`, `KubePodHighMemory`, `CertManagerCertExpiringSoon`, `IstioGateway5xxRate`, `IstioGateway429Saturation`
- `k8s/controller/prometheusrule.yaml` — `ConfigMapReloadErrors`, `ConfigMapReloadWatchDown`, `ConfigMapReloadMetricsAbsent`, `ConfigMapReloadHighRestartRate`, `ConfigMapReloadDroppedRestarts`, `ConfigMapReloadLeaderFlapping`, `ConfigMapReloadNoActiveLeader`, `ConfigMapReloadPendingRestartsStuck`

Configure Alertmanager routes to deliver these to your notification channel
(PagerDuty, Slack, Opsgenie) as described in `docs/operations.md`.
