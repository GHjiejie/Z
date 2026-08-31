"""Native acceptance inside the test-only image. Missing isolation is failure."""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import sys

from apps.production_entrypoint import verify_release_runtime


class NoSkippedAcceptance:
    def __init__(self):
        self.skipped = False

    def pytest_runtest_logreport(self, report):
        self.skipped |= report.skipped

    def pytest_collectreport(self, report):
        self.skipped |= report.skipped

    def pytest_sessionfinish(self, session, exitstatus):
        if self.skipped:
            session.exitstatus = 1


def main() -> int:
    verify_release_runtime("worker")
    from packages.knowledge.ingestion.isolated import IsolatedDocumentParser
    parser = IsolatedDocumentParser()
    asyncio.run(parser.validate_runtime())
    if not parser.require_os_sandbox or not parser.runtime_verified:
        raise RuntimeError("Native parser policy was not verified")
    # No model calls, production secrets or database are required for this gate.
    import pytest
    result = pytest.main(["-q", "-p", "no:cacheprovider", "tests/test_parser_os_sandbox.py",
                         "tests/test_repository_network.py", "-k", "real_linux or repository_network", "--tb=short"],
                         plugins=[NoSkippedAcceptance()])
    print(json.dumps({"native_acceptance": "passed" if result == 0 else "failed",
        "python": sys.version.split()[0],
        "packages": {name: importlib.metadata.version(name) for name in ("pypdf", "python-docx", "psutil")}}))
    return int(result)


if __name__ == "__main__":
    raise SystemExit(main())
