"""Retail-only Steam relay browser adapter. Compatible with Python 2.7.

The original ENet connection receives a temporary loopback endpoint only after
the native helper verifies the host greeting. No gameplay packet runs here.
"""
import atexit
import json
import os
import sys

from aosfix_runtime import method
from aos_steam_bridge import BridgeClient, CLOCK, decode, integer, parse_row, local_host_port


class Preferences(object):
    def __init__(self, path):
        self.path = path
        self.favourites = set()
        self.history = set()
        try:
            with open(path, 'rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                return
            values = json.loads(raw.decode('utf-8'))
            for key in ('favourites', 'history'):
                entries = values.get(key, [])
                if isinstance(entries, list):
                    setattr(self, key, set(v for v in entries[:500] if isinstance(v, type(u'')) and v.startswith('steam:')))
        except (IOError, OSError, ValueError, TypeError, AttributeError):
            pass

    def save(self):
        folder = os.path.dirname(self.path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        data = dict(favourites=sorted(self.favourites)[:500], history=sorted(self.history)[:500])
        # State is disposable; a partial file is ignored on the next launch.
        with open(self.path, 'wb') as stream:
            stream.write(json.dumps(data, ensure_ascii=True).encode('ascii'))


class RelaySession(object):
    def __init__(self, runtime, bridge, preferences):
        self.runtime = runtime
        self.bridge = bridge
        self.preferences = preferences
        self.serial = 0
        self.menu = None
        self.list_request = None
        self.join_request = None
        self.pending = None
        self.active_manager = None
        self.module = None
        self.last_error = None

    def request(self):
        self.serial += 1
        return str(self.serial)

    def start(self):
        if self.bridge.process is None:
            try:
                self.bridge.start()
            except (IOError, OSError, ValueError) as error:
                self.failure(str(error))
                return False
        return True

    def failure(self, message):
        self.last_error = message
        self.runtime.log('Retail relay: ' + message)
        if self.menu is not None:
            self.menu.status_text = 'Steam relay: ' + message
        self.pending = None

    def cancel(self):
        if self.join_request is not None and self.bridge.process is not None:
            self.bridge.send('CANCEL', self.join_request)
        self.join_request = None
        self.pending = None

    def refresh(self, menu):
        self.menu = menu
        self.cancel()
        self.list_request = None
        strings = self.module.strings
        if menu.network_type not in (strings.INTERNET_ALL, strings.INTERNET_USER, strings.FAVORITES, strings.HISTORY):
            return
        if self.start():
            self.list_request = self.request()
            self.bridge.send('LIST', self.list_request)

    def join(self, menu, server):
        if self.pending is not None:
            return
        self.cancel()
        if not self.start():
            return
        row = server._relay
        self.pending = (menu, server)
        # Same-machine hosting uses the local server, verified against its
        # freshly published instance. Another machine on the same Steam
        # account must not be mistaken for a local server.
        port = local_host_port(row)
        if port is not None:
            try:
                self._complete_join(port)
            except Exception as error:
                self.failure(str(error))
                self.active_manager = None
            return
        if self.bridge.steam == row['steam']:
            self.failure('Local host not found. Refresh, or use its LAN entry.')
            return
        self.join_request = self.request()
        menu.status_text = 'Connecting through Steam... (Back cancels)'
        self.bridge.send('JOIN', self.join_request, row['steam'], row['virtual_port'], row['instance'])

    def _complete_join(self, port):
        menu, server = self.pending
        self.pending = None
        self.active_manager = menu.manager
        from aoslib.scenes.ingame_menus.loadingMenu import LoadingMenu
        menu._relay_handoff = True
        try:
            menu.parent.set_menu(LoadingMenu, identifier='127.0.0.1:%d' % port,
                from_server_menu=True, expected_map=server.map, expected_mode=server.mode_id,
                expected_classic=server.classic, expected_skin=server.texture_skin,
                previous_menu=type(menu))
            self.preferences.history.add(server._relay['key'])
            try:
                self.preferences.save()
            except (IOError, OSError) as error:
                self.runtime.log('Relay history could not be saved: %s' % error)
        finally:
            menu._relay_handoff = False

    def _add_row(self, row):
        menu = self.menu
        strings = self.module.strings
        if menu.network_type == strings.FAVORITES and row['key'] not in self.preferences.favourites:
            return
        if menu.network_type == strings.HISTORY and row['key'] not in self.preferences.history:
            return
        # Native mode/skin assets must still exist; retain retail's own content
        # validation at Connect and let its handshake select the actual map.
        from aoslib.scenes.frontend.serverInfo import ServerInfo
        if row['skin'] and row['skin'] not in self.module.DLC_APPID_LIST:
            raise ValueError('Server requires an unknown texture skin')
        display_mode = ('c' if row['classic'] else '') + row['mode']
        if display_mode not in self.module.A2448:
            raise ValueError('Server advertises an unsupported retail mode')
        tags = ['v168', 'playlist=0']
        if row['classic']:
            tags.append('classic')
        if row['skin']:
            tags.append('skin=' + row['skin'])
        name = '[Steam] ' + row['name']
        server = ServerInfo(name, 2130706433, 1, 1, max(0, row['ping']), row['map'], row['mode'],
                            row['players'], row['maximum'], tags, 0)
        server._relay = row
        # Browser deduplication uses (ip, port). These strings never reach DNS,
        # the native query client, Steam favourites, or the gameplay dialler.
        server.ip = 'steam:' + row['steam']
        server.port = row['virtual_port']
        server.identifier = row['key']
        menu.on_server_response(server, row['ping'] / 1000.0 if row['ping'] >= 0 else -0.001)

    def poll(self, dt=0):
        self.bridge.reap()
        try:
            for fields in self.bridge.poll():
                kind = fields[0]
                if kind == 'ROW' and self.menu is not None and fields[1] == self.list_request:
                    try:
                        self._add_row(parse_row(fields))
                    except (ValueError, KeyError, TypeError, UnicodeError) as error:
                        self.runtime.log('Skipped incompatible relay advertisement: %s' % error)
                elif kind == 'DONE' and len(fields) == 2 and fields[1] == self.list_request and self.menu is not None:
                    if self.pending is None:
                        self.menu.status_text = self.module.strings.RECEIVED_N_SERVERS.format(len(self.menu.list_display.lines))
                elif kind == 'JOINED' and len(fields) == 3 and fields[1] == self.join_request and self.pending:
                    port = integer(fields[2], 1, 65535)
                    self._complete_join(port)
                elif kind == 'LEFT' and len(fields) == 3 and fields[1] == self.join_request:
                    self.cancel()
                    self.failure(decode(fields[2], 1024))
                    if self.active_manager is not None:
                        manager, self.active_manager = self.active_manager, None
                        manager.disconnect()
                        manager.set_main_menu()
                elif kind == 'ERROR' and len(fields) == 3:
                    if fields[1] in ('0', self.join_request, self.list_request):
                        self.failure(decode(fields[2], 1024))
                        if fields[1] == self.join_request:
                            self.cancel()
                        if fields[1] == '0':
                            self.cancel()
                            if self.active_manager is not None:
                                manager, self.active_manager = self.active_manager, None
                                manager.disconnect()
                                manager.set_main_menu()
        except Exception as error:
            # A bridge/UI failure must never escape pyglet's event loop.
            self.failure(str(error))
            self.cancel()

    def close(self):
        self.bridge.close()
        self.bridge.reap(force=True)


def _browser(runtime, session, module):
    session.module = module
    cls = module.ServerMenu
    names = ('on_start', 'on_stop', 'refresh', 'connect', 'favorite_pressed', 'update_favorite', 'finished_getting_servers_callback')
    old = dict((name, method(cls, name)) for name in names)

    def on_start(self, *args, **kwargs):
        session.active_manager = None
        session.cancel()
        session.menu = self
        return old['on_start'](self, *args, **kwargs)

    def on_stop(self, *args, **kwargs):
        if not getattr(self, '_relay_handoff', False):
            session.cancel()
        if session.menu is self:
            session.menu = None
            session.list_request = None
        return old['on_stop'](self, *args, **kwargs)

    def refresh(self, *args, **kwargs):
        waiting = self.manager.setting_favourites
        result = old['refresh'](self, *args, **kwargs)
        if not waiting:
            session.refresh(self)
        return result

    def connect(self, *args, **kwargs):
        line = self.list_display.get_selected()
        server = getattr(line, 'server', None)
        if getattr(server, '_relay', None) is None:
            session.cancel()
            return old['connect'](self, *args, **kwargs)
        valid, owns_content = self.valid_to_connect_to_selection(line)
        if valid and owns_content:
            session.join(self, server)

    def favorite_pressed(self, *args, **kwargs):
        line = self.list_display.get_selected()
        row = getattr(getattr(line, 'server', None), '_relay', None)
        if row is None:
            return old['favorite_pressed'](self, *args, **kwargs)
        favourites = session.preferences.favourites
        if row['key'] in favourites:
            favourites.remove(row['key'])
        else:
            favourites.add(row['key'])
        try:
            session.preferences.save()
        except (IOError, OSError) as error:
            runtime.log('Relay favourites could not be saved: %s' % error)
        self.update_favorite(line)

    def update_favorite(self, line):
        row = getattr(getattr(line, 'server', None), '_relay', None)
        if row is None:
            return old['update_favorite'](self, line)
        line.image = module.global_images.favorite_star if row['key'] in session.preferences.favourites else None

    def finished(self, *args, **kwargs):
        result = old['finished_getting_servers_callback'](self, *args, **kwargs)
        if session.pending and session.pending[0] is self:
            self.status_text = 'Connecting through Steam... (Back cancels)'
        return result

    replacements = dict(on_start=on_start, on_stop=on_stop, refresh=refresh, connect=connect,
                        favorite_pressed=favorite_pressed, update_favorite=update_favorite,
                        finished_getting_servers_callback=finished)
    runtime.replace('network.browser', [(cls, name, old[name], replacements[name]) for name in names])


def _loading(runtime, session, module):
    cls = module.LoadingMenu
    original = method(cls, 'on_navigation')

    def navigate(self, *args, **kwargs):
        session.cancel()
        session.active_manager = None
        return original(self, *args, **kwargs)

    runtime.replace('network.cancel_loading', [(cls, 'on_navigation', original, navigate)])


def _main_menu(runtime, session, module):
    cls = module.SelectMenu
    original = method(cls, 'on_start')

    def start(self, *args, **kwargs):
        session.cancel()
        session.active_manager = None
        return original(self, *args, **kwargs)

    runtime.replace('network.leave', [(cls, 'on_start', original, start)])


def _presence(runtime, session, module):
    """Keep the ephemeral loopback dial address out of Steam friend presence."""
    original = getattr(module, 'SteamSetRichPresenceServer', None)
    if not callable(original):
        return

    def presence(*args, **kwargs):
        if session.active_manager is not None:
            import shared.steam
            shared.steam.SteamClearRichPresence()
            return
        return original(*args, **kwargs)

    tag = 'network.presence.' + module.__name__
    runtime.replace(tag, [(module, 'SteamSetRichPresenceServer', original, presence)])


def install(runtime):
    root = os.path.dirname(os.path.abspath(sys.executable))
    bridge = BridgeClient(os.path.join(root, 'relay', 'aos-retail-relay.exe'), runtime.log)
    state = os.path.join(os.environ.get('LOCALAPPDATA', root), 'AoSRetailFixes', 'relay_servers.json')
    session = RelaySession(runtime, bridge, Preferences(state))
    runtime.relay_session = session
    import pyglet.clock
    pyglet.clock.schedule_interval(session.poll, 0.02)
    atexit.register(session.close)
    for name, tag, patch in [
        ('aoslib.scenes.frontend.serverMenu', 'network.browser', _browser),
        ('aoslib.scenes.ingame_menus.loadingMenu', 'network.cancel_loading', _loading),
        ('aoslib.scenes.frontend.selectMenu', 'network.leave', _main_menu),
        ('shared.steam', 'network.presence.shared', _presence),
        ('aoslib.gamemanager', 'network.presence.manager', _presence)]:
        runtime.watch(name, tag, lambda module, patch=patch: patch(runtime, session, module))
