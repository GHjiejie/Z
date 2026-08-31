"""Strict rule syntax and firing/recovery checks with a verified promtool binary."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--promtool', required=True, type=Path)
    arguments = parser.parse_args()
    tool = arguments.promtool.resolve(strict=True)
    version = subprocess.run([str(tool), '--version'], check=True, capture_output=True, text=True, timeout=10)
    if 'promtool, version 3.13.2 ' not in version.stdout + version.stderr:
        raise RuntimeError('Use the verified Prometheus 3.13.2 acceptance tool')
    root = Path(__file__).resolve().parents[1] / 'deploy' / 'monitoring'
    subprocess.run([str(tool), 'check', 'rules', 'alerts.yaml'], cwd=root, check=True, timeout=20)
    subprocess.run([str(tool), 'test', 'rules', 'alerts.test.yaml'], cwd=root, check=True, timeout=30)


if __name__ == '__main__':
    main()
