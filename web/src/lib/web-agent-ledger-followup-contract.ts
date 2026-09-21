export type WebAgentLedgerFollowupActionCode =
  | "CONFIRM_MORE_EVIDENCE"
  | "RESUME"
  | "WITHDRAW"
  | "SUPERSEDE";

export type WebAgentLedgerFollowupAutomationStatus =
  | "WAITING_FOR_PLAN"
  | "WAITING_FOR_REPLAN"
  | "QUEUED"
  | "RUNNING"
  | "VERIFYING"
  | "BLOCKED"
  | "RECOVERY_REQUIRED";

export type WebManagedEvidenceSourceSelection = Readonly<{
  objectType: "EVIDENCE_FILE" | "MATERIAL_OBJECT";
  objectId: string;
}>;

export function webAgentLedgerExceptionCapacityMessage(
  code: unknown,
): string | null {
  if (code === "AGENT_LEDGER_REEXTRACTION_SOURCE_WINDOW_EXCEEDED") {
    return "该异常组关联的来源页超过单次重新提取上限（64页）。请先按材料范围拆分后重新分流，或选择本组允许的其他处置。";
  }
  if (code === "AGENT_LEDGER_REEXTRACTION_COHORT_CAPACITY_EXCEEDED") {
    return "本案已达到重新提取任务的来源范围上限（99组）。请先完成、撤回或替代已有重新提取任务，再重试。";
  }
  return null;
}

export function buildWebAgentLedgerFollowupActionPayload(input: Readonly<{
  expectedVersion: number;
  action: WebAgentLedgerFollowupActionCode;
  reasonNote: string;
  sources: readonly WebManagedEvidenceSourceSelection[];
}>): Readonly<Record<string, unknown>> {
  return {
    expected_version: input.expectedVersion,
    action: input.action,
    reason_note: input.reasonNote,
    managed_evidence_sources: input.sources.map((source) => ({
      object_type: source.objectType,
      object_id: source.objectId,
    })),
  };
}

export function buildWebAgentLedgerRecoveryPayload(
  expectedVersion: number,
): Readonly<Record<string, unknown>> {
  return { expected_version: expectedVersion };
}

export function buildWebAgentLedgerPageQuery(offset: number, limit: number): string {
  if (
    !Number.isInteger(offset)
    || offset < 0
    || offset > Number.MAX_SAFE_INTEGER
    || !Number.isInteger(limit)
    || limit < 1
    || limit > 50
  ) {
    throw new Error("分页参数无效。");
  }
  return `offset=${offset}&limit=${limit}`;
}

export function webAgentLedgerAutomationLabel(
  value: WebAgentLedgerFollowupAutomationStatus | null,
): string {
  if (value === null) return "待完成";
  return ({
    WAITING_FOR_PLAN: "正在安排重新提取",
    WAITING_FOR_REPLAN: "正在等待重新规划",
    QUEUED: "重新提取已排队",
    RUNNING: "正在重新提取",
    VERIFYING: "正在独立核验",
    BLOCKED: "自动任务受阻",
    RECOVERY_REQUIRED: "需要恢复分析控制",
  })[value];
}
