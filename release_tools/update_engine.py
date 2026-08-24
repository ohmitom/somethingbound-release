"""Safe, local-first launcher update mechanics.

The engine intentionally has no scheduler, hosting integration, signing, or
product-specific process management.  It downloads one manifest-selected
artifact, verifies its declared size and SHA-256, then swaps it into a fixed
``current`` path.  The prior payload and state remain in ``previous`` so a
failed swap can be restored and a successful update can be rolled back by a
later operator action.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .channel_manifest import (
    ChannelManifestValidationError,
    artifact_url_path,
    find_artifact,
    validate_channel_manifest,
)
from .manifest import _GIT_SHA_RE, _semver_key, compare_versions


class UpdateError(RuntimeError):
    """Base class for failures that must prevent or undo an installation."""


class ArtifactVerificationError(UpdateError):
    """Downloaded bytes did not match the manifest."""


class AtomicInstallError(UpdateError):
    """The install swap failed and the previous payload was restored."""


class InstalledStateError(UpdateError):
    """The install directory has no valid installed-build identity."""


@dataclass(frozen=True)
class InstalledBuild:
    """The identity of the build currently selected by the launcher."""

    version: str
    git_sha: str
    artifact_name: str | None = None

    def as_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"version": self.version, "gitSha": self.git_sha}
        if self.artifact_name is not None:
            result["artifactName"] = self.artifact_name
        return result


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of one update attempt."""

    updated: bool
    installed: InstalledBuild
    artifact_path: Path | None = None
    previous_path: Path | None = None


def _validate_installed_build(installed: InstalledBuild) -> None:
    if _semver_key(installed.version) is None:
        raise InstalledStateError("installed version must be a SemVer 2.0.0 value")
    if _GIT_SHA_RE.fullmatch(installed.git_sha) is None:
        raise InstalledStateError("installed gitSha must be 40 lowercase hexadecimal characters")


def update_available(
    manifest: dict[str, Any], installed_version: str, installed_git_sha: str
) -> bool:
    """Return whether a validated manifest represents a non-regressive update.

    A newer version is an update.  A same-version artifact with a different
    Git SHA is also considered an update, which is useful for a local channel.
    A manifest older than the installed build raises instead of silently
    downgrading.
    """

    errors = validate_channel_manifest(manifest, installed_version=installed_version)
    if errors:
        raise ChannelManifestValidationError(errors)
    installed = InstalledBuild(installed_version, installed_git_sha)
    _validate_installed_build(installed)
    version_order = compare_versions(manifest["version"], installed_version)
    if version_order < 0:
        # Normally caught by validate_channel_manifest; retain this guard for
        # callers that replace validation in a test double.
        raise UpdateError("refusing to install a regressive version")
    return version_order > 0 or manifest["gitSha"] != installed_git_sha


def _resolve_source(url: str, *, base_dir: Path | None) -> Path | str:
    local_path = artifact_url_path(url)
    if local_path is not None:
        if not local_path.is_absolute() and base_dir is not None:
            local_path = base_dir / local_path
        return local_path
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise UpdateError(f"unsupported artifact URL scheme: {parsed.scheme or '(none)'}")
    if parsed.username is not None or parsed.password is not None:
        raise UpdateError("artifact URL must not contain credentials")
    return url


def download_and_verify(
    artifact: dict[str, Any],
    destination: str | Path,
    *,
    base_dir: str | Path | None = None,
    timeout: float = 15.0,
    opener: Callable[..., Any] | None = None,
) -> Path:
    """Download one artifact to ``destination`` and verify size and SHA-256.

    ``file:``, relative local paths, and HTTP(S) URLs are supported.  The
    destination is written directly by this helper; callers should pass a
    temporary path when an atomic installation is required.
    """

    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = _resolve_source(
        artifact["url"],
        base_dir=Path(base_dir) if base_dir is not None else None,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        if isinstance(source, Path):
            stream = source.open("rb")
        else:
            request_opener = opener or urllib.request.urlopen
            stream = request_opener(source, timeout=timeout)
        with stream:
            with destination_path.open("wb") as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        destination_path.unlink(missing_ok=True)
        raise UpdateError(f"cannot download artifact {artifact['name']!r}: {exc}") from exc

    actual_sha = digest.hexdigest()
    if size != artifact["size"] or actual_sha != artifact["sha256"]:
        destination_path.unlink(missing_ok=True)
        details = []
        if size != artifact["size"]:
            details.append(f"size expected {artifact['size']}, got {size}")
        if actual_sha != artifact["sha256"]:
            details.append(f"SHA-256 expected {artifact['sha256']}, got {actual_sha}")
        raise ArtifactVerificationError(
            f"artifact {artifact['name']!r} verification failed: {', '.join(details)}"
        )
    return destination_path


def _replace_existing(source: Path, destination: Path) -> None:
    """Use the platform atomic replacement primitive, with a clear error."""

    try:
        os.replace(source, destination)
    except OSError as exc:
        raise AtomicInstallError(f"cannot atomically replace {destination}: {exc}") from exc


def atomic_install(
    source: str | Path,
    target: str | Path,
    *,
    backup: str | Path | None = None,
    failure_hook: Callable[[str], None] | None = None,
) -> Path:
    """Atomically install ``source`` and restore ``target`` if a swap fails.

    The source must already be fully downloaded and verified.  ``backup`` is
    kept after success and contains the previous target.  ``failure_hook`` is
    only a deterministic test seam; raising from it exercises the rollback
    path without making a real filesystem failure flaky.
    """

    source_path = Path(source)
    target_path = Path(target)
    backup_path = Path(backup) if backup is not None else target_path.with_name(
        target_path.name + ".previous"
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    had_target = target_path.exists()
    moved_old_target = False
    installed_new_target = False
    try:
        if had_target:
            _replace_existing(target_path, backup_path)
            moved_old_target = True
        _replace_existing(source_path, target_path)
        installed_new_target = True
        if failure_hook is not None:
            failure_hook("after-swap")
    except Exception as exc:
        try:
            if installed_new_target and target_path.exists():
                target_path.unlink()
            if moved_old_target and backup_path.exists():
                _replace_existing(backup_path, target_path)
        except Exception as rollback_exc:
            raise AtomicInstallError(
                f"install failed ({exc}); rollback failed ({rollback_exc})"
            ) from exc
        source_path.unlink(missing_ok=True)
        if isinstance(exc, AtomicInstallError):
            raise
        raise AtomicInstallError(f"install failed and was rolled back: {exc}") from exc
    return target_path


class UpdateEngine:
    """Coordinate manifest comparison, download verification, and atomic swap."""

    def __init__(
        self,
        install_dir: str | Path,
        *,
        failure_hook: Callable[[str], None] | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.install_dir = Path(install_dir)
        self.failure_hook = failure_hook
        self.opener = opener
        self.active_path = self.install_dir / "current"
        self.previous_path = self.install_dir / "previous"
        self.state_path = self.install_dir / "installed.json"
        self.previous_state_path = self.install_dir / "previous.json"

    def read_installed(self) -> InstalledBuild:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise InstalledStateError(f"installed state is missing: {self.state_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise InstalledStateError(f"installed state is unreadable: {self.state_path}") from exc
        if not isinstance(value, dict):
            raise InstalledStateError("installed state must be an object")
        version = value.get("version")
        git_sha = value.get("gitSha")
        artifact_name = value.get("artifactName")
        if not isinstance(version, str) or not isinstance(git_sha, str):
            raise InstalledStateError("installed state requires version and gitSha")
        if artifact_name is not None and not isinstance(artifact_name, str):
            raise InstalledStateError("installed state artifactName must be a string")
        installed = InstalledBuild(version, git_sha, artifact_name)
        _validate_installed_build(installed)
        return installed

    def seed_installed(self, installed: InstalledBuild) -> InstalledBuild:
        """Write a local development identity when no launcher state exists."""

        _validate_installed_build(installed)
        self.install_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.install_dir / ".installed.json.tmp"
        temporary.write_text(json.dumps(installed.as_json(), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)
        return installed

    def is_update_available(self, manifest: dict[str, Any], installed: InstalledBuild) -> bool:
        return update_available(manifest, installed.version, installed.git_sha)

    def update(
        self,
        manifest: dict[str, Any],
        *,
        installed: InstalledBuild | None = None,
        base_dir: str | Path | None = None,
        artifact_name: str | None = None,
    ) -> UpdateResult:
        """Install the manifest artifact when it is newer than ``installed``."""

        current = installed or self.read_installed()
        _validate_installed_build(current)
        errors = validate_channel_manifest(manifest, installed_version=current.version)
        if errors:
            raise ChannelManifestValidationError(errors)
        if not self.is_update_available(manifest, current):
            return UpdateResult(False, current, self.active_path if self.active_path.exists() else None)

        artifact = find_artifact(manifest, artifact_name)
        self.install_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".download-", dir=self.install_dir)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            download_and_verify(
                artifact,
                temporary,
                base_dir=base_dir,
                opener=self.opener,
            )
            state_temp = self.install_dir / ".installed.json.download"
            new_build = InstalledBuild(manifest["version"], manifest["gitSha"], artifact["name"])
            state_temp.write_text(
                json.dumps(new_build.as_json(), indent=2) + "\n", encoding="utf-8"
            )
            active_existed_before_install = self.active_path.exists()
            had_state = self.state_path.exists()
            moved_old_state = False
            installed_new_state = False
            atomic_install_done = False
            try:
                atomic_install(
                    temporary,
                    self.active_path,
                    backup=self.previous_path,
                    failure_hook=self.failure_hook,
                )
                atomic_install_done = True
                if had_state:
                    _replace_existing(self.state_path, self.previous_state_path)
                    moved_old_state = True
                _replace_existing(state_temp, self.state_path)
                installed_new_state = True
            except Exception as exc:
                try:
                    if installed_new_state and self.state_path.exists():
                        self.state_path.unlink()
                    if moved_old_state and self.previous_state_path.exists():
                        _replace_existing(self.previous_state_path, self.state_path)
                    if atomic_install_done:
                        if active_existed_before_install:
                            if self.previous_path.exists():
                                _replace_existing(self.previous_path, self.active_path)
                        elif self.active_path.exists():
                            self.active_path.unlink()
                except Exception as rollback_exc:
                    raise AtomicInstallError(
                        f"state installation failed ({exc}); state rollback failed ({rollback_exc})"
                    ) from exc
                state_temp.unlink(missing_ok=True)
                raise
            return UpdateResult(
                True,
                new_build,
                self.active_path,
                self.previous_path if self.previous_path.exists() else None,
            )
        finally:
            temporary.unlink(missing_ok=True)
            (self.install_dir / ".installed.json.download").unlink(missing_ok=True)


__all__ = [
    "ArtifactVerificationError",
    "AtomicInstallError",
    "InstalledBuild",
    "InstalledStateError",
    "UpdateEngine",
    "UpdateError",
    "UpdateResult",
    "atomic_install",
    "download_and_verify",
    "update_available",
]
