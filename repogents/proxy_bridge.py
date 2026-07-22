from __future__ import annotations

import os
import select
import signal
import socket
import socketserver
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


def main(arguments: Sequence[str] | None = None) -> int:
    args = list(arguments if arguments is not None else sys.argv[1:])
    if len(args) < 3 or args[1] != "--":
        print("usage: proxy_bridge.py UNIX_SOCKET -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    unix_socket = args[0]
    command = args[2:]
    server = BridgeServer(("127.0.0.1", 0), BridgeHandler)
    server.unix_socket = unix_socket  # type: ignore[attr-defined]
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy = f"http://127.0.0.1:{port}"
    environment = dict(os.environ)
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
    process = subprocess.Popen(command, env=environment)

    def forward(signum: int, frame: object) -> None:
        del frame
        if process.poll() is None:
            process.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        return process.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
