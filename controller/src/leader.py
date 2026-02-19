from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from kubernetes.client import CoordinationV1Api, V1Lease, V1LeaseSpec, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from controller.src.metrics import METRICS

LOGGER = logging.getLogger(__name__)


class LeaseLeaderElector:
    """Lease-based leader election using the ``coordination.k8s.io/v1`` Lease API.

    Ensures only one controller replica actively watches and restarts
    deployments at a time.  The algorithm:

    1. Try to read the Lease object.  If it does not exist, create it and
       become leader.
    2. If the Lease exists and *we* are the holder, renew it (update
       ``renewTime``).
    3. If another identity holds the Lease, wait until
       ``renewTime + leaseDurationSeconds`` has passed (i.e. the holder
       failed to renew), then take over.
    4. On ``409 Conflict`` (concurrent update), retry on the next cycle.

    The ``retry_period_seconds`` (default 2 s) controls the polling
    interval.  When leadership is lost (e.g. network partition longer
    than the lease duration), ``on_stopped_leading`` is invoked so the
    controller watch loop can be stopped cleanly.

    All timestamps use UTC to avoid timezone ambiguity across nodes.
    """

    def __init__(
        self,
        coordination_api: CoordinationV1Api,
        namespace: str,
        lease_name: str,
        identity: str,
        lease_duration_seconds: int = 15,
        renew_deadline_seconds: int = 10,
        retry_period_seconds: int = 2,
    ) -> None:
        if lease_duration_seconds < 1:
            raise ValueError("lease_duration_seconds must be >= 1")
        if renew_deadline_seconds < 1:
            raise ValueError("renew_deadline_seconds must be >= 1")
        if retry_period_seconds < 0:
            raise ValueError("retry_period_seconds must be >= 0")
        if renew_deadline_seconds >= lease_duration_seconds:
            raise ValueError("renew_deadline_seconds must be smaller than lease_duration_seconds")
        if retry_period_seconds >= renew_deadline_seconds:
            raise ValueError("retry_period_seconds must be smaller than renew_deadline_seconds")

        self.coordination_api = coordination_api
        self.namespace = namespace
        self.lease_name = lease_name
        self.identity = identity
        self.lease_duration_seconds = lease_duration_seconds
        self.renew_deadline_seconds = renew_deadline_seconds
        self.retry_period_seconds = retry_period_seconds
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def _now_utc(self) -> datetime:
        return datetime.now(UTC)

    def _try_acquire_or_renew(self) -> bool:
        """Attempt a single acquire-or-renew cycle.  Returns True on success."""
        now = self._now_utc()
        try:
            lease = self.coordination_api.read_namespaced_lease(
                name=self.lease_name,
                namespace=self.namespace,
            )
        except ApiException as exc:
            if exc.status == 404:
                return self._create_lease(now)
            LOGGER.warning("Failed to read lease %s: %s", self.lease_name, exc.reason)
            return False

        spec = lease.spec
        if spec is None:
            return self._update_lease(lease, now)

        holder = spec.holder_identity
        renew_time = spec.renew_time
        duration = spec.lease_duration_seconds or self.lease_duration_seconds

        if holder == self.identity:
            return self._update_lease(lease, now)

        if renew_time is not None:
            renew_aware = renew_time if renew_time.tzinfo else renew_time.replace(tzinfo=UTC)
            elapsed = (now - renew_aware).total_seconds()
            if elapsed < duration:
                return False

        return self._update_lease(lease, now)

    def _create_lease(self, now: datetime) -> bool:
        """Create a new Lease object, claiming leadership.

        Returns False on ``409 Conflict`` (another replica beat us to it).
        """
        lease = V1Lease(
            metadata=V1ObjectMeta(name=self.lease_name, namespace=self.namespace),
            spec=V1LeaseSpec(
                holder_identity=self.identity,
                lease_duration_seconds=self.lease_duration_seconds,
                acquire_time=now,
                renew_time=now,
            ),
        )
        try:
            self.coordination_api.create_namespaced_lease(
                namespace=self.namespace,
                body=lease,
            )
            LOGGER.info("Acquired leader lease %s", self.lease_name)
            return True
        except ApiException as exc:
            if exc.status == 409:
                LOGGER.debug("Lease %s already exists, will retry", self.lease_name)
                return False
            LOGGER.warning("Failed to create lease %s: %s", self.lease_name, exc.reason)
            return False

    def _update_lease(self, lease: V1Lease, now: datetime) -> bool:
        """Update an existing Lease to renew or acquire leadership.

        Sets ``acquireTime`` when leadership is first obtained or when
        taking over from a different holder.
        """
        if lease.spec is None:
            lease.spec = V1LeaseSpec()
        previous_holder = lease.spec.holder_identity
        lease.spec.holder_identity = self.identity
        lease.spec.renew_time = now
        lease.spec.lease_duration_seconds = self.lease_duration_seconds
        if lease.spec.acquire_time is None or previous_holder != self.identity:
            lease.spec.acquire_time = now
        try:
            self.coordination_api.replace_namespaced_lease(
                name=self.lease_name,
                namespace=self.namespace,
                body=lease,
            )
            return True
        except ApiException as exc:
            if exc.status == 409:
                LOGGER.debug("Lease %s update conflict, will retry", self.lease_name)
                return False
            LOGGER.warning("Failed to update lease %s: %s", self.lease_name, exc.reason)
            return False

    def _release_lease(self) -> None:
        """Clear holderIdentity on the Lease to allow immediate takeover."""
        try:
            lease = self.coordination_api.read_namespaced_lease(
                name=self.lease_name, namespace=self.namespace
            )
            if lease.spec and lease.spec.holder_identity == self.identity:
                lease.spec.holder_identity = None
                self.coordination_api.replace_namespaced_lease(
                    name=self.lease_name, namespace=self.namespace, body=lease
                )
                LOGGER.info("Released leader lease %s", self.lease_name)
        except Exception:
            LOGGER.warning("Failed to release leader lease %s", self.lease_name, exc_info=True)

    def run(
        self,
        on_started_leading: Callable[[], None],
        on_stopped_leading: Callable[[], None],
        stop_event: threading.Event,
    ) -> None:
        """Block until leader, run callback, then keep renewing."""
        LOGGER.info(
            "Starting leader election for lease %s (identity=%s)",
            self.lease_name,
            self.identity,
        )
        acquire_wait_started = time.monotonic()
        last_renew_success = acquire_wait_started
        METRICS.leader_state.set(0)

        while not stop_event.is_set():
            try:
                acquired = self._try_acquire_or_renew()
            except Exception:
                LOGGER.exception("Unexpected error in leader election cycle")
                acquired = False
            if acquired and not self._is_leader:
                self._is_leader = True
                LOGGER.info("Became leader (identity=%s)", self.identity)
                METRICS.leader_state.set(1)
                METRICS.leader_transitions_total.labels(transition="acquired").inc()
                acquired_at = time.monotonic()
                last_renew_success = acquired_at
                METRICS.leader_acquire_latency_seconds.observe(acquired_at - acquire_wait_started)
                on_started_leading()
            elif acquired and self._is_leader:
                last_renew_success = time.monotonic()
            elif not acquired and self._is_leader:
                elapsed_since_last_renew = time.monotonic() - last_renew_success
                if elapsed_since_last_renew < self.renew_deadline_seconds:
                    LOGGER.warning(
                        "Lease renewal failed; holding leadership for up to %ss "
                        "(elapsed %.2fs)",
                        self.renew_deadline_seconds,
                        elapsed_since_last_renew,
                    )
                else:
                    self._is_leader = False
                    LOGGER.warning(
                        "Lost leader lease after %.2fs without successful renewal",
                        elapsed_since_last_renew,
                    )
                    METRICS.leader_state.set(0)
                    METRICS.leader_transitions_total.labels(transition="lost").inc()
                    acquire_wait_started = time.monotonic()
                    on_stopped_leading()
            stop_event.wait(timeout=self.retry_period_seconds)

        if self._is_leader:
            self._release_lease()
            self._is_leader = False
            METRICS.leader_state.set(0)
            METRICS.leader_transitions_total.labels(transition="lost").inc()
            on_stopped_leading()


def default_identity() -> str:
    """Return a unique identity for this replica, defaulting to the pod name.

    In Kubernetes the ``HOSTNAME`` env var is set to the pod name by the
    downward API, giving each replica a stable identity for lease ownership.
    """
    return os.getenv("HOSTNAME", os.getenv("POD_NAME", "unknown"))
