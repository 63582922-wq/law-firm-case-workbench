# PostgreSQL 16 local-managed acceptance slice

This directory is the database boundary for the local managed Alpha stack. It
does not contain a usable password, client record, or provider credential.

The slice deliberately separates:

- `lawcase_schema_owner` and `lawcase_ledger_confirmation_owner`: `NOLOGIN`,
  `NOINHERIT` object-owner roles;
- `lawcase_migrator`: the only login allowed to assume the schema owner during
  deployment;
- five distinct runtime logins: Web application, OIDC directory, Web session
  gateway, Agent execution, and independent Agent verification;
- `keycloak`: a separate login and separate database with no `CONNECT` right
  on the Lawcase database.

All network connections are `hostssl` with SCRAM-SHA-256. `hostnossl` is
explicitly rejected. Unix-socket peer access is reserved for the PostgreSQL
container's bootstrap superuser.

## Compose integration contract

The surrounding stack should mount:

| Host path | Container path | Consumer |
| --- | --- | --- |
| generated 0600 environment | `/run/lawcase-config/local-managed.env` | PostgreSQL init, migration/seed job |
| generated TLS directory | `/run/lawcase-postgres-tls` | PostgreSQL only |
| `tls/ca.crt` only | `/run/lawcase-postgres-ca/ca.crt` | Web, Worker, verifier, Keycloak, probes |
| `backend/migrations` | `/migrations:ro` | migration/seed job |
| `initdb` | `/docker-entrypoint-initdb.d:ro` | PostgreSQL first initialization |

Use `postgres-entrypoint.sh` as the PostgreSQL entrypoint and pass `postgres`
as its command. Run `migrate-and-seed.sh` as a bounded one-shot job after the
database is healthy. Run `assert-runtime.sh` after that job before starting the
browser-facing services.

`container-test.sh` builds a fresh isolated PostgreSQL 16 database, applies the
complete migration sequence twice, checks every TLS role and isolation rule,
and then invokes the real Web and Agent startup database preflights through the
five-role runtime boundary. It requires the backend Python environment (or an
explicit `LAWCASE_BACKEND_PYTHON`) in addition to Docker and OpenSSL.

The migration job must share PostgreSQL's Unix socket and run as the container
`postgres` OS user with `LAWCASE_MIGRATION_ADMIN_SOCKET=/var/run/postgresql`.
This is necessary because 0048/0049 transfer object ownership after revoking
the target role's schema-create bit. The job temporarily makes the `NOLOGIN`
schema owner a migration administrator while first revoking its only login
membership. A trap demotes it and restores the non-superuser migrator
membership on both success and ordinary failure. No PostgreSQL superuser is
accepted over TCP, and all TCP clients remain TLS-only.

The migration runner discovers the current highest migration dynamically,
requires an unbroken `0001..NNNN` sequence, pins the SHA-256 of every applied
file, and refuses both source drift and an interrupted `APPLYING` record. It
never guesses that a half-observed migration succeeded.

The deterministic test identities come from the top-level generated
environment. The current contract is firm `111...`, lead `222...`, execution
worker `333...`, and verifier `444...`. They are synthetic identifiers, not
secrets. The OIDC issuer and subject are still supplied by the managed Keycloak
slice; the database seed refuses an issuer/subject collision with another
internal actor.
