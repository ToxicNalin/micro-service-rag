#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE auth_user LOGIN PASSWORD '${AUTH_DB_PASSWORD}';
    CREATE DATABASE auth_db OWNER auth_user;
    REVOKE ALL ON DATABASE auth_db FROM PUBLIC;
    CREATE ROLE ingestion_user LOGIN PASSWORD '${INGESTION_DB_PASSWORD}';
    CREATE DATABASE ingestion_db OWNER ingestion_user;
    REVOKE ALL ON DATABASE ingestion_db FROM PUBLIC;
EOSQL
