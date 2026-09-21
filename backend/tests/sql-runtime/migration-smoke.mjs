// Optional PGlite or explicitly isolated, empty project-local native PG16.
// The native adapter rejects existing application clusters and never loads case data.
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { readdir, readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const migrations = new URL('../../migrations/', import.meta.url);
const socketArgument = process.argv.find((argument) => argument.startsWith('--native-socket='));
function nativeDatabase(socket) {
  const child = spawn(fileURLToPath(new URL('../../.venv/bin/python', import.meta.url)),
    ['-u', fileURLToPath(new URL('./native-driver.py', import.meta.url)), socket],
    { stdio: ['pipe', 'pipe', 'inherit'] });
  const waiting = [];
  let failure = null;
  const fail = (error) => { failure = error; while (waiting.length) waiting.shift().reject(error); };
  child.on('error', fail);
  child.stdin.on('error', fail);
  child.on('exit', (code) => fail(new Error(`isolated native driver exited (${code})`)));
  createInterface({ input: child.stdout }).on('line', (line) => {
    const pending = waiting.shift();
    if (!pending) return fail(new Error('unexpected native driver response'));
    try {
      const result = JSON.parse(line);
      if (result.error) pending.reject(Object.assign(new Error(result.error), { code: result.code }));
      else pending.resolve(result);
    } catch (error) { pending.reject(error); }
  });
  const request = (sql, params, query) => new Promise((resolve, reject) => {
    if (failure) return reject(failure);
    waiting.push({ resolve, reject });
    child.stdin.write(`${JSON.stringify({ sql, params, query })}\n`);
  });
  return { exec: (sql) => request(sql, undefined, false), query: (sql, params) => request(sql, params, true),
    close: async () => { child.stdin.end(); } };
}
let db;
if (socketArgument) db = nativeDatabase(socketArgument.slice('--native-socket='.length));
else {
  const { PGlite } = await import('@electric-sql/pglite');
  const { pgcrypto } = await import('@electric-sql/pglite/contrib/pgcrypto');
  db = new PGlite({ extensions: { pgcrypto } });
}
let current = 'initialization';
let adminWindowOpen = false;
let closeAdminWindow = '';
try {
  const version = await db.query('SELECT version() AS version');
  console.log(version.rows[0].version);
  const roles = ['lawcase_schema_owner', 'lawcase_migrator', 'lawcase_agent_worker', 'lawcase_agent_verifier',
    'lawcase_identity_directory', 'lawcase_ledger_confirmation_owner', 'lawcase_web_application', 'lawcase_web_session_gateway'];
  for (const role of roles) await db.exec(`CREATE ROLE ${role} NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS`);
  await db.exec('GRANT lawcase_ledger_confirmation_owner TO lawcase_schema_owner; GRANT lawcase_schema_owner TO lawcase_migrator; ALTER SCHEMA public OWNER TO lawcase_schema_owner;');
  await db.exec('CREATE EXTENSION pgcrypto');
  // Follow the real isolated migration window, not an invented role policy.
  const runner = await readFile(new URL('../../deployment/local-managed-test/postgres/migrate-and-seed.sh', migrations), 'utf8');
  const openAdminWindow = runner.match(/REVOKE lawcase_schema_owner FROM lawcase_migrator;\s*ALTER ROLE lawcase_schema_owner SUPERUSER;/)?.[0];
  closeAdminWindow = runner.match(/ALTER ROLE lawcase_schema_owner NOSUPERUSER;\s*GRANT lawcase_schema_owner TO lawcase_migrator;\s*REVOKE CREATE ON SCHEMA public FROM lawcase_ledger_confirmation_owner;/)?.[0] ?? '';
  if (!openAdminWindow || !closeAdminWindow) throw new Error('migration role-window contract changed; review the deployment script');
  await db.exec(openAdminWindow);
  adminWindowOpen = true;
  const membership = await db.query("SELECT pg_has_role('lawcase_migrator', 'lawcase_schema_owner', 'MEMBER') AS reachable");
  assert.equal(membership.rows[0].reachable, false, 'migration elevated owner must not be reachable through migrator');
  await db.exec(await readFile(new URL('../../deployment/local-managed-test/postgres/prepare-schema.sql', migrations), 'utf8'));
  const focused = process.argv.includes('--focused-0078');
  if (focused) {
    // Minimal FK/role prerequisites, NOT an end-to-end schema acceptance fixture.
    await db.exec(`CREATE TABLE firms (firm_id uuid PRIMARY KEY);
      CREATE TABLE matters (matter_id uuid PRIMARY KEY, firm_id uuid);
      CREATE TABLE case_agent_runs (run_id uuid PRIMARY KEY, firm_id uuid, matter_id uuid);
      CREATE TABLE users (user_id uuid PRIMARY KEY, firm_id uuid NOT NULL, status text NOT NULL, UNIQUE(user_id,firm_id));
      CREATE TABLE matter_actor_roles (user_id uuid, firm_id uuid, matter_id uuid, role text, revoked_at timestamptz);
      CREATE TABLE case_agent_reviewable_document_packages (package_id uuid PRIMARY KEY, run_id uuid, firm_id uuid, matter_id uuid,
        UNIQUE(package_id,run_id,firm_id,matter_id), UNIQUE(package_id,firm_id,matter_id));
      CREATE TABLE case_agent_document_revision_requests
        (request_id uuid PRIMARY KEY, firm_id uuid, matter_id uuid, run_id uuid, requested_by uuid,
         UNIQUE(request_id,firm_id,matter_id));
      CREATE TABLE case_agent_document_revision_receipts
        (receipt_id uuid PRIMARY KEY, request_id uuid, firm_id uuid, matter_id uuid,
         UNIQUE(receipt_id,firm_id,matter_id));`);
    const predecessor = await readFile(new URL('0073_deterministic_document_package_revisions.sql', migrations), 'utf8');
    // Keep the real legacy mode/shape checks for the 0078 ALTER statements.
    for (const marker of ['ADD COLUMN generation_mode', 'ADD CONSTRAINT case_agent_reviewable_document_package_revision_shape']) {
      const start = predecessor.lastIndexOf('ALTER TABLE case_agent_reviewable_document_packages', predecessor.indexOf(marker));
      const end = predecessor.indexOf(';', start);
      if (start < 0 || end < start) throw new Error(`required package schema source not found: ${marker}`);
      await db.exec(predecessor.slice(start, end + 1));
    }
    const immutable = predecessor.match(/CREATE FUNCTION prohibit_case_agent_document_revision_request_change\(\)[\s\S]*?\$\$;/);
    if (!immutable) throw new Error('required immutable trigger source not found');
    await db.exec(immutable[0]);
    const immutableReceipt = predecessor.match(/CREATE FUNCTION prohibit_case_agent_document_revision_receipt_change\(\)[\s\S]*?\$\$;/);
    if (!immutableReceipt) throw new Error('required receipt immutable trigger source not found');
    await db.exec(immutableReceipt[0]);
    // Real legacy trigger definitions, but no row execution in this DDL/ACL fixture.
    for (const name of ['validate_case_agent_document_revision_request_insert', 'validate_case_agent_reviewable_document_revision_insert', 'enqueue_case_agent_document_revision_request']) {
      const definition = predecessor.match(new RegExp(`CREATE FUNCTION ${name}\\(\\)[\\s\\S]*?\\$\\$;`));
      if (!definition) throw new Error(`required legacy function not found: ${name}`);
      await db.exec(definition[0]);
    }
    for (const name of ['case_agent_document_revision_requests_insert_guard', 'case_agent_document_revision_requests_enqueue']) {
      const definition = predecessor.match(new RegExp(`CREATE TRIGGER ${name}[\\s\\S]*?;`));
      if (!definition) throw new Error(`required legacy trigger not found: ${name}`);
      await db.exec(definition[0]);
    }
  }
  const files = (await readdir(migrations)).filter((name) => /^\d{4}.*\.sql$/.test(name) && (!focused || name.startsWith('0078_'))).sort();
  for (const file of files) {
    current = file;
    // The production runner starts each psql migration with this role.
    await db.exec('SET ROLE lawcase_schema_owner');
    // Read the bounded allowlist from the deployed runner, not a separate policy.
    const ownerWindow = runner.match(/case "\$number" in\s*([0-9|]+)\)\s*printf '%s\\n' '(GRANT CREATE ON SCHEMA public TO lawcase_ledger_confirmation_owner;)'/);
    if (!ownerWindow) throw new Error('migration owner window contract changed');
    if (ownerWindow[1].split('|').includes(String(Number(file.slice(0, 4))))) await db.exec(ownerWindow[2]);
    await db.exec(await readFile(new URL(file, migrations), 'utf8'));
    await db.exec('REVOKE CREATE ON SCHEMA public FROM lawcase_ledger_confirmation_owner');
  }
  if (!focused) {
    current = 'post-migrate-hardening.sql';
    await db.exec(await readFile(new URL('../../deployment/local-managed-test/postgres/post-migrate-hardening.sql', migrations), 'utf8'));
  }
  await db.exec(`RESET ROLE; ${closeAdminWindow}`);
  adminWindowOpen = false;
  const owner = await db.query("SELECT rolsuper FROM pg_roles WHERE rolname = 'lawcase_schema_owner'");
  assert.equal(owner.rows[0].rolsuper, false, 'migration owner must be demoted before acceptance checks');
  current = '0078 runtime ACL checks';
  for (const [role, table, privilege, expected] of [
    ['lawcase_agent_worker', 'case_agent_document_content_generation_jobs', 'INSERT', false],
    ['lawcase_web_application', 'case_agent_document_content_generation_jobs', 'INSERT', false],
    ['lawcase_web_application', 'case_agent_document_content_generation_jobs', 'UPDATE', false],
    ['lawcase_agent_worker', 'case_agent_document_content_generation_reviews', 'INSERT', false],
    ['lawcase_web_application', 'case_agent_document_content_proposals', 'UPDATE', false],
    ['lawcase_agent_worker', 'case_agent_document_content_generation_job_events', 'INSERT', false],
    ['lawcase_web_application', 'case_agent_document_content_generation_reviews', 'INSERT', true],
    ['lawcase_agent_worker', 'case_agent_document_content_proposals', 'SELECT', true],
    ['lawcase_agent_worker', 'case_agent_document_content_generation_reviews', 'SELECT', true],
    ['lawcase_agent_verifier', 'case_agent_document_content_generation_jobs', 'SELECT', true],
    ['lawcase_agent_verifier', 'case_agent_document_content_generation_reviews', 'SELECT', true],
  ]) {
    const result = await db.query('SELECT has_table_privilege($1, $2, $3) AS allowed', [role, table, privilege]);
    assert.equal(result.rows[0].allowed, expected, `${role} ${privilege} ${table}`);
  }
  for (const [column, expected] of [['state', true], ['claim_version', false], ['claimed_by', false], ['lease_expires_at', false]]) {
    const result = await db.query("SELECT has_column_privilege('lawcase_agent_verifier', 'case_agent_document_content_generation_jobs', $1, 'UPDATE') AS allowed", [column]);
    assert.equal(result.rows[0].allowed, expected, `verifier UPDATE ${column}`);
  }
  for (const role of ['lawcase_web_application', 'lawcase_agent_worker', 'lawcase_agent_verifier']) {
    for (const relation of ['case_agent_document_content_recoveries', 'case_agent_document_revision_current_results']) {
      const read = await db.query("SELECT has_table_privilege($1, $2, 'SELECT') AS allowed", [role, relation]);
      assert.equal(read.rows[0].allowed, true, `${role} must read current results through case authorization`);
      const privileges = relation === 'case_agent_document_content_recoveries' ? 'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER' : 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER';
      const write = await db.query('SELECT has_table_privilege($1, $2, $3) AS allowed', [role, relation, privileges]);
      assert.equal(write.rows[0].allowed, false, `${role} must not mutate unreleased recovery results`);
      if (relation === 'case_agent_document_content_recoveries') {
        const append = await db.query("SELECT has_table_privilege($1, $2, 'INSERT') AS allowed", [role, relation]);
        assert.equal(append.rows[0].allowed, role === 'lawcase_agent_verifier', `${role} guarded recovery append boundary`);
      }
    }
    const locking = await db.query("SELECT has_function_privilege($1, 'lock_document_content_recovery_context(uuid,uuid,uuid)', 'EXECUTE') AS allowed", [role]);
    assert.equal(locking.rows[0].allowed, role === 'lawcase_agent_verifier', `${role} recovery lock function boundary`);
    const membership = await db.query("SELECT pg_has_role($1, 'lawcase_document_recovery_lock_owner', 'MEMBER') AS allowed", [role]);
    assert.equal(membership.rows[0].allowed, false, `${role} cannot become recovery lock owner`);
  }
  const lockOwner = await db.query("SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolinherit AS isolated FROM pg_roles WHERE rolname = 'lawcase_document_recovery_lock_owner'");
  assert.equal(lockOwner.rows[0].isolated, true, 'recovery lock owner must remain isolated');
  console.log(JSON.stringify({ status: 'MIGRATIONS_APPLIED', migrations: files.length,
    last: files.at(-1), focused, aclChecks: 37, rssMiB: Math.ceil(process.memoryUsage().rss / 1048576),
    fixture: focused ? 'minimal prerequisite schema, no case rows' : socketArgument ? 'fresh isolated native PostgreSQL 16' : 'empty in-memory database', source: fileURLToPath(migrations) }));
} catch (error) {
  console.error(JSON.stringify({ status: 'FAILED', migration: current,
    message: error.message, code: error.code, detail: error.detail, where: error.where?.slice(0, 240), position: error.position }));
  process.exitCode = 1;
} finally {
  try {
    if (adminWindowOpen) await db.exec(`ROLLBACK; RESET ROLE; ${closeAdminWindow}`);
  } finally { await db.close(); }
}
