"""Addresses, automatic port forwarding (UPnP IGD / NAT-PMP) and an honest
reachability check, all in the standard library.

Nothing here pretends to know more than it can measure. In particular there
is no free, trustworthy service that tests an arbitrary *UDP* port from the
internet, so ``assess()`` reports what was actually observed (local answer,
router mapping, carrier-grade NAT, hairpin answer) and says when the final
answer needs a friend outside the network.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Iterable

SSDP_ADDRESS = ("239.255.255.250", 1900)
SSDP_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
MAPPING_DESCRIPTION = "BattleSpades Server"
NATPMP_PORT = 5351
NATPMP_LIFETIME = 7200
PUBLIC_IP_SERVICES = (
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://checkip.amazonaws.com",
)
MAX_XML_BYTES = 256 * 1024
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

A2S_INFO = b"\xff\xff\xff\xffTSource Engine Query\x00"

Opener = Callable[..., object]


# --------------------------------------------------------------------------
# Addresses


def local_ip(target: str = "8.8.8.8") -> str | None:
    """The LAN address this computer uses to reach ``target`` (no packet sent)."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 80))
        address = sock.getsockname()[0]
        return None if address.startswith("0.") else address
    except OSError:
        return None
    finally:
        sock.close()


def public_ip(timeout: float = 4.0, opener: Opener = urllib.request.urlopen) -> str | None:
    """This network's public IPv4 address, from a plain-text HTTPS echo."""

    for url in PUBLIC_IP_SERVICES:
        try:
            with opener(urllib.request.Request(url, headers={"User-Agent": "BattleSpades"}), timeout=timeout) as response:
                text = response.read(64).decode("ascii", "replace").strip()
            address = ipaddress.ip_address(text)
            if address.version == 4 and address.is_global:
                return str(address)
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def classify(address: str | None) -> str:
    """public / private / cgnat / unknown."""

    if not address:
        return "unknown"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "unknown"
    if ip.version == 4 and ip in _CGNAT:
        return "cgnat"
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return "private"
    return "public" if ip.is_global else "unknown"


def default_gateway() -> str | None:
    """Best-effort IPv4 default gateway (used for NAT-PMP)."""

    try:
        if sys.platform.startswith("linux"):
            with open("/proc/net/route", encoding="ascii") as routes:
                for line in routes.readlines()[1:]:
                    fields = line.split()
                    if len(fields) > 2 and fields[1] == "00000000" and int(fields[3], 16) & 2:
                        return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
            return None
        if sys.platform == "darwin":
            output = subprocess.run(["route", "-n", "get", "default"], capture_output=True,
                                    text=True, timeout=3).stdout
            match = re.search(r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", output)
            return match.group(1) if match else None
        if sys.platform == "win32":
            output = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True, text=True,
                                    timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in output.splitlines():
                fields = line.split()
                if len(fields) >= 3 and fields[0] == "0.0.0.0" and fields[1] == "0.0.0.0":
                    try:
                        ipaddress.ip_address(fields[2])
                        return fields[2]
                    except ValueError:
                        continue
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


# --------------------------------------------------------------------------
# UPnP Internet Gateway Device


class UPnPError(RuntimeError):
    pass


@dataclass
class Gateway:
    location: str
    control_url: str
    service_type: str

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.location).hostname or ""


def ssdp_search(timeout: float = 2.5, *, sock_factory=socket.socket) -> list[str]:
    """Return the LOCATION URLs of gateways answering an SSDP M-SEARCH."""

    sock = sock_factory(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    locations: list[str] = []
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.4)
        for target in SSDP_TARGETS:
            message = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 2\r\n"
                f"ST: {target}\r\n\r\n"
            )
            sock.sendto(message.encode("ascii"), SSDP_ADDRESS)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _address = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            location = parse_ssdp_location(data)
            if location and location not in locations:
                locations.append(location)
    finally:
        sock.close()
    return locations


def parse_ssdp_location(data: bytes) -> str | None:
    for line in data.decode("latin-1", "replace").split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "location":
            value = value.strip()
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme == "http" and parsed.hostname:
                return value
    return None


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_device_description(xml_text: bytes | str, location: str) -> Gateway | None:
    """Find the WAN IP/PPP connection service in a device description."""

    root = ET.fromstring(xml_text)
    base = location
    for element in root.iter():
        if _strip_ns(element.tag) == "URLBase" and (element.text or "").strip():
            base = element.text.strip()
    services = []
    for element in root.iter():
        if _strip_ns(element.tag) != "service":
            continue
        fields = {_strip_ns(child.tag): (child.text or "").strip() for child in element}
        services.append(fields)
    for wanted in WAN_SERVICES:
        for fields in services:
            if fields.get("serviceType") == wanted and fields.get("controlURL"):
                return Gateway(location, urllib.parse.urljoin(base, fields["controlURL"]), wanted)
    return None


def soap_envelope(service: str, action: str, arguments: Iterable[tuple[str, object]]) -> bytes:
    from xml.sax.saxutils import escape

    body = "".join(f"<{name}>{escape(str(value))}</{name}>" for name, value in arguments)
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>'
    ).encode("utf-8")


def parse_soap_response(data: bytes) -> dict[str, str]:
    root = ET.fromstring(data)
    values: dict[str, str] = {}
    for element in root.iter():
        if len(element) == 0:
            values[_strip_ns(element.tag)] = (element.text or "").strip()
    return values


class UPnPClient:
    """Minimal IGD client: external address, add/remove UDP mappings."""

    def __init__(self, gateway: Gateway, opener: Opener = urllib.request.urlopen, timeout: float = 4.0) -> None:
        self.gateway = gateway
        self.opener = opener
        self.timeout = timeout

    @classmethod
    def discover(cls, timeout: float = 2.5, opener: Opener = urllib.request.urlopen,
                 search: Callable[..., list[str]] = ssdp_search) -> "UPnPClient":
        locations = search(timeout)
        if not locations:
            raise UPnPError("No UPnP router answered. UPnP may be turned off in the router settings.")
        problems = []
        for location in locations:
            try:
                with opener(location, timeout=4.0) as response:
                    data = response.read(MAX_XML_BYTES)
                gateway = parse_device_description(data, location)
                if gateway is not None:
                    return cls(gateway, opener)
            except (OSError, ET.ParseError, urllib.error.URLError) as exc:
                problems.append(str(exc))
        raise UPnPError("A UPnP device answered but offers no port-mapping service"
                        + (f" ({problems[0]})" if problems else "."))

    def call(self, action: str, arguments: Iterable[tuple[str, object]] = ()) -> dict[str, str]:
        request = urllib.request.Request(
            self.gateway.control_url,
            data=soap_envelope(self.gateway.service_type, action, arguments),
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{self.gateway.service_type}#{action}"',
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return parse_soap_response(response.read(MAX_XML_BYTES))
        except urllib.error.HTTPError as error:
            try:
                details = parse_soap_response(error.read(MAX_XML_BYTES))
            except Exception:
                details = {}
            code = details.get("errorCode", str(error.code))
            text = details.get("errorDescription", error.reason)
            raise UPnPError(f"router refused {action}: {code} {text}".strip()) from None
        except (OSError, ET.ParseError, urllib.error.URLError) as error:
            raise UPnPError(f"router did not answer {action}: {error}") from None

    def external_ip(self) -> str | None:
        return self.call("GetExternalIPAddress").get("NewExternalIPAddress") or None

    def add_mapping(self, port: int, internal_client: str, protocol: str = "UDP",
                    description: str = MAPPING_DESCRIPTION) -> None:
        arguments = [
            ("NewRemoteHost", ""), ("NewExternalPort", int(port)), ("NewProtocol", protocol),
            ("NewInternalPort", int(port)), ("NewInternalClient", internal_client),
            ("NewEnabled", 1), ("NewPortMappingDescription", description),
            ("NewLeaseDuration", 0),
        ]
        try:
            self.call("AddPortMapping", arguments)
        except UPnPError as error:
            if "725" not in str(error) and "OnlyPermanentLeases" not in str(error):
                # Some IGDv2 routers reject permanent leases; retry with a lease.
                arguments[-1] = ("NewLeaseDuration", 86400)
                try:
                    self.call("AddPortMapping", arguments)
                    return
                except UPnPError:
                    pass
            raise

    def delete_mapping(self, port: int, protocol: str = "UDP") -> None:
        self.call("DeletePortMapping", [("NewRemoteHost", ""), ("NewExternalPort", int(port)),
                                        ("NewProtocol", protocol)])


# --------------------------------------------------------------------------
# NAT-PMP (RFC 6886)


class NatPmpError(RuntimeError):
    pass


NATPMP_RESULTS = {
    1: "unsupported version", 2: "not authorized / refused", 3: "network failure",
    4: "out of resources", 5: "unsupported opcode",
}


def natpmp_request(port: int, lifetime: int = NATPMP_LIFETIME) -> bytes:
    """UDP mapping request: version 0, opcode 1."""

    return struct.pack("!BBHHHI", 0, 1, 0, int(port), int(port) if lifetime else 0, int(lifetime))


def natpmp_parse(data: bytes) -> tuple[int, int, int]:
    """Return (internal port, external port, lifetime) or raise."""

    if len(data) < 16:
        raise NatPmpError("short NAT-PMP answer")
    version, opcode, result, _epoch, internal, external, lifetime = struct.unpack("!BBHIHHI", data[:16])
    if version != 0 or opcode != 129:
        raise NatPmpError("unexpected NAT-PMP answer")
    if result:
        raise NatPmpError(f"router refused: {NATPMP_RESULTS.get(result, result)}")
    return internal, external, lifetime


def natpmp_map(gateway: str, port: int, lifetime: int = NATPMP_LIFETIME, *,
               sock_factory=socket.socket, attempts: int = 4) -> tuple[int, int]:
    """Map ``port`` (UDP) and return (external port, lifetime)."""

    sock = sock_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        delay = 0.25
        for _ in range(attempts):
            sock.settimeout(delay)
            sock.sendto(natpmp_request(port, lifetime), (gateway, NATPMP_PORT))
            try:
                data, _address = sock.recvfrom(64)
            except socket.timeout:
                delay *= 2
                continue
            _internal, external, granted = natpmp_parse(data)
            return external, granted
    except OSError as exc:
        raise NatPmpError(f"NAT-PMP failed: {exc}") from None
    finally:
        sock.close()
    raise NatPmpError("the router does not answer NAT-PMP")


# --------------------------------------------------------------------------
# High level: open / close all server ports


@dataclass
class ForwardResult:
    ok: bool
    method: str = ""                      # "UPnP" / "NAT-PMP"
    ports: list[int] = field(default_factory=list)
    external_ip: str | None = None
    message: str = ""


class PortForwarder:
    """Add mappings on start and remove exactly those on stop."""

    def __init__(self, *, upnp_factory: Callable[[], UPnPClient] = UPnPClient.discover,
                 gateway_finder: Callable[[], str | None] = default_gateway,
                 natpmp: Callable[..., tuple[int, int]] = natpmp_map,
                 lan_ip: Callable[[], str | None] = local_ip) -> None:
        self.upnp_factory = upnp_factory
        self.gateway_finder = gateway_finder
        self.natpmp = natpmp
        self.lan_ip = lan_ip
        self.client: UPnPClient | None = None
        self.natpmp_gateway: str | None = None
        self.mapped: list[int] = []
        self.method = ""
        self.renew_after = 0.0

    def open(self, ports: Iterable[int]) -> ForwardResult:
        ports = sorted({int(p) for p in ports if 1 <= int(p) <= 65535})
        problems = []
        try:
            client = self.upnp_factory()
            internal = self.lan_ip_for(client.gateway.host)
            if not internal:
                raise UPnPError("could not find this computer's LAN address")
            mapped = []
            for port in ports:
                client.add_mapping(port, internal)
                mapped.append(port)
            self.client, self.mapped, self.method = client, mapped, "UPnP"
            external = None
            try:
                external = client.external_ip()
            except UPnPError:
                pass
            return ForwardResult(True, "UPnP", mapped, external,
                                 f"Router forwarded UDP {', '.join(map(str, mapped))} to {internal}.")
        except UPnPError as exc:
            problems.append(f"UPnP: {exc}")
        gateway = self.gateway_finder()
        if gateway:
            try:
                mapped = []
                lifetime = NATPMP_LIFETIME
                for port in ports:
                    external, lifetime = self.natpmp(gateway, port)
                    if external != port:
                        problems.append(f"NAT-PMP mapped {port} to a different outside port ({external})")
                    mapped.append(port)
                self.natpmp_gateway, self.mapped, self.method = gateway, mapped, "NAT-PMP"
                self.renew_after = time.monotonic() + max(60, lifetime // 2)
                return ForwardResult(True, "NAT-PMP", mapped, None,
                                     f"Router forwarded UDP {', '.join(map(str, mapped))} (NAT-PMP).")
            except NatPmpError as exc:
                problems.append(f"NAT-PMP: {exc}")
        else:
            problems.append("NAT-PMP: default gateway not found")
        return ForwardResult(False, message="; ".join(problems))

    def lan_ip_for(self, host: str) -> str | None:
        if host:
            address = local_ip(host)
            if address:
                return address
        return self.lan_ip()

    def renew_due(self) -> bool:
        return self.method == "NAT-PMP" and bool(self.mapped) and time.monotonic() >= self.renew_after

    def renew(self) -> ForwardResult:
        if self.method != "NAT-PMP" or not self.natpmp_gateway:
            return ForwardResult(True, self.method, list(self.mapped))
        ports, self.mapped = list(self.mapped), []
        self.method = ""
        return self.open(ports)

    def close(self) -> ForwardResult:
        ports, method = list(self.mapped), self.method
        problems = []
        if method == "UPnP" and self.client is not None:
            for port in ports:
                try:
                    self.client.delete_mapping(port)
                except UPnPError as exc:
                    problems.append(str(exc))
        elif method == "NAT-PMP" and self.natpmp_gateway:
            for port in ports:
                try:
                    self.natpmp(self.natpmp_gateway, port, 0)
                except NatPmpError as exc:
                    problems.append(str(exc))
        self.client, self.natpmp_gateway, self.mapped, self.method = None, None, [], ""
        if problems:
            return ForwardResult(False, method, ports, message="; ".join(problems))
        return ForwardResult(True, method, ports, message="Port forwarding removed." if ports else "")


# --------------------------------------------------------------------------
# Reachability


def a2s_probe(host: str, port: int, timeout: float = 1.5, *, sock_factory=socket.socket) -> bool:
    """True when anything answers an A2S_INFO query at host:port (UDP)."""

    sock = sock_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(A2S_INFO, (host, int(port)))
        data, _address = sock.recvfrom(4096)
        return data.startswith(b"\xff\xff\xff\xff")
    except OSError:
        return False
    finally:
        sock.close()


@dataclass
class Finding:
    level: str      # ok / warn / bad / info
    text: str


def assess(*, port: int, server_running: bool, local_answer: bool | None,
           lan_ip: str | None, public: str | None, router_external: str | None,
           forwarding: ForwardResult | None, hairpin_answer: bool | None) -> list[Finding]:
    """Turn measurements into plain statements, never a guessed verdict."""

    findings: list[Finding] = []
    findings.append(Finding("info", f"This computer: {lan_ip or 'unknown'}  |  Public address: {public or 'unknown'}"))
    if not server_running:
        findings.append(Finding("warn", "Start the server first: the checks below need it answering."))
    elif local_answer:
        findings.append(Finding("ok", f"The server answers on UDP {port} on this computer."))
    else:
        findings.append(Finding("bad", f"The server does not answer on UDP {port} locally yet (still starting, or blocked)."))
    kind = classify(router_external)
    if router_external and public and router_external != public:
        if kind in ("cgnat", "private"):
            findings.append(Finding("bad", f"Your router's outside address ({router_external}) is not your public address: "
                                           "your provider uses carrier-grade NAT, so port forwarding cannot reach you. "
                                           "Use Steam P2P, or ask your provider for a public IPv4 address."))
        else:
            findings.append(Finding("warn", f"Router reports {router_external} but the internet sees {public}; "
                                            "there may be a second router in front of this one."))
    elif router_external and public:
        findings.append(Finding("ok", "Your router has the public address directly (no carrier-grade NAT)."))
    if forwarding is not None:
        if forwarding.ok:
            findings.append(Finding("ok", f"Automatic forwarding is active ({forwarding.method}: UDP {', '.join(map(str, forwarding.ports))})."))
        else:
            findings.append(Finding("warn", f"Automatic forwarding failed: {forwarding.message}"))
    if hairpin_answer:
        findings.append(Finding("ok", f"The server answered through your public address {public}:{port}. "
                                      "The router forwards the port; an outside firewall at your provider could still block it."))
    elif hairpin_answer is False and server_running and local_answer:
        findings.append(Finding("info", f"No answer through {public or 'the public address'}:{port}. Many routers cannot loop "
                                        "back to their own address, so this does not prove the port is closed."))
    findings.append(Finding("info", "Final test: ask a friend outside your network to join "
                                    f"{public or 'your public address'}:{port}. No website can reliably test a UDP game port."))
    return findings
