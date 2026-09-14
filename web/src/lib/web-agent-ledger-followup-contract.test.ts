import assert from "node:assert/strict";
import test from "node:test";

import {
  buildWebAgentLedgerFollowupActionPayload,
  buildWebAgentLedgerPageQuery,
  buildWebAgentLedgerRecoveryPayload,
  webAgentLedgerExceptionCapacityMessage,
  webAgentLedgerAutomationLabel,
} from "./web-agent-ledger-followup-contract.ts";

test("follow-up action payload exposes only bounded browser fields", () => {
  const payload = buildWebAgentLedgerFollowupActionPayload({
    expectedVersion: 9,
    action: "CONFIRM_MORE_EVIDENCE",
    reasonNote: "已核对新增流水",
    sources: [{ objectType: "MATERIAL_OBJECT", objectId: "source-1" }],
  });
  assert.deepEqual(Object.keys(payload).sort(), [
    "action",
    "expected_version",
    "managed_evidence_sources",
    "reason_note",
  ]);
  for (const forbidden of ["firm_id", "actor_id", "run_id", "graph_id", "object_key", "subject_hash"]) {
    assert.equal(forbidden in payload, false);
  }
});

test("recovery payload cannot carry a browser-selected run", () => {
  assert.deepEqual(buildWebAgentLedgerRecoveryPayload(12), { expected_version: 12 });
});

test("follow-up paging remains reachable beyond the former 500 item ceiling", () => {
  assert.equal(buildWebAgentLedgerPageQuery(500, 50), "offset=500&limit=50");
  assert.equal(buildWebAgentLedgerPageQuery(999_950, 50), "offset=999950&limit=50");
  assert.throws(() => buildWebAgentLedgerPageQuery(-1, 50), /分页参数无效/);
  assert.throws(() => buildWebAgentLedgerPageQuery(0, 51), /分页参数无效/);
});

test("re-extraction states are described as ongoing work", () => {
  assert.equal(webAgentLedgerAutomationLabel("RUNNING"), "正在重新提取");
  assert.equal(webAgentLedgerAutomationLabel("VERIFYING"), "正在独立核验");
  assert.equal(webAgentLedgerAutomationLabel("RECOVERY_REQUIRED"), "需要恢复分析控制");
  const sourceWindow = webAgentLedgerExceptionCapacityMessage(
    "AGENT_LEDGER_REEXTRACTION_SOURCE_WINDOW_EXCEEDED",
  );
  assert.match(sourceWindow ?? "", /64页/);
  assert.match(sourceWindow ?? "", /拆分/);

  const cohortCapacity = webAgentLedgerExceptionCapacityMessage(
    "AGENT_LEDGER_REEXTRACTION_COHORT_CAPACITY_EXCEEDED",
  );
  assert.match(cohortCapacity ?? "", /99组/);
  assert.match(cohortCapacity ?? "", /撤回或替代/);
  assert.equal(webAgentLedgerExceptionCapacityMessage("P0001"), null);
});
