"""Python 2/3 regression gates for retail relay protocol and UI lifecycle."""
import os
import socket
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aos_steam_bridge as wire
import aos_networkfix as network
import aosfix_runtime

STEAM = '76561198000000001'
SESSION = 'a' * 32


class Box(object):
    def __init__(self, **values):
        self.__dict__.update(values)


def row(request='1'):
    return ['ROW', request, '109775240000000001', STEAM, '168', SESSION,
            wire.encode('My server'), wire.encode('arena'), wire.encode('tdm'),
            '24', '3', '0', '0', '', '80']


class WireTests(unittest.TestCase):
    def test_local_host_record_requires_matching_fresh_instance(self):
        import json
        root = tempfile.mkdtemp()
        previous = os.environ.get('LOCALAPPDATA')
        os.environ['LOCALAPPDATA'] = root
        try:
            parsed = wire.parse_row(row())
            path = wire.local_host_path(STEAM, 168)
            os.makedirs(os.path.dirname(path))
            with open(path, 'w') as stream:
                json.dump({'instance': SESSION, 'port': 28630}, stream)
            self.assertEqual(wire.local_host_port(parsed), 28630)
            parsed['instance'] = 'b' * 32
            self.assertIsNone(wire.local_host_port(parsed))
            parsed['instance'] = SESSION
            os.utime(path, (1, 1))
            self.assertIsNone(wire.local_host_port(parsed))
        finally:
            if previous is None: os.environ.pop('LOCALAPPDATA', None)
            else: os.environ['LOCALAPPDATA'] = previous
            shutil.rmtree(root)

    def test_row_preserves_identity_and_utf8(self):
        fields = row(); fields[6] = wire.encode(u'My \u0438\u0433\u0440\u0430')
        parsed = wire.parse_row(fields)
        self.assertEqual(parsed['steam'], STEAM)
        self.assertEqual(parsed['name'], u'My \u0438\u0433\u0440\u0430')
        self.assertEqual(parsed['key'], 'steam:%s:168' % STEAM)

    def test_missing_or_unbounded_metadata_rejected(self):
        for index, value in [(3, '480'), (4, '1000'), (5, 'stale'), (6, 'ff'),
                             (6, wire.encode('a\nserver')), (9, '0'), (10, '25'),
                             (11, '2'), (6, wire.encode('x' * 97)), (2, '18446744073709551616')]:
            fields = row(); fields[index] = value
            self.assertRaises((ValueError, UnicodeError), wire.parse_row, fields)
        self.assertRaises(ValueError, wire.parse_row, row()[:-1])

    def test_wire_fields_cannot_inject_commands(self):
        bridge = wire.BridgeClient('unused')
        for text in ('one\nSTOP', 'one\ttwo', 'one\r'):
            self.assertRaises(ValueError, bridge.send, 'JOIN', text)

    def test_uint64_identity_has_no_float_rounding(self):
        self.assertEqual(wire.steam_id(STEAM), STEAM)
        for value in ('-1', '1e5', ' 42', '18446744073709551616'):
            self.assertRaises(ValueError, wire.integer, value)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.bridge = wire.BridgeClient('unused')
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(1)
        self.remote = socket.socket(); self.remote.connect(listener.getsockname())
        local, _ = listener.accept(); local.setblocking(False)
        listener.setblocking(False)
        self.remote.settimeout(1)
        self.bridge.listener = listener
        self.bridge.connection = local
        self.bridge.token = 'a' * 64
        self.bridge.started = self.bridge.accepted = wire.CLOCK()
        self.bridge.process = Box(poll=lambda: None, terminate=lambda: None)

    def tearDown(self):
        self.remote.close(); self.bridge.close(); self.bridge.reap(force=True)

    def test_fragmented_authenticated_control_and_ready(self):
        self.remote.sendall(b'HELLO\t1\t' + b'a' * 64)
        self.assertEqual(self.bridge.poll(), [])
        self.remote.sendall(('\nREADY\t%s\nSTATUS\t100\t0\n' % STEAM).encode('ascii'))
        events = self.bridge.poll()
        self.assertTrue(self.bridge.ready)
        self.assertEqual(self.bridge.steam, STEAM)
        self.assertEqual([f[0] for f in events], ['READY', 'STATUS'])

    def test_untrusted_local_connection_cannot_send_ready(self):
        self.remote.sendall(('HELLO\t1\twrong\nREADY\t%s\n' % STEAM).encode('ascii'))
        self.assertEqual(self.bridge.poll(), [])
        self.assertFalse(self.bridge.ready)
        self.assertIsNone(self.bridge.connection)

    def test_commands_wait_for_authentication(self):
        self.bridge.send('LIST', '1')
        self.bridge.poll()
        self.assertEqual(self.bridge.output, b'LIST\t1\n')
        self.remote.sendall(b'HELLO\t1\t' + b'a' * 64 + b'\n')
        self.bridge.poll()
        self.assertEqual(self.remote.recv(1024), b'LIST\t1\n')

    def test_oversized_line_closes_helper_session(self):
        self.bridge.authenticated = True
        self.remote.sendall(b'x' * 8193 + b'\n')
        result = self.bridge.poll()
        self.assertEqual(result[0][0], 'ERROR')
        self.assertIsNone(self.bridge.process)

    def test_idle_impostor_does_not_occupy_listener(self):
        self.bridge.accepted = wire.CLOCK() - 3
        self.bridge.poll()
        self.assertIsNone(self.bridge.connection)


class FakeBridge(object):
    def __init__(self):
        self.process = object(); self.steam = '76561198000000002'
        self.commands = []; self.events = []
    def send(self, *fields): self.commands.append(fields)
    def poll(self):
        result, self.events = self.events, []
        return result
    def reap(self, **kwargs): pass
    def close(self): self.process = None


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.bridge = FakeBridge()
        self.preferences = Box(favourites=set(), history=set(), save=lambda: None)
        self.session = network.RelaySession(aosfix_runtime.Runtime(self.logs.append), self.bridge, self.preferences)
        self.menu = Box(status_text='', network_type='all')
        self.server = Box(_relay=wire.parse_row(row()))
        self.session.module = Box(strings=Box(INTERNET_ALL='all', INTERNET_USER='user', FAVORITES='fav', HISTORY='history'))

    def test_cancel_discards_late_join(self):
        self.session.join(self.menu, self.server)
        request = self.session.join_request
        self.session.cancel()
        self.bridge.events = [('JOINED', request, '40001')]
        self.session.poll()
        self.assertIsNone(self.session.active_manager)
        self.assertIsNone(self.session.pending)
        self.assertIn(('CANCEL', request), self.bridge.commands)

    def test_duplicate_click_opens_one_connection(self):
        self.session.join(self.menu, self.server)
        self.session.join(self.menu, self.server)
        self.assertEqual(len([c for c in self.bridge.commands if c[0] == 'JOIN']), 1)

    def test_refresh_generation_discards_old_rows(self):
        rows = []; self.session._add_row = rows.append
        self.session.refresh(self.menu)
        previous = self.session.list_request
        self.session.refresh(self.menu)
        current = self.session.list_request
        self.bridge.events = [row(previous), row(current)]
        self.session.poll()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['request'], int(current))

    def test_official_list_is_not_augmented(self):
        self.menu.network_type = 'official'
        self.session.refresh(self.menu)
        self.assertIsNone(self.session.list_request)

    def test_helper_failure_disconnects_game_once(self):
        calls = []
        self.session.active_manager = Box(disconnect=lambda: calls.append('disconnect'), set_main_menu=lambda: calls.append('menu'))
        self.bridge.events = [('ERROR','0',wire.encode('closed'))]
        self.session.poll(); self.session.poll()
        self.assertEqual(calls, ['disconnect', 'menu'])

    def test_own_steam_identity_gets_lan_instruction(self):
        self.bridge.steam = STEAM
        self.session.join(self.menu, self.server)
        self.assertFalse(self.bridge.commands)
        self.assertIn('LAN', self.session.last_error)

    def test_join_error_cancels_pending_native_connection(self):
        self.session.join(self.menu, self.server)
        request = self.session.join_request
        self.bridge.events = [('ERROR',request,wire.encode('stale session'))]
        self.session.poll()
        self.assertIsNone(self.session.join_request)
        self.assertIn(('CANCEL',request),self.bridge.commands)


@unittest.skipUnless(sys.version_info[0] == 2, 'Retail code objects require Python 2.7')
class RetailBytecodeTests(unittest.TestCase):
    def setUp(self):
        from retail_bytecode import RetailBundle
        path = r'C:/Program Files (x86)/Steam/steamapps/common/aceofspades/aos.pkg'
        if not os.path.isfile(path): self.skipTest('Supported retail bundle not installed')
        self.bundle = RetailBundle(path)

    def test_actual_endpoint_parser_keeps_dynamic_port(self):
        code = self.bundle.code('aoslib.tools','get_server_details')
        fn = types.FunctionType(code, {'__builtins__': __builtins__})
        self.assertEqual(fn('127.0.0.1:45678'), ('127.0.0.1',45678))

    def test_actual_password_prompt_is_available(self):
        code = self.bundle.code('aoslib.scenes.ingame_menus.loadingMenu','LoadingMenu','packet_received')
        scope = {'__builtins__':__builtins__}
        for name in ('PasswordNeeded','InitialInfo','UGCMapInfo','MapDataValidation','MapDataStart','MapSyncStart','MapSyncEnd'):
            scope[name] = Box(id=112 if name == 'PasswordNeeded' else -1)
        fn = types.FunctionType(code,scope)
        received = []
        menu = Box(password_box=Box(set=received.append,visible=False))
        fn(menu,Box(id=112),None)
        self.assertTrue(menu.password_box.visible)
        self.assertEqual(received,[''])

    def test_actual_server_rows_deduplicate_by_host_and_virtual_port(self):
        make = types.FunctionType(self.bundle.code('aoslib.tools','make_server_identifier'), {'__builtins__':__builtins__})
        info_scope = dict(make_server_identifier=make,A2450={'tdm':6},A2441=6,A2448={'tdm':'TDM'},
                          strings=Box(get_by_id=lambda value:value),A2361=0,A2362=1,game_version=lambda:168)
        info = type('ServerInfo',(object,),{'__init__':self.bundle.method('aoslib.scenes.frontend.serverInfo','ServerInfo','__init__',info_scope)})
        restored = {}
        names = ['aoslib','aoslib.scenes','aoslib.scenes.frontend','aoslib.scenes.frontend.serverInfo']
        try:
            for name in names:
                restored[name] = sys.modules.get(name)
                module = types.ModuleType(name); module.__path__ = []
                sys.modules[name] = module
            sys.modules[names[-1]].ServerInfo = info
            response = self.bundle.method('aoslib.scenes.frontend.serverMenu','ServerMenu','on_server_response')
            menu_class = type('Menu',(object,),{'on_server_response':response})
            menu = menu_class(); lines=[]
            def add_line(columns, server=None):
                line=Box(columns=columns,server=server); lines.append(line); return line
            menu.list_display=Box(lines=lines,add_line=add_line,selected_column=0)
            menu.update_favorite=lambda line:None
            menu.check_filter=lambda server:False
            menu.network_type='all'
            session=network.RelaySession(aosfix_runtime.Runtime(lambda text:None),FakeBridge(),Box(favourites=set(),history=set()))
            session.menu=menu
            session.module=Box(DLC_APPID_LIST={},A2448={'tdm':'TDM'},strings=Box(FAVORITES='fav',HISTORY='history'))
            first=wire.parse_row(row())
            session._add_row(first); session._add_row(first)
            second=wire.parse_row(row()); second['steam']='76561198000000002'; second['key']='steam:76561198000000002:168'
            session._add_row(second)
            self.assertEqual(len(lines),2)
            self.assertEqual(lines[0].columns[4],('80',80))
            self.assertEqual(lines[0].server.identifier,first['key'])
            self.assertEqual(lines[0].server.mode_id,6)
            bad=wire.parse_row(row()); bad['skin']='does-not-exist'
            self.assertRaises(ValueError,session._add_row,bad)
        finally:
            for name, value in restored.items():
                if value is None: sys.modules.pop(name,None)
                else: sys.modules[name]=value


if __name__ == '__main__':
    unittest.main()
