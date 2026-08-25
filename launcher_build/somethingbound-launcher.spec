# PyInstaller spec for SomethingBoundLauncher.exe
#
# Build with launcher_build/build-launcher.ps1, which creates the isolated
# build environment first. Running PyInstaller directly against this spec works
# too, as long as the release repository root is the working directory.
#
# The launcher imports nothing outside the standard library, so the bundle is
# CPython, Tk, and release_tools. Everything else is excluded to keep the
# executable small and its start-up fast.

import os

block_cipher = None

# PyInstaller resolves paths in a spec against the spec's own directory, not
# the working directory, so both are derived from SPECPATH rather than getcwd.
build_dir = os.path.abspath(SPECPATH)  # noqa: F821 - injected by PyInstaller
repository_root = os.path.dirname(build_dir)

analysis = Analysis(
    [os.path.join(build_dir, "launcher_main.py")],
    pathex=[repository_root],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # None of these are imported by the launcher; excluding them keeps a
        # broad site-packages install from being swept into the bundle.
        "numpy",
        "pandas",
        "matplotlib",
        "PIL",
        "pytest",
        "setuptools",
        "pip",
        "unittest",
        "pydoc",
        "doctest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

archive = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

executable = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="SomethingBoundLauncher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # A launcher must not flash a console window behind its own window.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
