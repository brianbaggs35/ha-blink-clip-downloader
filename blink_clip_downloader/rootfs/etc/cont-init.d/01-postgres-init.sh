#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
# ==============================================================================
# One-shot PostgreSQL 17 data directory bootstrap.
#
# cont-init.d scripts run once, sequentially, before any services.d service
# starts (see just-containers/s6-overlay's legacy compat layer) — this is
# what makes it safe to leave the data directory ready and the "blink" role
# + "blink_clips" database created by the time services.d/postgresql/run
# (the actual long-running, s6-supervised server process) starts.
#
# The data directory lives under /data so it survives add-on
# updates/restarts the same way clip files and the old SQLite file always
# did — everything else in this container's filesystem is recreated fresh
# from the image on every start.
# ==============================================================================
set -e

PGDATA=/data/postgresql/17/main
PG_BIN=/usr/lib/postgresql/17/bin
SOCKET_DIR=/var/run/postgresql

mkdir -p /data/postgresql "${SOCKET_DIR}"
chown postgres:postgres /data/postgresql "${SOCKET_DIR}"

if [ ! -s "${PGDATA}/PG_VERSION" ]; then
  bashio::log.info "Initializing PostgreSQL 17 data directory at ${PGDATA}..."
  mkdir -p "${PGDATA}"
  chown postgres:postgres "${PGDATA}"

  su -s /bin/bash postgres -c "${PG_BIN}/initdb -D ${PGDATA} --auth-local=trust --auth-host=reject --no-instructions"

  # Local Unix-socket-only, trust-authenticated — the only thing that can
  # ever reach this server is another process inside this same container
  # (no TCP listener at all). This is the same trust boundary the previous
  # SQLite file relied on (filesystem permissions, no in-band auth), not a
  # weaker one.
  echo "local all all trust" > "${PGDATA}/pg_hba.conf"
  {
    echo "listen_addresses = ''"
    echo "unix_socket_directories = '${SOCKET_DIR}'"
  } >> "${PGDATA}/postgresql.conf"

  bashio::log.info "Creating blink role and blink_clips database..."
  su -s /bin/bash postgres -c "${PG_BIN}/pg_ctl -D ${PGDATA} -l /tmp/postgres-init.log -w start"
  su -s /bin/bash postgres -c "${PG_BIN}/createuser blink"
  su -s /bin/bash postgres -c "${PG_BIN}/createdb -O blink blink_clips"
  su -s /bin/bash postgres -c "${PG_BIN}/pg_ctl -D ${PGDATA} -w stop"
  bashio::log.info "PostgreSQL data directory ready."
fi
