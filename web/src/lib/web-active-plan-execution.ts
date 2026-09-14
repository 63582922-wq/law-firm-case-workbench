import type { WebCaseAgentRun } from "./web-lawyer-api.ts";

const DOCUMENT_LABELS = {
  CASE_REVIEW_MEMO: "案件审阅意见",
  SUPPLEMENTARY_EVIDENCE_CHECKLIST: "补证清单",
  EVIDENCE_CATALOGUE: "证据目录",
  PAYMENT_LEDGER: "收付款核对表",
  DEFENCE_STATEMENT: "答辩状",
} as const;

export function activePlanReviewNotice(run: WebCaseAgentRun): string {
  const kinds = run.requiredDocumentDeliverables ?? [];
  if (!kinds.length) return "暂未取得本次任务的文书清单，请刷新任务状态后继续终审。";
  return `本次需复核：${kinds.map((kind) => DOCUMENT_LABELS[kind]).join("、")}。请逐项打开并核对来源，下载可编辑文件及 PDF 审阅稿。下载不代表认可内容；实际完成复核后，再确认终审。`;
}

export function buildActivePlanExecutionPayload(expectedVersion: number): Readonly<{
  expected_version: number;
}> {
  if (!Number.isSafeInteger(expectedVersion) || expectedVersion < 1) {
    throw new Error("案件版本格式不正确");
  }
  return { expected_version: expectedVersion };
}

export function buildCaseAgentCompletionPayload(expectedRunVersion: number, documentReviewVersions: Readonly<Record<string, string>> = {}): Readonly<{
  expected_run_version: number;
  document_review_versions?: Readonly<Record<string, string>>;
}> {
  if (!Number.isSafeInteger(expectedRunVersion) || expectedRunVersion < 1) {
    throw new Error("任务版本格式不正确");
  }
  const bindings = Object.entries(documentReviewVersions);
  if (bindings.length > 128 || bindings.some(([id, version]) => !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id) || !/^[a-f0-9]{64}$/.test(version))) {
    throw new Error("文书审阅版本清单不正确");
  }
  return { expected_run_version: expectedRunVersion,
    ...(bindings.length ? { document_review_versions: Object.fromEntries(bindings.sort(([a], [b]) => a.localeCompare(b))) } : {}) };
}

/**
 * Browser review state is valid only for the exact immutable run projection it
 * was opened from. Every graph replacement or artifact mutation advances the
 * event version, while artifactCount also makes the intended boundary explicit
 * to future projection changes.
 */
export function activePlanReviewEpoch(run: WebCaseAgentRun | null): string | null {
  if (!run) return null;
  return `${run.runId}:${run.version}:${run.artifactCount}:${run.inputSnapshotStatus}:${(run.requiredDocumentDeliverables ?? []).join(",")}`;
}

/**
 * A matter version is an aggregate CAS clock.  The browser must not start a
 * paid replacement analysis merely because the verified output registered a
 * downstream plan candidate or the lawyer activated that plan.
 */
export function requiresNewCaseAgentRound(run: WebCaseAgentRun | null): boolean {
  return Boolean(
    run
    && (run.status === "STALE" || run.inputSnapshotStatus === "INPUTS_CHANGED"),
  );
}

export function caseAgentInputLineageNotice(
  run: WebCaseAgentRun | null,
): string | null {
  if (!run) return null;
  if (run.inputSnapshotStatus === "PLAN_CANDIDATE_REGISTERED") {
    return "本轮研判已转化为当前动态办案计划候选；当前版本变化来自候选登记，不是新的案情输入。";
  }
  if (run.inputSnapshotStatus === "PLAN_ACTIVE") {
    return "本轮研判已转化为当前已激活办案计划；当前版本变化来自计划确认，不是新的案情输入。";
  }
  if (requiresNewCaseAgentRound(run)) {
    return "案件的事实、证据、诉请、争点、程序或法源输入已变化；旧成果保留为审计记录，不能代替新案情研判。";
  }
  return null;
}

export type ActivePlanExecutionUiState =
  | "HIDDEN"
  | "RUNTIME_UNAVAILABLE"
  | "WAITING_SOURCE_REVIEW"
  | "OFFER_EXECUTION"
  | "EXECUTING"
  | "RECONCILIATION_REQUIRED"
  | "READY_FOR_FINAL_REVIEW"
  | "COMPLETED"
  | "INPUTS_CHANGED"
  | "TERMINATED"
  | "FAILED";

export function activePlanExecutionUiState(input: Readonly<{
  planStatus: "CANDIDATE" | "ACTIVE" | "STALE" | "SUPERSEDED" | null;
  canExecuteActivePlan: boolean;
  run: WebCaseAgentRun | null;
}>): ActivePlanExecutionUiState {
  if (input.planStatus !== "ACTIVE") return "HIDDEN";
  const run = input.run;
  if (requiresNewCaseAgentRound(run)) return "INPUTS_CHANGED";
  if (run?.activePlanExecution) {
    if (run.status === "FAILED") return "FAILED";
    if (run.status === "RECONCILIATION_REQUIRED") return "RECONCILIATION_REQUIRED";
    if (run.status === "READY_FOR_REVIEW") return "READY_FOR_FINAL_REVIEW";
    if (run.status === "COMPLETED") return "COMPLETED";
    if (run.status === "CANCELLED" || run.status === "STALE") return "TERMINATED";
    return "EXECUTING";
  }
  if (!input.canExecuteActivePlan) return "RUNTIME_UNAVAILABLE";
  if (run && (run.status === "READY_FOR_REVIEW" || run.status === "COMPLETED")) {
    return "OFFER_EXECUTION";
  }
  return "WAITING_SOURCE_REVIEW";
}

export function canCompleteActivePlanRun(input: Readonly<{
  run: WebCaseAgentRun | null;
  canCompleteCaseAgentRun: boolean;
  canReviewCaseAgentDocuments: boolean;
  reviewedRunEpoch: string | null;
  viewedDeliverableKinds: ReadonlySet<string>;
  downloadedDocumentFiles: ReadonlySet<string>;
}>): boolean {
  const currentEpoch = activePlanReviewEpoch(input.run);
  const required = input.run?.requiredDocumentDeliverables ?? [];
  return Boolean(
    input.run?.activePlanExecution
    && input.run.status === "READY_FOR_REVIEW"
    && !requiresNewCaseAgentRound(input.run)
    && input.canCompleteCaseAgentRun
    && input.canReviewCaseAgentDocuments
    && currentEpoch !== null
    && input.reviewedRunEpoch === currentEpoch
    && required.length > 0
    && new Set(required).size === required.length
    && required.every((kind) => Object.hasOwn(DOCUMENT_LABELS, kind)
      && input.viewedDeliverableKinds.has(kind)
      && input.downloadedDocumentFiles.has(`${kind}:editable`)
      && input.downloadedDocumentFiles.has(`${kind}:pdf-preview`)),
  );
}
