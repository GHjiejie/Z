"""Operator-approved, address-pinned Git transports; no ambient credentials.

Only clone may connect. All subsequent inspection/archive commands are offline.
Public DNS validation is repeated for every checkout, then the validated addresses
are passed to the transport itself, rather than asking Git to resolve them again.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psutil

from packages.coding.errors import RepositoryAccessError


def git_environment(directory: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath), "HOME": str(directory),
        "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_LITERAL_PATHSPECS": "1",
    }


def git_command(*arguments: str, allow_proxy: bool = False) -> list[str]:
    # -c overrides local repository settings too. GIT_ALLOW_PROTOCOL is the
    # independent allowlist that local protocol.*.allow cannot widen.
    return ["git", "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.fsmonitor=false", "-c", "credential.helper=",
            *([] if allow_proxy else ["-c", "http.proxy="]), "-c", "http.followRedirects=false",
            "-c", "http.sslVerify=true", *arguments]


def run_clone(command, env, *, timeout=180, output_limit=1024 * 1024):
    """Bound output/deadline; kill and reap the Git/SSH helper process group."""
    process = None
    try:
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                   cwd=env["HOME"])
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            size = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                for key, _ in selector.select(min(remaining, 0.1)):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(block)
                    if size > output_limit:
                        raise ValueError("Git output limit exceeded")
            if process.wait(timeout=max(0.001, deadline - time.monotonic())):
                raise RepositoryAccessError("Remote Git clone failed; verify the approved source and its trust configuration")
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
        raise RepositoryAccessError("Remote Git clone failed or exceeded its resource deadline") from exc
    finally:
        if process is not None:
            # Children can retain sockets even after the parent has exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            # SIGKILL delivery is asynchronous. A helper can briefly retain
            # sockets after its already-exited parent has been reaped. Retain
            # ownership until every member of our private group is terminated;
            # zombies hold no files/sockets and are reaped by their adopter.
            while True:
                running = False
                for member in psutil.process_iter():
                    try:
                        if (os.getpgid(member.pid) == process.pid
                                and member.status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}):
                            running = True
                    except (ProcessLookupError, PermissionError, psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                if not running:
                    break
                time.sleep(0.01)
            process.stdout.close()
            process.stderr.close()


def _uri(value: str) -> tuple[str, str, int, str]:
    try:
        if (not value or len(value) > 4096 or not value.isascii()
                or any(ord(char) <= 32 or ord(char) == 127 for char in value)
                or "\\" in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "ssh"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "%" in parsed.netloc
                or parsed.netloc.endswith(":")):
            raise ValueError
        host = parsed.hostname.rstrip(".").lower()
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            if (len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                         for label in host.split("."))):
                raise ValueError
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 22)
        if not 1 <= port <= 65535:
            raise ValueError
        authority = ("[" + host + "]") if ":" in host else host
        canonical = urlunsplit((parsed.scheme, f"{authority}:{port}", parsed.path or "/", "", ""))
        return parsed.scheme, host, port, canonical
    except ValueError as exc:
        raise RepositoryAccessError("Repository URI must be an absolute HTTPS/SSH URL without credentials, query or fragment") from exc


def resolve_addresses(host: str, port: int) -> list[str]:
    # libc resolution has no reliable in-process deadline. A separate, isolated
    # interpreter is killed/reaped on timeout; no lingering resolver threads.
    program = ("import json,socket,sys; print(json.dumps(sorted({r[4][0] for r in "
               "socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)})))")
    try:
        result = subprocess.run([sys.executable, "-I", "-c", program, host, str(port)],
                                capture_output=True, timeout=5, check=False,
                                env={"PATH": os.defpath, "LANG": "C.UTF-8"})
        if result.returncode or len(result.stdout) > 16384:
            raise ValueError
        addresses = json.loads(result.stdout)
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= 128:
            raise ValueError
        return addresses
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RepositoryAccessError("Remote repository DNS lookup failed or exceeded its deadline") from exc


def _public_address(value: str):
    try:
        if not isinstance(value, str) or "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
        # Do not allow IPv6 transition/translation addresses to tunnel into IPv4
        # ranges that would be rejected above (including NAT64, 6to4 and Teredo).
        if (not address.is_global or address.is_multicast or address.is_unspecified
                or (address.version == 6 and (address not in ipaddress.ip_network("2000::/3")
                    or address in ipaddress.ip_network("2002::/16")
                    or address in ipaddress.ip_network("2001::/32")))):
            raise ValueError
        return address
    except ValueError as exc:
        raise RepositoryAccessError("Remote repository host resolves to a non-public or translated network") from exc


@dataclass(frozen=True)
class RemoteTarget:
    scheme: str
    host: str
    port: int
    uri: str
    addresses: tuple[str, ...]


class RepositoryNetworkPolicy:
    def __init__(self, *, allowed_origins: str = "", production: bool = False,
                 ca_file: str = "", ssh_known_hosts_file: str = ""):
        self.production, self.ca_file = production, ca_file
        self.ssh_known_hosts_file = ssh_known_hosts_file
        self.origins = set()
        for origin in allowed_origins.split(","):
            if origin.strip():
                scheme, host, port, canonical = _uri(origin.strip())
                if urlsplit(canonical).path != "/":
                    raise RepositoryAccessError("Repository allowlist entries must be explicit origins, without repository paths")
                self.origins.add((scheme, host, port))

    @classmethod
    def from_environment(cls, *, production: bool | None = None):
        return cls(allowed_origins=os.getenv("DEEPAGENT_REPOSITORY_ALLOWED_ORIGINS", ""),
                   production=(os.getenv("DEEPAGENT_ENVIRONMENT", "development").strip().lower() in {"prod", "production"}) if production is None else production,
                   ca_file=os.getenv("DEEPAGENT_REPOSITORY_CA_FILE", ""),
                   ssh_known_hosts_file=os.getenv("DEEPAGENT_REPOSITORY_SSH_KNOWN_HOSTS_FILE", ""))

    def target(self, value: str) -> RemoteTarget:
        scheme, host, port, canonical = _uri(value)
        if ((self.production and not self.origins)
                or (self.origins and (scheme, host, port) not in self.origins)):
            raise RepositoryAccessError("Remote repository origin is not operator-approved")
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise RepositoryAccessError("Remote repository host is not allowed")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            values = resolve_addresses(host, port)
        else:
            values = [str(literal)]
        addresses = sorted({_public_address(value) for value in values}, key=lambda item: (item.version, int(item)))
        if not addresses:
            raise RepositoryAccessError("Remote repository host has no approved addresses")
        if scheme == "ssh":
            self._trust_file(self.ssh_known_hosts_file, "SSH known-hosts")
        return RemoteTarget(scheme, host, port, canonical, tuple(map(str, addresses)))

    @staticmethod
    def _trust_file(value: str, label: str) -> str:
        try:
            source = Path(value)
            metadata = source.lstat()
            if (not value or not source.is_absolute() or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_mode & 0o022 or not 0 < metadata.st_size <= 1024 * 1024):
                raise ValueError
            return str(source)
        except (OSError, ValueError) as exc:
            raise RepositoryAccessError(f"An operator-provided, non-writable {label} file is required") from exc

    def clone_command(self, target: RemoteTarget, destination: Path, directory: Path, *, tunnel=None):
        env = git_environment(directory)
        env["GIT_ALLOW_PROTOCOL"] = target.scheme
        arguments = []
        if target.scheme == "https":
            if tunnel is None or tunnel.target != target:
                raise RepositoryAccessError("HTTPS clone requires its address-pinned origin gate")
            # Keep the ephemeral proxy credential out of command-line arguments.
            # The scoped proxy also rejects alternate-object URLs to other hosts.
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.proxy", GIT_CONFIG_VALUE_0=tunnel.url)
            env["NO_PROXY"] = ""  # Explicitly include IP-literal repository hosts.
            try:
                configured = subprocess.run(["git", "config", "--get", "http.proxy"],
                    env=env, cwd=directory, capture_output=True, timeout=5, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RepositoryAccessError("Cannot verify Git origin-gate configuration support") from exc
            if configured.returncode or configured.stdout.rstrip(b"\n") != tunnel.url.encode():
                raise RepositoryAccessError("Git must support the explicit origin-gate configuration")
            if self.ca_file:
                arguments += ["-c", "http.sslCAInfo=" + self._trust_file(self.ca_file, "repository CA")]
            arguments += ["-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=15",
                          "-c", "fetch.uriprotocols=", "-c", "protocol.version=0"]
        else:
            ssh = shutil.which("ssh")
            if not ssh:
                raise RepositoryAccessError("OpenSSH is required for SSH repositories")
            known_hosts = self._trust_file(self.ssh_known_hosts_file, "SSH known-hosts")
            options = {
                "HostName": target.addresses[0], "HostKeyAlias": target.host,
                "Port": str(target.port), "BatchMode": "yes", "IdentitiesOnly": "yes",
                "IdentityAgent": "none", "IdentityFile": "none",
                "UserKnownHostsFile": known_hosts, "GlobalKnownHostsFile": os.devnull,
                "StrictHostKeyChecking": "yes", "CheckHostIP": "no", "VerifyHostKeyDNS": "no",
                "ProxyCommand": "none", "ProxyJump": "none", "ForwardAgent": "no",
                "ClearAllForwardings": "yes", "PermitLocalCommand": "no",
                "CanonicalizeHostname": "no", "ControlMaster": "no", "ControlPath": "none",
                "KnownHostsCommand": "none", "UpdateHostKeys": "no",
                "ConnectTimeout": "10", "ConnectionAttempts": "1", "RequestTTY": "no",
                "PasswordAuthentication": "no", "KbdInteractiveAuthentication": "no",
                "GSSAPIAuthentication": "no", "HostbasedAuthentication": "no",
            }
            command = [ssh, "-F", os.devnull]
            for key, value in options.items():
                command += ["-o", f"{key}={value}"]
            env["GIT_SSH_COMMAND"] = shlex.join(command)
            env["GIT_SSH_VARIANT"] = "ssh"
        # Full objects, no filters, templates, checkout hooks or submodule fetch.
        # The resulting repository is inspected offline and never executes content.
        arguments += ["clone", "--no-checkout", "--no-tags", "--template=", "--",
                      target.uri, str(destination)]
        return git_command(*arguments, allow_proxy=target.scheme == "https"), env
