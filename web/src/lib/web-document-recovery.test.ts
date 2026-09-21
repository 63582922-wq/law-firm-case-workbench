import assert from "node:assert/strict";
import test from "node:test";
import { documentRecoverySlot, rememberDocumentRecovery, readDocumentRecovery, forgetDocumentRecovery } from "./web-document-recovery.ts";

function storage() {
  const data = new Map<string, string>();
  return { data, getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => { data.set(key, value); }, removeItem: (key: string) => { data.delete(key); } };
}
const scope = ["case-1", "run-1", "document-1", "0", "0"];
const record = { key: "document-edit-test-0001", revision: 1, createdAt: 123 };

test("recovery survives a fresh read and separates actors and documents without persisting text", () => {
  const store = storage();
  const slot = documentRecoverySlot("a".repeat(64), scope);
  rememberDocumentRecovery(store, slot, record);
  assert.deepEqual(readDocumentRecovery(store, slot), record);
  assert.equal(readDocumentRecovery(store, documentRecoverySlot("b".repeat(64), scope)), null);
  assert.equal(readDocumentRecovery(store, documentRecoverySlot("a".repeat(64), ["case-2", ...scope.slice(1)])), null);
  assert.deepEqual(Object.keys(JSON.parse(store.data.get(slot)!)).sort(), ["createdAt", "key", "revision"]);
  assert.throws(() => rememberDocumentRecovery(store, slot, { ...record, text: "不得存储案件正文" } as typeof record));
});

test("unknown recovery is not overwritten or removed by another key", () => {
  const store = storage(); const slot = documentRecoverySlot("a".repeat(64), scope);
  rememberDocumentRecovery(store, slot, record);
  assert.throws(() => rememberDocumentRecovery(store, slot, { ...record, key: "document-edit-test-0002" }));
  forgetDocumentRecovery(store, slot, "different-key");
  assert.deepEqual(readDocumentRecovery(store, slot), record);
  forgetDocumentRecovery(store, slot, record.key);
  assert.equal(readDocumentRecovery(store, slot), null);
});

test("malformed or unavailable storage fails closed", () => {
  const store = storage(); const slot = documentRecoverySlot("a".repeat(64), scope);
  store.setItem(slot, "{");
  assert.throws(() => readDocumentRecovery(store, slot));
  assert.throws(() => rememberDocumentRecovery(store, slot, record));
  store.removeItem(slot);
  assert.throws(() => rememberDocumentRecovery({ ...store, setItem: () => {} }, slot, record));
  assert.throws(() => documentRecoverySlot("not-a-user-scope", scope));
});
