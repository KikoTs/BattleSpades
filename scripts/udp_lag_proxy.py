"""Local UDP impairment proxy for ENet clients.

Forwards datagrams between a client and a server while adding a fixed
one-way delay, random jitter and random loss in each direction, so the
retail client can be measured at internet-like ping without leaving the
machine.  One upstream socket is created per client address, so several
clients keep separate ENet peers on the server.

Example (client connects to 127.0.0.1:27018, server on 27016, ~80 ms RTT):

    py -3 scripts/udp_lag_proxy.py --listen 27018 --target 127.0.0.1:27016 --delay-ms 40 --jitter-ms 8 --loss 0.005
"""
from __future__ import annotations

import argparse
from collections import Counter
import heapq
import random
import select
import socket
import sys
import threading
import time


class Impairment:
    def __init__(self, delay_ms: float, jitter_ms: float, loss: float, seed: int) -> None:
        self.delay = delay_ms / 1000.0
        self.jitter = jitter_ms / 1000.0
        self.loss = loss
        self.rng = random.Random(seed)

    def schedule(self, now: float) -> float | None:
        if self.loss > 0.0 and self.rng.random() < self.loss:
            return None
        extra = self.rng.uniform(-self.jitter, self.jitter) if self.jitter > 0 else 0.0
        return now + max(0.0, self.delay + extra)


class Proxy:
    def __init__(self, listen: int, target: tuple[str, int], down: Impairment, up: Impairment) -> None:
        self.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listen_sock.bind(("127.0.0.1", listen))
        self.listen_sock.setblocking(False)
        self.target = target
        self.down = down  # server -> client
        self.up = up      # client -> server
        self.upstreams: dict[tuple[str, int], socket.socket] = {}
        self.by_upstream: dict[socket.socket, tuple[str, int]] = {}
        self.queue: list[tuple[float, int, socket.socket, bytes, tuple[str, int]]] = []
        self.seq = 0
        self.stats = {"up": 0, "down": 0, "dropped": 0}
        self.lock = threading.Lock()
        self.sniff = None

    def upstream_for(self, client: tuple[str, int]) -> socket.socket:
        sock = self.upstreams.get(client)
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sock.setblocking(False)
            self.upstreams[client] = sock
            self.by_upstream[sock] = client
            print(f"[proxy] new client {client} -> upstream port {sock.getsockname()[1]}", flush=True)
        return sock

    def enqueue(self, imp: Impairment, sock: socket.socket, data: bytes, dest: tuple[str, int]) -> None:
        at = imp.schedule(time.monotonic())
        if at is None:
            self.stats["dropped"] += 1
            return
        self.seq += 1
        heapq.heappush(self.queue, (at, self.seq, sock, data, dest))

    def run(self) -> None:
        last_report = time.monotonic()
        while True:
            timeout = 0.05
            if self.queue:
                timeout = max(0.0, min(timeout, self.queue[0][0] - time.monotonic()))
            readers = [self.listen_sock] + list(self.by_upstream)
            ready, _, _ = select.select(readers, [], [], timeout)
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(65535)
                except OSError:
                    continue
                if sock is self.listen_sock:
                    up = self.upstream_for(addr)
                    self.stats["up"] += 1
                    if self.sniff is not None:
                        self.sniff[0](data, self.sniff[1])
                    self.enqueue(self.up, up, data, self.target)
                else:
                    client = self.by_upstream[sock]
                    self.stats["down"] += 1
                    if self.sniff is not None:
                        self.sniff[0](data, self.sniff[2])
                    self.enqueue(self.down, self.listen_sock, data, client)
            now = time.monotonic()
            while self.queue and self.queue[0][0] <= now:
                _, _, sock, data, dest = heapq.heappop(self.queue)
                try:
                    sock.sendto(data, dest)
                except OSError:
                    pass
            if now - last_report >= 10.0:
                last_report = now
                print(f"[proxy] up={self.stats['up']} down={self.stats['down']} dropped={self.stats['dropped']} queued={len(self.queue)}", flush=True)
                if self.sniff is not None:
                    print("[sniff] client->server:", dict(self.sniff[1].most_common(12)), flush=True)
                    print("[sniff] server->client:", dict(self.sniff[2].most_common(12)), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--target", required=True, help="host:port of the real server")
    parser.add_argument("--delay-ms", type=float, default=40.0, help="one-way delay per direction")
    parser.add_argument("--jitter-ms", type=float, default=0.0, help="uniform +/- jitter per datagram")
    parser.add_argument("--loss", type=float, default=0.0, help="per-datagram drop probability")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--sniff", action="store_true", help="histogram ENet command types per direction")
    args = parser.parse_args(argv)
    host, port = args.target.rsplit(":", 1)
    down = Impairment(args.delay_ms, args.jitter_ms, args.loss, args.seed)
    up = Impairment(args.delay_ms, args.jitter_ms, args.loss, args.seed + 1)
    proxy = Proxy(args.listen, (host, int(port)), down, up)
    if args.sniff:
        from enet_sniff import classify
        proxy.sniff = (classify, Counter(), Counter())
    print(f"[proxy] listening on 127.0.0.1:{args.listen} -> {host}:{port} delay={args.delay_ms}ms jitter={args.jitter_ms}ms loss={args.loss}", flush=True)
    try:
        proxy.run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
