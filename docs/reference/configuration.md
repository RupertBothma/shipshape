# Configuration Reference

Authoritative environment variable reference for application and controller runtimes.

## App (`app/src/main.py`, `app/src/config.py`)

| Variable | Default | Required | Valid values / range | Operational impact |
|---|---|---|---|---|
| `MESSAGE` | none | Yes in cluster (`ALLOW_MESSAGE_FALLBACK=false`) | Non-empty string | Response body for `GET /`; missing value fails startup unless fallback is enabled. |
| `ALLOW_MESSAGE_FALLBACK` | `false` | No | Boolean (`true/false`, `1/0`, `yes/no`, `on/off`) | Allows local-dev fallback message when `MESSAGE` is absent. Should remain `false` in production. |
| `LOG_LEVEL` | `INFO` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | Controls app log verbosity. |
| `OTEL_ENABLED` | `false` | No | Boolean | Enables OpenTelemetry instrumentation for FastAPI + `requests`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector.monitoring.svc:4318` | No | HTTP(S) URL | Base OTLP endpoint used when trace-specific endpoint is not set. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | none | No | HTTP(S) URL ending with OTLP trace path | Overrides trace export endpoint directly. |
| `OTEL_SERVICE_NAME` | `helloworld` | No | Non-empty string | `service.name` resource attribute in traces. |
| `OTEL_SERVICE_NAMESPACE` | `shipshape` | No | Non-empty string | `service.namespace` resource attribute in traces. |

## Controller (`controller/src/__main__.py`, `controller/src/controller.py`, `controller/src/leader.py`)

| Variable | Default | Required | Valid values / range | Operational impact |
|---|---|---|---|---|
| `WATCH_NAMESPACE` | `shipshape` | No | Non-empty string | Namespace watched for ConfigMap events and deployment restarts. |
| `APP_SELECTOR` | `app=helloworld` | No | Kubernetes selector string with at least one `key=value` pair | Filters watched ConfigMaps and target deployments. Invalid selector fails startup. |
| `ROLLOUT_ANNOTATION_KEY` | `shipshape.io/restartedAt` | No | Non-empty string | Annotation key patched onto deployments to trigger rolling restart. |
| `DEBOUNCE_SECONDS` | `5` | No | Integer `>= 0` | Coalesces rapid ConfigMap updates before restart. |
| `LOG_LEVEL` | `INFO` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | Controller log verbosity. |
| `HEALTH_PORT` | `8080` | No | Integer `1-65535` | Health/metrics HTTP bind port. |
| `LEADER_ELECTION_ENABLED` | `true` | No | Boolean | Enables lease-based active/standby controller behavior. |
| `LEADER_ELECTION_LEASE_NAME` | `helloworld-controller-leader` | No | Non-empty string | Lease object name used for coordination. |
| `LEADER_ELECTION_IDENTITY` | `HOSTNAME` (fallback `POD_NAME`, then `unknown`) | No | Non-empty string | Unique identity used as lease holder. |
| `LEADER_ELECTION_LEASE_DURATION_SECONDS` | `15` | No | Integer `>= 1` | Max stale-leader hold window. |
| `LEADER_ELECTION_RENEW_DEADLINE_SECONDS` | `10` | No | Integer `>= 1` and `< LEADER_ELECTION_LEASE_DURATION_SECONDS` | Renewal failure tolerance before leadership loss. |
| `LEADER_ELECTION_RETRY_PERIOD_SECONDS` | `2` | No | Integer `>= 1` and `< LEADER_ELECTION_RENEW_DEADLINE_SECONDS` | Lease retry interval. |
| `LEADER_ELECTION_CONTROLLER_STOP_TIMEOUT_SECONDS` | `45` | No | Integer `>= 1` | Max wait for watch loop stop during leadership handoff before process shutdown; keep below pod `terminationGracePeriodSeconds` (manifest default: 60s). |
| `APP_VERSION` | `0.2.2` | No | Semver (`X.Y.Z`) | Exported in controller build info metrics; validated against `pyproject.toml` by `hack/validate_release_metadata.py`. |
| `GIT_SHA` | `unknown` | No | Git SHA string | Exported in controller build info metrics. |

## Operational Notes

- `hack/validate_release_metadata.py` enforces version coherence between:
  - `pyproject.toml` project version
  - `CHANGELOG.md` latest release heading
  - runtime constants in `app/src/main.py` and `controller/src/__main__.py`
  - signed release tag version (when provided)
- Logging redacts common secret patterns (`token`, `password`, bearer tokens, access-token query params) before serialization.
- Tracing scope is intentionally app-only for the current baseline:
  - app supports `OTEL_*` variables listed above.
  - controller has no `OTEL_*` configuration surface in this repository.
  - controller observability is metrics + structured logs (`/metrics`, `/healthz`, `/readyz`, `/leadz`).
