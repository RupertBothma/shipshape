# Tilt local dev workflow for the test environment.
# This mirrors skaffold.yaml (non-prod paths) while optimizing the inner loop.

load('ext://restart_process', 'docker_build_with_restart')

# Load dynamic registry from .local-registry-env if available
def load_dynamic_registry():
    registry = "localhost:5000"
    if os.path.exists(".local-registry-env"):
        blob = str(read_file(".local-registry-env"))
        for line in blob.splitlines():
            if line.startswith("REGISTRY_URL="):
                registry = line.split("=", 1)[1].strip()
    return registry

default_registry_url = load_dynamic_registry()

# Tilt uses local image handling for local clusters (for example Kind/Minikube),
# so images are not pushed to a remote registry during `tilt up`.
# Optional override: use a local registry if your setup requires it.
# Example: TILT_LOCAL_REGISTRY=localhost:5001 tilt up
local_registry = os.getenv("TILT_LOCAL_REGISTRY", "").strip()
if local_registry:
    default_registry(local_registry)


def _has_crd(name):
    probe = str(
        local(
            "kubectl get crd " + name + " -o name >/dev/null 2>&1 && echo yes || echo no",
            quiet=True,
        )
    )
    return "yes" in probe


required_crds = [
    "envoyfilters.networking.istio.io",
    "gateways.networking.istio.io",
    "virtualservices.networking.istio.io",
    "destinationrules.networking.istio.io",
    "authorizationpolicies.security.istio.io",
    "peerauthentications.security.istio.io",
    "certificates.cert-manager.io",
    "servicemonitors.monitoring.coreos.com",
    "prometheusrules.monitoring.coreos.com",
]

missing_crds = [crd for crd in required_crds if not _has_crd(crd)]
if missing_crds:
    missing_crds_list = "- " + "\n- ".join(missing_crds)
    fail(
        "Missing required CRDs for this Tilt workflow:\n"
        + missing_crds_list
        + "\n\nYour current cluster must have Istio, cert-manager CRDs, and Prometheus Operator CRDs installed.\n"
        + "See README.md prerequisites and deploy steps."
    )

# App image build + live sync. Restart the process after synced code changes
# because the production command is not running with --reload.
docker_build_with_restart(
    "{}/shipshape-helloworld".format(default_registry_url),
    ".",
    entrypoint=["uvicorn", "app.src.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"],
    restart_file="/workspace/.restart-proc",
    dockerfile="app/Dockerfile",
    only=["app"],
    live_update=[
        sync("app/src", "/workspace/app/src"),
    ],
)

# Controller image build + live sync with process restart for quick feedback.
docker_build_with_restart(
    "{}/shipshape-controller".format(default_registry_url),
    ".",
    entrypoint=["python", "-m", "controller.src"],
    restart_file="/workspace/.restart-proc",
    dockerfile="controller/Dockerfile",
    only=["controller"],
    live_update=[
        sync("controller/src", "/workspace/controller/src"),
    ],
)

# Keep namespace lifecycle stable in local dev: force-updating workloads should
# not delete/recreate the Namespace object, which can race with subsequent apply.
local_resource(
    "shipshape-namespace",
    cmd="kubectl apply -f k8s/namespace/namespace.yaml",
    deps=["k8s/namespace/namespace.yaml"],
)

# Keep apply order aligned with skaffold.yaml while using local overlays that
# make live_update writable in-cluster.
namespace_yaml = kustomize("k8s/namespace")
_, namespace_scoped_yaml = filter_yaml(namespace_yaml, kind="Namespace")
k8s_yaml(namespace_scoped_yaml)
k8s_yaml(kustomize("k8s/istio-ingress"))
k8s_yaml(kustomize("k8s/overlays/tilt-test"))
k8s_yaml(kustomize("k8s/overlays/tilt-controller"))

# App local endpoint: http://localhost:18000
k8s_resource(
    "helloworld-test",
    resource_deps=["shipshape-namespace"],
    port_forwards=[port_forward(18000, 8000)],
)

# Controller starts after app deployment is healthy to avoid startup churn.
# Local endpoint: http://localhost:18080/healthz (and /metrics)
k8s_resource(
    "helloworld-controller",
    resource_deps=["helloworld-test"],
    port_forwards=[port_forward(18080, 8080)],
)
