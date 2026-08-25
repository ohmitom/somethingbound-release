"""The SomethingBound launcher window.

Tkinter ships with CPython, so the launcher stays dependency-free and packages
into a single executable.  Every colour here is taken from the client's own HUD
chrome so the launcher and the game read as one product rather than two.

All work that can block - reaching the channel, downloading, unpacking - runs on
a worker thread and reports back through a queue that the Tk main loop drains.
Nothing touches a widget from the worker.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .launcher_core import Launcher, LauncherError, LauncherStatus
from .server import ServerEndpointConfigurationError
from .settings import (
    LauncherSettings,
    SettingsError,
    load_settings,
    normalise_server_endpoint,
    save_settings,
)

# Sampled from HudChrome in the client so the launcher matches the game.
BACKDROP = "#060a12"
FIELD = "#080d16"
FIELD_SOFT = "#0b121d"
TEAL = "#3fe0d4"
TEAL_DIM = "#1d6d68"
AMBER = "#ffb03c"
MAGENTA = "#ff3f9e"
TEXT = "#c8d6e4"
TEXT_DIM = "#6c8199"

TITLE_FONT = ("Segoe UI Semibold", 22)
LABEL_FONT = ("Segoe UI", 9)
BODY_FONT = ("Segoe UI", 10)
NOTES_FONT = ("Consolas", 9)
BUTTON_FONT = ("Segoe UI Semibold", 12)


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class _Worker:
    """Run one background job at a time and post results to a queue."""

    def __init__(self) -> None:
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def post(self, kind: str, payload: Any = None) -> None:
        self.messages.put((kind, payload))

    def run(self, name: str, job: Callable[[], Any]) -> None:
        if self.busy:
            return

        def target() -> None:
            try:
                self.post(f"{name}:done", job())
            except Exception as exc:  # noqa: BLE001 - reported to the player
                self.post(f"{name}:failed", exc)

        self._thread = threading.Thread(target=target, name=f"launcher-{name}", daemon=True)
        self._thread.start()


class LauncherWindow:
    """The launcher's single window."""

    def __init__(
        self,
        root: tk.Tk,
        settings: LauncherSettings,
        *,
        data_dir: Path | None = None,
    ) -> None:
        self.root = root
        self.settings = settings
        self.data_dir = data_dir
        self.launcher = Launcher(settings)
        self.worker = _Worker()
        self.status: LauncherStatus | None = None
        self._action: str = "none"

        root.title("SomethingBound")
        root.configure(background=BACKDROP)
        root.geometry("760x520")
        root.minsize(660, 460)

        self._configure_style()
        self._use_dark_title_bar()
        self._build_header()
        # The footer is packed against the bottom first so the notes panel,
        # which expands, can never squeeze the primary action off the window.
        self._build_footer()
        self._build_notes()

        self.root.after(80, self._pump)
        if settings.auto_update or self.launcher.installed() is None:
            self._set_action("none", "Checking the release channel...")
            self.refresh()
        else:
            # The player asked not to be interrupted by an update check, so
            # show the installed build and let Settings trigger one on demand.
            self._render(LauncherStatus(self.launcher.installed(), None, False))

    # -- construction ----------------------------------------------------

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        # "clam" is the only bundled theme that honours custom colours on
        # Windows; the native theme ignores background settings entirely.
        style.theme_use("clam")
        style.configure("Launcher.TFrame", background=BACKDROP)
        style.configure("Panel.TFrame", background=FIELD)
        style.configure(
            "Rule.TFrame", background=TEAL_DIM
        )
        style.configure(
            "Launcher.TLabel", background=BACKDROP, foreground=TEXT, font=BODY_FONT
        )
        style.configure(
            "Title.TLabel", background=BACKDROP, foreground=TEAL, font=TITLE_FONT
        )
        style.configure(
            "Caption.TLabel", background=BACKDROP, foreground=TEXT_DIM, font=LABEL_FONT
        )
        style.configure(
            "Version.TLabel", background=BACKDROP, foreground=TEXT_DIM, font=LABEL_FONT
        )
        style.configure(
            "Launcher.Horizontal.TProgressbar",
            background=TEAL,
            troughcolor=FIELD,
            bordercolor=FIELD,
            lightcolor=TEAL,
            darkcolor=TEAL,
            thickness=6,
        )
        style.configure(
            "Secondary.TButton",
            background=FIELD_SOFT,
            foreground=TEXT,
            bordercolor=TEAL_DIM,
            focuscolor=FIELD_SOFT,
            font=LABEL_FONT,
            padding=(14, 8),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", FIELD), ("disabled", FIELD)],
            foreground=[("disabled", TEXT_DIM)],
        )
        style.configure(
            "Launcher.TCheckbutton",
            background=BACKDROP,
            foreground=TEXT,
            font=LABEL_FONT,
            # clam names these indicatorbackground/foreground; the
            # indicatorcolor option belongs to other themes and is ignored.
            indicatorbackground=FIELD,
            indicatorforeground=TEAL,
            bordercolor=TEAL_DIM,
            focuscolor=BACKDROP,
        )
        style.map(
            "Launcher.TCheckbutton",
            background=[("active", BACKDROP)],
            indicatorbackground=[("active", FIELD_SOFT), ("selected", FIELD)],
            indicatorforeground=[("selected", TEAL)],
        )
        style.configure(
            "Launcher.Vertical.TScrollbar",
            background=TEAL_DIM,
            troughcolor=FIELD,
            bordercolor=FIELD,
            arrowcolor=TEXT_DIM,
            # clam draws a bevel from these two; matching them to the thumb
            # keeps the scrollbar flat instead of raised and pale.
            darkcolor=TEAL_DIM,
            lightcolor=TEAL_DIM,
            gripcount=0,
            relief="flat",
            arrowsize=12,
        )
        style.map(
            "Launcher.Vertical.TScrollbar",
            background=[("active", TEAL), ("disabled", FIELD)],
            darkcolor=[("active", TEAL), ("disabled", FIELD)],
            lightcolor=[("active", TEAL), ("disabled", FIELD)],
            arrowcolor=[("disabled", FIELD)],
        )

    def _use_dark_title_bar(self) -> None:
        """Ask Windows for a dark title bar so the frame matches the window.

        This is cosmetic and best effort: an older Windows build simply
        ignores the attribute and keeps its default frame.
        """

        if sys.platform != "win32":
            return
        try:
            import ctypes

            self.root.update_idletasks()
            handle = ctypes.windll.user32.GetParent(self.root.winfo_id())
            use_dark_mode = ctypes.c_int(1)
            # 20 is DWMWA_USE_IMMERSIVE_DARK_MODE on current Windows builds.
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                handle, 20, ctypes.byref(use_dark_mode), ctypes.sizeof(use_dark_mode)
            )
        except Exception:  # noqa: BLE001 - a light title bar is not a failure
            pass

    def _build_header(self) -> None:
        header = ttk.Frame(self.root, style="Launcher.TFrame", padding=(28, 22, 28, 12))
        header.pack(fill="x")

        ttk.Label(header, text="SOMETHINGBOUND", style="Title.TLabel").pack(anchor="w")
        self.channel_label = ttk.Label(header, text="", style="Caption.TLabel")
        self.channel_label.pack(anchor="w", pady=(2, 0))

        rule = ttk.Frame(self.root, style="Rule.TFrame", height=1)
        rule.pack(fill="x", padx=28)

    def _build_notes(self) -> None:
        body = ttk.Frame(self.root, style="Launcher.TFrame", padding=(28, 16, 28, 8))
        body.pack(fill="both", expand=True)

        heading = ttk.Frame(body, style="Launcher.TFrame")
        heading.pack(fill="x")
        ttk.Label(heading, text="WHAT'S NEW", style="Caption.TLabel").pack(side="left")
        self.build_label = ttk.Label(heading, text="", style="Version.TLabel")
        self.build_label.pack(side="right")

        panel = tk.Frame(
            body, background=FIELD, highlightthickness=1, highlightbackground=TEAL_DIM
        )
        panel.pack(fill="both", expand=True, pady=(8, 0))

        self.notes = tk.Text(
            panel,
            background=FIELD,
            foreground=TEXT,
            insertbackground=TEAL,
            selectbackground=TEAL_DIM,
            font=NOTES_FONT,
            relief="flat",
            wrap="word",
            width=1,
            height=6,
            padx=14,
            pady=12,
            borderwidth=0,
            highlightthickness=0,
        )
        scroll = ttk.Scrollbar(
            panel,
            orient="vertical",
            style="Launcher.Vertical.TScrollbar",
            command=self.notes.yview,
        )
        self.notes.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.notes.pack(side="left", fill="both", expand=True)

        self.notes.tag_configure("heading", foreground=TEAL, font=("Consolas", 10, "bold"))
        self.notes.tag_configure("summary", foreground=TEXT)
        self.notes.tag_configure("sha", foreground=TEXT_DIM)
        self.notes.tag_configure("problem", foreground=MAGENTA)

    def _build_footer(self) -> None:
        footer = ttk.Frame(self.root, style="Launcher.TFrame", padding=(28, 4, 28, 22))
        footer.pack(side="bottom", fill="x")

        self.progress = ttk.Progressbar(
            footer,
            style="Launcher.Horizontal.TProgressbar",
            mode="determinate",
            maximum=1000,
        )
        self.progress.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(footer, style="Launcher.TFrame")
        row.pack(fill="x")

        # The buttons are packed before the status text so a long status can
        # never take their width and clip their labels.  The text then wraps
        # inside whatever space is left.

        # A tk.Button, not ttk, because only the classic widget lets the
        # primary action carry a solid accent fill on Windows.
        self.action_button = tk.Button(
            row,
            text="PLAY",
            font=BUTTON_FONT,
            command=self._on_action,
            background=TEAL,
            foreground=BACKDROP,
            activebackground="#6ceee4",
            activeforeground=BACKDROP,
            disabledforeground=TEXT_DIM,
            relief="flat",
            borderwidth=0,
            width=12,
            pady=8,
            cursor="hand2",
        )
        self.action_button.pack(side="right")

        self.settings_button = ttk.Button(
            row, text="Settings", style="Secondary.TButton", command=self._open_settings
        )
        self.settings_button.pack(side="right", padx=(0, 10))

        self.check_button = ttk.Button(
            row,
            text="Check for updates",
            style="Secondary.TButton",
            command=self.refresh,
        )
        self.check_button.pack(side="right", padx=(0, 10))

        self.status_label = ttk.Label(row, text="", style="Launcher.TLabel", anchor="w")
        self.status_label.pack(side="left", fill="both", expand=True)
        self.status_label.bind("<Configure>", self._wrap_status)

    def _wrap_status(self, event: "tk.Event") -> None:
        """Keep the status text wrapping to the width it was actually given."""

        width = max(event.width - 8, 120)
        if self.status_label.cget("wraplength") != width:
            self.status_label.configure(wraplength=width)

    # -- rendering -------------------------------------------------------

    def _set_action(self, action: str, status_text: str) -> None:
        self._action = action
        labels = {
            "install": ("INSTALL", TEAL),
            "update": ("UPDATE", AMBER),
            "play": ("PLAY", TEAL),
            "none": ("PLAY", TEAL),
        }
        text, colour = labels.get(action, ("PLAY", TEAL))
        enabled = action != "none"
        self.action_button.configure(
            text=text,
            background=colour if enabled else FIELD_SOFT,
            foreground=BACKDROP if enabled else TEXT_DIM,
            state="normal" if enabled else "disabled",
            cursor="hand2" if enabled else "arrow",
        )
        self.status_label.configure(text=status_text)

    def _write_notes(self, status: LauncherStatus) -> None:
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")

        manifest = status.manifest
        if manifest is None and not status.checked_channel:
            self.notes.insert("end", "Updates were not checked.\n\n", "heading")
            self.notes.insert(
                "end",
                "Automatic update checks are switched off in Settings. "
                "Use Check for updates to look at the channel now.\n",
                "summary",
            )
        elif manifest is None:
            self.notes.insert("end", "The release channel could not be reached.\n\n", "problem")
            self.notes.insert("end", (status.channel_error or "").strip() + "\n", "sha")
            if status.can_play:
                self.notes.insert(
                    "end",
                    "\nThe installed build is unaffected and can still be played.\n",
                    "summary",
                )
        else:
            self.notes.insert(
                "end", f"{manifest['channel']} {manifest['version']}\n", "heading"
            )
            self.notes.insert("end", f"{manifest['notes']['summary']}\n\n", "summary")
            commits = manifest["notes"]["commits"]
            if commits:
                for commit in commits:
                    scope = f"({commit['scope']})" if commit.get("scope") else ""
                    reference = ""
                    if commit.get("pr") is not None:
                        raw = str(commit["pr"])
                        reference = f"  PR {raw if raw.startswith('#') else '#' + raw}"
                    self.notes.insert("end", f"  {commit['sha'][:7]} ", "sha")
                    self.notes.insert(
                        "end", f"{commit['category']}{scope}: {commit['subject']}{reference}\n"
                    )
            else:
                self.notes.insert("end", "  No individual changes were listed.\n", "sha")

            if status.channel_error:
                self.notes.insert("end", f"\n{status.channel_error}\n", "problem")

        self.notes.configure(state="disabled")

    def _render(self, status: LauncherStatus) -> None:
        self.status = status
        self.channel_label.configure(
            text=f"{self.settings.channel} channel  //  "
            + (self.settings.server_endpoint or "no server selected")
        )
        installed = status.installed_version
        self.build_label.configure(
            text=f"installed {installed}" if installed else "not installed"
        )
        self._write_notes(status)

        if status.channel_error is not None and not status.can_play:
            self._set_action("none", status.summary())
        elif status.is_first_install:
            self._set_action("install", status.summary())
        elif status.update_available:
            self._set_action("update", status.summary())
        else:
            self._set_action("play", status.summary())

        if self._action == "play" and self.settings.server_endpoint is None:
            self.status_label.configure(
                text=status.summary() + " Choose a server in Settings before playing."
            )

    # -- actions ---------------------------------------------------------

    def refresh(self) -> None:
        """Re-check the channel without blocking the window."""

        if self.worker.busy:
            return
        self._busy("Checking the release channel...")
        self.launcher = Launcher(self.settings)
        self.worker.run("status", self.launcher.status)

    def _busy(self, message: str) -> None:
        self.action_button.configure(state="disabled", cursor="arrow")
        self.settings_button.configure(state="disabled")
        self.check_button.configure(state="disabled")
        self.status_label.configure(text=message)
        self.progress.configure(value=0)

    def _on_action(self) -> None:
        if self.worker.busy or self.status is None:
            return
        if self._action in ("install", "update"):
            manifest = self.status.manifest
            if manifest is None:
                return
            self._busy("Preparing download...")

            def job() -> Any:
                return self.launcher.update(
                    manifest,
                    progress=lambda done, total: self.worker.post(
                        "progress", (done, total)
                    ),
                )

            self.worker.run("update", job)
        elif self._action == "play":
            self._play()

    def _play(self) -> None:
        try:
            self.launcher.play()
        except LauncherError as exc:
            self.status_label.configure(text=str(exc))
            return
        self.root.destroy()

    def report_settings_problem(self, problem: str) -> None:
        """Tell the player their settings were unreadable, once the window is up."""

        self.status_label.configure(
            text="Settings could not be read. Running with defaults."
        )
        self.root.after(
            120,
            lambda: messagebox.showwarning(
                "SomethingBound",
                "Your launcher settings could not be read, so the defaults are in "
                "use. Opening Settings and saving will replace the unreadable "
                "file.\n\n" + problem,
                parent=self.root,
            ),
        )

    def _open_settings(self) -> None:
        SettingsDialog(self)

    def apply_settings(self, settings: LauncherSettings) -> None:
        """Persist changed settings and re-check the channel against them."""

        self.settings = settings
        try:
            save_settings(settings, self.data_dir)
        except SettingsError as exc:
            self.status_label.configure(text=str(exc))
            return
        self.refresh()

    # -- worker pump -----------------------------------------------------

    def _idle(self) -> None:
        self.settings_button.configure(state="normal")
        self.check_button.configure(state="normal")

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.worker.messages.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _handle(self, kind: str, payload: Any) -> None:
        if kind == "progress":
            done, total = payload
            if total:
                self.progress.configure(value=int(done / total * 1000))
                self.status_label.configure(
                    text=f"Downloading {_human_bytes(done)} of {_human_bytes(total)}..."
                )
            return

        if kind == "status:done":
            self._idle()
            self._render(payload)
        elif kind == "update:done":
            self._idle()
            self.progress.configure(value=1000)
            if payload.updated:
                self.status_label.configure(
                    text=f"Installed {payload.installed.build.version}."
                )
            self.refresh()
        elif kind in ("status:failed", "update:failed"):
            self._idle()
            self.progress.configure(value=0)
            message = str(payload) or payload.__class__.__name__
            self.status_label.configure(text=message)
            if self.launcher.installed() is not None:
                self._set_action("play", message)
            else:
                self._set_action("none", message)


class SettingsDialog:
    """The server connection and channel settings window."""

    def __init__(self, owner: LauncherWindow) -> None:
        self.owner = owner
        settings = owner.settings

        self.window = tk.Toplevel(owner.root)
        self.window.title("SomethingBound settings")
        self.window.configure(background=BACKDROP)
        self.window.resizable(False, False)
        self.window.transient(owner.root)
        self.window.grab_set()

        frame = ttk.Frame(self.window, style="Launcher.TFrame", padding=24)
        frame.pack(fill="both", expand=True)

        self.server = tk.StringVar(value=settings.server_endpoint or "")
        self.channel = tk.StringVar(value=settings.channel)
        self.manifest = tk.StringVar(value=settings.manifest_url)
        self.install_dir = tk.StringVar(
            value="" if settings.install_dir is None else str(settings.install_dir)
        )
        self.auto_update = tk.BooleanVar(value=settings.auto_update)

        self._field(
            frame,
            "Server",
            self.server,
            "HTTPS address of the game server. http:// is accepted for localhost only.",
        )
        self._field(frame, "Channel", self.channel, "Which release channel to follow.")
        self._field(
            frame,
            "Manifest URL",
            self.manifest,
            "Leave blank to use the published channel for the name above.",
        )
        self._field(
            frame,
            "Install folder",
            self.install_dir,
            "Leave blank to install under your local application data.",
            browse=True,
        )

        ttk.Checkbutton(
            frame,
            text="Check for updates when the launcher opens",
            variable=self.auto_update,
            style="Launcher.TCheckbutton",
        ).pack(anchor="w", pady=(18, 0))

        self.error = ttk.Label(frame, text="", style="Caption.TLabel", wraplength=440)
        self.error.pack(anchor="w", pady=(12, 0))
        self.error.configure(foreground=MAGENTA)

        buttons = ttk.Frame(frame, style="Launcher.TFrame")
        buttons.pack(fill="x", pady=(18, 0))
        ttk.Button(
            buttons, text="Cancel", style="Secondary.TButton", command=self.window.destroy
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            buttons, text="Save", style="Secondary.TButton", command=self._save
        ).pack(side="right")

        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.update_idletasks()
        self._centre_on_owner()

    def _centre_on_owner(self) -> None:
        owner = self.owner.root
        x = owner.winfo_rootx() + (owner.winfo_width() - self.window.winfo_width()) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - self.window.winfo_height()) // 3
        self.window.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _field(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        hint: str,
        *,
        browse: bool = False,
    ) -> None:
        """Stack one labelled entry and its hint as a self-contained block."""

        block = ttk.Frame(parent, style="Launcher.TFrame")
        block.pack(fill="x", pady=(0, 14))

        ttk.Label(block, text=label.upper(), style="Caption.TLabel").pack(
            anchor="w", pady=(0, 4)
        )

        row = ttk.Frame(block, style="Launcher.TFrame")
        row.pack(fill="x")
        entry = tk.Entry(
            row,
            textvariable=variable,
            background=FIELD,
            foreground=TEXT,
            insertbackground=TEAL,
            selectbackground=TEAL_DIM,
            relief="flat",
            font=BODY_FONT,
            width=52,
            highlightthickness=1,
            highlightbackground=TEAL_DIM,
            highlightcolor=TEAL,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=5)
        if browse:
            ttk.Button(
                row,
                text="Browse",
                style="Secondary.TButton",
                command=lambda: self._browse(variable),
            ).pack(side="right", padx=(8, 0))

        ttk.Label(block, text=hint, style="Caption.TLabel", wraplength=440).pack(
            anchor="w", pady=(4, 0)
        )

    def _browse(self, variable: tk.StringVar) -> None:
        chosen = filedialog.askdirectory(parent=self.window, title="Install folder")
        if chosen:
            variable.set(chosen)

    def _save(self) -> None:
        try:
            endpoint = normalise_server_endpoint(self.server.get())
        except ServerEndpointConfigurationError as exc:
            self.error.configure(text=str(exc))
            return

        channel = self.channel.get().strip() or "playtest"
        install = self.install_dir.get().strip()
        settings = LauncherSettings(
            channel=channel,
            manifest_url=self.manifest.get().strip(),
            install_dir=Path(install) if install else None,
            server_endpoint=endpoint,
            executable_name=self.owner.settings.executable_name,
            auto_update=self.auto_update.get(),
        )
        self.window.destroy()
        self.owner.apply_settings(settings)


def run(settings: LauncherSettings, *, data_dir: Path | None = None) -> int:
    """Open the launcher window and return once it closes."""

    root = tk.Tk()
    LauncherWindow(root, settings, data_dir=data_dir)
    root.mainloop()
    return 0


def start(data_dir: Path | None = None) -> int:
    """Open the launcher, surviving settings it cannot read.

    A windowed executable that exits on a bad settings file simply never
    appears, which a player cannot tell apart from a broken download.  Show
    the problem and carry on from defaults instead.
    """

    problem: str | None = None
    try:
        settings = load_settings(data_dir)
    except SettingsError as exc:
        settings = LauncherSettings()
        problem = str(exc)

    root = tk.Tk()
    window = LauncherWindow(root, settings, data_dir=data_dir)
    if problem is not None:
        window.report_settings_problem(problem)
    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open the SomethingBound launcher.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="directory holding launcher.json (default: per-user application data)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return start(args.data_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
