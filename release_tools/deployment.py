"""Opt-in Railway deployment planning with a safe no-op default.

This module deliberately plans rather than performs deployments.  A later
workflow may consume a ready plan and invoke an operator-approved Railway
command.  Missing opt-in or configuration always produces a skip result, and
secret values are never included in the result.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

RAILWAY_DEPLOY_OPT_IN_ENV = "SOMETHINGBOUND_RAILWAY_DEPLOY"
RAILWAY_PROJECT_ID_ENV = "RAILWAY_PROJECT_ID"
RAILWAY_SERVICE_ID_ENV = "RAILWAY_SERVICE_ID"
RAILWAY_TOKEN_ENV = "RAILWAY_TOKEN"
_REQUIRED_RAILWAY_CONFIGURATION = (
    RAILWAY_PROJECT_ID_ENV,
    RAILWAY_SERVICE_ID_ENV,
    RAILWAY_TOKEN_ENV,
)
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class RailwayDeploymentPlan:
    """A sanitized decision for a workflow to consume.

    ``action == 'ready'`` means all required operator configuration is present;
    it is not evidence that Railway was contacted or that a deployment
    succeeded.  The plan intentionally contains no token, project ID, or
    service ID values.
    """

    action: Literal["skip", "ready"]
    reason: str

    @property
    def should_dispatch(self) -> bool:
        return self.action == "ready"


def _present(values: Mapping[str, object], name: str) -> bool:
    value = values.get(name)
    return isinstance(value, str) and bool(value.strip())


def plan_railway_deployment(
    env: Mapping[str, object] | None = None,
) -> RailwayDeploymentPlan:
    """Return a deterministic, sanitized Railway deployment decision.

    Deployment is disabled unless ``SOMETHINGBOUND_RAILWAY_DEPLOY`` is an
    explicit true-like value.  Even when enabled, all project/service/token
    values must be supplied by the operator or workflow environment.  No
    Railway API or CLI is invoked here.
    """

    values: Mapping[str, object] = os.environ if env is None else env
    opt_in = values.get(RAILWAY_DEPLOY_OPT_IN_ENV)
    if not isinstance(opt_in, str) or opt_in.strip().lower() not in _TRUE_VALUES:
        return RailwayDeploymentPlan(
            "skip",
            "Railway deployment is disabled; explicit opt-in is required",
        )

    missing = [
        name for name in _REQUIRED_RAILWAY_CONFIGURATION if not _present(values, name)
    ]
    if missing:
        return RailwayDeploymentPlan(
            "skip",
            "Railway deployment skipped; missing configuration: " + ", ".join(missing),
        )

    return RailwayDeploymentPlan(
        "ready",
        "Railway deployment is opted in and configured; no deployment was executed",
    )


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Plan an opt-in Railway deployment without contacting Railway."
    )


def main(argv: list[str] | None = None) -> int:
    # Parse arguments even though the current contract has no flags, so an
    # accidental invocation with unsupported options fails safely.
    build_parser().parse_args(argv)
    plan = plan_railway_deployment()
    print(f"railway deployment: {plan.action} ({plan.reason})")
    return 0


__all__ = [
    "RAILWAY_DEPLOY_OPT_IN_ENV",
    "RAILWAY_PROJECT_ID_ENV",
    "RAILWAY_SERVICE_ID_ENV",
    "RAILWAY_TOKEN_ENV",
    "RailwayDeploymentPlan",
    "plan_railway_deployment",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())