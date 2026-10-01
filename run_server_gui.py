#!/usr/bin/env python3
"""BattleSpades Server desktop host: frozen and source entrypoint."""

import multiprocessing


if __name__ == "__main__":
    multiprocessing.freeze_support()

    from server_gui import main

    raise SystemExit(main())
