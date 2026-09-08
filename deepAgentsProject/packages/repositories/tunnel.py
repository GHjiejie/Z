"""A short-lived HTTPS CONNECT gate for one approved repository origin.

TLS remains end-to-end between Git and the repository. The gate only connects
numeric, previously validated addresses; every new CONNECT (including Git HTTP
alternates) must name the same origin. It never resolves a hostname or forwards
proxy credentials to the destination.
"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import secrets
import select
import socket
import socketserver
import threading
import time
from contextlib import AbstractContextManager


class RepositoryTunnel(AbstractContextManager):
    max_connections = 8
    max_transfer_bytes = 1024**3

    def __init__(self, target):
        self.target = target
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(self.max_connections)
        self.sockets = set()
        self.threads = set()
        self.closed = False
        self.transferred = 0
        self.rejected_origins = 0
        password = secrets.token_hex(32)
        self.authorization = "Basic " + base64.b64encode(("repository:" + password).encode()).decode()
        self.server = _Server(("127.0.0.1", 0), self)
        self.url = f"http://repository:{password}@127.0.0.1:{self.server.server_address[1]}"
        self.listener = threading.Thread(target=self.server.serve_forever,
                                         kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.listener.start()
        return self

    def remember(self, sock):
        with self.lock:
            if self.closed:
                sock.close()
                raise OSError("Repository tunnel closed")
            self.sockets.add(sock)

    def forget(self, sock):
        if sock is not None:
            with self.lock:
                self.sockets.discard(sock)
            sock.close()

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.listener.join()
        with self.lock:
            self.closed = True
            sockets, threads = list(self.sockets), list(self.threads)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        # All operations are numeric-IP connects, bounded socket reads/writes or
        # select. Drain owned handlers; do not abandon network I/O on cancellation.
        for thread in threads:
            thread.join()

    def _connect(self):
        for value in self.target.addresses:
            address = ipaddress.ip_address(value)
            peer = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
            self.remember(peer)
            try:
                peer.settimeout(5)
                peer.connect((str(address), self.target.port))
                return peer
            except OSError:
                self.forget(peer)
                if self.closed:
                    break
        raise OSError("Approved repository addresses are unavailable")

    def _request(self, client):
        data = bytearray()
        deadline = time.monotonic() + 5
        while b"\r\n\r\n" not in data:
            client.settimeout(max(0.001, deadline - time.monotonic()))
            block = client.recv(4096)
            if not block or len(data) + len(block) > 16384 or time.monotonic() > deadline:
                raise ValueError
            data.extend(block)
        header, extra = bytes(data).split(b"\r\n\r\n", 1)
        lines = header.decode("ascii").split("\r\n")
        method, authority, version = lines[0].split(" ")
        host = f"[{self.target.host}]" if ":" in self.target.host else self.target.host
        if authority.lower() != f"{host}:{self.target.port}":
            with self.lock:
                self.rejected_origins += 1
            raise ValueError
        if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"} or extra:
            raise ValueError
        headers = {}
        for line in lines[1:]:
            key, value = line.split(":", 1)
            key = key.lower()
            if key in headers or key.strip() != key:
                raise ValueError
            headers[key] = value.strip()
        if not hmac.compare_digest(headers.get("proxy-authorization", ""), self.authorization):
            client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=repository\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return False
        if "transfer-encoding" in headers or headers.get("content-length", "0") != "0":
            raise ValueError
        return True

    def handle(self, client):
        peer = None
        try:
            if not self._request(client):
                return
            peer = self._connect()
            client.settimeout(5)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            idle_deadline = time.monotonic() + 30
            while not self.closed and time.monotonic() < idle_deadline:
                ready, _, _ = select.select([client, peer], [], [], 0.2)
                for source in ready:
                    block = source.recv(65536)
                    if not block:
                        return
                    with self.lock:
                        self.transferred += len(block)
                        if self.transferred > self.max_transfer_bytes:
                            return
                    destination = peer if source is client else client
                    destination.sendall(block)
                    idle_deadline = time.monotonic() + 30
        except (OSError, ValueError, UnicodeError):
            # Never reflect request URLs, headers, TLS data or Git stderr.
            pass
        finally:
            self.forget(peer)
            self.forget(client)


class _Server(socketserver.TCPServer):
    allow_reuse_address = False

    def __init__(self, address, gate):
        self.gate = gate
        super().__init__(address, _Handler)

    def process_request(self, request, client_address):
        if not self.gate.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            self.gate.remember(request)
            thread = threading.Thread(target=self._run, args=(request, client_address), daemon=True)
            with self.gate.lock:
                self.gate.threads.add(thread)
            thread.start()
        except BaseException:
            self.gate.forget(request)
            self.gate.slots.release()
            raise

    def _run(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        finally:
            self.shutdown_request(request)
            self.gate.slots.release()
            with self.gate.lock:
                self.gate.threads.discard(threading.current_thread())


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.gate.handle(self.request)
