"""Recover only the known, transactionally rolled-back 0094 syntax failure.

No schema history is deleted. Preserve the failed digest in this recovery
record and replace APPLYING only in the same transaction as successful DDL.
"""
from hashlib import sha256
from pathlib import Path
import subprocess

FAILED_DIGEST = "da7f297e4d3fb7238d20b6084a12cd375ee4d3f46d2406d4ca9552df49fe3b18"
PATH = Path(__file__).resolve().parents[1] / "migrations/0094_supplementary_material_stage.sql"


def main():
    source = PATH.read_text()
    current = sha256(source.encode()).hexdigest()
    if source.count("BEGIN;\n") != 1 or source.count("COMMIT;") != 1 or current == FAILED_DIGEST:
        raise RuntimeError("recovery source differs from the reviewed shape")
    body = source.replace("BEGIN;\n", "", 1).replace("COMMIT;", "", 1)
    guard = f"""BEGIN;
    SELECT pg_advisory_xact_lock(940094);
    DO $guard$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM public.lawcase_schema_migrations
        WHERE migration_number=94 AND filename='0094_supplementary_material_stage.sql'
          AND state='APPLYING' AND source_sha256='{FAILED_DIGEST}')
        OR to_regprocedure('public.guard_case_agent_supplementary_material_stage()') IS NOT NULL
        OR to_regprocedure('public.case_agent_is_bounded_supplementary_material_stage(uuid,uuid,uuid)') IS NOT NULL THEN
        RAISE EXCEPTION 'known rolled-back migration state no longer matches';
      END IF;
    END $guard$;
    """
    finish = f"""
    UPDATE public.lawcase_schema_migrations SET source_sha256='{current}',
      state='APPLIED', applied_at=clock_timestamp()
      WHERE migration_number=94 AND state='APPLYING' AND source_sha256='{FAILED_DIGEST}';
    COMMIT;
    """
    result = subprocess.run(["docker", "exec", "-i", "--user", "postgres",
        "lawcase-managed-alpha-postgres-1", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-d", "lawcase"], input=guard + body + finish,
        text=True, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError(result.stderr)
    print(f"0094 applied atomically; failed digest {FAILED_DIGEST}; applied digest {current}")


if __name__ == "__main__":
    main()
