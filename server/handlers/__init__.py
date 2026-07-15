"""Domain packet-handler boundaries for the incremental server refactor.

The active decorator registry remains in :mod:`protocol.packet_handler` while
characterization tests are added. Handlers move here one domain at a time:

``movement``
    Client input, position reports, reconciliation, and movement abilities.
``blocks``
    Individual/line/prefab build, paint, damage, collapse, and map journaling.
``combat``
    Shooting, melee, damage, death, projectiles, and explosions.
``equipment``
    Class/loadout selection and deployable authorization/lifecycles.
``teams``
    Team changes, spawn selection, and round lifecycle requests.
``social_admin``
    Chat, votes, authentication, and administrative commands.

Extraction rule: decoding stays in the protocol layer; a domain handler only
validates the authenticated sender, invokes one authoritative operation, and
requests replication. Do not move a handler without characterization tests for
its accepted packet and rejection paths. Importing this package intentionally
has no registration side effects until the migration begins.
"""
