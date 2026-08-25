"""The launcher's decisions, with no user interface attached.

Both the console launcher and the window drive this module, so what the player
sees and what a script does can never disagree about whether an update is
needed or whether the client is safe to start.  Nothing here writes to stdout
or touches a widget.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__ as LAUNCHER_VERSION
from .channel_manifest import (
    ChannelManifestValidationError,
    load_channel_manifest,
    manifest_source_base_dir,
)
from .game_process import GameLaunchError, find_executable, start_game
from .install_tree import InstalledTree, TreeInstaller, TreeUpdateResult
from .patch_notes import render_patch_notes
from .self_update import (
    SelfUpdateError,
    SelfUpdatePlan,
    can_replace,
    clean_retired,
    download_replacement,
    plan_self_update,
    relaunch,
    running_executable,
    staged_path,
    swap_in_place,
)
from .settings import LauncherSettings
from .update_engine import UpdateError


class LauncherError(RuntimeError):
    """A launcher operation failed in a way the player must be told about."""


@dataclass(frozen=True)
class LauncherStatus:
    """What the launcher knows after one check of the channel.

    ``manifest`` is ``None`` when the channel could not be reached.  That is
    not fatal: an already installed build stays playable offline, which is
    what ``can_play`` reports.
    """

    installed: InstalledTree | None
    manifest: dict[str, Any] | None
    update_available: bool
    channel_error: str | None = None

    @property
    def can_play(self) -> bool:
        return self.installed is not None

    @property
    def is_first_install(self) -> bool:
        return self.installed is None and self.manifest is not None

    @property
    def installed_version(self) -> str | None:
        return None if self.installed is None else self.installed.build.version

    @property
    def available_version(self) -> str | None:
        return None if self.manifest is None else self.manifest["version"]

    @property
    def checked_channel(self) -> bool:
        """Whether this status came from an actual look at the channel."""

        return self.manifest is not None or self.channel_error is not None

    def summary(self) -> str:
        """Return one line describing the launcher's state for a player."""

        if self.channel_error is not None:
            if self.installed is None:
                return "Cannot reach the update channel, and no build is installed."
            return (
                f"Offline. Playing installed build {self.installed_version} "
                "without checking for updates."
            )
        if not self.checked_channel:
            if self.installed is None:
                return "No build is installed."
            return f"Ready to play {self.installed_version}. Updates were not checked."
        if self.is_first_install:
            return f"Ready to install {self.available_version}."
        if self.update_available:
            return f"Update available: {self.installed_version} to {self.available_version}."
        return f"Up to date on {self.installed_version}."


class Launcher:
    """Coordinate settings, the release channel, the install, and the client."""

    def __init__(
        self,
        settings: LauncherSettings,
        *,
        opener: Callable[..., Any] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.settings = settings
        self.timeout = timeout
        self.installer = TreeInstaller(settings.resolved_install_dir(), opener=opener)
        self._opener = opener

    # -- state -----------------------------------------------------------

    def installed(self) -> InstalledTree | None:
        return self.installer.read_current()

    def fetch_manifest(self) -> dict[str, Any]:
        """Fetch and validate the channel manifest, or raise ``LauncherError``.

        The installed version is deliberately not passed as a baseline here.
        A channel that has been rolled back is a fact the launcher should be
        able to display, not an error that blanks the window.
        """

        source = self.settings.resolved_manifest_url()
        try:
            return load_channel_manifest(
                source, opener=self._opener, timeout=self.timeout
            )
        except ChannelManifestValidationError as exc:
            raise LauncherError(f"channel manifest was rejected: {exc}") from exc

    def status(self) -> LauncherStatus:
        """Check the channel and report what the player can do next."""

        installed = self.installed()
        try:
            manifest = self.fetch_manifest()
        except LauncherError as exc:
            return LauncherStatus(installed, None, False, str(exc))

        try:
            available = self.installer.is_update_available(manifest)
        except UpdateError as exc:
            # A channel that regressed below the installed build lands here.
            return LauncherStatus(installed, manifest, False, str(exc))
        return LauncherStatus(installed, manifest, available)

    # -- actions ---------------------------------------------------------

    def update(
        self,
        manifest: dict[str, Any],
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> TreeUpdateResult:
        """Install the manifest build, keeping the previous one for rollback."""

        base_dir = manifest_source_base_dir(self.settings.resolved_manifest_url())
        try:
            result = self.installer.install(
                manifest, base_dir=base_dir, progress=progress
            )
        except UpdateError as exc:
            raise LauncherError(str(exc)) from exc
        if result.updated:
            self.installer.prune()
        return result

    def rollback(self) -> InstalledTree:
        """Return to the retained previous build."""

        try:
            return self.installer.rollback()
        except UpdateError as exc:
            raise LauncherError(str(exc)) from exc

    def client_executable(self) -> Path:
        """Return the installed client executable, or raise if it is absent."""

        payload = self.installer.payload_dir()
        if payload is None:
            raise LauncherError("no build is installed")
        try:
            return find_executable(payload, self.settings.executable_name)
        except GameLaunchError as exc:
            raise LauncherError(str(exc)) from exc

    def play(self) -> subprocess.Popen:
        """Start the installed client against the configured server."""

        payload = self.installer.payload_dir()
        if payload is None:
            raise LauncherError("no build is installed")
        try:
            return start_game(
                payload,
                self.settings.executable_name,
                server_endpoint=self.settings.server_endpoint,
            )
        except GameLaunchError as exc:
            raise LauncherError(str(exc)) from exc

    # -- the launcher updating itself ------------------------------------

    def fetch_launcher_manifest(self) -> dict[str, Any]:
        """Fetch and validate the launcher's own channel."""

        source = self.settings.resolved_launcher_manifest_url()
        try:
            return load_channel_manifest(
                source, opener=self._opener, timeout=self.timeout
            )
        except ChannelManifestValidationError as exc:
            raise LauncherError(f"launcher channel was rejected: {exc}") from exc

    def launcher_plan(self, manifest: dict[str, Any] | None = None) -> SelfUpdatePlan:
        """Decide whether a newer launcher is published."""

        if manifest is None:
            manifest = self.fetch_launcher_manifest()
        return plan_self_update(manifest, LAUNCHER_VERSION)

    def update_launcher(
        self,
        manifest: dict[str, Any],
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Replace this launcher with the published one and start it.

        Returns the path the superseded launcher was retired to.  The caller
        must exit promptly afterwards: the replacement is already running.
        """

        executable = running_executable()
        if executable is None:
            raise LauncherError(
                "this launcher is running from source, so there is no executable "
                "to replace"
            )
        if not can_replace(executable):
            raise LauncherError(
                f"cannot write to {executable.parent}, so the launcher cannot update "
                "itself. Move it somewhere you own, such as your user folder."
            )

        base_dir = manifest_source_base_dir(
            self.settings.resolved_launcher_manifest_url()
        )
        try:
            staged = download_replacement(
                manifest,
                executable,
                base_dir=base_dir,
                opener=self._opener,
                progress=progress,
            )
            retired = swap_in_place(staged, executable)
            relaunch(executable)
        except (SelfUpdateError, UpdateError) as exc:
            staged_path(executable).unlink(missing_ok=True)
            raise LauncherError(str(exc)) from exc
        return retired

    @staticmethod
    def tidy_previous_launcher() -> bool:
        """Delete the launcher a previous self-update superseded."""

        executable = running_executable()
        if executable is None:
            return False
        return clean_retired(executable)

    # -- presentation helpers -------------------------------------------

    def patch_notes(self, manifest: dict[str, Any], *, expanded: bool = True) -> str:
        """Render the notes for a manifest against the installed build."""

        installed = self.installed()
        running_sha = manifest["gitSha"] if installed is None else installed.build.git_sha
        return render_patch_notes(manifest, running_sha, expanded=expanded)


__all__ = ["LAUNCHER_VERSION", "Launcher", "LauncherError", "LauncherStatus"]
