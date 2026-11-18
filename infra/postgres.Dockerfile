FROM pgvector/pgvector:0.8.2-pg17
COPY --chmod=755 infra/init-postgres.sh /docker-entrypoint-initdb.d/10-roles.sh
