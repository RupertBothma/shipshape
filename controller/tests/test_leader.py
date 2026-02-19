from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import V1Lease, V1LeaseSpec, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from controller.src.leader import LeaseLeaderElector, default_identity
from controller.src.metrics import METRICS


def _make_elector(
    coordination_api: Any = None,
    namespace: str = "shipshape",
    lease_name: str = "test-lease",
    identity: str = "pod-1",
    lease_duration_seconds: int = 15,
    renew_deadline_seconds: int = 10,
    retry_period_seconds: int = 0,
) -> LeaseLeaderElector:
    return LeaseLeaderElector(
        coordination_api=coordination_api or MagicMock(),
        namespace=namespace,
        lease_name=lease_name,
        identity=identity,
        lease_duration_seconds=lease_duration_seconds,
        renew_deadline_seconds=renew_deadline_seconds,
        retry_period_seconds=retry_period_seconds,
    )


def test_creates_lease_when_not_found() -> None:
    api = MagicMock()
    api.read_namespaced_lease.side_effect = ApiException(status=404, reason="Not Found")
    api.create_namespaced_lease.return_value = None

    elector = _make_elector(coordination_api=api, identity="pod-1")
    result = elector._try_acquire_or_renew()

    assert result is True
    api.create_namespaced_lease.assert_called_once()
    body = api.create_namespaced_lease.call_args.kwargs["body"]
    assert body.spec.holder_identity == "pod-1"


def test_renews_lease_when_already_holder() -> None:
    now = datetime.now(UTC)
    existing = V1Lease(
        metadata=V1ObjectMeta(name="test-lease", namespace="shipshape"),
        spec=V1LeaseSpec(
            holder_identity="pod-1",
            lease_duration_seconds=15,
            renew_time=now - timedelta(seconds=5),
            acquire_time=now - timedelta(seconds=30),
        ),
    )
    api = MagicMock()
    api.read_namespaced_lease.return_value = existing
    api.replace_namespaced_lease.return_value = None

    elector = _make_elector(coordination_api=api, identity="pod-1")
    result = elector._try_acquire_or_renew()

    assert result is True
    api.replace_namespaced_lease.assert_called_once()


def test_does_not_acquire_when_another_holder_active() -> None:
    now = datetime.now(UTC)
    existing = V1Lease(
        metadata=V1ObjectMeta(name="test-lease", namespace="shipshape"),
        spec=V1LeaseSpec(
            holder_identity="pod-2",
            lease_duration_seconds=15,
            renew_time=now - timedelta(seconds=2),
            acquire_time=now - timedelta(seconds=10),
        ),
    )
    api = MagicMock()
    api.read_namespaced_lease.return_value = existing

    elector = _make_elector(coordination_api=api, identity="pod-1")
    result = elector._try_acquire_or_renew()

    assert result is False
    api.replace_namespaced_lease.assert_not_called()


def test_acquires_expired_lease_from_another_holder() -> None:
    now = datetime.now(UTC)
    existing = V1Lease(
        metadata=V1ObjectMeta(name="test-lease", namespace="shipshape"),
        spec=V1LeaseSpec(
            holder_identity="pod-2",
            lease_duration_seconds=15,
            renew_time=now - timedelta(seconds=60),
            acquire_time=now - timedelta(seconds=120),
        ),
    )
    api = MagicMock()
    api.read_namespaced_lease.return_value = existing
    api.replace_namespaced_lease.return_value = None

    elector = _make_elector(coordination_api=api, identity="pod-1")
    result = elector._try_acquire_or_renew()

    assert result is True


def test_handles_409_conflict_on_create() -> None:
    api = MagicMock()
    api.read_namespaced_lease.side_effect = ApiException(status=404, reason="Not Found")
    api.create_namespaced_lease.side_effect = ApiException(status=409, reason="Conflict")

    elector = _make_elector(coordination_api=api, identity="pod-1")
    result = elector._try_acquire_or_renew()

    assert result is False


def test_run_calls_on_started_leading() -> None:
    api = MagicMock()
    api.read_namespaced_lease.side_effect = ApiException(status=404, reason="Not Found")
    api.create_namespaced_lease.return_value = None

    elector = _make_elector(coordination_api=api, identity="pod-1", retry_period_seconds=0)

    stop = threading.Event()
    started = threading.Event()

    def on_started() -> None:
        started.set()
        stop.set()

    elector.run(
        on_started_leading=on_started,
        on_stopped_leading=lambda: None,
        stop_event=stop,
    )

    assert started.is_set()
    assert not elector.is_leader  # Stopped leading after stop_event


def test_acquire_time_updated_on_takeover() -> None:
    """When taking over a lease from another holder, acquire_time must be refreshed."""
    now = datetime.now(UTC)
    old_acquire = now - timedelta(seconds=120)
    existing = V1Lease(
        metadata=V1ObjectMeta(name="test-lease", namespace="shipshape"),
        spec=V1LeaseSpec(
            holder_identity="pod-2",
            lease_duration_seconds=15,
            renew_time=now - timedelta(seconds=60),
            acquire_time=old_acquire,
        ),
    )
    api = MagicMock()
    api.read_namespaced_lease.return_value = existing
    api.replace_namespaced_lease.return_value = None

    elector = _make_elector(coordination_api=api, identity="pod-1")
    result = elector._try_acquire_or_renew()

    assert result is True
    body = api.replace_namespaced_lease.call_args.kwargs["body"]
    # acquire_time must be updated (not the stale old_acquire from pod-2)
    assert body.spec.acquire_time != old_acquire
    assert body.spec.holder_identity == "pod-1"


def test_acquire_time_preserved_on_renewal() -> None:
    """When renewing our own lease, acquire_time must not change."""
    now = datetime.now(UTC)
    original_acquire = now - timedelta(seconds=30)
    existing = V1Lease(
        metadata=V1ObjectMeta(name="test-lease", namespace="shipshape"),
        spec=V1LeaseSpec(
            holder_identity="pod-1",
            lease_duration_seconds=15,
            renew_time=now - timedelta(seconds=5),
            acquire_time=original_acquire,
        ),
    )
    api = MagicMock()
    api.read_namespaced_lease.return_value = existing
    api.replace_namespaced_lease.return_value = None

    elector = _make_elector(coordination_api=api, identity="pod-1")
    result = elector._try_acquire_or_renew()

    assert result is True
    body = api.replace_namespaced_lease.call_args.kwargs["body"]
    # acquire_time must be preserved (we are renewing, not acquiring)
    assert body.spec.acquire_time == original_acquire


def test_non_api_exception_does_not_crash_election_loop() -> None:
    """Verify that a non-ApiException from the coordination API doesn't crash the loop."""
    api = MagicMock()
    call_count = 0

    def flaky_read(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("network blip")
        raise ApiException(status=404, reason="Not Found")

    api.read_namespaced_lease.side_effect = flaky_read
    api.create_namespaced_lease.return_value = None

    elector = _make_elector(coordination_api=api, identity="pod-1", retry_period_seconds=0)

    stop = threading.Event()
    started = threading.Event()

    def on_started() -> None:
        started.set()
        stop.set()

    elector.run(
        on_started_leading=on_started,
        on_stopped_leading=lambda: None,
        stop_event=stop,
    )

    assert call_count >= 2
    assert started.is_set()


def test_release_lease_on_shutdown() -> None:
    """Verify the lease holderIdentity is cleared on graceful shutdown."""
    now = datetime.now(UTC)
    existing = V1Lease(
        metadata=V1ObjectMeta(name="test-lease", namespace="shipshape"),
        spec=V1LeaseSpec(
            holder_identity="pod-1",
            lease_duration_seconds=15,
            renew_time=now,
            acquire_time=now,
        ),
    )
    api = MagicMock()
    api.read_namespaced_lease.side_effect = [
        # First call: _try_acquire_or_renew reads and renews
        existing,
        # Second call: _release_lease reads and clears
        V1Lease(
            metadata=V1ObjectMeta(name="test-lease", namespace="shipshape"),
            spec=V1LeaseSpec(
                holder_identity="pod-1",
                lease_duration_seconds=15,
                renew_time=now,
                acquire_time=now,
            ),
        ),
    ]
    api.replace_namespaced_lease.return_value = None

    elector = _make_elector(coordination_api=api, identity="pod-1", retry_period_seconds=0)

    stop = threading.Event()

    def on_started() -> None:
        stop.set()

    elector.run(
        on_started_leading=on_started,
        on_stopped_leading=lambda: None,
        stop_event=stop,
    )

    # The last replace call should have cleared holderIdentity (lease release)
    last_replace_call = api.replace_namespaced_lease.call_args_list[-1]
    released_body = last_replace_call.kwargs["body"]
    assert released_body.spec.holder_identity is None


def test_default_identity_uses_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTNAME", "my-pod-abc123")
    monkeypatch.delenv("POD_NAME", raising=False)

    assert default_identity() == "my-pod-abc123"


def test_default_identity_falls_back_to_pod_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setenv("POD_NAME", "controller-xyz")

    assert default_identity() == "controller-xyz"


def test_constructor_rejects_invalid_timing_relationships() -> None:
    with pytest.raises(
        ValueError, match="renew_deadline_seconds must be smaller than lease_duration_seconds"
    ):
        _make_elector(lease_duration_seconds=10, renew_deadline_seconds=10)

    with pytest.raises(
        ValueError, match="retry_period_seconds must be smaller than renew_deadline_seconds"
    ):
        _make_elector(lease_duration_seconds=15, renew_deadline_seconds=5, retry_period_seconds=5)


def test_loses_leadership_after_renew_deadline_expires() -> None:
    elector = _make_elector(renew_deadline_seconds=1, retry_period_seconds=0)
    stop = threading.Event()
    stopped_calls = 0

    def on_started() -> None:
        return None

    def on_stopped() -> None:
        nonlocal stopped_calls
        stopped_calls += 1
        stop.set()

    with (
        pytest.MonkeyPatch.context() as mp,
        patch.object(elector, "_try_acquire_or_renew", side_effect=[True, False]),
        patch.object(elector, "_release_lease") as release_mock,
    ):
        mp.setattr(
            "controller.src.leader.time.monotonic",
            MagicMock(side_effect=[0.0, 0.1, 1.5, 1.6]),
        )
        elector.run(
            on_started_leading=on_started,
            on_stopped_leading=on_stopped,
            stop_event=stop,
        )

    assert stopped_calls == 1
    release_mock.assert_not_called()


def test_keeps_leadership_when_failure_is_within_renew_deadline() -> None:
    elector = _make_elector(renew_deadline_seconds=3, retry_period_seconds=0)
    stop = threading.Event()
    stopped_calls = 0

    def on_started() -> None:
        return None

    def on_stopped() -> None:
        nonlocal stopped_calls
        stopped_calls += 1

    calls = 0

    def try_cycle() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        stop.set()
        return False

    with (
        pytest.MonkeyPatch.context() as mp,
        patch.object(elector, "_try_acquire_or_renew", side_effect=try_cycle),
        patch.object(elector, "_release_lease") as release_mock,
    ):
        mp.setattr("controller.src.leader.time.monotonic", MagicMock(side_effect=[0.0, 0.1, 0.5]))
        elector.run(
            on_started_leading=on_started,
            on_stopped_leading=on_stopped,
            stop_event=stop,
        )

    assert stopped_calls == 1
    release_mock.assert_called_once()


def test_leader_metrics_track_acquire_latency_and_transitions() -> None:
    api = MagicMock()
    api.read_namespaced_lease.side_effect = ApiException(status=404, reason="Not Found")
    api.create_namespaced_lease.return_value = None

    elector = _make_elector(coordination_api=api, identity="pod-1", retry_period_seconds=0)

    stop = threading.Event()

    acquired_before = METRICS.leader_transitions_total.labels(transition="acquired")._value.get()
    lost_before = METRICS.leader_transitions_total.labels(transition="lost")._value.get()
    latency_sum_before = METRICS.leader_acquire_latency_seconds._sum.get()

    def on_started() -> None:
        stop.set()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("controller.src.leader.time.monotonic", MagicMock(side_effect=[10.0, 14.0]))
        elector.run(
            on_started_leading=on_started,
            on_stopped_leading=lambda: None,
            stop_event=stop,
        )

    acquired_after = METRICS.leader_transitions_total.labels(transition="acquired")._value.get()
    lost_after = METRICS.leader_transitions_total.labels(transition="lost")._value.get()
    latency_sum_after = METRICS.leader_acquire_latency_seconds._sum.get()

    assert acquired_after - acquired_before == 1
    assert lost_after - lost_before == 1
    assert latency_sum_after - latency_sum_before == pytest.approx(4.0)
    assert METRICS.leader_state._value.get() == 0
