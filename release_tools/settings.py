"""Persisted launcher settings, including the selected server connection.

The launcher must remember three things between runs: which release channel it
watches, where it installs builds, and which server the client should connect
to.  Only the last of those is something a player changes routinely, so the
server endpoint is validated with the same rules the release tooling already
applies to an operator-configured endpoint: HTTPS everywhere, plain HTTP only
for a localhost development server, and never embedded credentials.

Settings live beside the install, not inside it, so replacing or rolling back a
build never discards the player's server choice.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .server import ServerEndpointConfigurationError, validate_server_endpoint

DEFAULT_CHANNEL = "playtest"

# The launcher watches its own channel, separate from the client's, because the
# two version independently: the client ships constantly and the launcher
# almost never.
LAUNCHER_CHANNEL = "launcher"
DEFAULT_EXECUTABLE = "SomethingBound.exe"
SETTINGS_FILE_NAME = "launcher.json"

# The operator-run playtest server. A fresh install points here so a tester does
# not have to be told a URL. Storing an explicit null clears the selection, which
# is different from never having chosen one.
DEFAULT_SERVER_ENDPOINT = "https://somethingbound-server-production.up.railway.app"

# Distinguishes "the settings file omitted this key" from "the settings file
# explicitly cleared it".
_UNSET = object()

# The launcher reads its channel from here unless the player overrides it.
# Publishing writes the same path in the release repository.
DEFAULT_MANIFEST_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/ohmitom/somethingbound-release/main/channels/{channel}.json"
)

_FIELDS = (
    "channel",
    "manifestUrl",
    "installDir",
    "serverEndpoint",
    "executableName",
    "autoUpdate",
    "launcherManifestUrl",
)


class SettingsError(ValueError):
    """Stored launcher settings are unreadable or invalid."""


def default_data_dir() -> Path:
    """Return the per-user directory that holds launcher state.

    ``LOCALAPPDATA`` is the correct Windows location for machine-local data
    that should not roam.  Other platforms follow the XDG data convention so a
    developer running the launcher from source gets a sane path too.
    """

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "SomethingBound"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "SomethingBound"
    return Path.home() / ".local" / "share" / "SomethingBound"


def default_manifest_url(channel: str = DEFAULT_CHANNEL) -> str:
    return DEFAULT_MANIFEST_URL_TEMPLATE.format(channel=channel)


@dataclass(frozen=True)
class LauncherSettings:
    """Everything the launcher needs before it contacts anything."""

    channel: str = DEFAULT_CHANNEL
    manifest_url: str = ""
    install_dir: Path | None = None
    server_endpoint: str | None = DEFAULT_SERVER_ENDPOINT
    executable_name: str = DEFAULT_EXECUTABLE
    auto_update: bool = True
    launcher_manifest_url: str = ""

    def resolved_manifest_url(self) -> str:
        """Return the configured channel source, or the published default."""

        return self.manifest_url or default_manifest_url(self.channel)

    def resolved_launcher_manifest_url(self) -> str:
        """Return the launcher's own channel source.

        Pointing the client channel somewhere else, at a local file for
        development say, must not also redirect where the launcher looks for
        its own updates.
        """

        return self.launcher_manifest_url or default_manifest_url(LAUNCHER_CHANNEL)

    def resolved_install_dir(self) -> Path:
        """Return the configured install root, or the per-user default."""

        if self.install_dir is not None:
            return self.install_dir
        return default_data_dir() / "install"

    def as_json(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "manifestUrl": self.manifest_url,
            "installDir": None if self.install_dir is None else str(self.install_dir),
            "serverEndpoint": self.server_endpoint,
            "executableName": self.executable_name,
            "autoUpdate": self.auto_update,
            "launcherManifestUrl": self.launcher_manifest_url,
        }

    def with_server_endpoint(self, endpoint: str | None) -> "LauncherSettings":
        """Return a copy carrying a validated server endpoint.

        An empty or whitespace-only value clears the selection rather than
        storing a meaningless endpoint, which lets the launcher ask for one.
        """

        return replace(self, server_endpoint=normalise_server_endpoint(endpoint))


def normalise_server_endpoint(endpoint: str | None) -> str | None:
    """Validate and trim a player-supplied server endpoint.

    Raises ``ServerEndpointConfigurationError`` so the user interface can show
    the specific reason an endpoint was refused.
    """

    if endpoint is None:
        return None
    if not isinstance(endpoint, str):
        raise ServerEndpointConfigurationError("server endpoint must be a string")
    trimmed = endpoint.strip()
    if not trimmed:
        return None
    return validate_server_endpoint(trimmed, "server endpoint")


def _string(values: Mapping[str, Any], name: str, default: str) -> str:
    value = values.get(name, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise SettingsError(f"{name} must be a string")
    return value


def settings_from_json(values: Any) -> LauncherSettings:
    """Build settings from decoded JSON, rejecting anything unusable."""

    if not isinstance(values, dict):
        raise SettingsError("launcher settings must be an object")
    unknown = sorted(set(values) - set(_FIELDS))
    if unknown:
        raise SettingsError(f"unknown launcher settings: {', '.join(unknown)}")

    channel = _string(values, "channel", DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
    manifest_url = _string(values, "manifestUrl", "").strip()
    launcher_manifest_url = _string(values, "launcherManifestUrl", "").strip()
    executable_name = _string(values, "executableName", DEFAULT_EXECUTABLE).strip()
    if not executable_name:
        executable_name = DEFAULT_EXECUTABLE
    if "/" in executable_name or "\\" in executable_name:
        raise SettingsError("executableName must be a file name, not a path")

    install_dir_value = values.get("installDir")
    if install_dir_value is None:
        install_dir = None
    elif isinstance(install_dir_value, str) and install_dir_value.strip():
        install_dir = Path(install_dir_value)
    elif isinstance(install_dir_value, str):
        install_dir = None
    else:
        raise SettingsError("installDir must be a string or null")

    auto_update = values.get("autoUpdate", True)
    if not isinstance(auto_update, bool):
        raise SettingsError("autoUpdate must be true or false")

    stored_endpoint = values.get("serverEndpoint", _UNSET)
    if stored_endpoint is _UNSET:
        stored_endpoint = DEFAULT_SERVER_ENDPOINT
    try:
        server_endpoint = normalise_server_endpoint(stored_endpoint)
    except ServerEndpointConfigurationError as exc:
        raise SettingsError(f"stored server endpoint is invalid: {exc}") from exc

    return LauncherSettings(
        channel=channel,
        manifest_url=manifest_url,
        install_dir=install_dir,
        server_endpoint=server_endpoint,
        executable_name=executable_name,
        auto_update=auto_update,
        launcher_manifest_url=launcher_manifest_url,
    )


def settings_path(data_dir: str | Path | None = None) -> Path:
    root = default_data_dir() if data_dir is None else Path(data_dir)
    return root / SETTINGS_FILE_NAME


def load_settings(data_dir: str | Path | None = None) -> LauncherSettings:
    """Read stored settings, returning defaults when none exist yet."""

    path = settings_path(data_dir)
    try:
        # utf-8-sig, because Notepad, PowerShell Out-File, and Set-Content all
        # write a byte order mark by default on Windows. A player who edits
        # this file with any of them must not end up with a launcher that
        # cannot read its own settings.
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return LauncherSettings()
    except OSError as exc:
        raise SettingsError(f"cannot read launcher settings: {path} ({exc})") from exc

    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError(
            f"launcher settings are not valid JSON: {path} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    return settings_from_json(values)


def save_settings(
    settings: LauncherSettings, data_dir: str | Path | None = None
) -> Path:
    """Write settings atomically so a crash cannot truncate them."""

    path = settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".launcher-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(settings.as_json(), output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SettingsError(f"cannot write launcher settings: {path} ({exc})") from exc
    return path


__all__ = [
    "DEFAULT_CHANNEL",
    "DEFAULT_EXECUTABLE",
    "DEFAULT_SERVER_ENDPOINT",
    "DEFAULT_MANIFEST_URL_TEMPLATE",
    "LAUNCHER_CHANNEL",
    "LauncherSettings",
    "SETTINGS_FILE_NAME",
    "SettingsError",
    "default_data_dir",
    "default_manifest_url",
    "load_settings",
    "normalise_server_endpoint",
    "save_settings",
    "settings_from_json",
    "settings_path",
]
