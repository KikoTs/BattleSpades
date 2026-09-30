"""Headless anti-cheat laboratory.

Drives the REAL server code with scripted protocol clients through an
impaired network and records what every anti-cheat detector reports.

* ``clientmodel``  - a legitimate protocol-168 client: 60 Hz loop, retail
  clock sync, local movement prediction with the server's own mover, retail
  position reconciliation, weapon cadence/reload, tool actions.
* ``link``         - network impairment (delay, jitter, loss, burst loss,
  delay spikes) and the ENet delivery classes the game uses.
* ``lab``          - an in-process server (no sockets, virtual clock) plus
  the clients; used by the fast pytest versions.
* ``udp``          - the same clients over real ENet sockets through
  ``scripts/udp_lag_proxy.py``.
* ``scenarios``    - what the legitimate clients do.
* ``cheats``       - the same clients with one forged behaviour each.

Entry points: ``scripts/anticheat_false_positive_run.py`` and
``scripts/anticheat_cheat_run.py``.
"""
