from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from controller.src.kube import build_clients, load_kube_configuration, patch_deployment_restart


def test_load_kube_configuration_in_cluster() -> None:
    with (
        patch("controller.src.kube.config.load_incluster_config") as mock_incluster,
        patch("controller.src.kube.config.load_kube_config") as mock_kubeconfig,
    ):
        load_kube_configuration()

    mock_incluster.assert_called_once()
    mock_kubeconfig.assert_not_called()


def test_load_kube_configuration_local_fallback() -> None:
    from kubernetes.config.config_exception import ConfigException

    with (
        patch(
            "controller.src.kube.config.load_incluster_config",
            side_effect=ConfigException("not in cluster"),
        ),
        patch("controller.src.kube.config.load_kube_config") as mock_kubeconfig,
    ):
        load_kube_configuration()

    mock_kubeconfig.assert_called_once()


def test_build_clients_returns_tuple() -> None:
    with patch("controller.src.kube.client") as mock_client:
        mock_client.CoreV1Api.return_value = SimpleNamespace(name="core")
        mock_client.AppsV1Api.return_value = SimpleNamespace(name="apps")
        core, apps = build_clients()

    assert core.name == "core"
    assert apps.name == "apps"


def test_patch_deployment_restart_sends_correct_body() -> None:
    mock_apps_api = MagicMock()

    patch_deployment_restart(
        apps_api=mock_apps_api,
        namespace="shipshape",
        deployment_name="helloworld-test",
        annotation_key="shipshape.io/restartedAt",
        timestamp="2026-01-01T00:00:00Z",
    )

    mock_apps_api.patch_namespaced_deployment.assert_called_once()
    call_kwargs = mock_apps_api.patch_namespaced_deployment.call_args
    assert call_kwargs.kwargs["name"] == "helloworld-test"
    assert call_kwargs.kwargs["namespace"] == "shipshape"
    body: dict[str, Any] = call_kwargs.kwargs["body"]
    annotations = body["spec"]["template"]["metadata"]["annotations"]
    assert annotations["shipshape.io/restartedAt"] == "2026-01-01T00:00:00Z"


def test_patch_deployment_restart_merges_extra_annotations() -> None:
    mock_apps_api = MagicMock()

    patch_deployment_restart(
        apps_api=mock_apps_api,
        namespace="shipshape",
        deployment_name="helloworld-test",
        annotation_key="shipshape.io/restartedAt",
        timestamp="2026-01-01T00:00:00Z",
        extra_annotations={"shipshape.io/config-hash-helloworld-config-test": "abc123"},
    )

    body: dict[str, Any] = mock_apps_api.patch_namespaced_deployment.call_args.kwargs["body"]
    annotations = body["spec"]["template"]["metadata"]["annotations"]
    assert annotations["shipshape.io/restartedAt"] == "2026-01-01T00:00:00Z"
    assert annotations["shipshape.io/config-hash-helloworld-config-test"] == "abc123"
