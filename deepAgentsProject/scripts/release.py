"""Reproducible delivery checks. This command never pushes images or deploys."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("api", "worker", "migrate", "sandbox-service")
TRIVY = "aquasec/trivy@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
EXPORTS = {
    "requirements.prod.lock": ["--no-default-groups"],
    "requirements.txt": ["--no-default-groups", "--extra", "dev"],
    "requirements.tools.lock": ["--only-group", "delivery"],
    "requirements.build.lock": ["--only-group", "build"],
}


def run(command, *, capture=False, timeout=1800):
    return subprocess.run(command, cwd=ROOT, text=True, check=True, capture_output=capture, timeout=timeout)


def normalized_requirements(text):
    return "\n".join(line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))


def verify_locks():
    uv = [sys.executable, "-m", "uv", "--no-config", "--offline"]
    run([*uv, "lock", "--check", "--python", sys.executable, "--no-python-downloads"], timeout=60)
    for name, options in EXPORTS.items():
        expected = run([*uv, "export", "--locked", "--no-emit-project", "--format", "requirements.txt", *options], capture=True, timeout=60).stdout
        if normalized_requirements(expected) != normalized_requirements((ROOT / name).read_text()):
            raise RuntimeError(f"{name} differs from uv.lock; regenerate all exports before release")


def image_tag(target, revision):
    if target not in (*TARGETS, "acceptance") or not re.fullmatch(r"(?:[a-f0-9]{40}|uncommitted)", revision):
        raise ValueError("Build requires a supported target and a full commit SHA or uncommitted")
    return f"deepagent-{target}:{revision}"


def require_docker():
    try:
        run(["docker", "info", "--format", "{{.ServerVersion}}"], capture=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("A working Docker daemon is required; skipped acceptance is not a release pass") from error


def build_images(revision):
    require_docker()
    for target in (*TARGETS, "acceptance"):
        tag = image_tag(target, revision)
        run(["docker", "build", "--pull", "--file", "docker/platform/Dockerfile", "--target", target,
             "--build-arg", f"SOURCE_REVISION={revision}", "--tag", tag, "."])
    run(["docker", "run", "--rm", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
         "--network=none", "--pids-limit=128", "--memory=2g", "--cpus=2",
         "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700",
         image_tag("acceptance", revision)], timeout=180)
    print("Native image acceptance passed; deployment and end-to-end dependency acceptance are separate gates.")


def scan_images(revision, output):
    require_docker()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Scan the immutable Git source, never ignored .env files or runtime data.
    # git archive includes committed secrets if present, and the scanner rejects
    # them. Require an exact clean revision instead of mislabelling dirty builds.
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Scanning release evidence requires a full source commit SHA')
    if run(['git', 'rev-parse', 'HEAD'], capture=True).stdout.strip() != revision:
        raise ValueError('Release revision must match HEAD')
    if run(['git', 'status', '--porcelain', '--', '.'], capture=True).stdout.strip():
        raise ValueError('Release source must be committed and clean before scanning')
    import tempfile
    import tarfile
    import io
    prefix = run(['git', 'rev-parse', '--show-prefix'], capture=True).stdout.strip()
    archive = subprocess.run(['git', 'archive', revision + (':' + prefix.rstrip('/') if prefix else '')],
        cwd=ROOT, check=True, capture_output=True, timeout=60).stdout
    with tempfile.TemporaryDirectory(prefix='deepagent-source-scan-') as source:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(source, filter='data')
        scan_source(Path(source))
    metadata = {}
    for target in TARGETS:
        tag = image_tag(target, revision)
        archive = output / f"{target}.tar"
        if archive.exists():
            raise RuntimeError("Refusing to overwrite an existing image archive")
        run(["docker", "save", "--output", str(archive), tag])
        base = ["docker", "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                "--mount", f"type=bind,src={output},dst=/scan", TRIVY, "image", "--input", f"/scan/{target}.tar"]
        run([*base, "--severity", "HIGH,CRITICAL", "--exit-code", "1", "--format", "json", "--output", f"/scan/{target}.vulnerabilities.json"])
        run([*base, "--format", "cyclonedx", "--output", f"/scan/{target}.sbom.json"])
        inspection = run(["docker", "image", "inspect", tag], capture=True).stdout
        row = json.loads(inspection)[0]
        if row.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != revision:
            raise ValueError('Image source label does not match the inspected release')
        metadata[target] = {"image_id": row["Id"], "source_revision": revision, "repo_digests": row.get("RepoDigests", [])}
    # Local image IDs are not registry digests or signed provenance. Promotion
    # must bind the published registry digest to trusted CI evidence separately.
    with (output / "images.json").open("x") as stream:
        json.dump(metadata, stream, sort_keys=True, indent=2)


def scan_source(source):
    run(["docker", "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
         "--mount", f"type=bind,src={source},dst=/src,readonly", TRIVY, "fs", "--scanners", "vuln,secret",
         "--severity", "HIGH,CRITICAL", "--exit-code", "1", "/src"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["locks", "images", "scan", "config"])
    parser.add_argument("--kind", choices=['platform', 'migration', 'sandbox'])
    parser.add_argument("--repository-trust", choices=['https', 'ssh', 'both'])
    parser.add_argument("--revision", default="uncommitted")
    parser.add_argument("--output", type=Path, default=ROOT / ".release")
    args = parser.parse_args()
    if args.operation == "locks":
        verify_locks()
    elif args.operation == "images":
        build_images(args.revision)
    elif args.operation == "scan":
        scan_images(args.revision, args.output)
    else:
        from deployment_checks import validate_deployment
        validate_deployment(args.kind, repository_trust=args.repository_trust)


if __name__ == "__main__":
    main()
