"""Atomic installation of an unpacked build tree.

``update_engine`` installs a single verified file.  A game client is a
directory, and a directory cannot be swapped atomically on Windows once the
destination exists.  This module therefore keeps every build in its own
immutable directory and makes the active build a pointer file, so the only
operation that decides which build runs is a single ``os.replace`` of a small
JSON file.  That is atomic on every supported platform, survives a crash at
any point, and makes rollback a pointer rewrite instead of a file copy.

Layout under the install root::

    builds/<version>+<short sha>/   unpacked payload, never mutated in place
    current.json                    the active build pointer
    previous.json                   the prior pointer, used by rollback
    cache/                          transient verified downloads

The payload is verified by ``update_engine.download_and_verify`` before it is
unpacked, and unpacking refuses any archive entry that escapes the staging
directory.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .channel_manifest import find_artifact
from .update_engine import (
    InstalledBuild,
    UpdateError,
    _validate_installed_build,
    download_and_verify,
    update_available,
)


class InstallTreeError(UpdateError):
    """A build tree could not be unpacked or activated."""


class UnsafeArchiveError(InstallTreeError):
    """An archive entry would have escaped the staging directory."""


@dataclass(frozen=True)
class InstalledTree:
    """The build a pointer file selects, and where its payload lives."""

    build: InstalledBuild
    directory: Path
    installed_at: str

    def as_json(self) -> dict[str, Any]:
        value = dict(self.build.as_json())
        value["directory"] = self.directory.name
        value["installedAt"] = self.installed_at
        return value


@dataclass(frozen=True)
class TreeUpdateResult:
    """Outcome of one tree update attempt."""

    updated: bool
    installed: InstalledTree
    previous: InstalledTree | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_directory_name(version: str, git_sha: str) -> str:
    """Return the immutable directory name for one build identity.

    The short SHA disambiguates two builds that share a version, which the
    channel contract explicitly allows.
    """

    return f"{version}+{git_sha[:7]}"


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def unpack_archive(archive: str | Path, destination: str | Path) -> Path:
    """Unpack a zip into ``destination``, rejecting unsafe entries.

    Absolute paths, parent traversal, and symbolic links are refused outright
    rather than sanitised, because a manifest that contains one is not a
    manifest this launcher should trust at all.
    """

    archive_path = Path(archive)
    destination_path = Path(destination)
    destination_path.mkdir(parents=True, exist_ok=True)
    resolved_root = destination_path.resolve()

    try:
        with zipfile.ZipFile(archive_path) as bundle:
            for entry in bundle.infolist():
                name = entry.filename
                normalised = name.replace("\\", "/")
                head = normalised.split("/")[0]
                if normalised.startswith("/") or ":" in head:
                    raise UnsafeArchiveError(f"archive entry is an absolute path: {name!r}")
                if any(part == ".." for part in normalised.split("/")):
                    raise UnsafeArchiveError(f"archive entry escapes the payload: {name!r}")
                # The high 16 bits of external_attr carry the Unix mode, and
                # 0xA000 is S_IFLNK.  A symlink could point anywhere at all
                # once the payload is extracted.
                if (entry.external_attr >> 16) & 0xF000 == 0xA000:
                    raise UnsafeArchiveError(f"archive entry is a symbolic link: {name!r}")
                target = (resolved_root / normalised).resolve()
                if target != resolved_root and not _is_within(resolved_root, target):
                    raise UnsafeArchiveError(f"archive entry escapes the payload: {name!r}")
            bundle.extractall(destination_path)
    except zipfile.BadZipFile as exc:
        raise InstallTreeError(f"artifact is not a readable zip archive: {exc}") from exc
    except OSError as exc:
        raise InstallTreeError(f"cannot unpack artifact: {exc}") from exc

    return destination_path


def collapse_single_root(payload: str | Path) -> Path:
    """Return the meaningful payload root inside an unpacked directory.

    Build pipelines commonly zip a wrapper folder.  When the unpacked tree is
    exactly one directory and nothing else, that directory is the payload.
    """

    payload_path = Path(payload)
    entries = list(payload_path.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return payload_path


class TreeInstaller:
    """Install, activate, and roll back directory build payloads."""

    def __init__(
        self,
        install_dir: str | Path,
        *,
        opener: Callable[..., Any] | None = None,
        failure_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.install_dir = Path(install_dir)
        self.builds_dir = self.install_dir / "builds"
        self.cache_dir = self.install_dir / "cache"
        self.current_path = self.install_dir / "current.json"
        self.previous_path = self.install_dir / "previous.json"
        self.opener = opener
        self.failure_hook = failure_hook

    # -- pointer state ---------------------------------------------------

    def _read_pointer(self, path: Path) -> InstalledTree | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise InstallTreeError(f"build pointer is unreadable: {path} ({exc})") from exc
        if not isinstance(value, dict):
            raise InstallTreeError(f"build pointer must be an object: {path}")

        version = value.get("version")
        git_sha = value.get("gitSha")
        directory = value.get("directory")
        installed_at = value.get("installedAt")
        artifact_name = value.get("artifactName")
        if not isinstance(version, str) or not isinstance(git_sha, str):
            raise InstallTreeError(f"build pointer requires version and gitSha: {path}")
        if not isinstance(directory, str) or not directory:
            raise InstallTreeError(f"build pointer requires a directory: {path}")
        if "/" in directory or "\\" in directory or directory in (".", ".."):
            raise InstallTreeError(f"build pointer directory must be a single name: {path}")
        if artifact_name is not None and not isinstance(artifact_name, str):
            raise InstallTreeError(f"build pointer artifactName must be a string: {path}")
        if not isinstance(installed_at, str):
            installed_at = ""

        build = InstalledBuild(version, git_sha, artifact_name)
        _validate_installed_build(build)
        return InstalledTree(build, self.builds_dir / directory, installed_at)

    def read_current(self) -> InstalledTree | None:
        """Return the active build, or ``None`` when nothing is installed."""

        return self._read_pointer(self.current_path)

    def read_previous(self) -> InstalledTree | None:
        """Return the build a rollback would restore, if one is retained."""

        return self._read_pointer(self.previous_path)

    def _write_pointer(self, path: Path, tree: InstalledTree) -> None:
        self.install_dir.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix=".pointer-", dir=self.install_dir)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                json.dump(tree.as_json(), output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise InstallTreeError(f"cannot write build pointer {path}: {exc}") from exc

    # -- payload ---------------------------------------------------------

    def payload_dir(self) -> Path | None:
        """Return the active payload directory when it exists on disk."""

        current = self.read_current()
        if current is None or not current.directory.is_dir():
            return None
        return current.directory

    def is_update_available(self, manifest: dict[str, Any]) -> bool:
        """Return whether ``manifest`` is a non-regressive change to install."""

        current = self.read_current()
        if current is None:
            return True
        if not current.directory.is_dir():
            # The pointer survived but the payload did not; reinstall.
            return True
        return update_available(manifest, current.build.version, current.build.git_sha)

    def install(
        self,
        manifest: dict[str, Any],
        *,
        base_dir: str | Path | None = None,
        artifact_name: str | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> TreeUpdateResult:
        """Download, verify, unpack, and activate the manifest build."""

        current = self.read_current()
        if not self.is_update_available(manifest):
            assert current is not None
            return TreeUpdateResult(False, current, self.read_previous())

        artifact = find_artifact(manifest, artifact_name)
        self.builds_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        target = self.builds_dir / build_directory_name(manifest["version"], manifest["gitSha"])
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.builds_dir))
        handle, download_name = tempfile.mkstemp(prefix=".download-", dir=self.cache_dir)
        os.close(handle)
        download = Path(download_name)

        try:
            download_and_verify(
                artifact,
                download,
                base_dir=base_dir,
                opener=self.opener,
                progress=progress,
            )
            unpack_archive(download, staging)
            payload = collapse_single_root(staging)
            if payload != staging:
                # Lift the wrapper folder's contents so every install has the
                # same shape regardless of how the archive was produced.
                lifted = Path(tempfile.mkdtemp(prefix=".payload-", dir=self.builds_dir))
                lifted.rmdir()
                os.replace(payload, lifted)
                shutil.rmtree(staging, ignore_errors=True)
                staging = lifted

            if self.failure_hook is not None:
                self.failure_hook("before-activate")

            if target.exists():
                # A previous attempt left this build behind, or the same build
                # is being reinstalled after its payload was damaged.
                shutil.rmtree(target, ignore_errors=True)
            try:
                os.replace(staging, target)
            except OSError as exc:
                raise InstallTreeError(f"cannot place build payload at {target}: {exc}") from exc
            staging = target

            tree = InstalledTree(
                InstalledBuild(manifest["version"], manifest["gitSha"], artifact["name"]),
                target,
                _utc_now(),
            )
            if current is not None:
                self._write_pointer(self.previous_path, current)
            self._write_pointer(self.current_path, tree)
            if self.failure_hook is not None:
                self.failure_hook("after-activate")
            return TreeUpdateResult(True, tree, current)
        except Exception:
            if staging != target and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            download.unlink(missing_ok=True)

    def rollback(self) -> InstalledTree:
        """Activate the retained previous build.

        Only the pointer moves, so a rollback cannot damage either payload and
        can itself be rolled forward again.
        """

        previous = self.read_previous()
        if previous is None:
            raise InstallTreeError("no previous build is retained")
        if not previous.directory.is_dir():
            raise InstallTreeError(f"previous build payload is missing: {previous.directory}")
        current = self.read_current()
        self._write_pointer(self.current_path, previous)
        if current is not None:
            self._write_pointer(self.previous_path, current)
        return previous

    def prune(self, keep: int = 2) -> list[Path]:
        """Delete build payloads that are neither active nor retained.

        ``keep`` bounds how many additional recent builds survive, so a long
        lived install does not grow without limit.
        """

        if not self.builds_dir.is_dir():
            return []
        protected = set()
        for tree in (self.read_current(), self.read_previous()):
            if tree is not None:
                protected.add(tree.directory.name)

        candidates = [
            entry
            for entry in self.builds_dir.iterdir()
            if entry.is_dir()
            and entry.name not in protected
            and not entry.name.startswith(".")
        ]
        candidates.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)

        removed = []
        for entry in candidates[max(keep, 0):]:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
        return removed


__all__ = [
    "InstallTreeError",
    "InstalledTree",
    "TreeInstaller",
    "TreeUpdateResult",
    "UnsafeArchiveError",
    "build_directory_name",
    "collapse_single_root",
    "unpack_archive",
]
