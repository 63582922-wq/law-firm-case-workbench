import { alphaCalculationApiBase } from "@/lib/synthetic-calculation";
import { syntheticMatter } from "@/lib/synthetic-matter";

export type CaseDataSourceConfig =
  | { kind: "synthetic-alpha"; label: "本机合成数据" }
  | { kind: "persistent-preview"; label: "持久化内部预览"; apiBase: string; matterId: string }
  | { kind: "persistent-disabled"; label: "持久化模式未启用"; reason: string };

export type CaseReviewView = {
  sourceKind: "synthetic-alpha" | "persistent-preview";
  sourceLabel: string;
  matterTitle: string | null;
  matterVersion: number | null;
  snapshotHash: string;
  transactionSnapshotHash: string | null;
  requestId: string | null;
  facts: { factId: string; text: string; origin: string; status: string; evidenceCount: number }[];
  claims: { claimId: string; text: string; amount: string | null; currency: string | null; position: string; responseAmount: string | null }[];
  issues: { issueId: string; question: string; claimCount: number; factCount: number; status: string }[];
  transactions: { transactionId: string; date: string | null; amount: string; currency: string; nature: string; application: string; status: string }[];
  pendingFacts: { factId: string; text: string; origin: string; evidenceCount: number }[];
};

export type EvidenceReviewPage = {
  pageId: string;
  fileId: string;
  originalLabel: string;
  pageNumber: number;
  decisionId: string | null;
  disposition: "INCLUDE" | "EXCLUDE" | null;
  reason: string | null;
  annotations: { annotationId: string; x0: number; y0: number; x1: number; y1: number; label: string; status: string }[];
  syntheticPreview: { date: string; amount: string; counterpart: string; confidence: string; note: string } | null;
};

export type EvidenceReviewView = {
  sourceKind: "synthetic-alpha" | "persistent-preview";
  sourceLabel: string;
  matterVersion: number | null;
  snapshotHash: string;
  requestId: string | null;
  originals: { fileId: string; originalLabel: string; originalFileSha256: string; pageCount: number }[];
  pages: EvidenceReviewPage[];
  duplicateGroups: { groupId: string; status: string; canonicalPageId: string | null; pageIds: string[] }[];
  lockedManifest: { manifestId: string; contentHash: string; totalPages: number; includedPages: number; excludedPages: number } | null;
  derivatives: { derivativeId: string; artifactType: string; artifactSha256: string; pageCount: number; status: string }[];
  derivativeRuns: { runId: string; manifestId: string; status: string; attemptCount: number; failureCode: string | null }[];
};

export type EvidenceDerivative = EvidenceReviewView["derivatives"][number];

export type EvidenceDerivativeDelivery = {
  blob: Blob;
  fileName: string;
  artifactSha256: string;
};

export type EvidenceDerivativeRunReceipt = {
  runId: string;
  matterVersion: number;
  requestId: string | null;
};

type SyntheticReview = {
  mode: "synthetic-alpha-only";
  fact_snapshot_hash: string;
  transaction_snapshot_hash: string;
  facts: { fact_id: string; original_text: string; origin: string; evidence_count: number }[];
  claims: { claim_id: string; original_claim_text: string; claimed_amount: string | null; currency: string | null; response_position: string; response_amount: string | null }[];
  issues: { issue_id: string; question: string; claim_count: number; fact_count: number }[];
  transactions: { event_id: string; effective_date: string; kind: string; amount: string; currency: string; payment_application: string; evidence_ids: string[] }[];
  pending_facts: { fact_id: string; original_text: string; origin: string; evidence_count: number }[];
};

type PersistentSnapshot = {
  matter_id: string;
  title: string;
  stage: string;
  version: number;
  snapshot_hash: string;
  facts: { fact_id: string; original_text: string; origin: string; status: string; evidence_count: number }[];
  claims: { claim_id: string; original_claim_text: string; claimed_amount: string | null; currency: string | null; status: string; response: { position: string; partial_amount: string | null } | null }[];
  issues: { issue_id: string; question: string; status: string; claim_ids: string[]; confirmed_fact_ids: string[] }[];
  transactions: { transaction_id: string; local_date: string | null; amount: string; currency: string; status: string }[];
  payment_classifications: { transaction_id: string; nature: string; status: string }[];
  duplicate_groups: { duplicate_group_id: string; status: string; transaction_ids: string[]; canonical_transaction_id: string | null }[];
};

type PersistentEvidenceSnapshot = {
  matter_id: string;
  version: number;
  snapshot_hash: string;
  original_files: { evidence_file_id: string; original_label: string; original_file_sha256: string; page_count: number }[];
  pages: {
    evidence_page_id: string;
    evidence_file_id: string;
    page_number: number;
    decision: { decision_id: string; disposition: "INCLUDE" | "EXCLUDE"; reason: string } | null;
    annotations: { annotation_id: string; x0: string; y0: string; x1: string; y1: string; label: string; status: string }[];
  }[];
  duplicate_groups: { duplicate_group_id: string; status: string; canonical_page_id: string | null; evidence_page_ids: string[] }[];
  locked_manifest: { manifest_id: string; content_hash: string; total_pages: number; included_pages: number; excluded_pages: number } | null;
  derivatives: { derivative_id: string; artifact_type: string; artifact_sha256: string; page_count: number; status: string }[];
  derivative_runs: { run_id: string; manifest_id: string; status: string; attempt_count: number; failure_code: string | null }[];
};

type ErrorEnvelope = { code?: string; message?: string; request_id?: string; detail?: string };

export const caseDataSourceConfig = resolveCaseDataSourceConfig({
  mode: process.env.NEXT_PUBLIC_CASE_DATA_SOURCE,
  apiBase: process.env.NEXT_PUBLIC_PERSISTENT_CASE_API_BASE,
  matterId: process.env.NEXT_PUBLIC_PERSISTENT_MATTER_ID,
});

export function resolveCaseDataSourceConfig(input: { mode?: string; apiBase?: string; matterId?: string }): CaseDataSourceConfig {
  const mode = input.mode?.trim() || "synthetic-alpha";
  if (mode === "synthetic-alpha") return { kind: "synthetic-alpha", label: "本机合成数据" };
  if (mode !== "persistent-preview") {
    return { kind: "persistent-disabled", label: "持久化模式未启用", reason: "数据源模式无法识别，已停止读取。" };
  }
  const apiBase = input.apiBase?.trim().replace(/\/$/, "") || "";
  const matterId = input.matterId?.trim() || "";
  if (!apiBase || !matterId) {
    return { kind: "persistent-disabled", label: "持久化模式未启用", reason: "缺少持久化服务地址或案件标识，未回退到合成数据。" };
  }
  if (!isAllowedPreviewOrigin(apiBase)) {
    return { kind: "persistent-disabled", label: "持久化模式未启用", reason: "持久化服务地址不符合本机或 HTTPS 安全边界。" };
  }
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(matterId)) {
    return { kind: "persistent-disabled", label: "持久化模式未启用", reason: "案件标识不是有效 UUID，已停止读取。" };
  }
  return { kind: "persistent-preview", label: "持久化内部预览", apiBase, matterId };
}

export async function loadCaseReview(config: CaseDataSourceConfig = caseDataSourceConfig): Promise<CaseReviewView> {
  if (config.kind === "persistent-disabled") throw new Error(config.reason);
  if (config.kind === "synthetic-alpha") {
    const response = await fetch(`${alphaCalculationApiBase}/v1/alpha-review`, {
      headers: { "X-Alpha-Actor": "alpha_lead_lawyer" },
    });
    const payload = (await response.json()) as SyntheticReview | ErrorEnvelope;
    if (!response.ok || !("facts" in payload)) throw new Error(errorMessage(payload as ErrorEnvelope, "本机合成台账快照不可用"));
    return mapSyntheticReview(payload, response.headers.get("X-Request-ID"));
  }
  const response = await fetch(`${config.apiBase}/v1/matters/${config.matterId}/snapshot`, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  const payload = (await response.json()) as PersistentSnapshot | ErrorEnvelope;
  if (!response.ok || !("facts" in payload)) throw new Error(errorMessage(payload as ErrorEnvelope, "持久化案件快照不可用"));
  return mapPersistentSnapshot(payload, response.headers.get("X-Request-ID"));
}

export async function loadEvidenceReview(config: CaseDataSourceConfig = caseDataSourceConfig): Promise<EvidenceReviewView> {
  if (config.kind === "persistent-disabled") throw new Error(config.reason);
  if (config.kind === "synthetic-alpha") return mapSyntheticEvidence();
  const response = await fetch(`${config.apiBase}/v1/matters/${config.matterId}/evidence-snapshot`, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  const payload = (await response.json()) as PersistentEvidenceSnapshot | ErrorEnvelope;
  if (!response.ok || !("pages" in payload)) throw new Error(errorMessage(payload as ErrorEnvelope, "持久化证据快照不可用"));
  return mapPersistentEvidence(payload, response.headers.get("X-Request-ID"));
}

export async function fetchEvidenceDerivative(
  derivative: EvidenceDerivative,
  purpose: "INLINE_PREVIEW" | "DOWNLOAD",
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceDerivativeDelivery> {
  if (config.kind !== "persistent-preview") {
    throw new Error("只有已启用的本机持久化工作台可以读取证据派生件。");
  }
  if (derivative.status !== "VERIFIED") {
    throw new Error("该证据派生件尚未完成完整性核验。");
  }
  const accessResponse = await fetch(
    `${config.apiBase}/v1/matters/${config.matterId}/evidence-derivatives/${derivative.derivativeId}/access`,
    {
      method: "POST",
      credentials: "include",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ purpose }),
    },
  );
  const accessPayload = (await accessResponse.json()) as
    | { access_token: string; derivative_id: string; expires_at: string }
    | ErrorEnvelope;
  if (!accessResponse.ok || !("access_token" in accessPayload)) {
    throw new Error(errorMessage(accessPayload as ErrorEnvelope, "无法取得证据派生件的短时读取许可"));
  }
  if (accessPayload.derivative_id !== derivative.derivativeId) {
    throw new Error("证据派生件读取许可与当前记录不一致，已停止读取。");
  }
  const contentResponse = await fetch(
    `${config.apiBase}/v1/matters/${config.matterId}/evidence-derivatives/${derivative.derivativeId}/content`,
    {
      credentials: "include",
      headers: { Accept: "application/pdf", Authorization: `Bearer ${accessPayload.access_token}` },
      cache: "no-store",
    },
  );
  if (!contentResponse.ok) {
    const payload = (await contentResponse.json().catch(() => ({}))) as ErrorEnvelope;
    throw new Error(errorMessage(payload, "证据派生件读取失败"));
  }
  if (contentResponse.headers.get("Content-Type")?.split(";", 1)[0] !== "application/pdf") {
    throw new Error("证据派生件返回了非 PDF 内容，已停止读取。");
  }
  const returnedHash = contentResponse.headers.get("X-Artifact-SHA256");
  if (returnedHash !== derivative.artifactSha256) {
    throw new Error("证据派生件的服务端哈希与当前快照不一致，已停止读取。");
  }
  const contentLength = Number(contentResponse.headers.get("Content-Length") || "0");
  if (contentLength > 256 * 1024 * 1024) {
    throw new Error("证据派生件超过本机预览的大小上限。");
  }
  const blob = await contentResponse.blob();
  if (blob.size < 1 || blob.size > 256 * 1024 * 1024) {
    throw new Error("证据派生件为空或超过本机预览的大小上限。");
  }
  return {
    blob,
    fileName: derivative.artifactType === "ANNOTATED_RELATED_PAGES_PDF" ? "related-pages-red-box.pdf" : "related-pages.pdf",
    artifactSha256: returnedHash,
  };
}

export async function enqueueEvidenceDerivativeRun(
  review: EvidenceReviewView,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceDerivativeRunReceipt> {
  if (config.kind !== "persistent-preview") {
    throw new Error("只有持久化工作台可以建立正式证据派生任务。");
  }
  if (!review.lockedManifest || review.matterVersion === null) {
    throw new Error("必须先完成全部页级处置并锁定当前证据 Manifest。");
  }
  const approvalBinding = [
    "evidence-derivative-run-approval-v1",
    config.matterId,
    review.lockedManifest.manifestId,
    review.lockedManifest.contentHash,
    String(review.matterVersion),
  ].join("|");
  const approvalHash = await sha256Text(approvalBinding);
  let response: Response;
  try {
    response = await fetch(
      `${config.apiBase}/v1/matters/${config.matterId}/evidence-manifests/${review.lockedManifest.manifestId}/derivative-runs`,
      {
        method: "POST",
        credentials: "include",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          expected_version: review.matterVersion,
          manifest_content_hash: review.lockedManifest.contentHash,
          approval_hash: approvalHash,
        }),
      },
    );
  } catch {
    throw new Error("连接在任务确认前中断。请先刷新案件状态；系统不会盲目重试或重复生成。");
  }
  const payload = (await response.json()) as
    | { object_id: string; matter_version: number; object_type: string }
    | ErrorEnvelope;
  if (!response.ok || !("object_id" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "证据派生任务未建立"));
  }
  if (payload.object_type !== "EVIDENCE_DERIVATIVE_RUN") {
    throw new Error("任务回执类型不一致，已停止后续处理。");
  }
  return {
    runId: payload.object_id,
    matterVersion: payload.matter_version,
    requestId: response.headers.get("X-Request-ID"),
  };
}

export async function confirmSyntheticFact(factId: string): Promise<CaseReviewView> {
  if (caseDataSourceConfig.kind !== "synthetic-alpha") throw new Error("持久化预览中的确认必须通过案件版本化命令完成。 ");
  const response = await fetch(`${alphaCalculationApiBase}/v1/alpha-review/facts/${factId}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Alpha-Actor": "alpha_lead_lawyer" },
    body: JSON.stringify({ approval_hash: "alpha-ui-fact-confirmation" }),
  });
  const payload = (await response.json()) as SyntheticReview | ErrorEnvelope;
  if (!response.ok || !("facts" in payload)) throw new Error(errorMessage(payload as ErrorEnvelope, "确认未完成"));
  return mapSyntheticReview(payload, response.headers.get("X-Request-ID"));
}

function mapSyntheticReview(payload: SyntheticReview, requestId: string | null): CaseReviewView {
  return {
    sourceKind: "synthetic-alpha",
    sourceLabel: "本机合成数据",
    matterTitle: null,
    matterVersion: null,
    snapshotHash: payload.fact_snapshot_hash,
    transactionSnapshotHash: payload.transaction_snapshot_hash,
    requestId,
    facts: payload.facts.map((item) => ({ factId: item.fact_id, text: item.original_text, origin: item.origin, status: "CONFIRMED", evidenceCount: item.evidence_count })),
    claims: payload.claims.map((item) => ({ claimId: item.claim_id, text: item.original_claim_text, amount: item.claimed_amount, currency: item.currency, position: item.response_position, responseAmount: item.response_amount })),
    issues: payload.issues.map((item) => ({ issueId: item.issue_id, question: item.question, claimCount: item.claim_count, factCount: item.fact_count, status: "CONFIRMED" })),
    transactions: payload.transactions.map((item) => ({ transactionId: item.event_id, date: item.effective_date, amount: item.amount, currency: item.currency, nature: item.kind, application: item.payment_application, status: "APPROVED" })),
    pendingFacts: payload.pending_facts.map((item) => ({ factId: item.fact_id, text: item.original_text, origin: item.origin, evidenceCount: item.evidence_count })),
  };
}

function mapPersistentSnapshot(payload: PersistentSnapshot, requestId: string | null): CaseReviewView {
  const classifications = new Map(payload.payment_classifications.map((item) => [item.transaction_id, item]));
  return {
    sourceKind: "persistent-preview",
    sourceLabel: "持久化内部预览",
    matterTitle: payload.title,
    matterVersion: payload.version,
    snapshotHash: payload.snapshot_hash,
    transactionSnapshotHash: null,
    requestId,
    facts: payload.facts.filter((item) => item.status !== "CANDIDATE").map((item) => ({ factId: item.fact_id, text: item.original_text, origin: item.origin, status: item.status, evidenceCount: item.evidence_count })),
    claims: payload.claims.map((item) => ({ claimId: item.claim_id, text: item.original_claim_text, amount: item.claimed_amount, currency: item.currency, position: item.response?.position ?? "待律师回应", responseAmount: item.response?.partial_amount ?? null })),
    issues: payload.issues.map((item) => ({ issueId: item.issue_id, question: item.question, claimCount: item.claim_ids.length, factCount: item.confirmed_fact_ids.length, status: item.status })),
    transactions: payload.transactions.map((item) => {
      const classification = classifications.get(item.transaction_id);
      return { transactionId: item.transaction_id, date: item.local_date, amount: item.amount, currency: item.currency, nature: classification?.nature ?? "待分类", application: paymentApplication(classification?.nature), status: classification?.status ?? item.status };
    }),
    pendingFacts: payload.facts.filter((item) => item.status === "CANDIDATE").map((item) => ({ factId: item.fact_id, text: item.original_text, origin: item.origin, evidenceCount: item.evidence_count })),
  };
}

function mapSyntheticEvidence(): EvidenceReviewView {
  return {
    sourceKind: "synthetic-alpha",
    sourceLabel: "本机合成数据",
    matterVersion: null,
    snapshotHash: "synthetic-evidence-manifest-preview",
    requestId: null,
    originals: [{ fileId: "synthetic-wechat-ledger", originalLabel: "[合成] 微信交易记录.pdf", originalFileSha256: "synthetic-only", pageCount: syntheticMatter.evidence.length }],
    pages: syntheticMatter.evidence.map((item) => ({
      pageId: `synthetic-page-${item.page}`,
      fileId: "synthetic-wechat-ledger",
      originalLabel: "[合成] 微信交易记录.pdf",
      pageNumber: item.page,
      decisionId: item.confidence === "已核验" ? `synthetic-decision-${item.page}` : null,
      disposition: item.confidence === "已核验" ? "INCLUDE" : null,
      reason: item.confidence === "已核验" ? "[合成] 与目标主体相关。" : null,
      annotations: item.confidence === "已核验" ? [{ annotationId: `synthetic-annotation-${item.page}`, x0: 0.08, y0: 0.32, x1: 0.92, y1: 0.52, label: "[合成] 相关交易行", status: "APPROVED" }] : [],
      syntheticPreview: { date: item.date, amount: item.amount, counterpart: item.counterpart, confidence: item.confidence, note: item.note },
    })),
    duplicateGroups: [{ groupId: "synthetic-duplicate-17-18", status: "CANDIDATE", canonicalPageId: null, pageIds: ["synthetic-page-17", "synthetic-page-18"] }],
    lockedManifest: null,
    derivatives: [],
    derivativeRuns: [],
  };
}

function mapPersistentEvidence(payload: PersistentEvidenceSnapshot, requestId: string | null): EvidenceReviewView {
  const originals = new Map(payload.original_files.map((item) => [item.evidence_file_id, item]));
  return {
    sourceKind: "persistent-preview",
    sourceLabel: "持久化内部预览",
    matterVersion: payload.version,
    snapshotHash: payload.snapshot_hash,
    requestId,
    originals: payload.original_files.map((item) => ({ fileId: item.evidence_file_id, originalLabel: item.original_label, originalFileSha256: item.original_file_sha256, pageCount: item.page_count })),
    pages: payload.pages.map((item) => ({
      pageId: item.evidence_page_id,
      fileId: item.evidence_file_id,
      originalLabel: originals.get(item.evidence_file_id)?.original_label ?? "原始文件",
      pageNumber: item.page_number,
      decisionId: item.decision?.decision_id ?? null,
      disposition: item.decision?.disposition ?? null,
      reason: item.decision?.reason ?? null,
      annotations: item.annotations.map((annotation) => ({ annotationId: annotation.annotation_id, x0: Number(annotation.x0), y0: Number(annotation.y0), x1: Number(annotation.x1), y1: Number(annotation.y1), label: annotation.label, status: annotation.status })),
      syntheticPreview: null,
    })),
    duplicateGroups: payload.duplicate_groups.map((item) => ({ groupId: item.duplicate_group_id, status: item.status, canonicalPageId: item.canonical_page_id, pageIds: item.evidence_page_ids })),
    lockedManifest: payload.locked_manifest ? { manifestId: payload.locked_manifest.manifest_id, contentHash: payload.locked_manifest.content_hash, totalPages: payload.locked_manifest.total_pages, includedPages: payload.locked_manifest.included_pages, excludedPages: payload.locked_manifest.excluded_pages } : null,
    derivatives: payload.derivatives.map((item) => ({ derivativeId: item.derivative_id, artifactType: item.artifact_type, artifactSha256: item.artifact_sha256, pageCount: item.page_count, status: item.status })),
    derivativeRuns: payload.derivative_runs.map((item) => ({ runId: item.run_id, manifestId: item.manifest_id, status: item.status, attemptCount: item.attempt_count, failureCode: item.failure_code })),
  };
}

function paymentApplication(nature?: string): string {
  if (nature === "INTEREST_PAYMENT") return "仅付利息";
  if (nature === "PRINCIPAL_REPAYMENT") return "仅还本金";
  if (nature === "DISBURSEMENT" || nature === "REPAYMENT_UNSPECIFIED") return "按已批准顺序";
  return "不进入计算";
}

function errorMessage(payload: ErrorEnvelope, fallback: string): string {
  const message = payload.message || payload.detail || fallback;
  return payload.request_id ? `${message}（请求号 ${payload.request_id}）` : message;
}

function isAllowedPreviewOrigin(value: string): boolean {
  try {
    const url = new URL(value);
    if (url.protocol === "https:") return true;
    return url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  } catch {
    return false;
  }
}

async function sha256Text(value: string): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error("当前浏览器无法建立审批输入哈希。");
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
