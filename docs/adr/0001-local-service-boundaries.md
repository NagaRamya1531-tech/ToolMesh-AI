# ADR 0001 — Local topology and data ownership

Accepted for M0.

Start with separate control-plane and gateway modules in one API deployment. This keeps bootstrap small and prepares policy, approval and execution records to share a transactional boundary. Reconsider process separation when independent scaling, isolation or deployments have a demonstrated benefit. M1 introduces domain services on distinct credentials and persistence boundaries.

Use Postgres for durable state and Redis for transient runtime coordination. Compose bootstraps a local privileged migration account for pgvector provisioning and a read-only app account for the M0 schema. Managed environments should provision extensions separately and delegate scoped migration privileges before deployment.

Use Nginx to serve the compiled console and proxy readiness on the same origin. This keeps local requests simple and gives the production build a real integration path. Vite stays available for development with a configurable upstream.
