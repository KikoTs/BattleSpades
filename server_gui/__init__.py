"""BattleSpades Server desktop host.

The window (``server_gui.app``) is a thin layer over plain logic modules that
never import a GUI toolkit, so they can be tested without a display:

* ``paths``        - where the server, config, logs and GUI state live
* ``config_doc``   - comment-preserving config.toml editing and validation
* ``catalog``      - game modes and maps the server really supports
* ``process``      - the dedicated server as a supervised child process
* ``network``      - local/public address, UPnP / NAT-PMP, reachability
* ``firewall``     - host firewall rules and instructions
* ``logparse``     - log line levels and well-known events
* ``console``      - the operator command palette
* ``gui_state``    - remembered window state and the single-instance lock
"""

__all__ = ["main"]


def main(argv=None) -> int:
    """Start the desktop host (imports the toolkit lazily)."""

    from server_gui.app import main as run_app

    return run_app(argv)
