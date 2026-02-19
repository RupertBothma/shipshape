from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from kubernetes import watch
from kubernetes.client import ApiException, AppsV1Api, CoreV1Api

from controller.src.kube import patch_deployment_restart
from controller.src.metrics import METRICS


@dataclass(frozen=True)
class RestartResult:
    """Immutable record of a single rolling-restart operation.

    Returned by every restart attempt so callers can inspect outcomes
    without querying the Kubernetes API again.
    """

    environment: str
    matched_deployments: int
    restarted: int
    failed: int


def utc_now_rfc3339() -> str:
    """Return the current UTC time as a compact RFC 3339 string (e.g. ``2024-01-15T08:30:00Z``).

    Used as the restart annotation value so Kubernetes sees a template change
    and triggers a rolling update.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ConfigMapReloader:
    """Watches ConfigMaps in a namespace and triggers rolling restarts on data changes.

    The controller uses the Kubernetes watch API to stream ConfigMap events.  It
    maintains a SHA-256 hash of each ConfigMap's ``data`` field keyed by
    ``(env, configmap_name)`` and only triggers a restart when the hash actually
    changes.  This avoids spurious restarts caused by metadata-only updates or
    repeated watch ADDED events during re-list.

    A per-key debounce window (default 5 s) coalesces rapid successive changes
    so that a burst of ``kubectl patch`` calls results in a single restart
    rather than a storm.

    Key internal state:
        ``_last_data_hash``
            Maps ``(env, configmap_name)`` to the last-seen SHA-256 digest.
        ``_last_restart``
            Maps the same key to the ``time.monotonic()`` timestamp of the
            last restart, used for debounce calculation.
        ``_pending_restarts``
            Maps keys to a monotonic due-at timestamp when a restart was
            deferred because the debounce window had not yet elapsed, or when
            a failed restart was queued for retry.
        ``_pending_retry_attempts``
            Tracks retry attempt counters per key for bounded exponential
            backoff when restart patch operations fail.
    """

    def __init__(
        self,
        core_api: CoreV1Api,
        apps_api: AppsV1Api,
        namespace: str,
        app_selector: str,
        rollout_annotation_key: str,
        debounce_seconds: int,
        config_map_name: str | None = None,
        logger: logging.Logger | None = None,
        now_fn: Callable[[], str] = utc_now_rfc3339,
    ) -> None:
        self.core_api = core_api
        self.apps_api = apps_api
        self.namespace = namespace
        self.app_selector = app_selector
        self.rollout_annotation_key = rollout_annotation_key
        self.debounce_seconds = debounce_seconds
        # Backward-compatibility with older constructor shape used by tests/consumers.
        self.config_map_name = config_map_name
        self.logger = logger or logging.getLogger(__name__)
        self.now_fn = now_fn

        # Debounce bookkeeping keyed by (env, configmap_name)
        self._last_restart: dict[tuple[str, str], float] = {}
        self._last_data_hash: dict[tuple[str, str], str] = {}
        self._pending_restarts: dict[tuple[str, str], float] = {}
        self._pending_retry_attempts: dict[tuple[str, str], int] = {}
        METRICS.pending_restarts.set(0)

        self._app_label_filters = self._parse_selector(app_selector)
        self.ready = threading.Event()
        self._external_stop = threading.Event()
        self._active_watcher: watch.Watch | None = None
        self._watcher_lock = threading.Lock()

    @staticmethod
    def _parse_selector(selector: str) -> dict[str, str]:
        """Parse a Kubernetes label selector string (``k=v,k2=v2``) into a dict."""
        result: dict[str, str] = {}
        for part in selector.split(","):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                result[key.strip()] = value.strip()
        return result

    def _matches_app_labels(self, labels: dict[str, str]) -> bool:
        """Return True if *labels* contain every key-value pair from the app selector."""
        return all(labels.get(k) == v for k, v in self._app_label_filters.items())

    def _deployment_selector_for_env(self, env: str) -> str:
        """Build a label selector that targets deployments for a specific environment.

        Extends the base ``app_selector`` with ``env=<env>`` so that a
        ConfigMap change in the *test* environment only restarts *test*
        deployments, not *prod* ones.
        """
        clauses = [part.strip() for part in self.app_selector.split(",") if part.strip()]
        if not any(part.startswith("env=") for part in clauses):
            clauses.append(f"env={env}")
        return ",".join(clauses)

    def _debounce_remaining(self, env: str, config_map_name: str, now_monotonic: float) -> float:
        """Return seconds remaining in the debounce window for a given key.

        Returns ``0.0`` when the key has never been restarted or the full
        debounce window has elapsed, meaning a restart may proceed
        immediately.  A positive value means the caller should defer the
        restart by that many seconds.

        Uses ``time.monotonic()`` timestamps to be immune to wall-clock
        adjustments (e.g. NTP jumps).
        """
        if self.debounce_seconds <= 0:
            return 0.0

        key = (env, config_map_name)
        last_seen = self._last_restart.get(key)
        if last_seen is None:
            return 0.0

        elapsed = now_monotonic - last_seen
        return max(0.0, float(self.debounce_seconds) - elapsed)

    def _schedule_pending_restart(
        self,
        env: str,
        config_map_name: str,
        now_monotonic: float,
        delay_seconds: float,
        *,
        reset_retry_attempt: bool = False,
    ) -> None:
        """Enqueue or postpone a restart so it fires after the debounce window.

        If a restart is already pending for this key, the due-at timestamp is
        pushed forward (never brought earlier) so the coalescing effect always
        uses the *latest* change within the window.
        """
        key = (env, config_map_name)
        due_at = now_monotonic + delay_seconds
        existing_due = self._pending_restarts.get(key)
        if existing_due is None or due_at > existing_due:
            self._pending_restarts[key] = due_at
            METRICS.pending_restarts.set(len(self._pending_restarts))
        if reset_retry_attempt:
            self._pending_retry_attempts.pop(key, None)

    def _mark_restart_executed(self, env: str, config_map_name: str, now_monotonic: float) -> None:
        key = (env, config_map_name)
        self._last_restart[key] = now_monotonic
        self._pending_restarts.pop(key, None)
        self._pending_retry_attempts.pop(key, None)
        METRICS.pending_restarts.set(len(self._pending_restarts))

    def request_stop(self) -> None:
        """Request a cooperative stop and immediately interrupt any open watch stream."""
        self._external_stop.set()
        with self._watcher_lock:
            active_watcher = self._active_watcher
        if active_watcher is not None:
            active_watcher.stop()

    def _should_stop(self, stop_event: threading.Event) -> bool:
        return stop_event.is_set() or self._external_stop.is_set()

    def _schedule_retry(self, env: str, config_map_name: str, now_monotonic: float) -> None:
        """Schedule a retry after a failed restart attempt using bounded exponential backoff."""
        key = (env, config_map_name)
        retry_attempt = self._pending_retry_attempts.get(key, 0) + 1
        self._pending_retry_attempts[key] = retry_attempt

        delay_seconds = min(30.0, float(2 ** (retry_attempt - 1)))
        due_at = now_monotonic + delay_seconds
        self._pending_restarts[key] = due_at
        METRICS.pending_restarts.set(len(self._pending_restarts))
        METRICS.retry_total.labels(env=env).inc()

        self.logger.warning(
            "Restart for %s/%s failed; scheduling retry attempt %d in %.1fs",
            env,
            config_map_name,
            retry_attempt,
            delay_seconds,
        )

    @staticmethod
    def _record_restart_result(result: RestartResult) -> None:
        METRICS.restarts_total.labels(env=result.environment).inc(result.restarted)
        METRICS.errors_total.labels(env=result.environment).inc(result.failed)

    def _restart_and_record(
        self,
        env: str,
        config_map_name: str,
        now_monotonic: float,
        *,
        force: bool = False,
    ) -> RestartResult:
        """Execute one restart attempt and reconcile queue state.

        Failed attempts are retried with bounded exponential backoff unless
        ``force=True`` (shutdown/handoff flush), in which case the intent is
        dropped after recording failure to avoid blocking termination forever.
        """
        result = self._restart_deployments_for_env(env=env, config_map_name=config_map_name)
        self._record_restart_result(result)
        if result.failed == 0:
            self._mark_restart_executed(
                env=env,
                config_map_name=config_map_name,
                now_monotonic=now_monotonic,
            )
            return result

        if force:
            key = (env, config_map_name)
            self._pending_restarts.pop(key, None)
            self._pending_retry_attempts.pop(key, None)
            METRICS.pending_restarts.set(len(self._pending_restarts))
            METRICS.dropped_restarts_total.inc()
            self.logger.error(
                "Forced restart for %s/%s failed during shutdown; dropping pending intent",
                env,
                config_map_name,
            )
            return result

        self._schedule_retry(
            env=env,
            config_map_name=config_map_name,
            now_monotonic=now_monotonic,
        )
        return result

    def _drain_pending_restarts(self, now_monotonic: float) -> None:
        """Process all pending restarts whose debounce window has elapsed.

        Iterates through scheduled restarts and executes any whose due-at
        timestamp is at or before *now_monotonic*.  Executed entries are
        removed from the pending map by ``_restart_and_record``.
        """
        due_restarts = [
            key for key, due_at in self._pending_restarts.items() if due_at <= now_monotonic
        ]
        for env, config_map_name in due_restarts:
            self.logger.info(
                "Processing debounced ConfigMap restart for %s/%s",
                env,
                config_map_name,
            )
            self._restart_and_record(
                env=env,
                config_map_name=config_map_name,
                now_monotonic=now_monotonic,
            )

    def _flush_pending_restarts_on_shutdown(self) -> None:
        """Force-process all pending restarts before shutdown.

        Restarts still inside the debounce window are executed immediately so
        leadership handoff or process termination cannot silently lose a
        previously observed ConfigMap change.
        """
        if not self._pending_restarts:
            return

        pending_count = len(self._pending_restarts)
        self.logger.warning(
            "Forcing %d pending restart(s) before shutdown", pending_count
        )

        for env, config_map_name in list(self._pending_restarts.keys()):
            self.logger.warning(
                "Forcing pending ConfigMap restart for %s/%s due to shutdown or leadership handoff",
                env,
                config_map_name,
            )
            try:
                self._restart_and_record(
                    env=env,
                    config_map_name=config_map_name,
                    now_monotonic=time.monotonic(),
                    force=True,
                )
            except Exception:
                # Unexpected failures should not leave stale queue entries.
                self.logger.exception(
                    "Failed forced pending restart for %s/%s during shutdown",
                    env,
                    config_map_name,
                )
                self._pending_restarts.pop((env, config_map_name), None)
                self._pending_retry_attempts.pop((env, config_map_name), None)
                METRICS.pending_restarts.set(len(self._pending_restarts))
                METRICS.dropped_restarts_total.inc()

    def _next_watch_timeout_seconds(self, now_monotonic: float) -> int:
        """Return the next watch timeout in seconds, shortened for pending restarts.

        When restarts are pending the timeout is clamped so the watch
        loop wakes up in time to drain them.  Without pending restarts
        the default 30-second timeout is returned.
        """
        if not self._pending_restarts:
            return 30

        nearest_due = min(self._pending_restarts.values())
        remaining = max(1.0, nearest_due - now_monotonic)
        return min(30, max(1, math.ceil(remaining)))

    @staticmethod
    def _normalize_data(raw_data: Any) -> dict[str, str]:
        """Coerce ConfigMap ``data`` into a stable ``dict[str, str]``.

        Handles ``None`` values (possible when a key exists with no value)
        and non-dict inputs gracefully so that downstream hashing is
        deterministic.
        """
        if not isinstance(raw_data, dict):
            return {}
        return {
            k: ("" if v is None else str(v))
            for k, v in raw_data.items()
            if isinstance(k, str)
        }

    @staticmethod
    def _hash_data(data: dict[str, str]) -> str:
        """Return a SHA-256 hex digest of the ConfigMap data.

        We hash the data content (rather than comparing ``resourceVersion``)
        because ``resourceVersion`` changes on *any* object mutation
        including label or annotation edits that do not affect the
        application.  Hashing only ``data`` avoids false-positive restarts.
        """
        stable_payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return sha256(stable_payload.encode("utf-8")).hexdigest()

    def _config_hash_annotation_key(self, config_map_name: str) -> str:
        """Return the deployment template annotation key storing ConfigMap data hash."""
        prefix, separator, _ = self.rollout_annotation_key.partition("/")

        normalized_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", config_map_name).strip("-.")
        if not normalized_name:
            normalized_name = "configmap"

        annotation_name = f"config-hash-{normalized_name}"
        if len(annotation_name) > 63:
            suffix = sha256(config_map_name.encode("utf-8")).hexdigest()[:10]
            max_prefix_length = 63 - len("config-hash--") - len(suffix)
            trimmed = normalized_name[: max(1, max_prefix_length)].rstrip("-.")
            if not trimmed:
                trimmed = "configmap"
            annotation_name = f"config-hash-{trimmed}-{suffix}"

        if separator:
            return f"{prefix}/{annotation_name}"
        return annotation_name

    @staticmethod
    def _deployment_template_annotations(deployment: Any) -> dict[str, str]:
        """Extract pod template annotations from a deployment object safely."""
        spec = getattr(deployment, "spec", None)
        template = getattr(spec, "template", None)
        metadata = getattr(template, "metadata", None)
        annotations = getattr(metadata, "annotations", None)
        if not isinstance(annotations, dict):
            return {}
        return {
            k: ("" if v is None else str(v))
            for k, v in annotations.items()
            if isinstance(k, str)
        }

    def _sync_cache_from_list(self, config_maps: Any, restart_on_change: bool = False) -> None:
        """Seed or refresh the data-hash cache from a full ConfigMap listing.

        Called at startup (``restart_on_change=False``) to populate the
        baseline, and after a ``410 Gone`` re-list (``restart_on_change=True``)
        to detect changes that occurred while the watch was disconnected.
        """
        items = getattr(config_maps, "items", None) or []
        for config_map in items:
            metadata = getattr(config_map, "metadata", None)
            if metadata is None:
                continue

            labels = metadata.labels or {}
            if not self._matches_app_labels(labels):
                continue

            env = labels.get("env")
            config_map_name = getattr(metadata, "name", None)
            if not env or not config_map_name:
                continue

            key = (env, config_map_name)
            current_hash = self._hash_data(self._normalize_data(getattr(config_map, "data", None)))
            previous_hash = self._last_data_hash.get(key)
            self._last_data_hash[key] = current_hash

            if not restart_on_change:
                continue
            if previous_hash is None or previous_hash == current_hash:
                continue
            now_monotonic = time.monotonic()
            debounce_remaining = self._debounce_remaining(
                env=env,
                config_map_name=config_map_name,
                now_monotonic=now_monotonic,
            )
            if debounce_remaining > 0:
                self._schedule_pending_restart(
                    env=env,
                    config_map_name=config_map_name,
                    now_monotonic=now_monotonic,
                    delay_seconds=debounce_remaining,
                    reset_retry_attempt=True,
                )
                continue

            self.logger.info(
                "Detected data drift after re-list for %s/%s; restarting matching deployments",
                env,
                config_map_name,
            )
            self._restart_and_record(
                env=env,
                config_map_name=config_map_name,
                now_monotonic=now_monotonic,
            )

    def _reconcile_startup_drift(self, config_maps: Any) -> None:
        """Reconcile startup drift by comparing ConfigMap hashes with deployment annotations.

        This detects ConfigMap changes that happened while the controller was down,
        but only for deployments where a prior controller-managed hash annotation
        already exists (or where rollout annotations indicate an older controller
        version that did not persist hash metadata).
        """
        items = getattr(config_maps, "items", None) or []
        for config_map in items:
            metadata = getattr(config_map, "metadata", None)
            if metadata is None:
                continue

            labels = metadata.labels or {}
            if not self._matches_app_labels(labels):
                continue

            env = labels.get("env")
            config_map_name = getattr(metadata, "name", None)
            if not env or not config_map_name:
                continue

            key = (env, config_map_name)
            current_hash = self._last_data_hash.get(key)
            if current_hash is None:
                continue

            selector = self._deployment_selector_for_env(env)
            try:
                deployments = self.apps_api.list_namespaced_deployment(
                    namespace=self.namespace,
                    label_selector=selector,
                )
            except ApiException:
                self.logger.exception(
                    "Failed startup drift check for %s/%s (selector=%s)",
                    env,
                    config_map_name,
                    selector,
                )
                continue

            deployment_items = deployments.items or []
            if not deployment_items:
                continue

            hash_annotation_key = self._config_hash_annotation_key(config_map_name)
            stale_deployments: list[str] = []
            annotation_unknown = 0

            for deployment in deployment_items:
                deployment_name = (
                    getattr(getattr(deployment, "metadata", None), "name", None) or "<unknown>"
                )
                annotations = self._deployment_template_annotations(deployment)
                observed_hash = annotations.get(hash_annotation_key)

                if observed_hash is None:
                    if self.rollout_annotation_key in annotations:
                        stale_deployments.append(deployment_name)
                    else:
                        annotation_unknown += 1
                    continue

                if observed_hash != current_hash:
                    stale_deployments.append(deployment_name)

            if annotation_unknown:
                self.logger.info(
                    "Startup drift check skipped for %d deployment(s) in env %s "
                    "without hash annotation for %s",
                    annotation_unknown,
                    env,
                    config_map_name,
                )

            if not stale_deployments:
                continue

            self.logger.warning(
                "Detected startup ConfigMap drift for %s/%s "
                "(stale deployments: %s); reconciling with restart",
                env,
                config_map_name,
                ", ".join(stale_deployments),
            )
            self._restart_and_record(
                env=env,
                config_map_name=config_map_name,
                now_monotonic=time.monotonic(),
            )

    def _has_meaningful_data_change(
        self,
        env: str,
        config_map_name: str,
        event_type: str,
        config_map: Any,
    ) -> bool:
        """Determine if a ConfigMap event represents a real data change.

        Returns ``False`` (suppress restart) when:
        - The event is an initial ``ADDED`` with no prior baseline — this
          happens during watch replay when the controller first starts.
        - The SHA-256 digest of ``data`` is identical to the last-seen hash,
          meaning only metadata (labels, annotations, resourceVersion) changed.

        Always updates ``_last_data_hash`` as a side effect so the next
        comparison uses the freshest baseline.
        """
        key = (env, config_map_name)
        current_hash = self._hash_data(self._normalize_data(getattr(config_map, "data", None)))
        previous_hash = self._last_data_hash.get(key)
        self._last_data_hash[key] = current_hash

        if previous_hash is None:
            if event_type == "ADDED":
                self.logger.info(
                    "Ignoring initial ADDED event for %s/%s with no prior data baseline",
                    env,
                    config_map_name,
                )
                return False
            return True

        if previous_hash == current_hash:
            self.logger.info("Ignoring unchanged data for %s/%s", env, config_map_name)
            return False

        return True

    def _restart_deployments_for_env(self, env: str, config_map_name: str) -> RestartResult:
        """Patch every matching Deployment's pod template to trigger a rolling restart.

        Finds deployments by the computed label selector for *env*, then patches
        each with a ``shipshape.io/restartedAt`` annotation containing an
        RFC 3339 timestamp.  Kubernetes treats this as a template change and
        performs a zero-downtime rolling update.
        """
        selector = self._deployment_selector_for_env(env)
        try:
            deployments = self.apps_api.list_namespaced_deployment(
                namespace=self.namespace,
                label_selector=selector,
            )
        except ApiException:
            self.logger.exception(
                "Failed to list deployments for env %s with selector %s", env, selector
            )
            return RestartResult(environment=env, matched_deployments=0, restarted=0, failed=1)

        items = deployments.items or []
        restarted = 0
        failed = 0
        timestamp = self.now_fn()
        config_hash = self._last_data_hash.get((env, config_map_name))
        hash_annotation_key = self._config_hash_annotation_key(config_map_name)
        hash_annotations = (
            {hash_annotation_key: config_hash}
            if config_hash is not None
            else None
        )

        for deployment in items:
            deployment_name = getattr(getattr(deployment, "metadata", None), "name", None)
            if not deployment_name:
                failed += 1
                self.logger.error(
                    "Encountered deployment with missing metadata.name in env %s", env
                )
                continue

            if config_hash is not None:
                annotations = self._deployment_template_annotations(deployment)
                if annotations.get(hash_annotation_key) == config_hash:
                    self.logger.info(
                        "Deployment %s in env %s already has config hash %s; skipping patch",
                        deployment_name,
                        env,
                        config_hash,
                    )
                    continue
            try:
                patch_deployment_restart(
                    apps_api=self.apps_api,
                    namespace=self.namespace,
                    deployment_name=deployment_name,
                    annotation_key=self.rollout_annotation_key,
                    timestamp=timestamp,
                    extra_annotations=hash_annotations,
                )
                restarted += 1
                self.logger.info(
                    "Triggered rolling restart for deployment %s in env %s", deployment_name, env
                )
            except ApiException:
                failed += 1
                self.logger.exception(
                    "Failed to patch deployment %s in namespace %s", deployment_name, self.namespace
                )

        if not items:
            self.logger.warning(
                "ConfigMap %s changed, but no deployments matched selector %s",
                config_map_name,
                selector,
            )

        return RestartResult(
            environment=env,
            matched_deployments=len(items),
            restarted=restarted,
            failed=failed,
        )

    def handle_configmap_event(self, event_type: str, config_map: Any) -> RestartResult | None:
        """Process a single ConfigMap watch event.

        Filters out irrelevant events (wrong labels, no ``env`` label,
        replay ADDED events, unchanged data), applies debounce logic, and
        either restarts immediately or schedules a deferred restart.

        Returns a :class:`RestartResult` when a restart was executed
        immediately, or ``None`` when the event was filtered, debounced,
        or invalid.
        """
        if event_type not in {"ADDED", "MODIFIED"}:
            return None

        metadata = getattr(config_map, "metadata", None)
        if metadata is None:
            return None

        labels = metadata.labels or {}
        if not self._matches_app_labels(labels):
            return None

        env = labels.get("env")
        if not env:
            self.logger.warning("Skipping ConfigMap %s because env label is missing", metadata.name)
            return None

        config_map_name = metadata.name
        if not config_map_name:
            self.logger.warning("Skipping ConfigMap with empty name in env %s", env)
            return None

        key = (env, config_map_name)

        if not self._has_meaningful_data_change(
            env=env,
            config_map_name=config_map_name,
            event_type=event_type,
            config_map=config_map,
        ):
            return None

        now_monotonic = time.monotonic()
        debounce_remaining = self._debounce_remaining(
            env=env,
            config_map_name=config_map_name,
            now_monotonic=now_monotonic,
        )
        if debounce_remaining > 0:
            self._schedule_pending_restart(
                env=env,
                config_map_name=config_map_name,
                now_monotonic=now_monotonic,
                delay_seconds=debounce_remaining,
                reset_retry_attempt=True,
            )
            self.logger.info(
                "Debounced ConfigMap event for %s/%s; scheduled retry in %.2fs",
                env,
                config_map_name,
                debounce_remaining,
            )
            METRICS.debounced_total.labels(env=env).inc()
            return None

        # A fresh immediate restart attempt supersedes older retry state
        # associated with previous failures for the same key.
        self._pending_restarts.pop(key, None)
        self._pending_retry_attempts.pop(key, None)
        METRICS.pending_restarts.set(len(self._pending_restarts))

        return self._restart_and_record(
            env=env,
            config_map_name=config_map_name,
            now_monotonic=now_monotonic,
        )

    def run_forever(self, shutdown_event: threading.Event | None = None) -> None:
        """Main control loop — list-then-watch ConfigMaps until shutdown.

        1. Retries the initial ConfigMap list with exponential backoff so
           transient API startup failures do not crash-loop the controller.
        2. Performs an initial list to seed ``_last_data_hash`` baselines.
        3. Reconciles startup drift by comparing ConfigMap hashes with
           deployment hash annotations.
        4. Opens a streaming watch from the list's ``resourceVersion``.
        5. On ``410 Gone`` (etcd compaction), re-lists and resumes.
        6. On transient errors, applies exponential backoff with jitter
           (capped at 30 s) to avoid thundering-herd reconnects.
        7. Drains pending (debounced) restarts on every loop iteration and
           after every event, shortening the watch timeout when restarts
           are queued so they fire on time.
        8. Retries failed restart attempts with bounded exponential backoff
           (1 s to 30 s cap) until they succeed.
        9. On shutdown/leadership handoff, force-processes any remaining
           pending restart intents. Failures in this forced flush are dropped
           and surfaced via metrics/alerts to avoid blocking termination.

        ``401`` / ``403`` responses from the Kubernetes API are treated as
        configuration errors (RBAC/auth) and terminate the loop immediately
        with a clear log message rather than retrying forever.
        """
        stop = shutdown_event or threading.Event()
        self._external_stop.clear()

        resource_version: str | None = None
        startup_backoff_seconds = 1
        while not self._should_stop(stop):
            try:
                initial = self.core_api.list_namespaced_config_map(
                    namespace=self.namespace,
                    label_selector=self.app_selector,
                )
                resource_version = getattr(
                    getattr(initial, "metadata", None), "resource_version", None
                )
                self._sync_cache_from_list(initial, restart_on_change=False)
                self._reconcile_startup_drift(initial)
                self.ready.set()
                self.logger.info("Starting watch from resourceVersion %s", resource_version)
                break
            except ApiException as exc:
                if exc.status in {401, 403}:
                    self.logger.error(
                        "Kubernetes API access denied during initial list (status=%s). "
                        "Check controller RBAC and service account permissions.",
                        exc.status,
                    )
                    self.ready.clear()
                    return
                self.logger.exception("Initial Kubernetes ConfigMap list failed")
                METRICS.watch_errors_total.inc()
            except Exception:
                self.logger.exception("Unexpected error during initial ConfigMap list")
                METRICS.watch_errors_total.inc()

            jittered = startup_backoff_seconds * (0.5 + random.random())  # noqa: S311
            stop.wait(timeout=jittered)
            startup_backoff_seconds = min(startup_backoff_seconds * 2, 30)

        if self._should_stop(stop):
            self.ready.clear()
            return

        # Exponential backoff counter (seconds) for transient API errors.
        # Reset to 1 on every successful watch iteration; doubled on error
        # up to a 30 s cap.  Jitter is applied at sleep time.
        backoff_seconds = 1
        watch_stream_count = 0

        while not self._should_stop(stop):
            self._drain_pending_restarts(now_monotonic=time.monotonic())
            watcher = watch.Watch()
            with self._watcher_lock:
                self._active_watcher = watcher
            try:
                timeout_seconds = self._next_watch_timeout_seconds(now_monotonic=time.monotonic())
                if watch_stream_count > 0:
                    METRICS.watch_reconnects_total.inc()
                watch_stream_count += 1
                stream = watcher.stream(
                    self.core_api.list_namespaced_config_map,
                    namespace=self.namespace,
                    label_selector=self.app_selector,
                    resource_version=resource_version,
                    timeout_seconds=timeout_seconds,
                )

                for event in stream:
                    if self._should_stop(stop):
                        break

                    obj = event.get("object")
                    if obj is None:
                        continue

                    metadata = getattr(obj, "metadata", None)
                    if metadata and metadata.resource_version:
                        resource_version = metadata.resource_version

                    event_type = str(event.get("type", ""))
                    self.handle_configmap_event(event_type=event_type, config_map=obj)
                    self._drain_pending_restarts(now_monotonic=time.monotonic())

                backoff_seconds = 1
                self._drain_pending_restarts(now_monotonic=time.monotonic())
            except ApiException as exc:
                # 410 Gone means etcd compacted past our resourceVersion.
                # We must re-list to get a fresh snapshot and resume watching
                # from the new resourceVersion.
                if exc.status == 410:
                    self.logger.warning("Watch resource version expired, re-listing")
                    try:
                        fresh = self.core_api.list_namespaced_config_map(
                            namespace=self.namespace,
                            label_selector=self.app_selector,
                        )
                        resource_version = getattr(
                            getattr(fresh, "metadata", None), "resource_version", None
                        )
                        self._sync_cache_from_list(fresh, restart_on_change=True)
                    except ApiException as relist_exc:
                        if relist_exc.status in {401, 403}:
                            self.logger.error(
                                "Kubernetes API access denied during 410 re-list (status=%s). "
                                "Check controller RBAC and service account permissions.",
                                relist_exc.status,
                            )
                            self.ready.clear()
                            return
                        self.logger.exception("Failed to re-list after 410")
                        METRICS.watch_errors_total.inc()
                        resource_version = None
                    continue

                if exc.status in {401, 403}:
                    self.logger.error(
                        "Kubernetes API watch denied (status=%s). "
                        "Check controller RBAC and service account permissions.",
                        exc.status,
                    )
                    METRICS.watch_errors_total.inc()
                    self.ready.clear()
                    return

                self.logger.exception("Kubernetes API watch error")
                METRICS.watch_errors_total.inc()
                jittered = backoff_seconds * (0.5 + random.random())  # noqa: S311
                stop.wait(timeout=jittered)
                backoff_seconds = min(backoff_seconds * 2, 30)
            except Exception:
                self.logger.exception("Unexpected watch error")
                METRICS.watch_errors_total.inc()
                jittered = backoff_seconds * (0.5 + random.random())  # noqa: S311
                stop.wait(timeout=jittered)
                backoff_seconds = min(backoff_seconds * 2, 30)
            finally:
                watcher.stop()
                with self._watcher_lock:
                    if self._active_watcher is watcher:
                        self._active_watcher = None

        self._flush_pending_restarts_on_shutdown()

        self.ready.clear()


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.getenv(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc

    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got: {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got: {value}")
    return value


def build_controller_from_env(core_api: CoreV1Api, apps_api: AppsV1Api) -> ConfigMapReloader:
    """Construct a :class:`ConfigMapReloader` from environment variables.

    Environment variables (with defaults):
        ``WATCH_NAMESPACE``  — Kubernetes namespace to watch (``shipshape``).
        ``APP_SELECTOR``     — Label selector for ConfigMaps (``app=helloworld``).
        ``ROLLOUT_ANNOTATION_KEY`` — Annotation set on pod templates (``shipshape.io/restartedAt``).
        ``DEBOUNCE_SECONDS`` — Minimum seconds between restarts per key (``5``).
    """
    namespace = os.getenv("WATCH_NAMESPACE", "shipshape")
    if not namespace.strip():
        raise ValueError("WATCH_NAMESPACE must be a non-empty string")

    app_selector = os.getenv("APP_SELECTOR", "app=helloworld")
    parsed = ConfigMapReloader._parse_selector(app_selector)
    if not parsed:
        raise ValueError(
            f"APP_SELECTOR must contain at least one key=value pair, got: {app_selector!r}"
        )

    rollout_annotation_key = os.getenv("ROLLOUT_ANNOTATION_KEY", "shipshape.io/restartedAt")
    debounce_seconds = env_int("DEBOUNCE_SECONDS", 5, minimum=0)

    return ConfigMapReloader(
        core_api=core_api,
        apps_api=apps_api,
        namespace=namespace,
        app_selector=app_selector,
        rollout_annotation_key=rollout_annotation_key,
        debounce_seconds=debounce_seconds,
    )
