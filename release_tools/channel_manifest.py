"""Release-channel manifest validation for the launcher foundation.

The channel manifest is intentionally small and dependency-free.  JSON Schema
is shipped for consumers that have a schema validator; this module performs the
same structural checks plus the cross-field checks needed before a launcher
uses a release (including the installed-version regression check).
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .manifest import _semver_key, compare_versions


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# A channel manifest describes a release; it is never large.  The cap stops a
# wrong or hostile URL from streaming without bound into launcher memory.
MANIFEST_SIZE_LIMIT = 4 * 1024 * 1024
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_DRIVE_FILE_PATH_RE = re.compile(r"^/[A-Za-z]:[\\/]")

_TOP_LEVEL_FIELDS = ("channel", "version", "gitSha", "releasedAt", "notes", "artifacts")
_NOTE_FIELDS = ("summary", "commits")
_COMMIT_FIELDS = ("sha", "subject", "category", "scope", "pr")
_ARTIFACT_FIELDS = ("name", "url", "sha256", "size")


class ChannelManifestValidationError(ValueError):
    """Raised when a channel manifest is unsafe or cannot be interpreted."""

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
    required: tuple[str, ...],
    allowed: tuple[str, ...],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        _error(errors, path, "must be an object")
        return None
    for key in required:
        if key not in value:
            _error(errors, f"{path}.{key}" if path else key, "is required")
    for key in sorted(set(value) - set(allowed)):
        _error(errors, f"{path}.{key}" if path else key, "is not permitted")
    return value


def _check_string(
    value: Any, path: str, errors: list[str], *, non_empty: bool = True
) -> bool:
    if not isinstance(value, str) or (non_empty and not value.strip()):
        _error(errors, path, "must be a non-empty string" if non_empty else "must be a string")
        return False
    return True


def _check_semver(value: Any, path: str, errors: list[str]) -> str | None:
    if _semver_key(value) is None:
        _error(errors, path, "must be a SemVer 2.0.0 value")
        return None
    return value


def _check_sha(value: Any, path: str, errors: list[str], *, length: int) -> None:
    expression = _SHA256_RE if length == 64 else _GIT_SHA_RE
    if not isinstance(value, str) or expression.fullmatch(value) is None:
        description = "64 lowercase hexadecimal characters" if length == 64 else "40 lowercase hexadecimal characters"
        _error(errors, path, f"must be {description}")


def _check_timestamp(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        _error(errors, path, "must be a UTC RFC 3339 timestamp ending in Z")
        return
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _error(errors, path, "must be a valid UTC RFC 3339 timestamp")


def _check_artifact_url(value: Any, path: str, errors: list[str]) -> None:
    if not _check_string(value, path, errors):
        return
    if "\x00" in value or any(char.isspace() for char in value):
        _error(errors, path, "must not contain whitespace or NUL characters")
        return
    if _WINDOWS_DRIVE_PATH_RE.match(value):
        return
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("", "file", "http", "https"):
            raise ValueError("unsupported scheme")
        if parsed.scheme in ("http", "https") and (not parsed.netloc or parsed.hostname is None):
            raise ValueError("missing host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credentials are not allowed")
        if parsed.scheme == "file" and not parsed.path and not parsed.netloc:
            raise ValueError("missing path")
    except ValueError:
        _error(errors, path, "must be a local path, file URL, or HTTP(S) URL without credentials")


def _check_commit(value: Any, path: str, errors: list[str]) -> None:
    commit = _object(
        value,
        path,
        errors,
        required=("sha", "subject", "category", "scope"),
        allowed=_COMMIT_FIELDS,
    )
    if commit is None:
        return
    if "sha" in commit:
        _check_sha(commit["sha"], f"{path}.sha", errors, length=40)
    for key in ("subject", "category"):
        if key in commit:
            _check_string(commit[key], f"{path}.{key}", errors)
    if "scope" in commit and commit["scope"] is not None:
        _check_string(commit["scope"], f"{path}.scope", errors)
    if "pr" in commit:
        pr = commit["pr"]
        if pr is not None and (
            (isinstance(pr, bool))
            or not isinstance(pr, (int, str))
            or (isinstance(pr, str) and not pr.strip())
            or (isinstance(pr, int) and pr < 1)
        ):
            _error(errors, f"{path}.pr", "must be a positive number, non-empty string, or null")


def _check_artifact(value: Any, path: str, errors: list[str]) -> None:
    artifact = _object(
        value,
        path,
        errors,
        required=_ARTIFACT_FIELDS,
        allowed=_ARTIFACT_FIELDS,
    )
    if artifact is None:
        return
    name = artifact.get("name")
    if not _check_string(name, f"{path}.name", errors):
        pass
    elif (
        "/" in name
        or "\\" in name
        or name in (".", "..")
        or ":" in name
    ):
        _error(errors, f"{path}.name", "must be a safe file name without path separators")
    if "url" in artifact:
        _check_artifact_url(artifact["url"], f"{path}.url", errors)
    if "sha256" in artifact:
        _check_sha(artifact["sha256"], f"{path}.sha256", errors, length=64)
    size = artifact.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        _error(errors, f"{path}.size", "must be a non-negative integer")


def validate_channel_manifest(
    manifest: Any,
    *,
    installed_version: str | None = None,
    current_version: str | None = None,
) -> list[str]:
    """Return all validation errors for a launcher channel manifest.

    ``installed_version`` (or its descriptive alias ``current_version``) is
    optional.  When supplied, a manifest older than the installed build is
    rejected before any artifact is downloaded.  Equality is allowed because a
    same-version build with a different Git SHA can still be a legitimate
    local development update.
    """

    errors: list[str] = []
    if installed_version is not None and current_version is not None:
        if installed_version != current_version:
            _error(errors, "installed_version", "aliases disagree")
    baseline = current_version if current_version is not None else installed_version

    root = _object(
        manifest,
        "",
        errors,
        required=_TOP_LEVEL_FIELDS,
        allowed=_TOP_LEVEL_FIELDS,
    )
    if root is None:
        return errors

    channel = root.get("channel")
    if not _check_string(channel, "channel", errors) or _CHANNEL_RE.fullmatch(channel) is None:
        _error(errors, "channel", "must contain only letters, numbers, '.', '_' or '-'")

    version = _check_semver(root.get("version"), "version", errors)
    _check_sha(root.get("gitSha"), "gitSha", errors, length=40)
    _check_timestamp(root.get("releasedAt"), "releasedAt", errors)

    notes = _object(
        root.get("notes"),
        "notes",
        errors,
        required=_NOTE_FIELDS,
        allowed=_NOTE_FIELDS,
    )
    if notes is not None:
        if "summary" in notes:
            _check_string(notes["summary"], "notes.summary", errors)
        commits = notes.get("commits")
        if not isinstance(commits, list):
            _error(errors, "notes.commits", "must be an array")
        else:
            for index, commit in enumerate(commits):
                _check_commit(commit, f"notes.commits[{index}]", errors)

    artifacts = root.get("artifacts")
    if not isinstance(artifacts, list):
        _error(errors, "artifacts", "must be an array")
    elif not artifacts:
        _error(errors, "artifacts", "must contain at least one artifact")
    else:
        names: set[str] = set()
        for index, artifact in enumerate(artifacts):
            path = f"artifacts[{index}]"
            _check_artifact(artifact, path, errors)
            if isinstance(artifact, dict) and isinstance(artifact.get("name"), str):
                name = artifact["name"]
                if name in names:
                    _error(errors, f"{path}.name", "must be unique")
                names.add(name)

    if baseline is not None:
        installed_key = _semver_key(baseline)
        if installed_key is None:
            _error(errors, "installed_version", "must be a SemVer 2.0.0 value")
        elif version is not None and compare_versions(version, baseline) < 0:
            _error(
                errors,
                "version",
                f"must not regress below installed version {baseline}",
            )

    return errors


def manifest_source_base_dir(source: str | Path) -> Path | None:
    """Return the directory that relative artifact URLs resolve against.

    A remote manifest has no local base directory, so its artifacts must
    carry absolute URLs.
    """

    if isinstance(source, Path):
        return source.resolve().parent
    scheme = urlsplit(source).scheme
    if scheme in ("http", "https") and not _WINDOWS_DRIVE_PATH_RE.match(source):
        return None
    return Path(source).resolve().parent


def _check_manifest_url(url: str) -> None:
    """Reject a manifest URL that is not a safe transport for a release."""

    if not url or any(character.isspace() for character in url):
        raise ChannelManifestValidationError(
            [f"{url!r}: manifest URL must not contain whitespace"]
        )
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise ChannelManifestValidationError(
            [f"{url}: manifest URL must not contain credentials"]
        )
    if not parsed.netloc or parsed.hostname is None:
        raise ChannelManifestValidationError([f"{url}: manifest URL has no host"])
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname.rstrip(".").lower() in _LOCAL_HOSTS:
        # Local HTTP serves a development channel from python -m http.server.
        return
    raise ChannelManifestValidationError(
        [f"{url}: manifest URL must use HTTPS (HTTP is permitted only for localhost)"]
    )


def read_channel_manifest_source(
    source: str | Path,
    *,
    timeout: float = 15.0,
    opener: Callable[..., Any] | None = None,
) -> str:
    """Return the raw JSON text of a local or HTTP(S) channel manifest."""

    if not isinstance(source, Path):
        scheme = urlsplit(source).scheme
        if scheme in ("http", "https") and not _WINDOWS_DRIVE_PATH_RE.match(source):
            _check_manifest_url(source)
            request_opener = opener or urllib.request.urlopen
            try:
                with request_opener(source, timeout=timeout) as stream:
                    raw = stream.read(MANIFEST_SIZE_LIMIT + 1)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise ChannelManifestValidationError(
                    [f"{source}: cannot fetch manifest ({exc})"]
                ) from exc
            if len(raw) > MANIFEST_SIZE_LIMIT:
                raise ChannelManifestValidationError(
                    [f"{source}: manifest is larger than {MANIFEST_SIZE_LIMIT} bytes"]
                )
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ChannelManifestValidationError(
                    [f"{source}: manifest is not valid UTF-8 ({exc})"]
                ) from exc

    manifest_path = Path(source)
    try:
        return manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChannelManifestValidationError(
            [f"{manifest_path}: cannot read manifest ({exc})"]
        ) from exc
    except UnicodeDecodeError as exc:
        raise ChannelManifestValidationError(
            [f"{manifest_path}: manifest is not valid UTF-8 ({exc})"]
        ) from exc


def load_channel_manifest(
    path: str | Path,
    *,
    installed_version: str | None = None,
    current_version: str | None = None,
    timeout: float = 15.0,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Parse and validate a channel manifest from a local path or HTTPS URL."""

    raw = read_channel_manifest_source(path, timeout=timeout, opener=opener)
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChannelManifestValidationError(
            [f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}"]
        ) from exc

    errors = validate_channel_manifest(
        manifest,
        installed_version=installed_version,
        current_version=current_version,
    )
    if errors:
        raise ChannelManifestValidationError(errors)
    return manifest


def find_artifact(manifest: dict[str, Any], name: str | None = None) -> dict[str, Any]:
    """Return the named artifact, or the only artifact when no name is given."""

    artifacts = manifest["artifacts"]
    if name is None:
        if len(artifacts) != 1:
            raise ValueError("artifact name is required when a manifest has multiple artifacts")
        return artifacts[0]
    for artifact in artifacts:
        if artifact["name"] == name:
            return artifact
    raise ValueError(f"manifest does not contain artifact {name!r}")


def verify_channel_artifact(
    manifest: dict[str, Any], name: str, path: str | Path
) -> list[str]:
    """Verify a local artifact against a validated channel manifest."""

    errors = validate_channel_manifest(manifest)
    if errors:
        return errors
    try:
        artifact = find_artifact(manifest, name)
    except ValueError as exc:
        return [f"artifacts: {exc}"]
    digest = hashlib.sha256()
    size = 0
    artifact_path = Path(path)
    try:
        with artifact_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        return [f"artifacts.{name}: cannot read {artifact_path} ({exc})"]
    result: list[str] = []
    if size != artifact["size"]:
        result.append(f"artifacts.{name}: size mismatch (expected {artifact['size']}, got {size})")
    actual = digest.hexdigest()
    if actual != artifact["sha256"]:
        result.append(
            f"artifacts.{name}: SHA-256 mismatch (expected {artifact['sha256']}, got {actual})"
        )
    return result


def artifact_url_path(url: str) -> Path | None:
    """Return a local path for a plain path or ``file:`` URL, if applicable."""

    if _WINDOWS_DRIVE_PATH_RE.match(url):
        return Path(url)
    parsed = urlsplit(url)
    if parsed.scheme not in ("", "file"):
        return None
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            # A UNC path is intentionally not accepted by the local foundation.
            raise ValueError("file URLs with a remote host are not supported")
        raw_path = unquote(parsed.path)
        if _WINDOWS_DRIVE_FILE_PATH_RE.match(raw_path):
            raw_path = raw_path[1:]
        return Path(raw_path)
    return Path(url)


__all__ = [
    "MANIFEST_SIZE_LIMIT",
    "ChannelManifestValidationError",
    "artifact_url_path",
    "find_artifact",
    "load_channel_manifest",
    "manifest_source_base_dir",
    "read_channel_manifest_source",
    "validate_channel_manifest",
    "verify_channel_artifact",
]
