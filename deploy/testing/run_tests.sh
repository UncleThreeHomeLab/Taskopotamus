#!/bin/sh
set -eu
if [ "$(id -u)" = 0 ]; then
    setpriv --reuid=postgres --regid=postgres --init-groups sh "$0" "$@"
    # PostgreSQL cannot run as root; keep this list aligned with root-only security tests.
    PYTHONPATH=/app/tests python -m unittest \
        test_runtime_proxy.RuntimeProxyTests \
        test_workspace_lifecycle.WorkspaceLifecycleTests \
        test_runtime_sessions.RequestScopeTests.test_snapshot_is_immutable_and_unsafe_files_rejected \
        test_helm_chart.HelmChartTests.test_volume_initializers_preserve_contents_and_private_ownership
    exit
fi
export TEST_DATABASE_SOCKET="$(mktemp -d /tmp/taskopotamus-tests-XXXXXX)"
touch "$TEST_DATABASE_SOCKET/test-only"
initdb -D "$TEST_DATABASE_SOCKET/data" --auth-local=trust --auth-host=reject >/dev/null
pg_ctl -D "$TEST_DATABASE_SOCKET/data" -l "$TEST_DATABASE_SOCKET/postgres.log" \
    -o "-k $TEST_DATABASE_SOCKET -c listen_addresses=''" -w start >/dev/null
trap 'pg_ctl -D "$TEST_DATABASE_SOCKET/data" -m immediate -w stop >/dev/null' EXIT
python -m unittest discover -s tests "$@"
