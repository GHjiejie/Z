from __future__ import annotations

import os
import shlex
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from packages.coding.errors import RepositoryAccessError
from packages.coding.models import RepositoryCreate, RepositorySnapshotCreate
from packages.domain.models import TenantContext
from packages.persistence import Database
from packages.repositories import network
from packages.repositories.network import RemoteTarget, RepositoryNetworkPolicy, git_command, git_environment, run_clone
from packages.repositories.service import RepositoryService
from packages.repositories.tunnel import RepositoryTunnel


@pytest.mark.parametrize("uri", [
    "http://repository.example/r", "git://repository.example/r", "file:///tmp/r", "ext::command",
    "https://user:password@repository.example/r", "ssh://git@repository.example/r",
    "https://repository.example/r?token=secret", "https://repository.example/r#fragment",
    "https://repository.example:0/r", "https://repository.example:65536/r", "https://repository.example:/r",
    "https://repository.example/%0a/r?x=y", "https://repository.example\n/r", "https://repository.example\\@127.0.0.1/r",
    "https://%31%32%37.0.0.1/r", "https://[fe80::1%25en0]/r", "https://-host.example/r",
])
def test_malformed_or_credential_bearing_origins_never_resolve(monkeypatch, uri):
    monkeypatch.setattr(network, "resolve_addresses", lambda *a: pytest.fail("No DNS for invalid URI"))
    with pytest.raises(RepositoryAccessError):
        RepositoryNetworkPolicy().target(uri)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
    "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1", "2002:7f00:1::1",
    "2001::1", "fe80::1%en0",
])
def test_every_resolved_address_must_be_public(monkeypatch, address):
    monkeypatch.setattr(network, "resolve_addresses", lambda *a: ["93.184.216.34", address])
    with pytest.raises(RepositoryAccessError, match="non-public"):
        RepositoryNetworkPolicy().target("https://repository.example/repo.git")


def test_origins_are_operator_owned_exact_and_required_in_production(monkeypatch):
    monkeypatch.setattr(network, "resolve_addresses", lambda *a: ["2606:4700:4700::1111", "93.184.216.34"])
    with pytest.raises(RepositoryAccessError, match="operator-approved"):
        RepositoryNetworkPolicy(production=True).target("https://repository.example/r")
    policy = RepositoryNetworkPolicy(allowed_origins="https://repository.example", production=True)
    target = policy.target("https://REPOSITORY.example./r")
    assert target.host == "repository.example"
    assert target.addresses == ("93.184.216.34", "2606:4700:4700::1111")
    for uri in ("https://other.example/r", "https://repository.example:444/r", "ssh://repository.example/r"):
        with pytest.raises(RepositoryAccessError, match="operator-approved"):
            policy.target(uri)
    with pytest.raises(RepositoryAccessError, match="without repository paths"):
        RepositoryNetworkPolicy(allowed_origins="https://repository.example/repo.git")


def test_checkout_revalidates_dns_and_cleans_up_failed_enter(tmp_path, monkeypatch):
    policy = RepositoryNetworkPolicy()
    service = RepositoryService(None, tmp_path, [], network_policy=policy)
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    monkeypatch.setattr(network, "resolve_addresses", lambda *a: next(answers))
    uri = "https://repository.example/repo.git"
    service._validate_remote_uri(uri)
    checkout = service._repository_checkout({"provider": "generic_git", "canonical_uri": uri})
    monkeypatch.setattr("packages.repositories.service.run_clone", lambda *a: pytest.fail("No clone after failed revalidation"))
    with pytest.raises(RepositoryAccessError, match="non-public"):
        with checkout:
            pytest.fail("Unsafe checkout must never become visible")
    assert checkout.temporary and not Path(checkout.temporary.name).exists()


def test_dns_resolution_is_a_bounded_reaped_subprocess(monkeypatch):
    def timeout(command, **kwargs):
        assert command[:3] == [sys.executable, "-I", "-c"]
        assert kwargs["timeout"] == 5
        assert "HOME" not in kwargs["env"]
        raise subprocess.TimeoutExpired(command, 5)
    monkeypatch.setattr(network.subprocess, "run", timeout)
    with pytest.raises(RepositoryAccessError, match="DNS lookup failed"):
        network.resolve_addresses("repository.example", 443)


def test_ssh_pins_ip_and_host_key_without_loading_user_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "resolve_addresses", lambda *a: ["93.184.216.34"])
    monkeypatch.setattr(network.shutil, "which", lambda _: "/usr/bin/ssh")
    with pytest.raises(RepositoryAccessError, match="known-hosts"):
        RepositoryNetworkPolicy().target("ssh://repository.example/repo.git")
    trust = tmp_path / "known_hosts"
    trust.write_text("repository.example ssh-ed25519 fixture-public-key\n")
    trust.chmod(0o600)
    policy = RepositoryNetworkPolicy(ssh_known_hosts_file=str(trust))
    target = policy.target("ssh://repository.example:2222/repo.git")
    command, env = policy.clone_command(target, tmp_path / "checkout", tmp_path)
    ssh = shlex.split(env["GIT_SSH_COMMAND"])
    assert "HostName=93.184.216.34" in ssh and "HostKeyAlias=repository.example" in ssh
    assert "Port=2222" in ssh and "StrictHostKeyChecking=yes" in ssh
    assert "IdentityAgent=none" in ssh and "IdentityFile=none" in ssh
    assert "ProxyCommand=none" in ssh and "GlobalKnownHostsFile=" + os.devnull in ssh
    assert env["GIT_ALLOW_PROTOCOL"] == "ssh"
    assert "--no-checkout" in command and "--filter=blob:none" not in command
    trust.chmod(0o666)
    with pytest.raises(RepositoryAccessError, match="known-hosts"):
        policy.clone_command(target, tmp_path / "checkout", tmp_path)


def test_git_environment_drops_ambient_secrets_and_network_overrides(tmp_path, monkeypatch):
    for name in ("SSH_AUTH_SOCK", "GIT_CONFIG_PARAMETERS", "GIT_SSH_COMMAND", "GIT_SSL_NO_VERIFY",
                 "HTTPS_PROXY", "ALL_PROXY", "GIT_ASKPASS", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "should-not-be-inherited")
    env = git_environment(tmp_path)
    assert "should-not-be-inherited" not in env.values()
    assert env["GIT_ALLOW_PROTOCOL"] == "" and env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    command = git_command("status")
    assert "core.fsmonitor=false" in command and "core.hooksPath=" + os.devnull in command


def test_git_that_ignores_the_proxy_configuration_cannot_start_clone(tmp_path, monkeypatch):
    target = RemoteTarget("https", "repository.invalid", 443, "https://repository.invalid/r", ("93.184.216.34",))
    gate = SimpleNamespace(target=target, url="http://synthetic:test@127.0.0.1:12345")
    monkeypatch.setattr(network.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout=b""))
    with pytest.raises(RepositoryAccessError, match="must support"):
        RepositoryNetworkPolicy().clone_command(target, tmp_path / "checkout", tmp_path, tunnel=gate)


@pytest.fixture
def origin(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    work = tmp_path / "source"
    work.mkdir()
    env = git_environment(tmp_path)
    env.update(GIT_ALLOW_PROTOCOL="file", GIT_AUTHOR_NAME="fixture", GIT_COMMITTER_NAME="fixture",
               GIT_AUTHOR_EMAIL="fixture@example.invalid", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    def git(*args):
        return subprocess.run(git_command(*map(str, args)), env=env, check=True, capture_output=True)
    git("init", "--initial-branch=main", work)
    (work / "README.md").write_text("verified remote source\n")
    git("-C", work, "add", "README.md")
    git("-C", work, "commit", "-m", "fixture")
    git("clone", "--bare", work, root / "repo.git")
    git("-C", root / "repo.git", "update-server-info")
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Repository test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
          .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "repository.invalid")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("repository.invalid")]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_file, cert_file, key_file = (tmp_path / name for name in ("ca.pem", "server.pem", "server.key"))
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.chmod(0o600)
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw): super().__init__(*a, directory=str(root), **kw)
        def log_message(self, *a): pass
        def do_GET(self):
            self.server.requests.append(self.path)
            if self.server.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "https://other.invalid:443/stolen")
                self.end_headers()
            elif self.server.mode == "alternate" and "/objects/" in self.path:
                if self.path.endswith("/info/http-alternates"):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"https://other.invalid:443/repo.git/objects\n")
                else:
                    self.send_error(404)
            else:
                super().do_GET()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests, server.mode = [], "normal"
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_file, key_file)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    target = RemoteTarget("https", "repository.invalid", server.server_port,
                          f"https://repository.invalid:{server.server_port}/repo.git", ("127.0.0.1",))
    try:
        yield SimpleNamespace(server=server, target=target, ca_file=ca_file, root=root, work=work, git=git)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_https_git_clone_archive_and_probe_without_local_root_access(origin, tmp_path, monkeypatch):
    db = Database(":memory:")
    db.initialize()
    policy = RepositoryNetworkPolicy(ca_file=str(origin.ca_file))
    # Only this local TLS fixture bypasses public-address validation. Production
    # target construction is exercised separately, with private IPs rejected.
    monkeypatch.setattr(policy, "target", lambda uri: origin.target)
    service = RepositoryService(db, tmp_path / "snapshots", [], network_policy=policy)
    context = TenantContext(tenant_id="repository-test", project_id="repository-test")
    try:
        repository = service.create_repository(RepositoryCreate(name="HTTPS fixture", provider="generic_git",
                                               canonical_uri=origin.target.uri), context)
        snapshot = service.create_snapshot(repository["id"], RepositorySnapshotCreate(), context)
        assert snapshot["file_count"] == 1
        assert snapshot["source_mode"] == "committed_ref"
        assert service.probe(repository["id"], context)["version_controlled"] is True
        assert service.allowed_local_roots == []
        assert any("/objects/" in path for path in origin.server.requests)
        with pytest.raises(RepositoryAccessError, match="outside configured roots"):
            service._resolve_local_path(str(origin.work))
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["redirect", "alternate", "wrong_host", "untrusted_ca"])
def test_real_git_rejects_redirects_alternate_hosts_and_invalid_tls(origin, tmp_path, mode):
    from dataclasses import replace
    target = origin.target
    if mode in {"redirect", "alternate"}:
        origin.server.mode = mode
    if mode == "wrong_host":
        target = replace(target, host="wrong.invalid", uri=target.uri.replace("repository.invalid", "wrong.invalid"))
    policy = RepositoryNetworkPolicy(ca_file="" if mode == "untrusted_ca" else str(origin.ca_file))
    with RepositoryTunnel(target) as tunnel:
        command, env = policy.clone_command(target, tmp_path / "destination", tmp_path, tunnel=tunnel)
        if mode == "alternate":
            # Git also rejects HTTP alternates when followRedirects=false. Relax
            # that CLIENT setting only here to prove the independent gate still
            # rejects a real cross-origin alternate request at connection time.
            position = command.index("clone")
            command[position:position] = ["-c", "http.followRedirects=true"]
        with pytest.raises(RepositoryAccessError, match="Remote Git clone failed"):
            run_clone(command, env, timeout=10)
    if mode == "alternate":
        assert any(path.endswith("/info/http-alternates") for path in origin.server.requests)
        assert tunnel.rejected_origins > 0, "A real alternate-origin CONNECT must reach and be rejected by the gate"
    if mode == "redirect":
        assert len(origin.server.requests) == 1
    assert not tunnel.sockets and not tunnel.threads


@pytest.mark.parametrize("authority,authorized", [("other.invalid:443", True), ("repository.invalid:443", False)])
def test_gate_rejects_other_origins_and_missing_credentials_before_any_connect(authority, authorized, monkeypatch):
    target = RemoteTarget("https", "repository.invalid", 443, "https://repository.invalid/repo.git", ("93.184.216.34",))
    with RepositoryTunnel(target) as tunnel:
        monkeypatch.setattr(tunnel, "_connect", lambda: pytest.fail("No upstream connection is permitted"))
        client = socket.create_connection(tunnel.server.server_address, timeout=2)
        with client:
            authorization = f"Proxy-Authorization: {tunnel.authorization}\r\n" if authorized else ""
            client.sendall(f"CONNECT {authority} HTTP/1.1\r\n{authorization}\r\n".encode())
            response = client.recv(4096)
            assert response == b"" or response.startswith(b"HTTP/1.1 407")


@pytest.mark.parametrize("script", ["import time; time.sleep(30)", "import sys; sys.stdout.write('x'*2000000)"])
def test_clone_runner_bounds_deadline_and_output(script, tmp_path):
    started = time.monotonic()
    with pytest.raises(RepositoryAccessError):
        run_clone([sys.executable, "-I", "-c", script], git_environment(tmp_path), timeout=0.3, output_limit=65536)
    assert time.monotonic() - started < 3


def test_offline_archive_ignores_ambient_git_configuration(origin, tmp_path, monkeypatch):
    config = tmp_path / "untrusted.gitconfig"
    config.write_text('[core]\n\tfsmonitor = /not/an/approved/program\n[http]\n\tsslVerify = false\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    service = RepositoryService(None, tmp_path / "archives", [origin.work])
    commit = service._git(origin.work, "rev-parse", "HEAD").strip()
    archive, manifest = service._archive_committed(origin.work, commit)
    assert archive and len(manifest) == 1
    _, manifest = service._archive_working_tree(origin.work)
    assert len(manifest) == 1


def test_linked_worktree_cannot_read_git_metadata_outside_approved_roots(origin, tmp_path):
    linked = tmp_path / "linked"
    origin.git("-C", origin.work, "worktree", "add", "--detach", linked)
    service = RepositoryService(None, tmp_path / "archives", [linked])
    with pytest.raises(RepositoryAccessError, match="metadata or shared object"):
        service._git_metadata(linked)
    with pytest.raises(RepositoryAccessError, match="metadata or shared object"):
        service._archive_committed(linked, "HEAD")
    service.allowed_local_roots.append(origin.work)
    assert service._git_metadata(linked)[0]
    assert service._archive_committed(linked, "HEAD")[1]


def test_local_git_alternates_cannot_import_unapproved_objects(origin, tmp_path):
    alternates = origin.work / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(tmp_path / "outside-objects") + "\n")
    service = RepositoryService(None, tmp_path / "archives", [origin.work])
    with pytest.raises(RepositoryAccessError, match="metadata or shared object"):
        service._git_metadata(origin.work)


def test_tunnel_exit_drains_an_idle_authenticated_connection(monkeypatch):
    target = RemoteTarget("https", "repository.invalid", 443, "https://repository.invalid/r", ("93.184.216.34",))
    tunnel = RepositoryTunnel(target)
    upstream, remote = socket.socketpair()
    def connect():
        tunnel.remember(upstream)
        return upstream
    monkeypatch.setattr(tunnel, "_connect", connect)
    try:
        with tunnel:
            client = socket.create_connection(tunnel.server.server_address, timeout=2)
            client.sendall(f"CONNECT repository.invalid:443 HTTP/1.1\r\nProxy-Authorization: {tunnel.authorization}\r\n\r\n".encode())
            assert client.recv(4096).startswith(b"HTTP/1.1 200")
            assert tunnel.threads
        assert not tunnel.threads and not tunnel.sockets
        assert client.recv(1) == b""
    finally:
        remote.close()
        client.close()


def test_clone_deadline_terminates_a_child_that_outlives_git(tmp_path):
    port_file = tmp_path / "child-port"
    child = ("import socket,time; from pathlib import Path; s=socket.socket(); "
             "s.bind(('127.0.0.1',0)); s.listen(); "
             f"Path({str(port_file)!r}).write_text(str(s.getsockname()[1])); time.sleep(30)")
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-I','-c',{child!r}])"
    with pytest.raises(RepositoryAccessError, match="deadline"):
        run_clone([sys.executable, "-I", "-c", parent], git_environment(tmp_path), timeout=2)
    port = int(port_file.read_text())
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pytest.fail("Orphaned helper still owns a listening socket")


def test_clone_failure_never_reflects_remote_stderr(tmp_path):
    with pytest.raises(RepositoryAccessError) as error:
        run_clone([sys.executable, "-I", "-c", "import sys; sys.stderr.write('sensitive-remote-diagnostic'); sys.exit(1)"],
                  git_environment(tmp_path))
    assert "sensitive-remote-diagnostic" not in str(error.value)
