#!/bin/sh
set -eu
psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$TOOLMESH_APP_PASSWORD" <<'SQL'
CREATE ROLE toolmesh_app LOGIN PASSWORD :'app_password';
REVOKE CONNECT, TEMPORARY ON DATABASE toolmesh FROM PUBLIC;
GRANT CONNECT ON DATABASE toolmesh TO toolmesh_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO toolmesh_app;
SQL
