"""Bounded, nonblocking controller for the portable retail relay (Python 2/3).

Only the parent launches the helper. No TCP control port is exposed to the LAN.
Game datagrams never pass through Python. Text fields use UTF-8 hex on the wire.
"""
from __future__ import print_function

import binascii
import collections
import errno
import json
import os
import socket
import subprocess
import sys
import time

CLOCK = getattr(time, 'monotonic', None) or (time.clock if sys.platform == 'win32' else time.time)
MAX_BUFFER = 262144
WOULD_BLOCK = (errno.EAGAIN, errno.EWOULDBLOCK, 10035)


def encode(value):
    if not isinstance(value, bytes):
        value = value.encode('utf-8')
    return binascii.hexlify(value).decode('ascii')


def decode(value, limit=160):
    if len(value) > limit * 2 or len(value) % 2:
        raise ValueError('Invalid relay text size')
    text = binascii.unhexlify(value.encode('ascii')).decode('utf-8', 'strict')
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError('Invalid relay text')
    return text


def integer(value, minimum=0, maximum=4294967295):
    if not value or len(value) > 20 or any(c not in '0123456789' for c in value):
        raise ValueError('Invalid relay number')
    result = int(value)
    if result < minimum or result > maximum:
        raise ValueError('Relay number outside range')
    return result


def steam_id(value):
    result = integer(value, 1, 18446744073709551615)
    # Public-universe individual accounts, instance 1 (desktop).
    if result >> 56 != 1 or ((result >> 52) & 15) != 1 or not (result & 0xffffffff):
        raise ValueError('Invalid Steam user identity')
    return str(result)


def instance_id(value):
    if len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid server session')
    return value


def local_host_path(steam, virtual_port):
    root = os.environ.get('LOCALAPPDATA')
    if not root:
        raise ValueError('LOCALAPPDATA is unavailable')
    return os.path.join(root, 'AoSRetailFixes', 'host-%s-%d.json' %
                        (steam_id(steam), integer(str(virtual_port), 0, 999)))


def local_host_port(row):
    """Only a fresh, local host record may bypass a self-P2P connection."""
    try:
        path = local_host_path(row['steam'], row['virtual_port'])
        if abs(time.time() - os.path.getmtime(path)) > 20:
            return None
        with open(path, 'rb') as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            return None
        value = json.loads(data.decode('ascii'))
        if value['instance'] != row['instance']:
            return None
        return integer(str(value['port']), 1, 65535)
    except (IOError, OSError, ValueError, KeyError, TypeError):
        return None


def parse_row(fields):
    """Validate hostile lobby metadata before it reaches retail's UI."""
    if len(fields) != 15 or fields[0] != 'ROW':
        raise ValueError('Invalid server row')
    maximum = integer(fields[9], 1, 255)
    ping = -1 if fields[14].startswith('-') else integer(fields[14], 0, 999999)
    result = dict(request=integer(fields[1]), lobby=str(integer(fields[2], 1, 18446744073709551615)),
                  steam=steam_id(fields[3]), virtual_port=integer(fields[4], 0, 999),
                  instance=instance_id(fields[5]), name=decode(fields[6], 96),
                  map=decode(fields[7], 96), mode=decode(fields[8], 16),
                  maximum=maximum, players=integer(fields[10], 0, maximum),
                  password=bool(integer(fields[11], 0, 1)), classic=integer(fields[12], 0, 1),
                  skin=decode(fields[13], 32), ping=ping)
    if not result['name'] or not result['map'] or not result['mode']:
        raise ValueError('Incomplete server row')
    result['key'] = 'steam:%s:%d' % (result['steam'], result['virtual_port'])
    return result


class BridgeClient(object):
    """One helper per owning process; poll() never waits for I/O or Steam."""

    def __init__(self, executable, log=None, clock=CLOCK):
        self.executable = os.path.abspath(executable)
        self.log = log or (lambda message: None)
        self.clock = clock
        self.process = None
        self.listener = None
        self.connection = None
        self.authenticated = False
        self.ready = False
        self.steam = None
        self.error = None
        self.input = b''
        self.output = b''
        self.token = ''
        self.started = 0
        self.accepted = 0
        self.pending = collections.deque()

    def start(self):
        if self.process is not None:
            return
        if not os.path.isfile(self.executable):
            raise IOError('Portable Steam helper is missing: ' + self.executable)
        if not os.path.isfile(os.path.join(os.path.dirname(self.executable), 'steam_api64.dll')):
            raise IOError('steam_api64.dll is missing beside the portable Steam helper')
        self.error = None
        self.steam = None
        self.token = binascii.hexlify(os.urandom(32)).decode('ascii')
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(4)
            listener.setblocking(False)
            env = os.environ.copy()
            env['AOS_RELAY_CONTROL_PORT'] = str(listener.getsockname()[1])
            env['AOS_RELAY_CONTROL_TOKEN'] = str(self.token)
            env['SteamAppId'] = env['SteamGameId'] = '224540'
            # Frozen Python/Steam may alter the DLL search path. The helper's
            # runtime is next to its executable, isolated from retail's DLL.
            with open(os.devnull, 'wb') as null:
                self.process = subprocess.Popen([self.executable], cwd=os.path.dirname(self.executable),
                    env=env, stdin=subprocess.PIPE, stdout=null, stderr=null,
                    creationflags=0x08000000 if sys.platform == 'win32' else 0)
            self.process.stdin.close()
            self.listener = listener
            self.started = self.clock()
        except Exception:
            listener.close()
            raise

    def send(self, *fields):
        values = [str(value) for value in fields]
        if any(any(c in value for c in '\r\n\t') for value in values):
            raise ValueError('Invalid control field')
        message = ('\t'.join(values) + '\n').encode('ascii')
        if len(message) > 8192 or len(self.output) + len(message) > MAX_BUFFER:
            raise ValueError('Relay command queue full')
        self.output += message

    def _fail(self, message):
        self.error = message
        self.log('Steam relay: ' + message)
        self.close()
        return [('ERROR', '0', encode(message))]

    def poll(self):
        if self.process is None:
            return []
        try:
            if self.connection is not None and not self.authenticated and self.clock() - self.accepted > 2:
                self.connection.close()
                self.connection = None
                self.input = b''
            if self.connection is None:
                try:
                    connection, address = self.listener.accept()
                except socket.error as error:
                    if error.errno not in WOULD_BLOCK:
                        raise
                else:
                    if address[0] != '127.0.0.1':
                        connection.close()
                    else:
                        connection.setblocking(False)
                        self.connection = connection
                        self.accepted = self.clock()
            result = []
            if self.connection is not None:
                for unused in range(16):
                    try:
                        data = self.connection.recv(4096)
                    except socket.error as error:
                        if error.errno in WOULD_BLOCK:
                            break
                        raise
                    if not data:
                        # Parse complete buffered errors before reporting EOF.
                        if not self.input:
                            return self._fail('Relay helper closed its connection')
                        break
                    self.input += data
                    if len(self.input) > MAX_BUFFER:
                        return self._fail('Relay response too large')
                for unused in range(128):
                    if b'\n' not in self.input:
                        break
                    line, self.input = self.input.split(b'\n', 1)
                    if len(line) > 8192:
                        return self._fail('Relay response line too large')
                    fields = line.decode('ascii', 'strict').split('\t')
                    if len(fields) > 24:
                        return self._fail('Invalid relay response')
                    if not self.authenticated:
                        if fields != ['HELLO', '1', self.token]:
                            self.connection.close()
                            self.connection = None
                            self.input = b''
                            break
                        self.authenticated = True
                        self.listener.close()
                        self.listener = None
                        continue
                    if fields[0] == 'READY' and len(fields) == 2:
                        self.steam = steam_id(fields[1])
                        self.ready = True
                    result.append(fields)
                if self.authenticated and self.output:
                    try:
                        sent = self.connection.send(self.output)
                        self.output = self.output[sent:]
                    except socket.error as error:
                        if error.errno not in WOULD_BLOCK:
                            raise
            if not self.ready and self.clock() - self.started > 25:
                return result + self._fail('Steam startup timed out')
            if self.process.poll() is not None:
                for fields in result:
                    if len(fields) == 3 and fields[:2] == ['ERROR', '0']:
                        self.error = decode(fields[2], 1024)
                        self.close()
                        return result
                return result + self._fail('Relay helper exited (code %s)' % self.process.returncode)
            return result
        except (socket.error, ValueError, UnicodeError) as error:
            return self._fail(str(error))

    def close(self):
        """Close IPC first, then terminate only our child. Never block the UI."""
        self.ready = False
        self.authenticated = False
        for sock in (self.connection, self.listener):
            if sock is not None:
                sock.close()
        self.connection = self.listener = None
        if self.process is not None:
            if self.process.poll() is None:
                # IPC closure allows Steam to leave its lobby. The server can
                # reap asynchronously; retail never waits here for a process.
                self.pending.append((self.process, self.clock()))
            self.process = None
        self.input = self.output = b''

    def reap(self, force=False):
        for unused in range(len(self.pending)):
            process, when = self.pending.popleft()
            if process.poll() is not None:
                continue
            if force or self.clock() - when > 2:
                try:
                    process.terminate()
                except OSError:
                    pass
                if not force:
                    self.pending.append((process, when))
            else:
                self.pending.append((process, when))
