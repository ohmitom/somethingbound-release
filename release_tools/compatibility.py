"""Deterministic launcher and server compatibility decisions.

A release manifest declares the lowest version in each compatibility dimension
that it supports.  This module applies those declarations without contacting a
network or a deployment provider.  A launcher can therefore select a release
locally, while an operator or server health check can use the same rules before
promotion.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .manifest import (
    VERSION_KEYS,
    _semver_key,
    compare_versions,
    load_manifest,
    validate_manifest,
)

# Client updates need the installed client and the shared data/protocol
# dimensions.  The server dimension is checked after endpoint health reports
# the selected server's versions.
CLIENT_COMPATIBILITY_KEYS = ("client", "protocol", "content", "ruleset", "schema")
SERVER_COMPATIBILITY_KEYS = VERSION_KEYS


def _available_version_errors(
    available_versions: Mapping[str, Any], keys: Sequence[str]
) -> list[str]:
    errors: list[str] = []
    for key in keys:
        if key not in available_versions:
            errors.append(f"availableVersions.{key}: is required")
            continue
        value = available_versions[key]
        if _semver_key(value) is None:
            errors.append(f"availableVersions.{key}: must be a SemVer 2.0.0 value")
    return errors


def compatibility_errors(
    manifest: Any,
    available_versions: Mapping[str, Any],
    *,
    keys: Sequence[str],
) -> list[str]:
    """Return why available versions cannot use ``manifest`` safely.

    ``keys`` makes the same primitive usable for a launcher (which has no
    server version until endpoint health is checked) and for server promotion.
    Invalid manifests are rejected rather than being interpreted partially.
    """

    errors = validate_manifest(manifest)
    if errors:
        return [f"manifest: {error}" for error in errors]
    if not isinstance(available_versions, Mapping):
        return ["availableVersions: must be an object"]

    errors = _available_version_errors(available_versions, keys)
    if errors:
        return errors

    minimum_versions = manifest["minimumCompatibleVersions"]
    for key in keys:
        available = available_versions[key]
        minimum = minimum_versions[key]
        if compare_versions(available, minimum) < 0:
            errors.append(
                f"availableVersions.{key}: {available} is below "
                f"minimumCompatibleVersions.{key} ({minimum})"
            )
    return errors


def client_compatibility_errors(
    manifest: Any, installed_versions: Mapping[str, Any]
) -> list[str]:
    """Return compatibility errors for a launcher update candidate."""

    return compatibility_errors(
        manifest,
        installed_versions,
        keys=CLIENT_COMPATIBILITY_KEYS,
    )


def server_compatibility_errors(
    manifest: Any, server_versions: Mapping[str, Any]
) -> list[str]:
    """Return compatibility errors for a server health/promotion result."""

    return compatibility_errors(manifest, server_versions, keys=SERVER_COMPATIBILITY_KEYS)


def select_update(
    candidates: Iterable[Mapping[str, Any]],
    *,
    current_release: str,
    installed_versions: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Select the greatest compatible release newer than ``current_release``.

    Malformed, incompatible, and older/equal candidates are safely ignored.
    Ties are resolved by the release Git SHA, so selection does not depend on
    the order in which a launcher received candidate manifests.  Invalid local
    version state is a configuration error and raises ``ValueError``.
    """

    if _semver_key(current_release) is None:
        raise ValueError("current_release must be a SemVer 2.0.0 value")
    if not isinstance(installed_versions, Mapping):
        raise ValueError("installed_versions must be an object")
    state_errors = _available_version_errors(
        installed_versions, CLIENT_COMPATIBILITY_KEYS
    )
    if state_errors:
        raise ValueError("; ".join(state_errors))

    compatible: list[Mapping[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        manifest_errors = validate_manifest(candidate)
        if manifest_errors:
            continue
        release_version = candidate["release"]["version"]
        if compare_versions(release_version, current_release) <= 0:
            continue
        if client_compatibility_errors(candidate, installed_versions):
            continue
        compatible.append(candidate)

    if not compatible:
        return None
    return max(
        compatible,
        key=lambda candidate: (
            _semver_key(candidate["release"]["version"]),
            candidate["release"]["gitSha"],
        ),
    )


def _version_argument(value: str) -> tuple[str, str]:
    key, separator, version = value.partition("=")
    if not separator or key not in CLIENT_COMPATIBILITY_KEYS or not version:
        allowed = ", ".join(CLIENT_COMPATIBILITY_KEYS)
        raise argparse.ArgumentTypeError(f"must use KEY=VERSION where KEY is one of: {allowed}")
    return key, version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select the greatest compatible SomethingBound release manifest."
    )
    parser.add_argument(
        "current_release",
        help="currently installed human-facing release version",
    )
    parser.add_argument(
        "manifests",
        nargs="+",
        type=Path,
        help="candidate manifest JSON files",
    )
    parser.add_argument(
        "--version",
        dest="versions",
        action="append",
        type=_version_argument,
        metavar="KEY=VERSION",
        help="installed version dimension (repeat for client, protocol, content, ruleset, schema)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    installed_versions = dict(args.versions or [])
    try:
        candidates = [load_manifest(path) for path in args.manifests]
        selected = select_update(
            candidates,
            current_release=args.current_release,
            installed_versions=installed_versions,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if selected is None:
        print("no compatible update")
    else:
        release = selected["release"]
        print(f"selected update: {release['version']} ({release['gitSha']})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
