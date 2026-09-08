"""Strict CI groups; missing real dependencies are failures, not green skips."""
import argparse
import os

from packages.operations.release_checks import NoSkippedAcceptance


DOCKER_TESTS = (
    'test_docker_sandbox_is_non_root_secret_free_offline_and_bounded',
    'test_docker_tmpfs_workspace_is_capacity_limited_and_survives_interrupt',
    'test_real_docker_paired_restore_preserves_files_scratch_and_git_baseline',
    'test_cancelling_run_terminates_real_docker_command_and_preserves_partial_state',
    'test_real_docker_agent_change_is_verified_and_does_not_touch_host_checkout',
)
SQLITE_ONLY_SKIPS = {
    'SKIP LOCKED requires PostgreSQL', 'Split-process workers require PostgreSQL', 'PostgreSQL role/view contract',
}


class IntegrationGate(NoSkippedAcceptance):
    def __init__(self, group):
        super().__init__()
        self.group = group
        self.selected = set()

    def pytest_collection_finish(self, session):
        self.selected = {item.originalname or item.name for item in session.items}
        if self.group == 'docker' and self.selected != set(DOCKER_TESTS):
            self.skipped = True

    def pytest_runtest_logreport(self, report):
        if report.skipped and self.group == 'platform' and '[sqlite]' in report.nodeid:
            if isinstance(report.longrepr, tuple) and report.longrepr[2].removeprefix('Skipped: ') in SQLITE_ONLY_SKIPS:
                return
        super().pytest_runtest_logreport(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('group', choices=['platform', 'docker'])
    args = parser.parse_args()
    if not os.getenv('DEEPAGENT_TEST_POSTGRES_URL'):
        raise RuntimeError('Strict integration requires a dedicated PostgreSQL test database')
    import pytest
    expression = ' or '.join(DOCKER_TESTS)
    if args.group == 'platform':
        expression = 'not (' + expression + ') and not real_linux'
    return int(pytest.main(['-q', '-p', 'no:cacheprovider', '-k', expression, '--tb=short'], plugins=[IntegrationGate(args.group)]))


if __name__ == '__main__':
    raise SystemExit(main())
