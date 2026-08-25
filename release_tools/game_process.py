"""Locating and starting the installed game client.

The launcher hands the client two things: where to find itself, and which
server to talk to.  The endpoint is passed both as a command line argument and
as an environment variable, because a Unity player reads command line arguments
reliably but a developer running the client directly from the editor has only
the environment.  The client prefers the argument when both are present.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

from .server import MANUAL_SERVER_ENDPOINT_ENV

SERVER_ENDPOINT_ARGUMENT = "--server-endpoint"

# A Unity build puts the player executable at the payload root, but a zip made
# from a build folder can nest it one level down.  Searching deeper than this
# would risk starting an unrelated bundled tool.
_MAX_SEARCH_DEPTH = 2


class GameLaunchError(RuntimeError):
    """The installed client could not be located or started."""


def find_executable(payload_dir: str | Path, executable_name: str) -> Path:
    """Return the client executable inside an installed payload.

    The search is shallow and name-driven so that a payload containing several
    executables, such as Unity's crash handler, cannot be started by accident.
    """

    root = Path(payload_dir)
    if not root.is_dir():
        raise GameLaunchError(f"installed build directory is missing: {root}")

    wanted = executable_name.casefold()
    candidates: list[Path] = []
    for depth in range(_MAX_SEARCH_DEPTH):
        pattern = "/".join(["*"] * depth + [executable_name]) if depth else executable_name
        for candidate in root.glob(pattern):
            if candidate.is_file() and candidate.name.casefold() == wanted:
                candidates.append(candidate)
        if candidates:
            break

    if not candidates:
        raise GameLaunchError(
            f"{executable_name} was not found in the installed build at {root}"
        )
    return sorted(candidates)[0]


def build_command(
    executable: str | Path,
    *,
    server_endpoint: str | None = None,
    extra_arguments: Sequence[str] = (),
) -> list[str]:
    """Return the argument vector used to start the client."""

    command = [str(executable)]
    if server_endpoint:
        command += [SERVER_ENDPOINT_ARGUMENT, server_endpoint]
    command += [str(argument) for argument in extra_arguments]
    return command


def build_environment(
    server_endpoint: str | None = None, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the environment the client is started with."""

    environment = dict(os.environ if base is None else base)
    if server_endpoint:
        environment[MANUAL_SERVER_ENDPOINT_ENV] = server_endpoint
    else:
        # Never leak the launcher's own endpoint into a client that was told
        # to run without one.
        environment.pop(MANUAL_SERVER_ENDPOINT_ENV, None)
    return environment


def start_game(
    payload_dir: str | Path,
    executable_name: str,
    *,
    server_endpoint: str | None = None,
    extra_arguments: Sequence[str] = (),
    popen: object | None = None,
) -> subprocess.Popen:
    """Start the installed client and return without waiting for it.

    The client is detached from the launcher so closing the launcher window
    does not terminate a running game.
    """

    executable = find_executable(payload_dir, executable_name)
    command = build_command(
        executable, server_endpoint=server_endpoint, extra_arguments=extra_arguments
    )

    creation_flags = 0
    start_new_session = False
    if sys.platform == "win32":
        # Detach from the launcher's console and job so the game survives the
        # launcher exiting.
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        start_new_session = True

    spawn = popen or subprocess.Popen
    try:
        return spawn(  # type: ignore[operator]
            command,
            cwd=str(executable.parent),
            env=build_environment(server_endpoint),
            creationflags=creation_flags,
            start_new_session=start_new_session,
            close_fds=True,
        )
    except OSError as exc:
        raise GameLaunchError(f"cannot start {executable}: {exc}") from exc


__all__ = [
    "SERVER_ENDPOINT_ARGUMENT",
    "GameLaunchError",
    "build_command",
    "build_environment",
    "find_executable",
    "start_game",
]
