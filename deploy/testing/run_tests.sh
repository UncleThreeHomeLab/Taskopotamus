#!/bin/sh
set -eu
export TEST_DATABASE_SOCKET="$(mktemp -d /tmp/taskopotamus-tests-XXXXXX)"
touch "$TEST_DATABASE_SOCKET/test-only"
initdb -D "$TEST_DATABASE_SOCKET/data" --auth-local=trust --auth-host=reject >/dev/null
pg_ctl -D "$TEST_DATABASE_SOCKET/data" -l "$TEST_DATABASE_SOCKET/postgres.log" \
    -o "-k $TEST_DATABASE_SOCKET -c listen_addresses=''" -w start >/dev/null
trap 'pg_ctl -D "$TEST_DATABASE_SOCKET/data" -m immediate -w stop >/dev/null' EXIT
python -m unittest discover -s tests "$@"
