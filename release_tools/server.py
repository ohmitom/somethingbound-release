"""Safe server endpoint selection and health compatibility primitives.

The launcher may receive a manually selected endpoint or an endpoint exposed by
an operator-managed Railway service.  This module only resolves configuration
and validates decoded health data; it never performs network requests or
assumes that a server is publicly reachable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from os import environ
from typing import Any, Literal
from urllib.parse import urlsplit

from .compatibility import server_compatibility_errors
from .manifest import _semver_key, validate_manifest

MANUAL_SERVER_ENDPOINT_ENV = "SOMETHINGBOUND_SERVER_ENDPOINT"
RAILWAY_SERVER_ENDPOINT_ENV = "RAILWAY_SERVER_ENDPOINT"
RAILWAY_PUBLIC_DOMAIN_ENV = "RAILWAY_PUBLIC_DOMAIN"

_HEALTH_KEYS = ("status", "release", "versions", "migrationLevel")
_HEALTH_RELEASE_KEYS = ("version", "gitSha")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ServerEndpointConfigurationError(ValueError):
    """Raised when a configured server endpoint is unsafe or malformed."""


@dataclass(frozen=True)
class ServerEndpoint:
    """The endpoint selected for a launcher session.

    ``source`` is intentionally explicit so a launcher can show whether it is
    using an operator choice, a Railway-provided value, or the manual
    fallback.  ``url`` is ``None`` for the fallback and must not be treated as
    a default public server.
    """

    url: str | None
    source: Literal["manual", "railway", "manual-fallback"]

    @property
    def requires_manual_selection(self) -> bool:
        return self.url is None


def _clean_optional(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ServerEndpointConfigurationError(f"{name} must be a string")
    value = value.strip()
    return value or None


def validate_server_endpoint(value: str, name: str = "server endpoint") -> str:
    """Return ``value`` when it is a safe server endpoint, or raise.

    HTTPS is required everywhere except a localhost development server, and
    credentials are never accepted.  ``name`` appears in the error so a user
    interface can say which field was refused.
    """

    return _validate_endpoint(value, name)


def _validate_endpoint(value: str, name: str) -> str:
    if any(character.isspace() for character in value):
        raise ServerEndpointConfigurationError(f"{name} must not contain whitespace")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing port validates malformed port syntax as well.
        parsed.port
    except ValueError as exc:
        raise ServerEndpointConfigurationError(f"{name} must be a valid URL") from exc

    if parsed.username is not None or parsed.password is not None:
        raise ServerEndpointConfigurationError(f"{name} must not contain credentials")
    if not parsed.netloc or hostname is None:
        raise ServerEndpointConfigurationError(f"{name} must be an HTTPS URL")

    normalized_host = hostname.rstrip(".").lower()
    if parsed.scheme == "https":
        return value
    if parsed.scheme == "http" and normalized_host in _LOCAL_HOSTS:
        # Local HTTP is useful for an explicitly selected development server;
        # all discovered or non-local endpoints must use TLS.
        return value
    raise ServerEndpointConfigurationError(
        f"{name} must use HTTPS (HTTP is permitted only for localhost development)"
    )


def resolve_server_endpoint(
    *, manual_endpoint: str | None = None, railway_endpoint: str | None = None
) -> ServerEndpoint:
    """Resolve an endpoint with manual selection taking precedence.

    No endpoint is synthesized when neither source is configured.  The caller
    receives a manual-fallback result and must ask the operator to select one.
    """

    manual = _clean_optional(manual_endpoint, "manual endpoint")
    if manual is not None:
        return ServerEndpoint(_validate_endpoint(manual, "manual endpoint"), "manual")

    railway = _clean_optional(railway_endpoint, "Railway endpoint")
    if railway is not None:
        return ServerEndpoint(_validate_endpoint(railway, "Railway endpoint"), "railway")

    return ServerEndpoint(None, "manual-fallback")


def resolve_server_endpoint_from_environment(
    env: Mapping[str, Any] | None = None,
) -> ServerEndpoint:
    """Resolve launcher endpoint configuration without contacting Railway.

    ``RAILWAY_SERVER_ENDPOINT`` is the preferred operator-provided value.  If
    it is absent, Railway's standard ``RAILWAY_PUBLIC_DOMAIN`` value is
    accepted as a hostname and receives an HTTPS scheme.  A manually selected
    ``SOMETHINGBOUND_SERVER_ENDPOINT`` always wins.
    """

    values: Mapping[str, Any] = environ if env is None else env
    railway_endpoint = values.get(RAILWAY_SERVER_ENDPOINT_ENV)
    if _clean_optional(railway_endpoint, RAILWAY_SERVER_ENDPOINT_ENV) is None:
        public_domain = _clean_optional(
            values.get(RAILWAY_PUBLIC_DOMAIN_ENV), RAILWAY_PUBLIC_DOMAIN_ENV
        )
        if public_domain is not None and "://" not in public_domain:
            railway_endpoint = f"https://{public_domain}"
        else:
            railway_endpoint = public_domain

    return resolve_server_endpoint(
        manual_endpoint=values.get(MANUAL_SERVER_ENDPOINT_ENV),
        railway_endpoint=railway_endpoint,
    )


def _health_object(
    value: Any,
    path: str,
    errors: list[str],
    *,
    required: tuple[str, ...],
    allowed: tuple[str, ...],
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{path}: must be an object")
        return None
    for key in required:
        if key not in value:
            errors.append(f"{path}.{key}: is required")
    for key in sorted(set(value) - set(allowed)):
        errors.append(f"{path}.{key}: is not permitted")
    return dict(value)


def validate_server_health(
    manifest: Any,
    health: Any,
    *,
    expected_migration_level: int | None = None,
) -> list[str]:
    """Return errors for a server health response tied to one release.

    A healthy response must identify the exact manifest release (version and
    source SHA), expose all compatibility dimensions, and report a non-negative
    integer migration level.  The optional expected migration level is supplied
    by an operator/deployment hook; this function does not execute migrations.
    """

    if expected_migration_level is not None and (
        isinstance(expected_migration_level, bool)
        or not isinstance(expected_migration_level, int)
        or expected_migration_level < 0
    ):
        raise ValueError("expected_migration_level must be a non-negative integer")

    errors: list[str] = []
    manifest_errors = validate_manifest(manifest)
    if manifest_errors:
        return [f"manifest: {error}" for error in manifest_errors]

    root = _health_object(
        health,
        "health",
        errors,
        required=_HEALTH_KEYS,
        allowed=_HEALTH_KEYS,
    )
    if root is None:
        return errors

    if root.get("status") != "ok":
        errors.append("health.status: must be 'ok'")

    release = _health_object(
        root.get("release"),
        "health.release",
        errors,
        required=_HEALTH_RELEASE_KEYS,
        allowed=_HEALTH_RELEASE_KEYS,
    )
    if release is not None:
        health_version = release.get("version")
        if _semver_key(health_version) is None:
            errors.append("health.release.version: must be a SemVer 2.0.0 value")
        elif health_version != manifest["release"]["version"]:
            errors.append(
                "health.release.version: must equal manifest release.version "
                f"({manifest['release']['version']})"
            )

        health_sha = release.get("gitSha")
        if not isinstance(health_sha, str) or _GIT_SHA_RE.fullmatch(health_sha) is None:
            errors.append(
                "health.release.gitSha: must be 40 lowercase hexadecimal characters"
            )
        elif health_sha != manifest["release"]["gitSha"]:
            errors.append(
                "health.release.gitSha: must equal manifest release.gitSha "
                f"({manifest['release']['gitSha']})"
            )

    versions = root.get("versions")
    if not isinstance(versions, Mapping):
        errors.append("health.versions: must be an object")
    else:
        compatibility_errors = server_compatibility_errors(manifest, versions)
        errors.extend(
            error.replace("availableVersions.", "health.versions.", 1)
            for error in compatibility_errors
        )

    migration_level = root.get("migrationLevel")
    if (
        isinstance(migration_level, bool)
        or not isinstance(migration_level, int)
        or migration_level < 0
    ):
        errors.append("health.migrationLevel: must be a non-negative integer")
    elif (
        expected_migration_level is not None
        and migration_level != expected_migration_level
    ):
        errors.append(
            "health.migrationLevel: "
            f"{migration_level} does not match expected migration level "
            f"({expected_migration_level})"
        )

    return errors


__all__ = [
    "MANUAL_SERVER_ENDPOINT_ENV",
    "RAILWAY_PUBLIC_DOMAIN_ENV",
    "RAILWAY_SERVER_ENDPOINT_ENV",
    "ServerEndpoint",
    "ServerEndpointConfigurationError",
    "resolve_server_endpoint",
    "resolve_server_endpoint_from_environment",
    "validate_server_endpoint",
    "validate_server_health",
]


