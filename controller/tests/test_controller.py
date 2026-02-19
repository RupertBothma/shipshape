from __future__ import annotations

import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import ApiException

from controller.src.controller import (
    ConfigMapReloader,
    build_controller_from_env,
    env_int,
)


class FakeAppsApi:
    def __init__(
        self,
        deployment_names: list[str],
        fail_names: set[str] | None = None,
        fail_counts: dict[str, int] | None = None,
        template_annotations: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.deployment_names = deployment_names
        self.fail_names = fail_names or set()
        self.fail_counts = dict(fail_counts or {})
        self.template_annotations = template_annotations or {}
        self.last_selector = ""
        self.patches: list[tuple[str, dict[str, Any]]] = []

    def list_namespaced_deployment(self, namespace: str, label_selector: str) -> SimpleNamespace:
        self.last_selector = label_selector
        items = []
        for name in self.deployment_names:
            annotations = dict(self.template_annotations.get(name, {}))
            items.append(
                SimpleNamespace(
                    metadata=SimpleNamespace(name=name),
                    spec=SimpleNamespace(
                        template=SimpleNamespace(
                            metadata=SimpleNamespace(annotations=annotations)
                        )
                    ),
                )
            )
        return SimpleNamespace(items=items)

    def patch_namespaced_deployment(self, name: str, namespace: str, body: dict[str, Any]) -> None:
        remaining_failures = self.fail_counts.get(name, 0)
        if remaining_failures > 0:
            self.fail_counts[name] = remaining_failures - 1
            raise ApiException(status=500, reason="boom")
        if name in self.fail_names:
            raise ApiException(status=500, reason="boom")
        annotations = (
            body.get("spec", {})
            .get("template", {})
            .get("metadata", {})
            .get("annotations", {})
        )
        if isinstance(annotations, dict):
            existing = self.template_annotations.setdefault(name, {})
            for key, value in annotations.items():
                if isinstance(key, str):
                    existing[key] = str(value)
        self.patches.append((name, body))


def make_config_map(
    app: str | None,
    env: str | None,
    name: str = "helloworld-config",
    data: dict[str, str] | None = None,
    resource_version: str = "1",
) -> SimpleNamespace:
    labels: dict[str, str] = {}
    if app is not None:
        labels["app"] = app
    if env is not None:
        labels["env"] = env

    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels, resource_version=resource_version),
        data=data if data is not None else {"MESSAGE": "hello"},
    )


def fixed_now() -> str:
    return datetime(2026, 1, 1, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _make_controller(
    apps_api: Any = None,
    core_api: Any = None,
    debounce_seconds: int = 0,
    app_selector: str = "app=helloworld",
) -> ConfigMapReloader:
    controller = ConfigMapReloader(
        core_api=core_api or SimpleNamespace(),
        apps_api=apps_api or FakeAppsApi([]),
        namespace="shipshape",
        app_selector=app_selector,
        config_map_name=None,
        rollout_annotation_key="shipshape.io/restartedAt",
        debounce_seconds=debounce_seconds,
        now_fn=fixed_now,
    )
    return controller


# ---------------------------------------------------------------------------
# Core event logic tests
# ---------------------------------------------------------------------------


def test_ignores_configmaps_without_helloworld_label() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    result = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(app="other", env="test"),
    )

    assert result is None
    assert apps_api.patches == []


def test_added_event_without_baseline_does_not_restart() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    result = controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(app="helloworld", env="test", name="helloworld-config-test"),
    )

    assert result is None
    assert apps_api.patches == []


def test_restarts_only_on_meaningful_data_change() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    cm = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "hello"},
    )
    controller.handle_configmap_event(event_type="ADDED", config_map=cm)

    unchanged = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "hello"},
        resource_version="2",
    )
    changed = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "updated"},
        resource_version="3",
    )

    second = controller.handle_configmap_event(event_type="MODIFIED", config_map=unchanged)
    third = controller.handle_configmap_event(event_type="MODIFIED", config_map=changed)

    assert second is None
    assert third is not None
    assert third.restarted == 1
    assert len(apps_api.patches) == 1


def test_modified_without_prior_baseline_restarts() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    result = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "first seen on MODIFIED"},
        ),
    )

    assert result is not None
    assert result.environment == "test"
    assert result.restarted == 1


def test_restarts_only_matching_environment_deployments() -> None:
    apps_api = FakeAppsApi(["helloworld-test", "helloworld-canary-test"])
    controller = _make_controller(apps_api=apps_api)

    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "before"},
        ),
    )

    result = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "after"},
            resource_version="2",
        ),
    )

    assert result is not None
    assert result.environment == "test"
    assert result.matched_deployments == 2
    assert result.restarted == 2
    assert result.failed == 0
    assert apps_api.last_selector == "app=helloworld,env=test"


def test_patch_contains_rollout_annotation() -> None:
    apps_api = FakeAppsApi(["helloworld-prod"])
    controller = _make_controller(apps_api=apps_api)

    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld",
            env="prod",
            name="helloworld-config-prod",
            data={"MESSAGE": "before"},
        ),
    )

    controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(
            app="helloworld",
            env="prod",
            name="helloworld-config-prod",
            data={"MESSAGE": "after"},
            resource_version="2",
        ),
    )

    assert len(apps_api.patches) == 1
    _, body = apps_api.patches[0]
    annotations = body["spec"]["template"]["metadata"]["annotations"]
    assert annotations["shipshape.io/restartedAt"] == "2026-01-01T00:00:00Z"
    hash_key = controller._config_hash_annotation_key("helloworld-config-prod")
    assert annotations[hash_key] == controller._hash_data({"MESSAGE": "after"})


def test_controller_handles_patch_failures_and_continues() -> None:
    apps_api = FakeAppsApi(
        deployment_names=["helloworld-test-a", "helloworld-test-b"],
        fail_names={"helloworld-test-a"},
    )
    controller = _make_controller(apps_api=apps_api)

    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "before"},
        ),
    )

    result = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "after"},
            resource_version="2",
        ),
    )

    assert result is not None
    assert result.matched_deployments == 2
    assert result.restarted == 1
    assert result.failed == 1
    assert ("test", "helloworld-config-test") in controller._pending_restarts


def test_controller_retries_failed_restart_until_success() -> None:
    apps_api = FakeAppsApi(
        deployment_names=["helloworld-test"],
        fail_counts={"helloworld-test": 1},
    )
    controller = _make_controller(apps_api=apps_api)

    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "before"},
        ),
    )

    with patch("controller.src.controller.time.monotonic", return_value=100.0):
        failed = controller.handle_configmap_event(
            event_type="MODIFIED",
            config_map=make_config_map(
                app="helloworld",
                env="test",
                name="helloworld-config-test",
                data={"MESSAGE": "after"},
                resource_version="2",
            ),
        )

    assert failed is not None
    assert failed.failed == 1
    assert ("test", "helloworld-config-test") in controller._pending_restarts
    assert ("test", "helloworld-config-test") not in controller._last_restart

    with patch("controller.src.controller.time.monotonic", return_value=101.0):
        controller._drain_pending_restarts(now_monotonic=101.0)

    assert len(apps_api.patches) == 1
    assert controller._pending_restarts == {}
    assert ("test", "helloworld-config-test") in controller._last_restart


def test_shutdown_flush_drops_pending_intent_when_restart_keeps_failing() -> None:
    apps_api = FakeAppsApi(
        deployment_names=["helloworld-test"],
        fail_names={"helloworld-test"},
    )
    controller = _make_controller(apps_api=apps_api)

    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "before"},
        ),
    )

    with patch("controller.src.controller.time.monotonic", return_value=100.0):
        failed = controller.handle_configmap_event(
            event_type="MODIFIED",
            config_map=make_config_map(
                app="helloworld",
                env="test",
                name="helloworld-config-test",
                data={"MESSAGE": "after"},
                resource_version="2",
            ),
        )

    assert failed is not None
    assert failed.failed == 1
    assert len(controller._pending_restarts) == 1

    with patch("controller.src.controller.time.monotonic", return_value=101.0):
        controller._flush_pending_restarts_on_shutdown()

    assert controller._pending_restarts == {}


def test_controller_debounces_fast_repeated_events() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api, debounce_seconds=60)

    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "v1"},
        ),
    )

    first = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "v2"},
            resource_version="2",
        ),
    )
    second = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "v3"},
            resource_version="3",
        ),
    )

    assert first is not None
    assert second is None
    assert len(apps_api.patches) == 1


def test_controller_debounce_coalesces_and_retries_latest_state() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api, debounce_seconds=60)

    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-test",
            data={"MESSAGE": "v1"},
        ),
    )

    with patch("controller.src.controller.time.monotonic", side_effect=[100.0, 110.0]):
        first = controller.handle_configmap_event(
            event_type="MODIFIED",
            config_map=make_config_map(
                app="helloworld",
                env="test",
                name="helloworld-config-test",
                data={"MESSAGE": "v2"},
                resource_version="2",
            ),
        )
        second = controller.handle_configmap_event(
            event_type="MODIFIED",
            config_map=make_config_map(
                app="helloworld",
                env="test",
                name="helloworld-config-test",
                data={"MESSAGE": "v3"},
                resource_version="3",
            ),
        )

    assert first is not None
    assert second is None
    assert len(apps_api.patches) == 1
    assert ("test", "helloworld-config-test") in controller._pending_restarts

    controller._drain_pending_restarts(now_monotonic=161.0)
    assert len(apps_api.patches) == 2
    assert controller._pending_restarts == {}


def test_custom_app_selector_matches_correctly() -> None:
    apps_api = FakeAppsApi(["myapp-test"])
    controller = _make_controller(apps_api=apps_api, app_selector="app=myapp")

    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(app="myapp", env="test", name="myapp-config-test"),
    )
    result = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(
            app="myapp",
            env="test",
            name="myapp-config-test",
            data={"MESSAGE": "changed"},
            resource_version="2",
        ),
    )

    assert result is not None
    assert result.restarted == 1


def test_custom_app_selector_rejects_non_matching() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api, app_selector="app=myapp")

    result = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(app="helloworld", env="test"),
    )

    assert result is None


def test_ignores_deleted_events() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    result = controller.handle_configmap_event(
        event_type="DELETED",
        config_map=make_config_map(app="helloworld", env="test"),
    )

    assert result is None
    assert apps_api.patches == []


def test_ignores_configmap_without_env_label() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    result = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(app="helloworld", env=None),
    )

    assert result is None


# ---------------------------------------------------------------------------
# Initial sync tests
# ---------------------------------------------------------------------------


def test_added_event_seeds_baseline_without_restart() -> None:
    """An ADDED event with no prior baseline seeds the hash but does not restart."""
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    result = controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(app="helloworld", env="test"),
    )

    assert result is None
    assert apps_api.patches == []
    # Baseline should now be set
    assert ("test", "helloworld-config") in controller._last_data_hash


def test_added_event_with_stale_baseline_triggers_restart() -> None:
    """An ADDED event where data differs from the cached baseline triggers a restart.

    This covers the case where a ConfigMap is recreated mid-watch.
    """
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    cm = make_config_map(app="helloworld", env="test")
    controller._last_data_hash[("test", "helloworld-config")] = "stale-hash"

    result = controller.handle_configmap_event(
        event_type="ADDED",
        config_map=cm,
    )

    assert result is not None
    assert result.restarted == 1


def test_added_event_mid_watch_new_configmap_seeds_baseline() -> None:
    """A brand-new ConfigMap appearing mid-watch seeds the baseline (no prior hash)."""
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    # Simulate mid-watch: no prior hash exists for this ConfigMap
    result = controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld", env="test", name="helloworld-config-new", data={"MESSAGE": "new"}
        ),
    )

    # First ADDED with no baseline should seed, not restart
    assert result is None
    assert ("test", "helloworld-config-new") in controller._last_data_hash

    # Subsequent MODIFIED with changed data should restart
    result = controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=make_config_map(
            app="helloworld",
            env="test",
            name="helloworld-config-new",
            data={"MESSAGE": "changed"},
            resource_version="2",
        ),
    )
    assert result is not None
    assert result.restarted == 1


def test_startup_reconciliation_restarts_when_deployment_hash_is_stale() -> None:
    config_map = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "v2"},
        resource_version="200",
    )
    stale_hash = ConfigMapReloader._hash_data({"MESSAGE": "v1"})

    apps_api = FakeAppsApi(
        ["helloworld-test"],
        template_annotations={
            "helloworld-test": {
                "shipshape.io/restartedAt": "2026-01-01T00:00:00Z",
            }
        },
    )
    controller = _make_controller(apps_api=apps_api)
    hash_key = controller._config_hash_annotation_key("helloworld-config-test")
    apps_api.template_annotations["helloworld-test"][hash_key] = stale_hash

    listing = SimpleNamespace(items=[config_map])
    controller._sync_cache_from_list(listing, restart_on_change=False)
    controller._reconcile_startup_drift(listing)

    assert len(apps_api.patches) == 1
    expected_hash = ConfigMapReloader._hash_data({"MESSAGE": "v2"})
    assert apps_api.template_annotations["helloworld-test"][hash_key] == expected_hash


def test_startup_reconciliation_skips_deployments_without_hash_or_restart_annotation() -> None:
    config_map = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "v2"},
        resource_version="200",
    )
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api)

    listing = SimpleNamespace(items=[config_map])
    controller._sync_cache_from_list(listing, restart_on_change=False)
    controller._reconcile_startup_drift(listing)

    assert len(apps_api.patches) == 0


def test_startup_reconciliation_repairs_legacy_restart_annotation_without_hash() -> None:
    config_map = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "v2"},
        resource_version="200",
    )
    apps_api = FakeAppsApi(
        ["helloworld-test"],
        template_annotations={
            "helloworld-test": {
                "shipshape.io/restartedAt": "2026-01-01T00:00:00Z",
            }
        },
    )
    controller = _make_controller(apps_api=apps_api)
    hash_key = controller._config_hash_annotation_key("helloworld-config-test")

    listing = SimpleNamespace(items=[config_map])
    controller._sync_cache_from_list(listing, restart_on_change=False)
    controller._reconcile_startup_drift(listing)

    assert len(apps_api.patches) == 1
    assert hash_key in apps_api.template_annotations["helloworld-test"]


# ---------------------------------------------------------------------------
# Watch loop tests
# ---------------------------------------------------------------------------


def _fake_core_api(
    resource_versions: list[str] | None = None,
    item_sets: list[list[SimpleNamespace]] | None = None,
) -> SimpleNamespace:
    versions = resource_versions or ["100"]
    items_by_call = item_sets or [[]]
    call_count = 0

    def fake_list(**kwargs: Any) -> SimpleNamespace:
        nonlocal call_count
        index = min(call_count, len(versions) - 1)
        items_index = min(call_count, len(items_by_call) - 1)
        call_count += 1
        return SimpleNamespace(
            metadata=SimpleNamespace(resource_version=versions[index]),
            items=items_by_call[items_index],
        )

    return SimpleNamespace(list_namespaced_config_map=fake_list)


def test_run_forever_processes_events_and_tracks_resource_version() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])

    initial = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "before"},
        resource_version="100",
    )
    event_obj = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "after"},
        resource_version="42",
    )
    fake_event = {"type": "MODIFIED", "object": event_obj}

    core_api = _fake_core_api(resource_versions=["100"], item_sets=[[initial]])
    controller = _make_controller(apps_api=apps_api, core_api=core_api)

    shutdown_event = threading.Event()
    mock_watcher = MagicMock()
    call_count = 0

    def patched_stream(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return iter([fake_event])
        shutdown_event.set()
        return iter([])

    mock_watcher.stream.side_effect = patched_stream

    with patch("controller.src.controller.watch.Watch", return_value=mock_watcher):
        controller.run_forever(shutdown_event=shutdown_event)

    assert len(apps_api.patches) == 1
    assert mock_watcher.stop.call_count >= 1


def test_run_forever_reconciles_startup_drift_before_watch() -> None:
    initial = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "after-downtime"},
        resource_version="100",
    )
    apps_api = FakeAppsApi(
        ["helloworld-test"],
        template_annotations={
            "helloworld-test": {
                "shipshape.io/restartedAt": "2026-01-01T00:00:00Z",
            }
        },
    )
    controller = _make_controller(
        apps_api=apps_api,
        core_api=_fake_core_api(resource_versions=["100"], item_sets=[[initial]]),
    )
    hash_key = controller._config_hash_annotation_key("helloworld-config-test")
    apps_api.template_annotations["helloworld-test"][hash_key] = ConfigMapReloader._hash_data(
        {"MESSAGE": "before-downtime"}
    )

    shutdown_event = threading.Event()
    mock_watcher = MagicMock()

    def patched_stream(*args: Any, **kwargs: Any) -> Any:
        shutdown_event.set()
        return iter([])

    mock_watcher.stream.side_effect = patched_stream

    with patch("controller.src.controller.watch.Watch", return_value=mock_watcher):
        controller.run_forever(shutdown_event=shutdown_event)

    assert len(apps_api.patches) == 1
    assert (
        apps_api.template_annotations["helloworld-test"][hash_key]
        == ConfigMapReloader._hash_data({"MESSAGE": "after-downtime"})
    )


def test_run_forever_resets_resource_version_on_410() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    initial = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "same"},
        resource_version="100",
    )
    core_api = _fake_core_api(
        resource_versions=["100", "200"],
        item_sets=[[initial], [initial]],
    )

    controller = _make_controller(apps_api=apps_api, core_api=core_api)

    call_count = 0
    resource_versions_seen: list[Any] = []
    shutdown_event = threading.Event()
    mock_watcher = MagicMock()

    def patched_stream(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        resource_versions_seen.append(kwargs.get("resource_version"))
        if call_count == 1:
            raise ApiException(status=410, reason="Gone")
        shutdown_event.set()
        return iter([])

    mock_watcher.stream.side_effect = patched_stream

    with patch("controller.src.controller.watch.Watch", return_value=mock_watcher):
        controller.run_forever(shutdown_event=shutdown_event)

    assert resource_versions_seen[0] == "100"
    assert resource_versions_seen[1] == "200"


def test_run_forever_relist_restarts_when_data_drift_detected() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])

    initial = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "before"},
        resource_version="100",
    )
    relist = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "after"},
        resource_version="200",
    )

    core_api = _fake_core_api(
        resource_versions=["100", "200"],
        item_sets=[[initial], [relist]],
    )
    controller = _make_controller(apps_api=apps_api, core_api=core_api)

    shutdown_event = threading.Event()
    mock_watcher = MagicMock()
    call_count = 0

    def patched_stream(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ApiException(status=410, reason="Gone")
        shutdown_event.set()
        return iter([])

    mock_watcher.stream.side_effect = patched_stream

    with patch("controller.src.controller.watch.Watch", return_value=mock_watcher):
        controller.run_forever(shutdown_event=shutdown_event)

    assert len(apps_api.patches) == 1


def test_run_forever_retries_initial_list_on_transient_error() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    shutdown_event = threading.Event()
    wait_values: list[float] = []

    initial = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "before"},
        resource_version="100",
    )

    list_attempts = 0

    def fake_list(**kwargs: Any) -> SimpleNamespace:
        nonlocal list_attempts
        list_attempts += 1
        if list_attempts == 1:
            raise ApiException(status=500, reason="temporary startup failure")
        return SimpleNamespace(metadata=SimpleNamespace(resource_version="100"), items=[initial])

    core_api = SimpleNamespace(list_namespaced_config_map=fake_list)
    controller = _make_controller(apps_api=apps_api, core_api=core_api)

    mock_watcher = MagicMock()

    def patched_stream(*args: Any, **kwargs: Any) -> Any:
        shutdown_event.set()
        return iter([])

    mock_watcher.stream.side_effect = patched_stream

    def fake_wait(timeout: float | None = None) -> bool:
        if timeout is not None:
            wait_values.append(timeout)
        return False

    with (
        patch("controller.src.controller.watch.Watch", return_value=mock_watcher),
        patch("controller.src.controller.threading.Event.wait", side_effect=fake_wait),
        patch("controller.src.controller.random.random", return_value=0.5),
    ):
        controller.run_forever(shutdown_event=shutdown_event)

    assert list_attempts == 2
    assert wait_values == [pytest.approx(1.0)]
    assert mock_watcher.stream.call_count == 1


def test_run_forever_exits_fast_on_startup_rbac_denied() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])

    def fake_list(**kwargs: Any) -> SimpleNamespace:
        raise ApiException(status=403, reason="forbidden")

    core_api = SimpleNamespace(list_namespaced_config_map=fake_list)
    controller = _make_controller(apps_api=apps_api, core_api=core_api)
    watch_factory = MagicMock()

    with patch("controller.src.controller.watch.Watch", watch_factory):
        controller.run_forever(shutdown_event=threading.Event())

    watch_factory.assert_not_called()
    assert not controller.ready.is_set()


def test_run_forever_exits_fast_on_watch_rbac_denied() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    shutdown_event = threading.Event()
    wait_values: list[float] = []

    controller = _make_controller(apps_api=apps_api, core_api=_fake_core_api())

    mock_watcher = MagicMock()

    def patched_stream(*args: Any, **kwargs: Any) -> Any:
        raise ApiException(status=401, reason="unauthorized")

    mock_watcher.stream.side_effect = patched_stream

    def fake_wait(timeout: float | None = None) -> bool:
        if timeout is not None:
            wait_values.append(timeout)
        return False

    with (
        patch("controller.src.controller.watch.Watch", return_value=mock_watcher),
        patch("controller.src.controller.threading.Event.wait", side_effect=fake_wait),
    ):
        controller.run_forever(shutdown_event=shutdown_event)

    assert wait_values == []
    assert not controller.ready.is_set()


def test_run_forever_applies_exponential_backoff_on_api_error() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    shutdown_event = threading.Event()
    wait_values: list[float] = []

    controller = _make_controller(apps_api=apps_api, core_api=_fake_core_api())

    call_count = 0

    mock_watcher = MagicMock()

    def patched_stream(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            raise ApiException(status=500, reason="Internal Server Error")
        shutdown_event.set()
        return iter([])

    mock_watcher.stream.side_effect = patched_stream

    def fake_wait(timeout: float | None = None) -> bool:
        if timeout is not None:
            wait_values.append(timeout)
        return False

    with (
        patch("controller.src.controller.watch.Watch", return_value=mock_watcher),
        patch("controller.src.controller.threading.Event.wait", side_effect=fake_wait),
        patch("controller.src.controller.random.random", return_value=0.5),
    ):
        controller.run_forever(shutdown_event=shutdown_event)

    assert len(wait_values) == 3
    assert wait_values[0] == pytest.approx(1.0)
    assert wait_values[1] == pytest.approx(2.0)
    assert wait_values[2] == pytest.approx(4.0)


def test_run_forever_resets_backoff_after_successful_stream() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    shutdown_event = threading.Event()
    wait_values: list[float] = []

    controller = _make_controller(apps_api=apps_api, core_api=_fake_core_api())

    call_count = 0

    mock_watcher = MagicMock()

    def patched_stream(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ApiException(status=500, reason="error")
        if call_count == 2:
            return iter([])
        if call_count == 3:
            raise ApiException(status=500, reason="error again")
        shutdown_event.set()
        return iter([])

    mock_watcher.stream.side_effect = patched_stream

    def fake_wait(timeout: float | None = None) -> bool:
        if timeout is not None:
            wait_values.append(timeout)
        return False

    with (
        patch("controller.src.controller.watch.Watch", return_value=mock_watcher),
        patch("controller.src.controller.threading.Event.wait", side_effect=fake_wait),
        patch("controller.src.controller.random.random", return_value=0.5),
    ):
        controller.run_forever(shutdown_event=shutdown_event)

    assert len(wait_values) == 2
    assert wait_values[0] == pytest.approx(1.0)
    assert wait_values[1] == pytest.approx(1.0)


def test_run_forever_handles_unexpected_exception_with_backoff() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    shutdown_event = threading.Event()
    wait_values: list[float] = []

    controller = _make_controller(apps_api=apps_api, core_api=_fake_core_api())

    call_count = 0

    mock_watcher = MagicMock()

    def patched_stream(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("network down")
        shutdown_event.set()
        return iter([])

    mock_watcher.stream.side_effect = patched_stream

    def fake_wait(timeout: float | None = None) -> bool:
        if timeout is not None:
            wait_values.append(timeout)
        return False

    with (
        patch("controller.src.controller.watch.Watch", return_value=mock_watcher),
        patch("controller.src.controller.threading.Event.wait", side_effect=fake_wait),
        patch("controller.src.controller.random.random", return_value=0.5),
    ):
        controller.run_forever(shutdown_event=shutdown_event)

    assert len(wait_values) == 1


def test_run_forever_shutdown_event_stops_loop() -> None:
    apps_api = FakeAppsApi(["helloworld-test"])
    shutdown_event = threading.Event()
    shutdown_event.set()
    controller = _make_controller(apps_api=apps_api, core_api=_fake_core_api())

    mock_watcher = MagicMock()
    mock_watcher.stream.return_value = iter([])

    with patch("controller.src.controller.watch.Watch", return_value=mock_watcher):
        controller.run_forever(shutdown_event=shutdown_event)

    assert apps_api.patches == []


def test_next_watch_timeout_defaults_without_pending_restarts() -> None:
    controller = _make_controller()
    assert controller._next_watch_timeout_seconds(now_monotonic=100.0) == 30


def test_next_watch_timeout_tracks_pending_restart_deadline() -> None:
    controller = _make_controller()
    controller._pending_restarts[("test", "helloworld-config-test")] = 105.2
    assert controller._next_watch_timeout_seconds(now_monotonic=100.0) == 6


# ---------------------------------------------------------------------------
# env_int() tests
# ---------------------------------------------------------------------------


def test_env_int_returns_default_when_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_ENV_INT", raising=False)
    assert env_int("TEST_ENV_INT", 42) == 42


def test_env_int_parses_valid_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ENV_INT", "10")
    assert env_int("TEST_ENV_INT", 42) == 10


def test_env_int_raises_on_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ENV_INT", "abc")
    with pytest.raises(ValueError, match="TEST_ENV_INT must be an integer"):
        env_int("TEST_ENV_INT", 42)


def test_env_int_raises_on_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ENV_INT", "")
    with pytest.raises(ValueError, match="TEST_ENV_INT must be an integer"):
        env_int("TEST_ENV_INT", 42)


def test_env_int_parses_negative_without_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ENV_INT", "-5")
    assert env_int("TEST_ENV_INT", 42) == -5


def test_env_int_enforces_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ENV_INT", "-1")
    with pytest.raises(ValueError, match="TEST_ENV_INT must be >= 0, got: -1"):
        env_int("TEST_ENV_INT", 42, minimum=0)


def test_env_int_enforces_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_ENV_INT", "70000")
    with pytest.raises(ValueError, match="TEST_ENV_INT must be <= 65535, got: 70000"):
        env_int("TEST_ENV_INT", 42, maximum=65535)


# ---------------------------------------------------------------------------
# build_controller_from_env() tests
# ---------------------------------------------------------------------------


def test_build_controller_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCH_NAMESPACE", raising=False)
    monkeypatch.delenv("APP_SELECTOR", raising=False)
    monkeypatch.delenv("ROLLOUT_ANNOTATION_KEY", raising=False)
    monkeypatch.delenv("DEBOUNCE_SECONDS", raising=False)

    core_api = SimpleNamespace()
    apps_api = SimpleNamespace()

    controller = build_controller_from_env(core_api=core_api, apps_api=apps_api)

    assert controller.namespace == "shipshape"
    assert controller.app_selector == "app=helloworld"
    assert controller.rollout_annotation_key == "shipshape.io/restartedAt"
    assert controller.debounce_seconds == 5


def test_build_controller_from_env_custom_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCH_NAMESPACE", "custom-ns")
    monkeypatch.setenv("APP_SELECTOR", "app=myapp")
    monkeypatch.setenv("ROLLOUT_ANNOTATION_KEY", "custom.io/restart")
    monkeypatch.setenv("DEBOUNCE_SECONDS", "15")

    core_api = SimpleNamespace()
    apps_api = SimpleNamespace()

    controller = build_controller_from_env(core_api=core_api, apps_api=apps_api)

    assert controller.namespace == "custom-ns"
    assert controller.app_selector == "app=myapp"
    assert controller.rollout_annotation_key == "custom.io/restart"
    assert controller.debounce_seconds == 15


def test_build_controller_from_env_invalid_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBOUNCE_SECONDS", "not-a-number")

    core_api = SimpleNamespace()
    apps_api = SimpleNamespace()

    with pytest.raises(ValueError, match="DEBOUNCE_SECONDS must be an integer"):
        build_controller_from_env(core_api=core_api, apps_api=apps_api)


def test_build_controller_from_env_negative_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBOUNCE_SECONDS", "-1")
    with pytest.raises(ValueError, match="DEBOUNCE_SECONDS must be >= 0, got: -1"):
        build_controller_from_env(core_api=SimpleNamespace(), apps_api=SimpleNamespace())


# ---------------------------------------------------------------------------
# _parse_selector tests
# ---------------------------------------------------------------------------


def test_parse_selector_single_label() -> None:
    result = ConfigMapReloader._parse_selector("app=helloworld")
    assert result == {"app": "helloworld"}


def test_parse_selector_multiple_labels() -> None:
    result = ConfigMapReloader._parse_selector("app=helloworld,tier=frontend")
    assert result == {"app": "helloworld", "tier": "frontend"}


def test_parse_selector_with_spaces() -> None:
    result = ConfigMapReloader._parse_selector("app = helloworld , tier = frontend")
    assert result == {"app": "helloworld", "tier": "frontend"}


# ---------------------------------------------------------------------------
# Shutdown drain tests
# ---------------------------------------------------------------------------


def test_shutdown_drains_due_pending_restarts() -> None:
    """Pending restarts that are past their due-at time are drained on shutdown."""
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api, debounce_seconds=60)

    # Seed baseline
    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld", env="test", name="helloworld-config-test", data={"MESSAGE": "v1"}
        ),
    )

    # Trigger change + debounce
    with patch("controller.src.controller.time.monotonic", return_value=100.0):
        controller.handle_configmap_event(
            event_type="MODIFIED",
            config_map=make_config_map(
                app="helloworld",
                env="test",
                name="helloworld-config-test",
                data={"MESSAGE": "v2"},
                resource_version="2",
            ),
        )

    # First event restarts immediately; second change would be debounced
    with patch("controller.src.controller.time.monotonic", return_value=105.0):
        controller.handle_configmap_event(
            event_type="MODIFIED",
            config_map=make_config_map(
                app="helloworld",
                env="test",
                name="helloworld-config-test",
                data={"MESSAGE": "v3"},
                resource_version="3",
            ),
        )

    assert len(controller._pending_restarts) == 1

    # Simulate shutdown after debounce window elapses
    with patch("controller.src.controller.time.monotonic", return_value=200.0):
        controller._drain_pending_restarts(now_monotonic=200.0)

    assert controller._pending_restarts == {}
    assert len(apps_api.patches) == 2


def test_shutdown_forces_not_yet_due_pending_restarts() -> None:
    """Pending restarts are forced during shutdown, even inside debounce."""
    apps_api = FakeAppsApi(["helloworld-test"])
    controller = _make_controller(apps_api=apps_api, debounce_seconds=60)

    controller.handle_configmap_event(
        event_type="ADDED",
        config_map=make_config_map(
            app="helloworld", env="test", name="helloworld-config-test", data={"MESSAGE": "v1"}
        ),
    )

    with patch("controller.src.controller.time.monotonic", return_value=100.0):
        controller.handle_configmap_event(
            event_type="MODIFIED",
            config_map=make_config_map(
                app="helloworld",
                env="test",
                name="helloworld-config-test",
                data={"MESSAGE": "v2"},
                resource_version="2",
            ),
        )

    with patch("controller.src.controller.time.monotonic", return_value=105.0):
        controller.handle_configmap_event(
            event_type="MODIFIED",
            config_map=make_config_map(
                app="helloworld",
                env="test",
                name="helloworld-config-test",
                data={"MESSAGE": "v3"},
                resource_version="3",
            ),
        )

    assert len(apps_api.patches) == 1
    assert len(controller._pending_restarts) == 1

    with patch("controller.src.controller.time.monotonic", return_value=106.0):
        controller._flush_pending_restarts_on_shutdown()

    assert len(apps_api.patches) == 2
    assert controller._pending_restarts == {}


def test_leader_handoff_flushes_pending_restart_before_new_leader_baseline_sync() -> None:
    """A pending restart is executed by the old leader before handoff."""
    old_apps_api = FakeAppsApi(["helloworld-test"])
    old_controller = _make_controller(apps_api=old_apps_api, debounce_seconds=60)

    cm_v1 = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "v1"},
    )
    cm_v2 = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "v2"},
        resource_version="2",
    )
    cm_v3 = make_config_map(
        app="helloworld",
        env="test",
        name="helloworld-config-test",
        data={"MESSAGE": "v3"},
        resource_version="3",
    )

    old_controller.handle_configmap_event(event_type="ADDED", config_map=cm_v1)
    with patch("controller.src.controller.time.monotonic", return_value=100.0):
        old_controller.handle_configmap_event(event_type="MODIFIED", config_map=cm_v2)
    with patch("controller.src.controller.time.monotonic", return_value=105.0):
        old_controller.handle_configmap_event(event_type="MODIFIED", config_map=cm_v3)

    assert len(old_apps_api.patches) == 1
    assert len(old_controller._pending_restarts) == 1

    with patch("controller.src.controller.time.monotonic", return_value=106.0):
        old_controller._flush_pending_restarts_on_shutdown()

    assert len(old_apps_api.patches) == 2
    assert old_controller._pending_restarts == {}

    new_apps_api = FakeAppsApi(["helloworld-test"])
    new_controller = _make_controller(apps_api=new_apps_api, debounce_seconds=60)
    new_controller._sync_cache_from_list(
        SimpleNamespace(items=[cm_v3]),
        restart_on_change=False,
    )

    assert len(new_apps_api.patches) == 0

    same_data_result = new_controller.handle_configmap_event(
        event_type="MODIFIED",
        config_map=cm_v3,
    )
    assert same_data_result is None
    assert len(new_apps_api.patches) == 0

# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


def test_build_controller_from_env_empty_namespace_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCH_NAMESPACE", "")
    with pytest.raises(ValueError, match="WATCH_NAMESPACE must be a non-empty string"):
        build_controller_from_env(core_api=SimpleNamespace(), apps_api=SimpleNamespace())


def test_build_controller_from_env_invalid_selector_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCH_NAMESPACE", raising=False)
    monkeypatch.setenv("APP_SELECTOR", "no-equals-sign")
    with pytest.raises(ValueError, match="APP_SELECTOR must contain at least one key=value pair"):
        build_controller_from_env(core_api=SimpleNamespace(), apps_api=SimpleNamespace())
