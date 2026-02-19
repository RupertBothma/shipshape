# Debugging Guide

Quick-reference triage steps for on-call engineers.

## Local Rapid Iteration (Optional)

For faster inner-loop changes across app/controller/manifests:
```bash
skaffold dev
# switch to prod overlay resources:
skaffold dev -p prod
```

Default local forwards from `skaffold.yaml`:
- app (`helloworld-test`) -> `http://127.0.0.1:18000`
- controller health/metrics -> `http://127.0.0.1:18080`

## 1. Get Cluster Overview

```bash
kubectl -n shipshape get pods -o wide
kubectl -n shipshape get events --sort-by=.lastTimestamp
kubectl -n shipshape top pods
```

## 2. App Debugging

### Check app logs
```bash
# Follow live logs (structured JSON)
kubectl -n shipshape logs -f deployment/helloworld-test
kubectl -n shipshape logs -f deployment/helloworld-prod

# Filter errors
kubectl -n shipshape logs deployment/helloworld-test | jq 'select(.level == "ERROR")'
```

### Check app health
```bash
# Port-forward to a pod
kubectl -n shipshape port-forward deployment/helloworld-test 8000:8000

# Then in another terminal:
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl http://localhost:8000/metrics
```

### Exec into app pod
```bash
kubectl -n shipshape exec -it deployment/helloworld-test -- sh
# Check environment variables
env | grep MESSAGE
```

### Pod-attached Python debugger (`debugpy`)
Use this only for short-lived incident debugging in non-prod.
`debugpy` is not bundled in the runtime image, so install it to `/tmp` for this
session before starting Uvicorn under the debugger.
```bash
# 1) Patch the test deployment to install debugpy in /tmp and start under debugpy
kubectl -n shipshape patch deployment helloworld-test --type merge -p '{
  "spec": {
    "template": {
      "spec": {
        "containers": [{
          "name": "helloworld",
          "command": ["sh", "-c"],
          "args": [
            "python -m pip install --no-cache-dir --target /tmp/debugpy debugpy && exec env PYTHONPATH=/tmp/debugpy:${PYTHONPATH} python -m debugpy --listen 0.0.0.0:5678 -m uvicorn app.src.main:create_app --factory --host 0.0.0.0 --port 8000"
          ]
        }]
      }
    }
  }
}'

# 2) Wait for rollout and forward debugger port
kubectl -n shipshape rollout status deployment/helloworld-test --timeout=180s
kubectl -n shipshape port-forward deployment/helloworld-test 5678:5678

# 3) Attach from IDE (VS Code/PyCharm) to localhost:5678
```

If pod egress to package indexes is blocked, build a temporary debug image with
`debugpy` preinstalled instead of installing at runtime.

After debugging, restore normal startup command by re-applying manifests:
```bash
kubectl apply -k k8s/overlays/test
kubectl -n shipshape rollout status deployment/helloworld-test --timeout=180s
```

### Enable verbose logging
Set `LOG_LEVEL=DEBUG` in the Deployment's env vars for detailed output:
```bash
kubectl -n shipshape set env deployment/helloworld-test LOG_LEVEL=DEBUG
```
Remember to revert after debugging: `kubectl -n shipshape set env deployment/helloworld-test LOG_LEVEL=INFO`

## 3. Controller Debugging

### Check controller logs
```bash
kubectl -n shipshape logs -f deployment/helloworld-controller

# Filter for specific events
kubectl -n shipshape logs deployment/helloworld-controller | jq 'select(.msg | test("restart|error|denied"))'
```

### Check controller health
```bash
kubectl -n shipshape port-forward deployment/helloworld-controller 8080:8080

curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
curl http://localhost:8080/leadz
curl http://localhost:8080/metrics
```

### Verify RBAC
```bash
kubectl -n shipshape auth can-i get configmaps --as system:serviceaccount:shipshape:helloworld-controller
kubectl -n shipshape auth can-i list configmaps --as system:serviceaccount:shipshape:helloworld-controller
kubectl -n shipshape auth can-i watch configmaps --as system:serviceaccount:shipshape:helloworld-controller
kubectl -n shipshape auth can-i patch deployments --as system:serviceaccount:shipshape:helloworld-controller
```

### Check leader election
```bash
kubectl -n shipshape get lease helloworld-controller-leader -o yaml
```

### Check pending metrics
```bash
kubectl -n shipshape port-forward deployment/helloworld-controller 8080:8080
curl -s http://localhost:8080/metrics | grep configmap_reload
```

## 4. Istio / Networking

### Check Istio proxy logs
```bash
kubectl -n shipshape logs <pod-name> -c istio-proxy
```

### Verify Istio routing
```bash
kubectl -n shipshape get gateway,virtualservice,destinationrule
kubectl -n shipshape get peerauthentication,authorizationpolicy
```

### Test ingress end-to-end
```bash
# Get ingress gateway IP
INGRESS_IP=$(kubectl -n istio-system get svc istio-ingressgateway -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

curl -v -H "Host: test.helloworld.shipshape.example.com" "https://$INGRESS_IP/" --insecure
curl -v -H "Host: prod.helloworld.shipshape.example.com" "https://$INGRESS_IP/" --insecure
```

## 5. Certificate Issues

```bash
kubectl -n shipshape get certificate
kubectl -n shipshape describe certificate helloworld-cert-test
kubectl -n shipshape describe certificate helloworld-cert-prod

# Check cert-manager logs
kubectl -n cert-manager logs -l app=cert-manager --tail=50
```

## 6. Common Issues

**Pod stuck in Pending:** Check ResourceQuota and node resources.
```bash
kubectl -n shipshape describe resourcequota shipshape-quota
kubectl describe nodes | grep -A5 "Allocated resources"
```

**ConfigMap change not triggering restart:** Check controller logs for "Ignoring unchanged data" or "Debounced". Verify labels: `app=helloworld` and `env=<test|prod>`.

**503 on readyz:** Controller is either not ready (initial list failed) or not the leader. Check `/leadz` and controller logs.
