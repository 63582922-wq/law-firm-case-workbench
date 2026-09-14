import assert from "node:assert/strict";
import test from "node:test";

import {
  activePlanReviewEpoch,
  activePlanExecutionUiState,
  buildActivePlanExecutionPayload,
  buildCaseAgentCompletionPayload,
  caseAgentInputLineageNotice,
  canCompleteActivePlanRun,
  requiresNewCaseAgentRound,
} from "./web-active-plan-execution.ts";
import type { WebCaseAgentRun } from "./web-lawyer-api.ts";

test("completion payload binds a snapshot of exact document versions", () => {
  const id = "00000000-0000-4000-8000-000000000001";
  const versions = { [id]: "a".repeat(64) };
  const payload = buildCaseAgentCompletionPayload(9, versions);
  versions[id] = "b".repeat(64);
  assert.deepEqual(payload, { expected_run_version: 9, document_review_versions: { [id]: "a".repeat(64) } });
  assert.throws(() => buildCaseAgentCompletionPayload(9, { invalid: "a".repeat(64) }), /版本清单/);
  assert.throws(() => buildCaseAgentCompletionPayload(9, { [id]: "wrong" }), /版本清单/);
});

function run(
  status: WebCaseAgentRun["status"],
  activePlanExecution: boolean,
): WebCaseAgentRun {
  return {
    runId: "00000000-0000-4000-8000-000000000001",
    matterId: "00000000-0000-4000-8000-000000000002",
    objective: "生成可复核成果",
    status,
    phaseLabel: "律师复核",
    progress: { completed: 2, total: 2 },
    currentWork: null,
    openDecisionCount: 0,
    openApprovalCount: 0,
    artifactCount: 6,
    statusMessage: "等待律师复核",
    failureMessage: null,
    failureCode: null,
    version: 9,
    snapshotMatterVersion: 8,
    inputSnapshotStatus: "CURRENT",
    createdAt: "2026-08-15T00:00:00+08:00",
    updatedAt: "2026-08-15T00:10:00+08:00",
    actions: { canPause: false, canResume: false, canCancel: false },
    activePlanExecution,
    requiredDocumentDeliverables: activePlanExecution ? ["CASE_REVIEW_MEMO", "PAYMENT_LEDGER"] : [],
  };
}

test("final review follows the server manifest, including defence-only and mixed plans", () => {
  for (const kinds of [["DEFENCE_STATEMENT"], ["DEFENCE_STATEMENT", "PAYMENT_LEDGER", "CASE_REVIEW_MEMO"]] as const) {
    const current = { ...run("READY_FOR_REVIEW", true), requiredDocumentDeliverables: kinds };
    const input = {
      run: current, canCompleteCaseAgentRun: true, canReviewCaseAgentDocuments: true,
      reviewedRunEpoch: activePlanReviewEpoch(current),
      viewedDeliverableKinds: new Set<string>(kinds),
      downloadedDocumentFiles: new Set(kinds.flatMap((kind) => [`${kind}:editable`, `${kind}:pdf-preview`])),
    };
    assert.equal(canCompleteActivePlanRun(input), true);
    input.downloadedDocumentFiles.delete("DEFENCE_STATEMENT:pdf-preview");
    assert.equal(canCompleteActivePlanRun(input), false);
    assert.equal(canCompleteActivePlanRun({ ...input, run: { ...current, requiredDocumentDeliverables: [] } }), false);
    assert.equal(canCompleteActivePlanRun({ ...input, run: { ...current, requiredDocumentDeliverables: undefined } }), false);
  }
});

test("unrelated downloads cannot satisfy a defence-only task", () => {
  const current = { ...run("READY_FOR_REVIEW", true), requiredDocumentDeliverables: ["DEFENCE_STATEMENT"] as const };
  assert.equal(canCompleteActivePlanRun({
    run: current, canCompleteCaseAgentRun: true, canReviewCaseAgentDocuments: true,
    reviewedRunEpoch: activePlanReviewEpoch(current),
    viewedDeliverableKinds: new Set(["CASE_REVIEW_MEMO", "PAYMENT_LEDGER"]),
    downloadedDocumentFiles: new Set(["CASE_REVIEW_MEMO", "PAYMENT_LEDGER"].flatMap((kind) => [`${kind}:editable`, `${kind}:pdf-preview`])),
  }), false);
});

test("only a reviewed source run offers the one explicit active-plan execution", () => {
  assert.equal(activePlanExecutionUiState({ planStatus: "ACTIVE", canExecuteActivePlan: true, run: run("READY_FOR_REVIEW", false) }), "OFFER_EXECUTION");
  assert.equal(activePlanExecutionUiState({ planStatus: "ACTIVE", canExecuteActivePlan: true, run: run("EXECUTING", false) }), "WAITING_SOURCE_REVIEW");
  assert.equal(activePlanExecutionUiState({ planStatus: "ACTIVE", canExecuteActivePlan: false, run: run("READY_FOR_REVIEW", false) }), "RUNTIME_UNAVAILABLE");
});

test("browser write payloads carry only the reviewed server version", () => {
  assert.deepEqual(buildActivePlanExecutionPayload(12), { expected_version: 12 });
  assert.deepEqual(buildCaseAgentCompletionPayload(9), { expected_run_version: 9 });
  assert.throws(() => buildActivePlanExecutionPayload(0), /案件版本/);
  assert.throws(() => buildCaseAgentCompletionPayload(Number.NaN), /任务版本/);
});

test("a derived plan version never offers a duplicate paid analysis", () => {
  const candidatePlan = {
    ...run("READY_FOR_REVIEW", false),
    inputSnapshotStatus: "PLAN_CANDIDATE_REGISTERED" as const,
  };
  assert.equal(requiresNewCaseAgentRound(candidatePlan), false);
  assert.match(caseAgentInputLineageNotice(candidatePlan) ?? "", /候选登记/);

  const activePlan = {
    ...candidatePlan,
    inputSnapshotStatus: "PLAN_ACTIVE" as const,
  };
  assert.equal(requiresNewCaseAgentRound(activePlan), false);
  assert.match(caseAgentInputLineageNotice(activePlan) ?? "", /计划确认/);

  const changed = {
    ...candidatePlan,
    inputSnapshotStatus: "INPUTS_CHANGED" as const,
  };
  assert.equal(requiresNewCaseAgentRound(changed), true);
  assert.match(caseAgentInputLineageNotice(changed) ?? "", /不能代替/);
});

test("an execution run never exposes the execute action again", () => {
  assert.equal(activePlanExecutionUiState({ planStatus: "ACTIVE", canExecuteActivePlan: true, run: run("EXECUTING", true) }), "EXECUTING");
  assert.equal(activePlanExecutionUiState({ planStatus: "ACTIVE", canExecuteActivePlan: true, run: run("RECONCILIATION_REQUIRED", true) }), "RECONCILIATION_REQUIRED");
  assert.equal(activePlanExecutionUiState({ planStatus: "ACTIVE", canExecuteActivePlan: true, run: run("FAILED", true) }), "FAILED");
  assert.equal(activePlanExecutionUiState({ planStatus: "ACTIVE", canExecuteActivePlan: true, run: run("CANCELLED", true) }), "TERMINATED");
  assert.equal(activePlanExecutionUiState({ planStatus: "ACTIVE", canExecuteActivePlan: true, run: run("STALE", true) }), "INPUTS_CHANGED");
  assert.equal(activePlanExecutionUiState({ planStatus: "ACTIVE", canExecuteActivePlan: true, run: run("COMPLETED", true) }), "COMPLETED");
});

test("changed case inputs invalidate cached review even without a new run event", () => {
  const previous = run("READY_FOR_REVIEW", true);
  const changed = { ...previous, inputSnapshotStatus: "INPUTS_CHANGED" as const };
  assert.notEqual(activePlanReviewEpoch(previous), activePlanReviewEpoch(changed));
  for (const status of ["READY_FOR_REVIEW", "COMPLETED"] as const) {
    for (const activePlanExecution of [true, false]) {
      assert.equal(activePlanExecutionUiState({
        planStatus: "ACTIVE", canExecuteActivePlan: true,
        run: { ...changed, status, activePlanExecution },
      }), "INPUTS_CHANGED");
    }
  }
  const kinds = changed.requiredDocumentDeliverables ?? [];
  assert.equal(canCompleteActivePlanRun({
    run: changed, canCompleteCaseAgentRun: true, canReviewCaseAgentDocuments: true,
    reviewedRunEpoch: activePlanReviewEpoch(changed),
    viewedDeliverableKinds: new Set(kinds),
    downloadedDocumentFiles: new Set(kinds.flatMap(kind => [`${kind}:editable`, `${kind}:pdf-preview`])),
  }), false);
});

test("final review stays locked until both exact document deliverables were opened", () => {
  const current = run("READY_FOR_REVIEW", true);
  const currentEpoch = activePlanReviewEpoch(current);
  assert.equal(canCompleteActivePlanRun({
    run: current,
    canCompleteCaseAgentRun: true,
    canReviewCaseAgentDocuments: true,
    reviewedRunEpoch: currentEpoch,
    viewedDeliverableKinds: new Set(["CASE_REVIEW_MEMO"]),
    downloadedDocumentFiles: new Set(),
  }), false);
  assert.equal(canCompleteActivePlanRun({
    run: current,
    canCompleteCaseAgentRun: true,
    canReviewCaseAgentDocuments: true,
    reviewedRunEpoch: currentEpoch,
    viewedDeliverableKinds: new Set(["CASE_REVIEW_MEMO", "PAYMENT_LEDGER"]),
    downloadedDocumentFiles: new Set([
      "CASE_REVIEW_MEMO:editable",
      "CASE_REVIEW_MEMO:pdf-preview",
      "PAYMENT_LEDGER:editable",
    ]),
  }), false);
  assert.equal(canCompleteActivePlanRun({
    run: current,
    canCompleteCaseAgentRun: true,
    canReviewCaseAgentDocuments: true,
    reviewedRunEpoch: currentEpoch,
    viewedDeliverableKinds: new Set(["CASE_REVIEW_MEMO", "PAYMENT_LEDGER"]),
    downloadedDocumentFiles: new Set([
      "CASE_REVIEW_MEMO:editable",
      "CASE_REVIEW_MEMO:pdf-preview",
      "PAYMENT_LEDGER:editable",
      "PAYMENT_LEDGER:pdf-preview",
    ]),
  }), true);
  assert.equal(canCompleteActivePlanRun({
    run: run("READY_FOR_REVIEW", false),
    canCompleteCaseAgentRun: true,
    canReviewCaseAgentDocuments: true,
    reviewedRunEpoch: activePlanReviewEpoch(run("READY_FOR_REVIEW", false)),
    viewedDeliverableKinds: new Set(["CASE_REVIEW_MEMO", "PAYMENT_LEDGER"]),
    downloadedDocumentFiles: new Set([
      "CASE_REVIEW_MEMO:editable",
      "CASE_REVIEW_MEMO:pdf-preview",
      "PAYMENT_LEDGER:editable",
      "PAYMENT_LEDGER:pdf-preview",
    ]),
  }), false);
});

test("review and download proof cannot survive a same-run graph or artifact epoch change", () => {
  const reviewed = run("READY_FOR_REVIEW", true);
  const replanned: WebCaseAgentRun = {
    ...reviewed,
    version: reviewed.version + 3,
    artifactCount: reviewed.artifactCount + 1,
  };
  const reviewedFiles = new Set([
    "CASE_REVIEW_MEMO:editable",
    "CASE_REVIEW_MEMO:pdf-preview",
    "PAYMENT_LEDGER:editable",
    "PAYMENT_LEDGER:pdf-preview",
  ]);
  assert.notEqual(activePlanReviewEpoch(reviewed), activePlanReviewEpoch(replanned));
  assert.equal(canCompleteActivePlanRun({
    run: replanned,
    canCompleteCaseAgentRun: true,
    canReviewCaseAgentDocuments: true,
    reviewedRunEpoch: activePlanReviewEpoch(reviewed),
    viewedDeliverableKinds: new Set(["CASE_REVIEW_MEMO", "PAYMENT_LEDGER"]),
    downloadedDocumentFiles: reviewedFiles,
  }), false);
});
