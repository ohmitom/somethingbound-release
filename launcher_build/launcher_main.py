"""Frozen entry point for SomethingBoundLauncher.exe.

PyInstaller needs a plain script to start from. Keeping it separate from the
package means the packaged executable and ``python -m release_tools.launcher``
run exactly the same code.
"""

from __future__ import annotations

import multiprocessing
import sys

from release_tools.launcher import main

if __name__ == "__main__":
    # Required before anything else in a frozen build: without it, a child
    # process would re-run the launcher instead of the worker it meant to start.
    multiprocessing.freeze_support()
    sys.exit(main())
