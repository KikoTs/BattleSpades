BattleSpades plugins
====================

Place trusted top-level `.py` files in this directory. Each file may define one
or more subclasses of `plugins.base_plugin.BasePlugin`; they are discovered at
server startup. Filenames beginning with `_` and files without a `.py` suffix
are ignored.

Rename `_example_plugin.py.disabled` to `example_plugin.py` to enable the sample.
Plugins run arbitrary Python code in the dedicated server process, so inspect
their source before enabling them.
