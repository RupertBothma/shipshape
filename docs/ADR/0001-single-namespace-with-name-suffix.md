# ADR 0001: Single Namespace With Kustomize Name Suffix

- Status: Accepted
- Date: 2026-02-08
- Deciders: Shipshape maintainers
- Supersedes: none

## Context
The platform requirement is to run `test` and `prod` in the same cluster and namespace while preventing resource-name collisions.

## Decision
Deploy both environments in namespace `shipshape` and differentiate resources with Kustomize `nameSuffix` (`-test`, `-prod`) plus `env` labels.

## Consequences
- Positive:
  - Meets runtime constraint without duplicating base manifests.
  - Keeps routing, policy, and monitoring queries environment-scoped through labels.
- Negative:
  - Namespace-level misconfiguration can impact both environments.
  - RBAC remains namespace-scoped, not namespace-isolated per environment.
- Follow-up work:
  - Re-evaluate split namespace or split cluster topology when stricter isolation is required.

## Alternatives Considered
1. Separate namespaces (`shipshape-test`, `shipshape-prod`)
2. Separate clusters for test and prod
