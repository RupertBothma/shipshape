from __future__ import annotations

from dataclasses import dataclass, field

from prometheus_client import Counter, Gauge, Histogram, Info


@dataclass(frozen=True)
class ControllerMetrics:
    """Prometheus metrics exported by the controller on ``/metrics``.

    All counters use an ``env`` label so operators can alert on per-environment
    restart rates and error budgets independently.
    """

    restarts_total: Counter = field(
        default_factory=lambda: Counter(
            "configmap_reload_restarts_total",
            "Total deployment restarts triggered by ConfigMap changes",
            ["env"],
        )
    )
    errors_total: Counter = field(
        default_factory=lambda: Counter(
            "configmap_reload_errors_total",
            "Total deployment restart errors",
            ["env"],
        )
    )
    debounced_total: Counter = field(
        default_factory=lambda: Counter(
            "configmap_reload_debounced_total",
            "Total ConfigMap events suppressed by debounce",
            ["env"],
        )
    )
    watch_errors_total: Counter = field(
        default_factory=lambda: Counter(
            "configmap_reload_watch_errors_total",
            "Total Kubernetes watch errors",
        )
    )
    watch_reconnects_total: Counter = field(
        default_factory=lambda: Counter(
            "configmap_reload_watch_reconnects_total",
            "Total watch stream reconnects after the initial connection",
        )
    )
    leader_transitions_total: Counter = field(
        default_factory=lambda: Counter(
            "configmap_reload_leader_transitions_total",
            "Total leadership state transitions",
            ["transition"],
        )
    )
    leader_state: Gauge = field(
        default_factory=lambda: Gauge(
            "configmap_reload_leader_state",
            "Whether this controller replica is currently leader (1=yes, 0=no)",
        )
    )
    leader_acquire_latency_seconds: Histogram = field(
        default_factory=lambda: Histogram(
            "configmap_reload_leader_acquire_latency_seconds",
            "Seconds spent waiting to acquire leadership",
            buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, float("inf")),
        )
    )
    pending_restarts: Gauge = field(
        default_factory=lambda: Gauge(
            "configmap_reload_pending_restarts",
            "Current number of debounced restarts waiting to be processed",
        )
    )
    retry_total: Counter = field(
        default_factory=lambda: Counter(
            "configmap_reload_retry_total",
            "Total restart retry attempts scheduled after failed patch operations",
            ["env"],
        )
    )
    dropped_restarts_total: Counter = field(
        default_factory=lambda: Counter(
            "configmap_reload_dropped_restarts_total",
            "Total pending restarts dropped on shutdown",
        )
    )
    build_info: Info = field(
        default_factory=lambda: Info(
            "configmap_reload",
            "Build information for the controller",
        )
    )


METRICS = ControllerMetrics()
