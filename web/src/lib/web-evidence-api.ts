"use client";

import { webApiFetch } from "@/lib/web-api-client";
import { WebLawyerApiError } from "@/lib/web-lawyer-api";

const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/i;

export type WebEvidenceDecision = Readonly<{
  decisionId: string;
  disposition: "INCLUDE" | "EXCLUDE";
  reason: string;
  status: string;
}>;

export type WebEvidenceAnnotation = Readonly<{
  annotationId: string;
  purpose: string;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  label: string;
  status: string;
}>;

export type WebEvidencePage = Readonly<{
  evidencePageId: string;
  evidenceFileId: string;
  originalLabel: string;
  pageNumber: number;
  decision: WebEvidenceDecision | null;
  pendingDecision: WebEvidenceDecision | null;
  annotations: readonly WebEvidenceAnnotation[];
}>;

export type WebEvidencePageBatch = Readonly<{
  matterId: string;
  matterVersion: number;
  totalCount: number;
  items: readonly WebEvidencePage[];
  nextCursor: string | null;
  hasMore: boolean;
}>;

export type WebEvidenceSummary = Readonly<{
  matterId: string;
  matterVersion: number;
  canBatchConfirmPageDecisions: boolean;
  totalPages: number;
  unresolvedPageCount: number;
  pendingDecisionCount: number;
  unresolvedDuplicateCount: number;
  manifestReadinessHash: string;
  originalFiles: readonly Readonly<{
    evidenceFileId: string;
    originalLabel: string;
    byteSize: number;
    mediaType: string;
    pageCount: number;
  }>[];
  lockedManifest: Readonly<{ manifestId: string; status: string; totalPages: number; includedPages: number; excludedPages: number }> | null;
  derivatives: readonly Readonly<{ derivativeId: string; artifactType: string; pageCount: number; status: string }>[];
  derivativeRuns: readonly Readonly<{ runId: string; manifestId: string; status: string; attemptCount: number; failureCode: string | null }>[];
}>;

export type WebEvidenceReceipt = Readonly<{
  commandName: string;
  matterId: string;
  matterVersion: number;
  auditEventId: string;
  objectType: string;
  objectId: string;
}>;

export type WebAgentEvidenceCandidateBatch = Readonly<{
  runId: string;
  decisionIds: readonly string[];
  pageIds: readonly string[];
  includeCount: number;
  excludeCount: number;
  excluded: readonly Readonly<{ category: string; count: number; pageIds: readonly string[] }>[];
  status: "CANDIDATE";
  requiresLeadLawyerConfirmation: true;
}>;

export async function readWebEvidenceSummary(caseId: string, signal?: AbortSignal): Promise<WebEvidenceSummary> {
  const response = await webApiFetch(`/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-summary`, { signal });
  const payload = await readJson(response, "读取证据摘要");
  const record = asRecord(payload, "证据摘要响应格式不正确");
  return parseSummary(record.summary);
}

export async function listWebEvidencePages(
  caseId: string,
  options: { expectedVersion?: number; cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
): Promise<WebEvidencePageBatch> {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit ?? 50));
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.expectedVersion !== undefined) params.set("expected_version", String(requiredInteger(options.expectedVersion, "案件版本")));
  const response = await webApiFetch(
    `/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-pages?${params.toString()}`,
    { signal: options.signal },
  );
  const payload = await readJson(response, "读取证据页面");
  return parsePageBatch(payload);
}

export function webEvidencePagePreviewUrl(caseId: string, evidencePageId: string): string {
  const path = `/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-pages/${encodeURIComponent(normalizeId(evidencePageId, "证据页面编号"))}/preview`;
  return process.env.NEXT_PUBLIC_WEB_API_PREFIX === "/api/local/v1" ? `/api/local/v1${path}` : `/api/v1${path}`;
}

export function webEvidenceDerivativeDownloadUrl(caseId: string, derivativeId: string): string {
  const path = `/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-derivatives/${encodeURIComponent(normalizeId(derivativeId, "派生件编号"))}/download`;
  return process.env.NEXT_PUBLIC_WEB_API_PREFIX === "/api/local/v1" ? `/api/local/v1${path}` : `/api/v1${path}`;
}

export async function createWebEvidenceDecisionCandidate(
  caseId: string,
  evidencePageId: string,
  input: { expectedVersion: number; disposition: "INCLUDE" | "EXCLUDE"; reason: string },
): Promise<WebEvidenceReceipt> {
  const response = await webApiFetch(
    `/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-pages/${encodeURIComponent(normalizeId(evidencePageId, "证据页面编号"))}/decisions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebEvidenceIdempotencyKey("decision-candidate") },
      body: JSON.stringify({ expected_version: requiredInteger(input.expectedVersion, "案件版本"), disposition: input.disposition, reason: normalizeReason(input.reason) }),
    },
  );
  return parseReceipt(asRecord(await readJson(response, "保存页面处理候选"), "页面处理候选响应格式不正确").receipt);
}

export async function confirmWebEvidenceDecision(caseId: string, decisionId: string, expectedVersion: number): Promise<WebEvidenceReceipt> {
  const response = await webApiFetch(
    `/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-page-decisions/${encodeURIComponent(normalizeId(decisionId, "页面决定编号"))}/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebEvidenceIdempotencyKey("decision-confirm") },
      body: JSON.stringify({ expected_version: requiredInteger(expectedVersion, "案件版本") }),
    },
  );
  return parseReceipt(asRecord(await readJson(response, "确认页面处理决定"), "页面处理确认响应格式不正确").receipt);
}

export async function confirmWebEvidenceDecisionsBatch(
  caseId: string,
  decisionIds: readonly string[],
  expectedVersion: number,
): Promise<WebEvidenceReceipt> {
  if (!Array.isArray(decisionIds) || decisionIds.length < 1 || decisionIds.length > 100) {
    throw protocolError("批量确认须包含 1 至 100 个待确认决定");
  }
  const normalizedIds = decisionIds.map((item) => normalizeId(item, "页面决定编号"));
  if (new Set(normalizedIds).size !== normalizedIds.length) throw protocolError("批量确认不能包含重复决定");
  const response = await webApiFetch(
    `/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-page-decisions/confirm-batch`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebEvidenceIdempotencyKey("decision-batch-confirm") },
      body: JSON.stringify({
        expected_version: requiredInteger(expectedVersion, "案件版本"),
        decision_ids: [...normalizedIds].sort(),
      }),
    },
  );
  return parseReceipt(asRecord(await readJson(response, "批量确认页面处理决定"), "批量确认响应格式不正确").receipt);
}

export async function stageWebAgentEvidenceDecisionCandidates(
  caseId: string,
  runId: string,
  expectedVersion: number,
): Promise<Readonly<{ receipt: WebEvidenceReceipt; candidateBatch: WebAgentEvidenceCandidateBatch }>> {
  const response = await webApiFetch(
    `/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/agent-runs/${encodeURIComponent(normalizeId(runId, "Agent任务编号"))}/evidence-decision-candidates`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebEvidenceIdempotencyKey("agent-candidate-stage") },
      // The browser intentionally supplies no page selection, disposition,
      // confidence, hash, reason code, threshold, or include/exclude choice.
      body: JSON.stringify({ expected_version: requiredInteger(expectedVersion, "案件版本") }),
    },
  );
  const payload = asRecord(await readJson(response, "采用 Agent 低风险建议"), "Agent 候选转换响应格式不正确");
  return {
    receipt: parseReceipt(payload.receipt),
    candidateBatch: parseAgentEvidenceCandidateBatch(payload.candidate_batch),
  };
}

export async function createWebEvidenceAnnotationCandidate(
  caseId: string,
  evidencePageId: string,
  input: { expectedVersion: number; x0: number; y0: number; x1: number; y1: number; label: string },
): Promise<WebEvidenceReceipt> {
  const response = await webApiFetch(
    `/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-pages/${encodeURIComponent(normalizeId(evidencePageId, "证据页面编号"))}/annotations`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebEvidenceIdempotencyKey("annotation-candidate") },
      body: JSON.stringify({
        expected_version: requiredInteger(input.expectedVersion, "案件版本"),
        x0: checkedCoordinate(input.x0), y0: checkedCoordinate(input.y0),
        x1: checkedCoordinate(input.x1), y1: checkedCoordinate(input.y1),
        label: normalizeReason(input.label),
      }),
    },
  );
  return parseReceipt(asRecord(await readJson(response, "保存红框候选"), "红框候选响应格式不正确").receipt);
}

export async function confirmWebEvidenceAnnotation(caseId: string, annotationId: string, expectedVersion: number): Promise<WebEvidenceReceipt> {
  const response = await webApiFetch(
    `/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-annotations/${encodeURIComponent(normalizeId(annotationId, "红框编号"))}/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebEvidenceIdempotencyKey("annotation-confirm") },
      body: JSON.stringify({ expected_version: requiredInteger(expectedVersion, "案件版本") }),
    },
  );
  return parseReceipt(asRecord(await readJson(response, "确认红框"), "红框确认响应格式不正确").receipt);
}

export async function lockWebEvidenceManifest(caseId: string, expectedVersion: number, readinessHash: string): Promise<WebEvidenceReceipt> {
  if (!SHA256_PATTERN.test(readinessHash)) throw protocolError("证据清单就绪指纹格式不正确");
  const response = await webApiFetch(
    `/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-manifest/lock`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebEvidenceIdempotencyKey("manifest-lock") },
      body: JSON.stringify({ expected_version: requiredInteger(expectedVersion, "案件版本"), readiness_hash: readinessHash.toLowerCase() }),
    },
  );
  return parseReceipt(asRecord(await readJson(response, "锁定证据清单"), "证据清单锁定响应格式不正确").receipt);
}

export async function enqueueWebEvidenceDerivativeRun(caseId: string, expectedVersion: number, manifestId: string): Promise<WebEvidenceReceipt> {
  const response = await webApiFetch(
    `/api/v1/cases/${encodeURIComponent(normalizeId(caseId, "案件编号"))}/evidence-derivative-runs`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebEvidenceIdempotencyKey("derivative-enqueue") },
      body: JSON.stringify({ expected_version: requiredInteger(expectedVersion, "案件版本"), manifest_id: normalizeId(manifestId, "证据清单编号") }),
    },
  );
  return parseReceipt(asRecord(await readJson(response, "请求生成证据 PDF"), "证据 PDF 生成响应格式不正确").receipt);
}

async function readJson(response: Response, operation: string): Promise<unknown> {
  const requestId = response.headers.get("x-request-id");
  if (!response.ok) {
    throw new WebLawyerApiError(messageForStatus(operation, response.status), { status: response.status, requestId });
  }
  if (!(response.headers.get("content-type") ?? "").toLowerCase().includes("application/json")) {
    throw new WebLawyerApiError(`${operation}未返回可核验的 JSON 回执。`, { status: response.status, requestId });
  }
  try {
    return await response.json();
  } catch {
    throw new WebLawyerApiError(`${operation}返回内容无法解析。`, { status: response.status, requestId });
  }
}

function parseSummary(value: unknown): WebEvidenceSummary {
  const record = asRecord(value, "证据摘要格式不正确");
  return {
    matterId: normalizeId(record.matter_id, "案件编号"),
    matterVersion: requiredInteger(record.matter_version, "案件版本"),
    canBatchConfirmPageDecisions: requiredBoolean(record.can_batch_confirm_page_decisions, "批量确认权限"),
    totalPages: nonNegativeInteger(record.total_pages, "总页数"),
    unresolvedPageCount: nonNegativeInteger(record.unresolved_page_count, "未决定页数"),
    pendingDecisionCount: nonNegativeInteger(record.pending_decision_count, "待确认决定数"),
    unresolvedDuplicateCount: nonNegativeInteger(record.unresolved_duplicate_count, "未处理重复组数"),
    manifestReadinessHash: parseSha256(record.manifest_readiness_hash, "证据清单就绪指纹"),
    originalFiles: array(record.original_files).map((item) => {
      const row = asRecord(item, "原始材料格式不正确");
      return {
        evidenceFileId: normalizeId(row.evidence_file_id, "证据文件编号"),
        originalLabel: text(row.original_label, "原始材料名称", 500),
        byteSize: nonNegativeInteger(row.byte_size, "材料大小"),
        mediaType: text(row.media_type, "材料类型", 128),
        pageCount: requiredInteger(row.page_count, "材料页数"),
      };
    }),
    lockedManifest: record.locked_manifest === null || record.locked_manifest === undefined ? null : parseManifest(record.locked_manifest),
    derivatives: array(record.derivatives).map((item) => {
      const row = asRecord(item, "派生件格式不正确");
      return {
        derivativeId: normalizeId(row.derivative_id, "派生件编号"),
        artifactType: text(row.artifact_type, "派生件类型", 80),
        pageCount: requiredInteger(row.page_count, "派生件页数"),
        status: text(row.status, "派生件状态", 80),
      };
    }),
    derivativeRuns: array(record.derivative_runs).map((item) => {
      const row = asRecord(item, "派生任务格式不正确");
      return {
        runId: normalizeId(row.run_id, "派生任务编号"),
        manifestId: normalizeId(row.manifest_id, "证据清单编号"),
        status: text(row.status, "派生任务状态", 80),
        attemptCount: nonNegativeInteger(row.attempt_count, "派生任务尝试次数"),
        failureCode: row.failure_code === null || row.failure_code === undefined ? null : text(row.failure_code, "派生任务失败代码", 80),
      };
    }),
  };
}

function parsePageBatch(value: unknown): WebEvidencePageBatch {
  const record = asRecord(value, "证据页面响应格式不正确");
  const items = array(record.items).map(parsePage);
  return {
    matterId: normalizeId(record.matter_id, "案件编号"),
    matterVersion: requiredInteger(record.matter_version, "案件版本"),
    totalCount: nonNegativeInteger(record.total_count, "证据页面总数"),
    items,
    nextCursor: record.next_cursor === null || record.next_cursor === undefined ? null : text(record.next_cursor, "下一页游标", 1024),
    hasMore: record.has_more === true,
  };
}

function parsePage(value: unknown): WebEvidencePage {
  const record = asRecord(value, "证据页面格式不正确");
  return {
    evidencePageId: normalizeId(record.evidence_page_id, "证据页面编号"),
    evidenceFileId: normalizeId(record.evidence_file_id, "证据文件编号"),
    originalLabel: text(record.original_label, "材料名称", 500),
    pageNumber: requiredInteger(record.page_number, "页码"),
    decision: record.decision === null || record.decision === undefined ? null : parseDecision(record.decision),
    pendingDecision: record.pending_decision === null || record.pending_decision === undefined ? null : parseDecision(record.pending_decision),
    annotations: array(record.annotations).map(parseAnnotation),
  };
}

function parseDecision(value: unknown): WebEvidenceDecision {
  const record = asRecord(value, "页面决定格式不正确");
  const disposition = text(record.disposition, "页面处理方式", 16);
  if (disposition !== "INCLUDE" && disposition !== "EXCLUDE") throw protocolError("页面处理方式不正确");
  return {
    decisionId: normalizeId(record.decision_id, "页面决定编号"),
    disposition,
    reason: text(record.reason, "页面决定理由", 2_000),
    status: text(record.status, "页面决定状态", 80),
  };
}

function parseAnnotation(value: unknown): WebEvidenceAnnotation {
  const record = asRecord(value, "红框格式不正确");
  return {
    annotationId: normalizeId(record.annotation_id, "红框编号"),
    purpose: text(record.purpose, "红框用途", 120),
    x0: checkedCoordinate(record.x0), y0: checkedCoordinate(record.y0),
    x1: checkedCoordinate(record.x1), y1: checkedCoordinate(record.y1),
    label: text(record.label, "红框说明", 500),
    status: text(record.status, "红框状态", 80),
  };
}

function parseManifest(value: unknown): WebEvidenceSummary["lockedManifest"] {
  const record = asRecord(value, "锁定清单格式不正确");
  return {
    manifestId: normalizeId(record.manifest_id, "锁定清单编号"),
    status: text(record.status, "锁定清单状态", 80),
    totalPages: nonNegativeInteger(record.total_pages, "清单总页数"),
    includedPages: nonNegativeInteger(record.included_pages, "纳入页数"),
    excludedPages: nonNegativeInteger(record.excluded_pages, "排除页数"),
  };
}

function parseReceipt(value: unknown): WebEvidenceReceipt {
  const record = asRecord(value, "证据操作回执格式不正确");
  return {
    commandName: text(record.command_name, "操作名称", 128),
    matterId: normalizeId(record.matter_id, "案件编号"),
    matterVersion: requiredInteger(record.matter_version, "案件版本"),
    auditEventId: normalizeId(record.audit_event_id, "审计编号"),
    objectType: text(record.object_type, "对象类型", 128),
    objectId: normalizeId(record.object_id, "对象编号"),
  };
}

function parseAgentEvidenceCandidateBatch(value: unknown): WebAgentEvidenceCandidateBatch {
  const record = asRecord(value, "Agent 证据候选批次格式不正确");
  const status = text(record.status, "Agent 证据候选状态", 32);
  if (status !== "CANDIDATE" || record.requires_lead_lawyer_confirmation !== true) {
    throw protocolError("Agent 证据建议不能绕过主办律师确认");
  }
  const decisionIds = array(record.decision_ids).map((item) => normalizeId(item, "页面决定编号"));
  const pageIds = array(record.page_ids).map((item) => normalizeId(item, "证据页面编号"));
  if (decisionIds.length !== pageIds.length || new Set(decisionIds).size !== decisionIds.length || new Set(pageIds).size !== pageIds.length) {
    throw protocolError("Agent 证据候选批次绑定不正确");
  }
  const includeCount = nonNegativeInteger(record.include_count, "建议纳入页数");
  const excludeCount = nonNegativeInteger(record.exclude_count, "建议排除页数");
  if (includeCount + excludeCount !== decisionIds.length) throw protocolError("Agent 证据候选计数不正确");
  return {
    runId: normalizeId(record.run_id, "Agent任务编号"),
    decisionIds,
    pageIds,
    includeCount,
    excludeCount,
    excluded: array(record.excluded).map((item) => {
      const row = asRecord(item, "Agent 例外分组格式不正确");
      const excludedPageIds = array(row.page_ids).map((pageId) => normalizeId(pageId, "例外页面编号"));
      const count = nonNegativeInteger(row.count, "例外页数");
      if (count !== excludedPageIds.length) throw protocolError("Agent 例外页数不正确");
      return { category: text(row.category, "例外类型", 80), count, pageIds: excludedPageIds };
    }),
    status: "CANDIDATE",
    requiresLeadLawyerConfirmation: true,
  };
}

function createWebEvidenceIdempotencyKey(prefix: string): string {
  const suffix = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
  return `${prefix}-${suffix}`.slice(0, 128);
}

function normalizeId(value: unknown, label: string): string {
  if (typeof value !== "string" || !ID_PATTERN.test(value)) throw protocolError(`${label}格式不正确`);
  return value;
}

function text(value: unknown, label: string, maxLength: number): string {
  if (typeof value !== "string") throw protocolError(`${label}格式不正确`);
  const normalized = value.trim();
  if (!normalized || normalized.length > maxLength || /[\u0000-\u001f\u007f]/.test(normalized)) throw protocolError(`${label}格式不正确`);
  return normalized;
}

function requiredInteger(value: unknown, label: string): number {
  if (!Number.isInteger(value) || (value as number) < 1 || (value as number) > Number.MAX_SAFE_INTEGER) throw protocolError(`${label}格式不正确`);
  return value as number;
}

function nonNegativeInteger(value: unknown, label: string): number {
  if (!Number.isInteger(value) || (value as number) < 0 || (value as number) > Number.MAX_SAFE_INTEGER) throw protocolError(`${label}格式不正确`);
  return value as number;
}

function requiredBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw protocolError(`${label}格式不正确`);
  return value;
}

function checkedCoordinate(value: unknown): number {
  const numeric = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  if (!Number.isFinite(numeric) || numeric < 0 || numeric > 1) throw protocolError("红框坐标格式不正确");
  return numeric;
}

function normalizeReason(value: string): string {
  return text(value, "说明", 2_000);
}

function parseSha256(value: unknown, label: string): string {
  const normalized = text(value, label, 64).toLowerCase();
  if (!SHA256_PATTERN.test(normalized)) throw protocolError(`${label}格式不正确`);
  return normalized;
}

function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw protocolError("列表格式不正确");
  return value;
}

function asRecord(value: unknown, message: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw protocolError(message);
  return value as Record<string, unknown>;
}

function protocolError(message: string): WebLawyerApiError {
  return new WebLawyerApiError(message, { status: null, requestId: null });
}

function messageForStatus(operation: string, status: number): string {
  if (status === 401 || status === 403) return "登录状态或案件权限已失效。";
  if (status === 409) return `${operation}时案件状态已变化；请刷新后再继续。`;
  if (status === 422) return `${operation}未被服务端接受；请核对页面状态与当前案件版本。`;
  return `${operation}暂未完成。`;
}
