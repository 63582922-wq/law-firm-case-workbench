import { alphaCalculationApiBase } from "@/lib/synthetic-calculation";

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
