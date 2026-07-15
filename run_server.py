#!/usr/bin/env python3
"""BattleSpades frozen and source entrypoint."""

import multiprocessing


if __name__ == "__main__":
    # PyInstaller's child-process command line must be intercepted before
    # importing the server or bot runtime, otherwise a worker recursively
    # launches a second dedicated server.
    multiprocessing.freeze_support()

    from server.launcher import run

    raise SystemExit(run())
