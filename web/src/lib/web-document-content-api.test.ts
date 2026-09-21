import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { runInNewContext } from "node:vm";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const compiled = ts.transpileModule(readFileSync(new URL("./web-lawyer-api.ts", import.meta.url), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const id = "00000000-0000-4000-8000-000000000001";

function api(fetch: (path: string, init?: RequestInit) => Promise<Response>) {
  const exports: Record<string, (...args: unknown[]) => Promise<unknown>> = {};
  runInNewContext(compiled, { exports, require: () => ({ webApiFetch: fetch }),
    Response, URL, Uint8Array, TextEncoder, crypto, console, process: { env: {} } });
  return exports;
}

test("paragraph save keeps the caller's retry key and only sends the edit contract", async () => {
  const calls: { path: string; init?: RequestInit }[] = [];
  const client = api(async (path, init) => {
    calls.push({ path, init });
    return Response.json({ proposal_id: id, status: "NEEDS_SOURCE_AND_LAWYER_REVIEW", court_ready: false });
  });
  const change = { section_index: 0, paragraph_index: 0, expected_text_hash: "a".repeat(64),
    replacement_text: "请求核对证据。", reason: "补充审阅要求", source_refs: ["source-test"] };
  for (let attempt = 0; attempt < 2; attempt++) {
    assert.equal(await client.saveWebDocumentContentProposal(id, id, id, 1, [change], "document-edit-test-0001"), id);
  }
  assert.equal(calls[0].init?.body, calls[1].init?.body);
  assert.equal(new Headers(calls[0].init?.headers).get("Idempotency-Key"), "document-edit-test-0001");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { expected_revision_number: 1, changes: [change] });
});

test("saved read retains old-version status and rejects misleading acceptance or foreign id", async () => {
  const payload = { proposal_id: id, expected_revision_number: 1,
    status: "NEEDS_SOURCE_AND_LAWYER_REVIEW", court_ready: false, based_on_current_version: false,
    changes: [{ before: "原文", after: "修改后", reason: "复核" }] };
  const client = api(async () => Response.json(payload));
  const result = await client.readWebDocumentContentProposal(id, id, id, id) as { basedOnCurrentVersion: boolean };
  assert.equal(result.basedOnCurrentVersion, false);
  payload.court_ready = true;
  await assert.rejects(client.readWebDocumentContentProposal(id, id, id, id));
  payload.court_ready = false;
  payload.proposal_id = "00000000-0000-4000-8000-000000000002";
  await assert.rejects(client.readWebDocumentContentProposal(id, id, id, id));
});

test("generation status is read only and old services do not imply success", async () => {
  const payload: Record<string, unknown> = { proposal_id: id, expected_revision_number: 1,
    based_on_current_version: true, status: "NEEDS_SOURCE_AND_LAWYER_REVIEW", court_ready: false,
    changes: [{ before: "原文", after: "修改", reason: "复核" }] };
  const client = api(async (_path, init) => {
    assert.equal(init?.method ?? "GET", "GET");
    assert.equal(init?.body, undefined);
    return Response.json(payload);
  });
  assert.equal((await client.readWebDocumentContentProposal(id, id, id, id) as { generationStatus: string }).generationStatus, "UNAVAILABLE");
  for (const status of ["NOT_AUTHORIZED", "QUEUED", "GENERATING", "RECOVERING", "UNKNOWN", "UNKNOWN_REGISTERED", "UNKNOWN_FILES_VERIFIED", "FAILED", "GENERATED_REVIEW_COPY"]) {
    payload.generation_status = status;
    assert.equal((await client.readWebDocumentContentProposal(id, id, id, id) as { generationStatus: string }).generationStatus, status);
  }
  payload.generation_status = "COURT_READY";
  await assert.rejects(client.readWebDocumentContentProposal(id, id, id, id));
});

test("generation authorization binds the review version, identity namespace and stable request key", async () => {
  const calls: { path: string; init?: RequestInit }[] = [];
  const payload = { review_id: id, status: "AUTHORIZED_NOT_GENERATED", court_ready: false };
  const client = api(async (path, init) => { calls.push({ path, init }); return Response.json(payload); });
  const args = [id, id, id, id, 2, " 已核对修改及来源 ", "document-generation-test-0001", "a".repeat(64)];
  assert.equal(await client.authorizeWebDocumentContentGeneration(...args), id);
  assert.ok(calls[0].path.endsWith(`/${id}/generation-reviews`));
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(new Headers(calls[0].init?.headers).get("Idempotency-Key"), args[6]);
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), {
    expected_revision_number: 2, review_note: "已核对修改及来源", recovery_namespace: "a".repeat(64),
  });
  payload.court_ready = true;
  await assert.rejects(client.authorizeWebDocumentContentGeneration(...args));
  const count = calls.length;
  args[7] = "invalid";
  await assert.rejects(client.authorizeWebDocumentContentGeneration(...args));
  assert.equal(calls.length, count);
});

test("download sends the reviewed version and rejects a changed response before saving", async () => {
  const calls: { path: string; init?: RequestInit }[] = [];
  const client = api(async (path, init) => {
    calls.push({ path, init });
    return new Response("%PDF-test", { headers: { "Content-Type": "application/pdf", "X-Document-Review-Version": "b".repeat(64) } });
  });
  await assert.rejects(client.downloadWebCaseAgentDocumentFile(id, id, id, "pdf-preview", "DOCX", "a".repeat(64)), /下载版本/);
  assert.equal(calls.length, 1);
  assert.equal(new Headers(calls[0].init?.headers).get("X-Document-Review-Version"), "a".repeat(64));
  await assert.rejects(client.downloadWebCaseAgentDocumentFile(id, id, id, "pdf-preview", "DOCX", "invalid"), /版本/);
  assert.equal(calls.length, 1);
});

test("history list is bounded and does not accept a looping cursor or duplicate records", async () => {
  const payload: { items: object[]; next_after: string | null } = { items: [{ proposal_id: id,
    expected_revision_number: 1, created_at: "2026-09-05T12:00:00+08:00",
    status: "NEEDS_SOURCE_AND_LAWYER_REVIEW", court_ready: false }], next_after: null };
  let path = "";
  const client = api(async (url) => { path = url; return Response.json(payload); });
  const result = await client.listWebDocumentContentProposals(id, id, id) as { items: object[]; nextAfter: string | null };
  assert.equal(result.items.length, 1);
  assert.equal(result.nextAfter, null);
  assert.ok(path.endsWith("/document-content-proposals"));
  payload.next_after = id;
  await assert.rejects(client.listWebDocumentContentProposals(id, id, id, id));
  payload.next_after = null;
  payload.items.push(payload.items[0]);
  await assert.rejects(client.listWebDocumentContentProposals(id, id, id));
});

test("save reconciliation uses a read with the original key and preserves unconfirmed status", async () => {
  const payload: { status: string; proposal_id: string | null; court_ready: boolean } = {
    status: "UNCONFIRMED", proposal_id: null, court_ready: false,
  };
  const calls: { path: string; init?: RequestInit }[] = [];
  const client = api(async (path, init) => { calls.push({ path, init }); return Response.json(payload); });
  assert.equal(await client.resolveWebDocumentContentProposal(id, id, id, "document-edit-test-0001"), null);
  assert.ok(calls[0].path.endsWith("/document-content-proposal-status"));
  assert.equal(calls[0].init?.method ?? "GET", "GET");
  assert.equal(calls[0].init?.body, undefined);
  assert.equal(new Headers(calls[0].init?.headers).get("Idempotency-Key"), "document-edit-test-0001");
  payload.status = "RECORDED"; payload.proposal_id = id;
  assert.equal(await client.resolveWebDocumentContentProposal(id, id, id, "document-edit-test-0001"), id);
  payload.status = "NOT_SAVED";
  await assert.rejects(client.resolveWebDocumentContentProposal(id, id, id, "document-edit-test-0001"));
});
