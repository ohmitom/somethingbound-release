from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from release_tools.deployment import (
    RAILWAY_DEPLOY_OPT_IN_ENV,
    RAILWAY_PROJECT_ID_ENV,
    RAILWAY_SERVICE_ID_ENV,
    RAILWAY_TOKEN_ENV,
    plan_railway_deployment,
)
from release_tools.server import (
    ServerEndpointConfigurationError,
    resolve_server_endpoint,
    resolve_server_endpoint_from_environment,
    validate_server_health,
)


ROOT = Path(__file__).resolve().parents[1]
UPDATE_MANIFEST_PATH = ROOT / "fixtures" / "manifest.update.json"
HEALTH_PATH = ROOT / "fixtures" / "server-health.ok.json"


class ServerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(UPDATE_MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.health = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))

    def test_manual_endpoint_wins_over_railway_endpoint(self) -> None:
        endpoint = resolve_server_endpoint(
            manual_endpoint="https://manual.example.invalid/game",
            railway_endpoint="https://railway.example.invalid/game",
        )

        self.assertEqual(endpoint.url, "https://manual.example.invalid/game")
        self.assertEqual(endpoint.source, "manual")
        self.assertFalse(endpoint.requires_manual_selection)

    def test_environment_uses_railway_public_domain_when_configured(self) -> None:
        endpoint = resolve_server_endpoint_from_environment(
            {"RAILWAY_PUBLIC_DOMAIN": "game.example.invalid"}
        )

        self.assertEqual(endpoint.url, "https://game.example.invalid")
        self.assertEqual(endpoint.source, "railway")

    def test_missing_endpoint_returns_explicit_manual_fallback(self) -> None:
        endpoint = resolve_server_endpoint_from_environment({})

        self.assertIsNone(endpoint.url)
        self.assertEqual(endpoint.source, "manual-fallback")
        self.assertTrue(endpoint.requires_manual_selection)

    def test_public_endpoint_rejects_credentials_and_plain_http(self) -> None:
        with self.assertRaises(ServerEndpointConfigurationError):
            resolve_server_endpoint(
                manual_endpoint="https://user:password@example.invalid/game"
            )
        with self.assertRaises(ServerEndpointConfigurationError):
            resolve_server_endpoint(manual_endpoint="http://game.example.invalid/game")

    def test_local_development_endpoint_can_be_selected_explicitly(self) -> None:
        endpoint = resolve_server_endpoint(manual_endpoint="http://localhost:8080")

        self.assertEqual(endpoint.url, "http://localhost:8080")
        self.assertEqual(endpoint.source, "manual")

    def test_server_health_fixture_matches_one_release_identity(self) -> None:
        self.assertEqual(
            validate_server_health(
                self.manifest,
                self.health,
                expected_migration_level=7,
            ),
            [],
        )

    def test_server_health_rejects_identity_compatibility_and_migration_mismatches(self) -> None:
        health = copy.deepcopy(self.health)
        health["release"]["gitSha"] = "0123456789abcdef0123456789abcdef01234567"
        health["versions"]["protocol"] = "2.0.0"
        health["migrationLevel"] = 6

        errors = validate_server_health(
            self.manifest,
            health,
            expected_migration_level=7,
        )

        self.assertIn(
            "health.release.gitSha: must equal manifest release.gitSha "
            "(fedcba9876543210fedcba9876543210fedcba98)",
            errors,
        )
        self.assertIn(
            "health.versions.protocol: 2.0.0 is below "
            "minimumCompatibleVersions.protocol (2.1.0)",
            errors,
        )
        self.assertIn(
            "health.migrationLevel: 6 does not match expected migration level (7)",
            errors,
        )

    def test_railway_deployment_is_a_no_op_without_explicit_opt_in(self) -> None:
        plan = plan_railway_deployment(
            {
                RAILWAY_PROJECT_ID_ENV: "project-placeholder",
                RAILWAY_SERVICE_ID_ENV: "service-placeholder",
                RAILWAY_TOKEN_ENV: "secret-placeholder",
            }
        )

        self.assertEqual(plan.action, "skip")
        self.assertFalse(plan.should_dispatch)
        self.assertNotIn("secret-placeholder", plan.reason)

    def test_railway_deployment_skips_when_opted_in_but_configuration_is_missing(self) -> None:
        plan = plan_railway_deployment({RAILWAY_DEPLOY_OPT_IN_ENV: "true"})

        self.assertEqual(plan.action, "skip")
        self.assertFalse(plan.should_dispatch)
        self.assertIn(RAILWAY_PROJECT_ID_ENV, plan.reason)
        self.assertIn(RAILWAY_SERVICE_ID_ENV, plan.reason)
        self.assertIn(RAILWAY_TOKEN_ENV, plan.reason)

    def test_railway_deployment_is_only_ready_when_all_values_are_present(self) -> None:
        plan = plan_railway_deployment(
            {
                RAILWAY_DEPLOY_OPT_IN_ENV: "true",
                RAILWAY_PROJECT_ID_ENV: "project-placeholder",
                RAILWAY_SERVICE_ID_ENV: "service-placeholder",
                RAILWAY_TOKEN_ENV: "secret-placeholder",
            }
        )

        self.assertEqual(plan.action, "ready")
        self.assertTrue(plan.should_dispatch)
        self.assertNotIn("project-placeholder", plan.reason)
        self.assertNotIn("service-placeholder", plan.reason)
        self.assertNotIn("secret-placeholder", plan.reason)

    def test_deployment_cli_never_prints_token_value(self) -> None:
        token = "secret-placeholder"
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                RAILWAY_DEPLOY_OPT_IN_ENV,
                RAILWAY_PROJECT_ID_ENV,
                RAILWAY_SERVICE_ID_ENV,
                RAILWAY_TOKEN_ENV,
            }
        }
        env.update(
            {
                RAILWAY_DEPLOY_OPT_IN_ENV: "true",
                RAILWAY_PROJECT_ID_ENV: "project-placeholder",
                RAILWAY_SERVICE_ID_ENV: "service-placeholder",
                RAILWAY_TOKEN_ENV: token,
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "release_tools.deployment"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("railway deployment: ready", result.stdout)
        self.assertNotIn(token, result.stdout)
        self.assertNotIn(token, result.stderr)


if __name__ == "__main__":
    unittest.main()
