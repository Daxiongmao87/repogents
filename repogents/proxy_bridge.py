from __future__ import annotations

import ipaddress
import json
import os
import select
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
from collections.abc import Sequence


class BridgeServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(self.server.unix_socket)  # type: ignore[attr-defined]
            relay(self.request, upstream)
        finally:
            upstream.close()


class TransparentBridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(server.unix_socket)  # type: ignore[attr-defined]
            host: str = server.route_host  # type: ignore[attr-defined]
            port: int = server.route_port  # type: ignore[attr-defined]
            authority = f"{host}:{port}"
            upstream.sendall(
                (
                    f"CONNECT {authority} HTTP/1.1\r\n"
                    f"Host: {authority}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            pending = read_connect_response(upstream)
            if pending:
                self.request.sendall(pending)
            relay(self.request, upstream)
        except (ConnectionError, OSError, ValueError):
            return
        finally:
            upstream.close()


class DatagramBridgeServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
    allow_reuse_address = True
    daemon_threads = True


class DnsDatagramHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        query, server_socket = self.request
        response = dns_response(
            query,
            self.server.host_addresses,  # type: ignore[attr-defined]
        )
        if response is not None:
            server_socket.sendto(response, self.client_address)


class DnsStreamHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        length_bytes = receive_exact(self.request, 2)
        if length_bytes is None:
            return
        length = struct.unpack("!H", length_bytes)[0]
        query = receive_exact(self.request, length)
        if query is None:
            return
        response = dns_response(
            query,
            self.server.host_addresses,  # type: ignore[attr-defined]
        )
        if response is not None:
            self.request.sendall(struct.pack("!H", len(response)) + response)


def relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, exceptional = select.select(sockets, [], sockets, 60)
        if exceptional or not readable:
            return
        for source in readable:
            destination = right if source is left else left
            data = source.recv(65536)
            if not data:
                return
            destination.sendall(data)


def read_connect_response(upstream: socket.socket) -> bytes:
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = upstream.recv(8192)
        if not chunk:
            raise ConnectionError("restricted proxy closed the tunnel")
        response.extend(chunk)
        if len(response) > 65536:
            raise ConnectionError("restricted proxy response is too large")
    header, _, pending = bytes(response).partition(b"\r\n\r\n")
    status = header.split(b"\r\n", 1)[0].split(b" ", 2)
    if len(status) < 2 or status[1] != b"200":
        raise ConnectionError("restricted proxy denied the tunnel")
    return pending


def read_routes(path: str | None) -> list[tuple[str, str, int]]:
    if path is None:
        return []
    with open(path, encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict) or set(value) != {"routes"}:
        raise ValueError("dependency route configuration must contain routes")
    raw_routes = value["routes"]
    if not isinstance(raw_routes, list):
        raise ValueError("dependency routes must be a list")
    network = ipaddress.IPv4Network("127.64.0.0/10")
    seen: set[tuple[str, int]] = set()
    routes: list[tuple[str, str, int]] = []
    for item in raw_routes:
        if not isinstance(item, dict) or set(item) != {"address", "host", "port"}:
            raise ValueError("dependency route is malformed")
        address = item["address"]
        host = item["host"]
        port = item["port"]
        if (
            not isinstance(address, str)
            or not isinstance(host, str)
            or not host
            or not host.isascii()
            or any(not (character.isalnum() or character in ".-") for character in host)
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not (1 <= port <= 65535)
        ):
            raise ValueError("dependency route contains an invalid endpoint")
        try:
            parsed_address = ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError as error:
            raise ValueError("dependency route address is invalid") from error
        if parsed_address not in network:
            raise ValueError("dependency route address is outside the isolated range")
        endpoint = (address, port)
        if endpoint in seen:
            raise ValueError("dependency route endpoint is duplicated")
        seen.add(endpoint)
        routes.append((address, host, port))
    return routes


def child_command(command: Sequence[str], drop_capabilities: bool) -> list[str]:
    if not drop_capabilities:
        return list(command)
    return [
        "/usr/bin/setpriv",
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        "--",
        *command,
    ]


def receive_exact(connection: socket.socket, length: int) -> bytes | None:
    value = bytearray()
    while len(value) < length:
        chunk = connection.recv(length - len(value))
        if not chunk:
            return None
        value.extend(chunk)
    return bytes(value)


def dns_response(
    query: bytes,
    host_addresses: dict[str, str],
) -> bytes | None:
    if len(query) < 12:
        return None
    transaction, flags, questions, _, _, _ = struct.unpack("!HHHHHH", query[:12])
    if flags & 0x8000 or flags & 0x7800 or questions != 1:
        return None
    offset = 12
    labels: list[str] = []
    while True:
        if offset >= len(query):
            return None
        label_length = query[offset]
        offset += 1
        if label_length == 0:
            break
        if label_length > 63 or offset + label_length > len(query):
            return None
        try:
            labels.append(query[offset : offset + label_length].decode("ascii"))
        except UnicodeDecodeError:
            return None
        offset += label_length
    if offset + 4 > len(query):
        return None
    query_type, query_class = struct.unpack("!HH", query[offset : offset + 4])
    question_end = offset + 4
    host = ".".join(labels).lower()
    address = host_addresses.get(host)
    response_flags = 0x8400 | (flags & 0x0100)
    answer_count = 0
    answer = b""
    if address is None:
        response_flags |= 3
    elif query_type == 1 and query_class == 1:
        answer_count = 1
        answer = (
            b"\xc0\x0c"
            + struct.pack("!HHIH", 1, 1, 0, 4)
            + socket.inet_aton(address)
        )
    header = struct.pack(
        "!HHHHHH",
        transaction,
        response_flags,
        1,
        answer_count,
        0,
        0,
    )
    return header + query[12:question_end] + answer


def main(arguments: Sequence[str] | None = None) -> int:
    args = list(arguments if arguments is not None else sys.argv[1:])
    try:
        separator = args.index("--")
    except ValueError:
        separator = -1
    if separator not in {1, 2} or len(args) <= separator + 1:
        print(
            "usage: proxy_bridge.py UNIX_SOCKET [ROUTES_JSON] -- COMMAND [ARG ...]",
            file=sys.stderr,
        )
        return 2
    unix_socket = args[0]
    route_path = args[1] if separator == 2 else None
    command = args[separator + 1 :]
    servers: list[socketserver.BaseServer] = []
    threads: list[threading.Thread] = []
    try:
        routes = read_routes(route_path)
        host_addresses: dict[str, str] = {}
        for address, host, _port in routes:
            existing = host_addresses.setdefault(host, address)
            if existing != address:
                raise ValueError("dependency host has inconsistent route addresses")

        proxy_server = BridgeServer(("127.0.0.1", 0), BridgeHandler)
        proxy_server.unix_socket = unix_socket  # type: ignore[attr-defined]
        servers.append(proxy_server)
        if host_addresses:
            datagram_server = DatagramBridgeServer(
                ("127.63.0.1", 53),
                DnsDatagramHandler,
            )
            datagram_server.host_addresses = host_addresses  # type: ignore[attr-defined]
            servers.append(datagram_server)
            stream_server = BridgeServer(("127.63.0.1", 53), DnsStreamHandler)
            stream_server.host_addresses = host_addresses  # type: ignore[attr-defined]
            servers.append(stream_server)
        for address, host, port in routes:
            route_server = BridgeServer((address, port), TransparentBridgeHandler)
            route_server.unix_socket = unix_socket  # type: ignore[attr-defined]
            route_server.route_host = host  # type: ignore[attr-defined]
            route_server.route_port = port  # type: ignore[attr-defined]
            servers.append(route_server)
    except (OSError, ValueError) as error:
        for server in servers:
            server.server_close()
        print(f"cannot configure restricted dependency routes: {error}", file=sys.stderr)
        return 2

    for server in servers:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    port = proxy_server.server_address[1]
    proxy = f"http://127.0.0.1:{port}"
    environment = dict(os.environ)
    drop_capabilities = (
        environment.pop("REPOGENTS_SANDBOX_CAPABILITY_DROP", None) == "1"
    )
    environment.update(
        {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "ALL_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "all_proxy": proxy,
            "NO_PROXY": "localhost,127.0.0.1,::1,[::1]",
            "no_proxy": "localhost,127.0.0.1,::1,[::1]",
        }
    )
    process = subprocess.Popen(
        child_command(command, drop_capabilities),
        env=environment,
    )

    def forward(signum: int, frame: object) -> None:
        del frame
        if process.poll() is None:
            process.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        return process.wait()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
