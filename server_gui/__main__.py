"""``python -m server_gui`` starts the desktop host."""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from server_gui import main

    raise SystemExit(main())
