"""Validation primitives for the version 1 SomethingBound release manifest.

The JSON schema in ``schema/release-manifest.schema.json`` is the public
machine-readable contract.  This module deliberately uses only the Python
standard library so that validation is deterministic in local checks and CI.
Cross-field rules that JSON Schema cannot express (for example, release-note
range identity) live here.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SUPPORTED_MANIFEST_SCHEMA_VERSION = 1
VERSION_KEYS = ("client", "server", "protocol", "content", "ruleset", "schema")

_MISSING = object()
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)


class ManifestValidationError(ValueError):
    """Raised when a manifest cannot be used safely."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _error(errors: list[str], path: str, message: str) -> None:
    errors.append(f"{path or '$'}: {message}")


def _object(
    value: Any,
    path: str,
    errors: list[str],
    *,
    required: tuple[str, ...] = (),
    allowed: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        _error(errors, path, "must be an object")
        return None

    for key in required:
        if key not in value:
            _error(errors, f"{path}.{key}" if path else key, "is required")
    if allowed is not None:
        for key in sorted(set(value) - set(allowed)):
            _error(errors, f"{path}.{key}" if path else key, "is not permitted")
    return value


def _semver_key(value: Any) -> tuple[Any, ...] | None:
    if not isinstance(value, str):
        return None
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        return None

    prerelease = match.group(4)
    if prerelease is None:
        prerelease_key: tuple[Any, ...] = ((2, ""),)
    else:
        identifiers: list[tuple[int, int | str]] = []
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                # SemVer forbids leading zeroes in numeric prerelease IDs.
                if len(identifier) > 1 and identifier.startswith("0"):
                    return None
                identifiers.append((0, int(identifier)))
            else:
                identifiers.append((1, identifier))
        prerelease_key = tuple(identifiers)

    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        prerelease_key,
    )


def _check_semver(value: Any, path: str, errors: list[str]) -> str | None:
    if _semver_key(value) is None:
        _error(errors, path, "must be a SemVer 2.0.0 value")
        return None
    return value


def _check_https_url(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        _error(errors, path, "must be an HTTPS URL without whitespace")
        return
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError:
        parsed = None
        hostname = None
        has_credentials = False
    if parsed is None or parsed.scheme != "https" or not parsed.netloc or hostname is None:
        _error(errors, path, "must be an HTTPS URL")
    elif has_credentials:
        _error(errors, path, "must not contain credentials")


def _check_timestamp(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        _error(errors, path, "must be a UTC RFC 3339 timestamp ending in Z")
        return
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _error(errors, path, "must be a valid UTC RFC 3339 timestamp")


def _check_version_set(
    value: Any, path: str, errors: list[str]
) -> dict[str, str] | None:
    obj = _object(
        value,
        path,
        errors,
        required=VERSION_KEYS,
        allowed=VERSION_KEYS,
    )
    if obj is None:
        return None
    result: dict[str, str] = {}
    for key in VERSION_KEYS:
        if key in obj:
            parsed = _check_semver(obj[key], f"{path}.{key}", errors)
            if parsed is not None:
                result[key] = parsed
    return result


def _check_reference(value: Any, path: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    obj = _object(
        value,
        path,
        errors,
        required=("version", "manifestUrl"),
        allowed=("version", "manifestUrl"),
    )
    if obj is None:
        return None
    version = _check_semver(obj.get("version"), f"{path}.version", errors)
    if "manifestUrl" in obj:
        _check_https_url(obj["manifestUrl"], f"{path}.manifestUrl", errors)
    return version


def _check_artifact(value: Any, path: str, errors: list[str]) -> None:
    obj = _object(
        value,
        path,
        errors,
        required=("url", "sha256", "sizeBytes"),
        allowed=("url", "sha256", "sizeBytes"),
    )
    if obj is None:
        return

    if "url" in obj:
        _check_https_url(obj["url"], f"{path}.url", errors)

    checksum = obj.get("sha256", _MISSING)
    if checksum is _MISSING:
        pass
    elif not isinstance(checksum, str) or _SHA256_RE.fullmatch(checksum) is None:
        _error(errors, f"{path}.sha256", "must be 64 lowercase hexadecimal characters")

    size = obj.get("sizeBytes", _MISSING)
    if size is not _MISSING and (
        isinstance(size, bool) or not isinstance(size, int) or size < 0
    ):
        _error(errors, f"{path}.sizeBytes", "must be a non-negative integer")


def compare_versions(left: str, right: str) -> int:
    """Compare two already validated SemVer values.

    The validator calls this only after checking both values.  Keeping the
    comparison here gives launcher and deployment checks one deterministic
    SemVer ordering implementation to share.
    """

    left_key = _semver_key(left)
    right_key = _semver_key(right)
    if left_key is None or right_key is None:  # pragma: no cover - guarded callers
        raise ValueError("cannot compare invalid semantic versions")
    return (left_key > right_key) - (left_key < right_key)


def validate_manifest(manifest: Any) -> list[str]:
    """Return deterministic validation errors for a decoded manifest object."""

    errors: list[str] = []
    root = _object(
        manifest,
        "",
        errors,
        required=(
            "manifestSchemaVersion",
            "release",
            "versions",
            "minimumCompatibleVersions",
            "artifacts",
        ),
        allowed=(
            "manifestSchemaVersion",
            "release",
            "versions",
            "minimumCompatibleVersions",
            "artifacts",
        ),
    )
    if root is None:
        return errors

    schema_version = root.get("manifestSchemaVersion", _MISSING)
    if schema_version is not _MISSING and (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SUPPORTED_MANIFEST_SCHEMA_VERSION
    ):
        _error(
            errors,
            "manifestSchemaVersion",
            f"must be {SUPPORTED_MANIFEST_SCHEMA_VERSION}",
        )

    release = _object(
        root.get("release"),
        "release",
        errors,
        required=(
            "version",
            "gitSha",
            "buildTimestamp",
            "releaseNotes",
            "previousRelease",
            "rollbackTarget",
        ),
        allowed=(
            "version",
            "gitSha",
            "buildTimestamp",
            "releaseNotes",
            "previousRelease",
            "rollbackTarget",
        ),
    )

    release_version: str | None = None
    previous_value: Any = _MISSING
    rollback_value: Any = _MISSING
    previous_version: str | None = None
    rollback_version: str | None = None
    if release is not None:
        release_version = _check_semver(
            release.get("version"), "release.version", errors
        )

        git_sha = release.get("gitSha", _MISSING)
        if git_sha is not _MISSING and (
            not isinstance(git_sha, str) or _GIT_SHA_RE.fullmatch(git_sha) is None
        ):
            _error(errors, "release.gitSha", "must be 40 lowercase hexadecimal characters")

        if "buildTimestamp" in release:
            _check_timestamp(release["buildTimestamp"], "release.buildTimestamp", errors)

        previous_value = release.get("previousRelease", _MISSING)
        if previous_value is not _MISSING:
            previous_version = _check_reference(
                previous_value, "release.previousRelease", errors
            )
        rollback_value = release.get("rollbackTarget", _MISSING)
        if rollback_value is not _MISSING:
            rollback_version = _check_reference(
                rollback_value, "release.rollbackTarget", errors
            )

        notes = _object(
            release.get("releaseNotes"),
            "release.releaseNotes",
            errors,
            required=("range", "text"),
            allowed=("range", "text", "url"),
        )
        notes_from: str | None = None
        notes_to: str | None = None
        if notes is not None:
            if "text" in notes and (
                not isinstance(notes["text"], str) or not notes["text"].strip()
            ):
                _error(errors, "release.releaseNotes.text", "must not be empty")
            if "url" in notes:
                _check_https_url(notes["url"], "release.releaseNotes.url", errors)

            note_range = _object(
                notes.get("range"),
                "release.releaseNotes.range",
                errors,
                required=("from", "to"),
                allowed=("from", "to"),
            )
            if note_range is not None:
                if "from" in note_range:
                    if note_range["from"] is not None:
                        notes_from = _check_semver(
                            note_range["from"],
                            "release.releaseNotes.range.from",
                            errors,
                        )
                if "to" in note_range:
                    notes_to = _check_semver(
                        note_range["to"],
                        "release.releaseNotes.range.to",
                        errors,
                    )

                if release_version is not None and notes_to is not None:
                    if notes_to != release_version:
                        _error(
                            errors,
                            "release.releaseNotes.range.to",
                            "must equal release.version",
                        )
                if previous_value is None and "from" in note_range:
                    if note_range["from"] is not None:
                        _error(
                            errors,
                            "release.releaseNotes.range.from",
                            "must be null when previousRelease is null",
                        )
                elif previous_version is not None and notes_from != previous_version:
                    _error(
                        errors,
                        "release.releaseNotes.range.from",
                        "must equal previousRelease.version",
                    )

    current_versions = _check_version_set(root.get("versions"), "versions", errors)
    minimum_versions = _check_version_set(
        root.get("minimumCompatibleVersions"),
        "minimumCompatibleVersions",
        errors,
    )
    if current_versions is not None and minimum_versions is not None:
        for key in VERSION_KEYS:
            current = current_versions.get(key)
            minimum = minimum_versions.get(key)
            if current is not None and minimum is not None and compare_versions(minimum, current) > 0:
                _error(
                    errors,
                    f"minimumCompatibleVersions.{key}",
                    f"must not be newer than versions.{key}",
                )

    artifacts = _object(
        root.get("artifacts"),
        "artifacts",
        errors,
        required=("client", "server"),
        allowed=("client", "server"),
    )
    if artifacts is not None:
        for kind in ("client", "server"):
            if kind in artifacts:
                _check_artifact(artifacts[kind], f"artifacts.{kind}", errors)

    if release_version is not None and previous_version is not None:
        if compare_versions(previous_version, release_version) >= 0:
            _error(
                errors,
                "release.previousRelease.version",
                "must be older than release.version",
            )
    if release_version is not None and rollback_version is not None:
        if compare_versions(rollback_version, release_version) >= 0:
            _error(
                errors,
                "release.rollbackTarget.version",
                "must be older than release.version",
            )

    return errors


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load and validate a JSON manifest, raising one error containing all issues."""

    manifest_path = Path(path)
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except OSError as exc:
        raise ManifestValidationError([f"{manifest_path}: cannot read manifest ({exc})"]) from exc
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(
            [f"{manifest_path}: invalid JSON at line {exc.lineno}, column {exc.colno}"]
        ) from exc

    errors = validate_manifest(manifest)
    if errors:
        raise ManifestValidationError(errors)
    return manifest


def verify_artifact(manifest: Any, kind: str, path: str | Path) -> list[str]:
    """Verify one local artifact against its manifest checksum and byte size."""

    errors = validate_manifest(manifest)
    if errors:
        return errors

    artifact = manifest["artifacts"].get(kind)
    if artifact is None:
        return [f"artifacts.{kind}: unknown artifact kind"]

    artifact_path = Path(path)
    digest = hashlib.sha256()
    size = 0
    try:
        with artifact_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        return [f"artifacts.{kind}: cannot read {artifact_path} ({exc})"]

    result: list[str] = []
    if size != artifact["sizeBytes"]:
        result.append(
            f"artifacts.{kind}: size mismatch (expected {artifact['sizeBytes']}, got {size})"
        )
    actual_checksum = digest.hexdigest()
    if actual_checksum != artifact["sha256"]:
        result.append(
            f"artifacts.{kind}: SHA-256 mismatch "
            f"(expected {artifact['sha256']}, got {actual_checksum})"
        )
    return result
