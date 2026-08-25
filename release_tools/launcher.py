"""The SomethingBound launcher entry point.

Running this with no subcommand opens the launcher window, which is what the
packaged executable does.  The subcommands expose exactly the same operations
without a window, so a playtest problem can be diagnosed over a terminal and a
scripted run can install a build unattended.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .launcher_core import Launcher, LauncherError
from .server import ServerEndpointConfigurationError
from .settings import (
    LauncherSettings,
    SettingsError,
    load_settings,
    save_settings,
    settings_path,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="somethingbound-launcher",
        description="Update and start the SomethingBound client.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"SomethingBound launcher {__version__}",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="directory holding launcher.json (default: per-user application data)",
    )

    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="report the installed build and the channel")

    update = commands.add_parser("update", help="install the current channel build")
    update.add_argument(
        "--collapsed-notes",
        action="store_true",
        help="show the commit count instead of expanding exact commits",
    )

    commands.add_parser("play", help="start the installed client and exit")
    commands.add_parser("rollback", help="return to the retained previous build")
    commands.add_parser(
        "self-update", help="replace this launcher with the published one"
    )

    config = commands.add_parser("config", help="show or change launcher settings")
    config.add_argument("--server", help="server endpoint the client connects to")
    config.add_argument("--channel", help="release channel to follow")
    config.add_argument("--manifest-url", help="explicit channel manifest URL")
    config.add_argument("--install-dir", type=Path, help="where builds are installed")
    config.add_argument(
        "--auto-update",
        choices=("on", "off"),
        help="whether the window checks the channel when it opens",
    )
    return parser


def _run_self_update(launcher: Launcher) -> int:
    manifest = launcher.fetch_launcher_manifest()
    plan = launcher.launcher_plan(manifest)
    if not plan.available:
        print(
            f"Launcher {plan.installed_version} is current "
            f"(channel has {plan.channel_version}): {plan.reason}"
        )
        return 0

    print(f"Updating the launcher from {plan.installed_version} to {plan.channel_version}")
    retired = launcher.update_launcher(manifest)
    print(f"The updated launcher has been started. The previous one is at {retired}")
    print("and is deleted the next time the launcher runs.")
    return 0


def _print_status(launcher: Launcher) -> int:
    status = launcher.status()
    print(status.summary())
    print(f"Launcher:  {__version__}")
    installed = status.installed
    if installed is not None:
        print(f"Installed: {installed.build.version} ({installed.build.git_sha})")
        print(f"Payload:   {installed.directory}")
    previous = launcher.installer.read_previous()
    if previous is not None:
        print(f"Rollback:  {previous.build.version} ({previous.build.git_sha})")
    print(f"Server:    {launcher.settings.server_endpoint or 'not selected'}")
    print(f"Channel:   {launcher.settings.resolved_manifest_url()}")
    if status.manifest is not None:
        print()
        print(launcher.patch_notes(status.manifest))
    return 0 if status.channel_error is None else 1


def _run_update(launcher: Launcher, *, collapsed: bool) -> int:
    status = launcher.status()
    if status.manifest is None:
        print(status.summary(), file=sys.stderr)
        return 1
    if not status.update_available and not status.is_first_install:
        print(status.summary())
        return 0

    reported = -1

    def progress(done: int, total: int) -> None:
        nonlocal reported
        if not total:
            return
        step = done * 100 // total // 10
        if step != reported:
            reported = step
            print(f"  downloading {step * 10}%", flush=True)

    result = launcher.update(status.manifest, progress=progress)
    if result.updated:
        print(f"Installed {result.installed.build.version} to {result.installed.directory}")
        if result.previous is not None:
            print(f"Previous build retained: {result.previous.build.version}")
    else:
        print("No update installed.")
    print()
    print(launcher.patch_notes(status.manifest, expanded=not collapsed))
    return 0


def _run_config(args: argparse.Namespace, settings: LauncherSettings) -> int:
    changed = settings
    if args.server is not None:
        changed = changed.with_server_endpoint(args.server)
    if args.channel:
        changed = replace(changed, channel=args.channel.strip())
    if args.manifest_url is not None:
        changed = replace(changed, manifest_url=args.manifest_url.strip())
    if args.install_dir is not None:
        changed = replace(changed, install_dir=args.install_dir)
    if args.auto_update is not None:
        changed = replace(changed, auto_update=args.auto_update == "on")

    if changed != settings:
        save_settings(changed, args.data_dir)
        print(f"Saved {settings_path(args.data_dir)}")

    print(f"channel:      {changed.channel}")
    print(f"manifest URL: {changed.resolved_manifest_url()}")
    print(f"install dir:  {changed.resolved_install_dir()}")
    print(f"server:       {changed.server_endpoint or 'not selected'}")
    print(f"auto update:  {'on' if changed.auto_update else 'off'}")
    return 0


def _attach_parent_console() -> None:
    """Let the packaged launcher print into the terminal that ran it.

    The executable is built for the Windows GUI subsystem so it never flashes a
    console behind its own window. The cost is that its subcommands write to
    nowhere, which makes diagnosing a playtester's launcher over a terminal
    impossible: a failure and a no-op look identical. Attaching to the calling
    console restores the output without ever creating a window.
    """

    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        ATTACH_PARENT_PROCESS = -1
        if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return
        for name, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
            if stream is None or getattr(stream, "closed", False):
                setattr(sys, name, open("CONOUT$", "w", encoding="utf-8", buffering=1))
    except Exception:  # noqa: BLE001 - console output is a convenience
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is not None:
        _attach_parent_console()

    try:
        settings = load_settings(args.data_dir)
    except SettingsError as exc:
        print(f"launcher settings error: {exc}", file=sys.stderr)
        return 1

    if args.command is None:
        from .gui import run

        return run(settings, data_dir=args.data_dir)

    try:
        if args.command == "config":
            return _run_config(args, settings)

        launcher = Launcher(settings)
        if args.command == "status":
            return _print_status(launcher)
        if args.command == "update":
            return _run_update(launcher, collapsed=args.collapsed_notes)
        if args.command == "self-update":
            return _run_self_update(launcher)
        if args.command == "rollback":
            restored = launcher.rollback()
            print(f"Rolled back to {restored.build.version} ({restored.build.git_sha})")
            return 0
        if args.command == "play":
            launcher.play()
            print("Started the client.")
            return 0
    except (LauncherError, SettingsError, ServerEndpointConfigurationError) as exc:
        print(f"launcher error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
