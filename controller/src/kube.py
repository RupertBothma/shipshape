from __future__ import annotations

import logging

from kubernetes import client, config
from kubernetes.client import AppsV1Api, CoreV1Api
from kubernetes.config.config_exception import ConfigException

LOGGER = logging.getLogger(__name__)


def load_kube_configuration() -> None:
    """Load Kubernetes client configuration.

    Attempts in-cluster config first (running inside a pod), falling back
    to the local kubeconfig for development.
    """
    try:
        config.load_incluster_config()
        LOGGER.info("Loaded in-cluster Kubernetes configuration")
    except ConfigException:
        config.load_kube_config()
        LOGGER.info("Loaded local kubeconfig")


def build_clients() -> tuple[CoreV1Api, AppsV1Api]:
    """Return CoreV1 and AppsV1 API clients using the active kube configuration."""
    return client.CoreV1Api(), client.AppsV1Api()


def patch_deployment_restart(
    apps_api: AppsV1Api,
    namespace: str,
    deployment_name: str,
    annotation_key: str,
    timestamp: str,
    extra_annotations: dict[str, str] | None = None,
) -> None:
    """Patch a Deployment's pod template annotation to trigger a rolling restart.

    This is the same mechanism used by ``kubectl rollout restart``: changing a
    pod template annotation causes the ReplicaSet controller to roll new pods.
    ``extra_annotations`` can persist additional reconciliation metadata.
    """
    annotations = {annotation_key: timestamp, **(extra_annotations or {})}

    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": annotations
                }
            }
        }
    }

    apps_api.patch_namespaced_deployment(
        name=deployment_name,
        namespace=namespace,
        body=body,
    )
