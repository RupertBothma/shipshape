.PHONY: install-dev lint format test test-cov typecheck release-metadata manifests manifests-validate production-gate check check-ci-core cleanup-local curl-test curl-prod deploy deploy-fresh registry-up build-images push-images push-images-immutable ci-smoke-test

# Load dynamic registry configuration if it exists
-include .local-registry-env

# Default fallback if not loaded
REGISTRY_URL ?= localhost:5000

install-dev:
	uv sync --extra dev

lint:
	uv run ruff check .

format:
	uv run ruff format .

test:
	uv run pytest

test-cov:
	uv run pytest --cov=app --cov=controller --cov-config=.coveragerc --cov-fail-under=80

typecheck:
	uv run --extra dev mypy app controller

release-metadata:
	uv run python hack/validate_release_metadata.py

manifests-validate:
	uv run python hack/validate_manifests.py \
		--overlay test \
		--overlay prod \
		--controller-egress-patch examples/controller-apiserver-cidr-patch.yaml \
		--controller-egress-patch examples/controller-egress/eks.patch.yaml \
		--controller-egress-patch examples/controller-egress/gke.patch.yaml \
		--controller-egress-patch examples/controller-egress/aks.patch.yaml
	uv run python hack/check_immutable_images.py
	uv run python hack/validate_trivyignore.py
	uv run python hack/validate_deployment_order.py

manifests: manifests-validate
	kustomize build k8s/namespace >/dev/null
	kustomize build k8s/monitoring >/dev/null
	kustomize build k8s/overlays/test >/dev/null
	kustomize build k8s/overlays/prod >/dev/null
	kustomize build k8s/istio-ingress >/dev/null
	kustomize build k8s/controller >/dev/null

production-gate:
	uv run python hack/validate_production_evidence.py

check: lint typecheck release-metadata test manifests

check-ci-core: lint typecheck release-metadata test-cov manifests

cleanup-local:
	./hack/cleanup-local-dev.sh

curl-test:
	@bash -c 'kubectl port-forward -n shipshape svc/helloworld-test 58291:80 &>/dev/null & p=$$!; sleep 1; curl -s localhost:58291; echo; kill $$p 2>/dev/null'

curl-prod:
	@bash -c 'kubectl port-forward -n shipshape svc/helloworld-prod 58292:80 &>/dev/null & p=$$!; sleep 1; curl -s localhost:58292; echo; kill $$p 2>/dev/null'

ENV ?= test
deploy:
	@echo "Current MESSAGE: $$(grep 'MESSAGE:' k8s/overlays/$(ENV)/app-vars.yaml | sed 's/.*MESSAGE: //')"
	@read -p "Enter new MESSAGE (or press Enter to keep): " msg; \
	if [ -n "$$msg" ]; then \
		sed -i'' -e "s/^  MESSAGE: .*/  MESSAGE: $$msg/" k8s/overlays/$(ENV)/app-vars.yaml; \
		echo "Updated MESSAGE to: $$msg"; \
	fi
	@prev_img=$$(kubectl -n shipshape get deployment helloworld-$(ENV) -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true); \
	kubectl apply -k k8s/overlays/$(ENV); \
	kust_img=$$(kustomize build k8s/overlays/$(ENV) 2>/dev/null | grep 'image:' | head -1 | awk '{print $$2}'); \
	if [ -n "$$prev_img" ] && [ "$$prev_img" != "$$kust_img" ]; then \
		echo "Restoring image: $$prev_img (kustomize has stale ref)"; \
		kubectl -n shipshape set image deployment/helloworld-$(ENV) helloworld="$$prev_img"; \
	fi
	kubectl rollout restart deployment/helloworld-$(ENV) -n shipshape
	kubectl rollout status deployment/helloworld-$(ENV) -n shipshape --timeout=120s
	@bash -c 'kubectl port-forward -n shipshape svc/helloworld-$(ENV) 58291:80 &>/dev/null & p=$$!; sleep 1; curl -s localhost:58291; echo; kill $$p 2>/dev/null'

# Safe deploy that ensures images are rebuilt, pushed immutably, and manifests updated before applying
deploy-fresh: registry-up push-images-immutable deploy

registry-up:
	./scripts/setup-local-registry.sh

build-images:
	./scripts/manage-images.sh --registry $(REGISTRY_URL)

push-images:
	./scripts/manage-images.sh --registry $(REGISTRY_URL) --push

push-images-immutable:
	./scripts/manage-images.sh --registry $(REGISTRY_URL) --push --immutable

ci-smoke-test:
	./scripts/ci-smoke-test.sh
