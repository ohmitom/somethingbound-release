"""The launcher replacing its own executable.

Windows will not let a running executable be overwritten, but it will let one
be *renamed*: the file name is not what the loader holds open, the image is.
So a launcher can move itself aside, put a verified new build at its own path,
start that, and exit.  The moved-aside copy is deleted by the next run, once
nothing has it open.

Ordering is what makes this safe:

1. Download and verify the replacement completely, beside the running
   executable so both are on one volume and every later step is a rename.
2. Rename the running executable to the retired name.
3. Rename the verified replacement onto the real name.
4. Start it and exit.

A failure before step 2 leaves everything untouched.  A failure at step 3 puts
the retired copy straight back.  The only exposed moment is between two renames
on the same volume, and it is recoverable from disk because the retired copy is
still there under a known name.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .channel_manifest import find_artifact
from .update_engine import UpdateError, download_and_verify

# The suffix a superseded launcher is retired to. It stays on disk until the
# new launcher starts, because Windows keeps the old image open until then.
RETIRED_SUFFIX = ".retired"
STAGED_SUFFIX = ".staged"


class SelfUpdateError(UpdateError):
    """The launcher could not replace itself."""


@dataclass(frozen=True)
class SelfUpdatePlan:
    """What a self-update would do, decided before anything is downloaded."""

    available: bool
    installed_version: str
    channel_version: str | None
    reason: str = ""


def is_frozen() -> bool:
    """Whether this is the packaged executable rather than a source checkout."""

    return bool(getattr(sys, "frozen", False))


def running_executable() -> Path | None:
    """Return the launcher executable, or ``None`` when running from source.

    Running from source has no single file to replace, so self-update is not
    something to fail at; it is something that does not apply.
    """

    if not is_frozen():
        return None
    return Path(sys.executable).resolve()


def retired_path(executable: str | Path) -> Path:
    path = Path(executable)
    return path.with_name(path.name + RETIRED_SUFFIX)


def staged_path(executable: str | Path) -> Path:
    path = Path(executable)
    return path.with_name(path.name + STAGED_SUFFIX)


def clean_retired(executable: str | Path) -> bool:
    """Delete a previous launcher left behind by an update.

    Called on every start.  A retired copy that is somehow still locked is not
    an error worth showing anyone: the next run will get it.
    """

    retired = retired_path(executable)
    if not retired.exists():
        return False
    try:
        retired.unlink()
    except OSError:
        return False
    return True


def can_replace(executable: str | Path) -> bool:
    """Whether this launcher may write to its own directory.

    A launcher installed under Program Files cannot update itself without
    elevation, and telling the player that is far better than failing halfway.
    """

    directory = Path(executable).parent
    probe = directory / ".somethingbound-write-probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError:
        return False
    return True


def plan_self_update(
    manifest: dict[str, Any], installed_version: str, *, frozen: bool | None = None
) -> SelfUpdatePlan:
    """Decide whether the launcher channel offers a newer launcher."""

    from .manifest import compare_versions

    channel_version = manifest["version"]
    if not (is_frozen() if frozen is None else frozen):
        return SelfUpdatePlan(
            False,
            installed_version,
            channel_version,
            "running from source, so there is no executable to replace",
        )
    if compare_versions(channel_version, installed_version) <= 0:
        return SelfUpdatePlan(False, installed_version, channel_version, "up to date")
    return SelfUpdatePlan(True, installed_version, channel_version)


def swap_in_place(staged: str | Path, executable: str | Path) -> Path:
    """Put a verified replacement at the running executable's own path.

    ``staged`` must already be downloaded and verified.  Returns the path the
    superseded launcher was retired to.
    """

    staged_file = Path(staged)
    target = Path(executable)
    retired = retired_path(target)

    if not staged_file.is_file():
        raise SelfUpdateError(f"no staged launcher at {staged_file}")

    # A leftover from an earlier update would block the rename below.
    if retired.exists():
        try:
            retired.unlink()
        except OSError as exc:
            raise SelfUpdateError(
                f"cannot clear the previously retired launcher {retired}: {exc}"
            ) from exc

    try:
        os.replace(target, retired)
    except OSError as exc:
        raise SelfUpdateError(f"cannot move the running launcher aside: {exc}") from exc

    try:
        os.replace(staged_file, target)
    except OSError as exc:
        try:
            os.replace(retired, target)
        except OSError as restore_exc:
            raise SelfUpdateError(
                f"could not install the new launcher ({exc}) and could not restore "
                f"the old one ({restore_exc}). The previous launcher is at {retired}."
            ) from exc
        raise SelfUpdateError(f"could not install the new launcher: {exc}") from exc

    return retired


def download_replacement(
    manifest: dict[str, Any],
    executable: str | Path,
    *,
    artifact_name: str | None = None,
    base_dir: str | Path | None = None,
    opener: Callable[..., Any] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Fetch and verify the replacement beside the running executable."""

    artifact = find_artifact(manifest, artifact_name)
    staged = staged_path(executable)
    staged.unlink(missing_ok=True)
    download_and_verify(
        artifact, staged, base_dir=base_dir, opener=opener, progress=progress
    )
    return staged


# PyInstaller's onefile bootloader marks its own process tree with these, and
# a child validates that its parent's executable matches its own. Inheriting
# them across a self-update makes the new launcher believe it is a child of the
# old one, which is a different executable, and it refuses to start with
# "parent process has different executable".
_BOOTLOADER_ENVIRONMENT_PREFIXES = ("_PYI",)
_BOOTLOADER_ENVIRONMENT_NAMES = ("_MEIPASS2",)


def clean_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment with the packager's private markers removed.

    Anything started from inside a frozen launcher must not inherit the
    bootloader's idea of which process tree it belongs to.
    """

    source = dict(os.environ if base is None else base)
    return {
        name: value
        for name, value in source.items()
        if not name.startswith(_BOOTLOADER_ENVIRONMENT_PREFIXES)
        and name not in _BOOTLOADER_ENVIRONMENT_NAMES
    }


def relaunch(executable: str | Path, arguments: list[str] | None = None) -> None:
    """Start the newly installed launcher, detached from this one."""

    command = [str(executable), *(arguments or [])]
    creation_flags = 0
    start_new_session = False
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        start_new_session = True

    try:
        subprocess.Popen(
            command,
            cwd=str(Path(executable).parent),
            env=clean_environment(),
            creationflags=creation_flags,
            start_new_session=start_new_session,
            close_fds=True,
        )
    except OSError as exc:
        raise SelfUpdateError(f"cannot start the updated launcher: {exc}") from exc


__all__ = [
    "RETIRED_SUFFIX",
    "clean_environment",
    "STAGED_SUFFIX",
    "SelfUpdateError",
    "SelfUpdatePlan",
    "can_replace",
    "clean_retired",
    "download_replacement",
    "is_frozen",
    "plan_self_update",
    "relaunch",
    "retired_path",
    "running_executable",
    "staged_path",
    "swap_in_place",
]
