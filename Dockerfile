FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error --location \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && gpg --batch --show-keys --with-colons /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        | grep -q '^fpr:::::::::B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8:' \
    && echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 make postgresql-client-16 postgresql-client-18 \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 toolbox \
    && useradd --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /bin/bash toolbox \
    && install -d -o 10001 -g 10001 -m 0700 /data

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg \
    && install -d /etc/apt/keyrings \
    && curl --fail --silent --show-error --location \
        https://packages.microsoft.com/keys/microsoft.asc \
        --output /etc/apt/keyrings/microsoft.asc \
    && echo 'deb [signed-by=/etc/apt/keyrings/microsoft.asc] https://packages.microsoft.com/repos/azure-cli/ bookworm main' \
        > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends azure-cli \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

ENV PG_MAJOR=16 \
    SCHEMA_BIN_DIR=/usr/lib/postgresql/18/bin \
    PATH=/usr/lib/postgresql/18/bin:$PATH \
    PGSSLMODE=verify-full \
    PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt \
    PGPASSFILE=/tmp/pgpass \
    TMPDIR=/data/tmp \
    AZURE_CONFIG_DIR=/tmp/.azure \
    AZURE_CORE_COLLECT_TELEMETRY=no \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/postgresql-toolbox
COPY Makefile server_backup.py server_compare.py azure_pitr.py azure_blob.py backup_common.py toolbox-entrypoint.sh README.md PITR.md ./
COPY tests ./tests
RUN install -m 0755 toolbox-entrypoint.sh /usr/local/bin/toolbox-entrypoint
USER 10001:10001
ENTRYPOINT ["toolbox-entrypoint"]
CMD ["sleep", "infinity"]
