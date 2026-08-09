import {
  alphaCalculationApiBase,
  alphaCalculationPreviewRequest,
  type CalculationPreview,
} from "@/lib/synthetic-calculation";
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

export type CalculationReviewView = {
  sourceKind: "synthetic-alpha" | "persistent-preview";
  sourceLabel: string;
  status: "ready" | "empty";
  emptyReason: string | null;
  matterVersion: number | null;
  snapshotHash: string | null;
  requestId: string | null;
  obligationId: string | null;
  startDate: string | null;
  endDate: string | null;
  currency: "CNY";
  allocationPolicy: string | null;
  legalBundleId: string | null;
  legalBundleHash: string | null;
  approvalHash: string | null;
  engineVersion: string | null;
  independentCheckMatch: boolean;
  totalInterestAccrued: string | null;
  totalInterestPaid: string | null;
  remainingPrincipal: string | null;
  remainingUnpaidInterest: string | null;
  unappliedPayments: string | null;
  lineItems: {
    lineSequence: number;
    periodStart: string;
    periodEnd: string;
    openingPrincipal: string;
    annualRate: string;
    dayCount: number;
    accruedInterest: string;
    closingPrincipal: string;
    accruedUnpaidInterest: string;
    ruleSegmentId: string;
    sourceRuleVersion: string;
    evidenceIds: string[];
  }[];
  paymentAllocations: {
    allocationSequence: number;
    paymentEventId: string;
    effectiveDate: string;
    paymentAmount: string;
    allocatedInterest: string;
    allocatedPrincipal: string;
    unappliedAmount: string;
    paymentApplication: string;
    evidenceIds: string[];
  }[];
};

export type LegalReviewView = {
  sourceKind: "synthetic-alpha" | "persistent-preview";
  sourceLabel: string;
  status: "discovery-only" | "reviewable";
  statusReason: string;
  matterVersion: number | null;
  snapshotHash: string | null;
  requestId: string | null;
  sources: {
    snapshotId: string | null;
    sourceId: string;
    publisher: string;
    authorityLevel: string;
    officialUrl: string;
    provisionLocator: string;
    verificationStatus: string;
    licenseStatus: string;
    contentSha256: string | null;
  }[];
  ruleVersions: {
    ruleVersionId: string;
    ruleVersion: string;
    issueKey: string;
    triggerEventKind: string;
    formulaKind: string;
    parameterSourceSnapshotId: string | null;
    parameterEvidenceLocator: string | null;
    baseAnnualRate: string | null;
    rateMultiplier: string | null;
    derivedAnnualRate: string;
    requiredFactKeys: string[];
    status: string;
  }[];
  legalEvents: { legalEventId: string; eventKind: string; localDate: string; status: string; evidenceIds: string[] }[];
  factBindings: { bindingId: string; factKey: string; factId: string; status: string; approvalHash: string }[];
  currentBundle: { bundleId: string; version: number; bundleHash: string; approvalHash: string } | null;
  bundleSegments: {
    segmentId: string;
    issueKey: string;
    ruleVersionId: string;
    sourceSnapshotId: string;
    parameterSourceSnapshotId: string | null;
    parameterEvidenceLocator: string | null;
    startDate: string;
    endDate: string;
    annualRate: string;
    applicabilityAnchor: string;
  }[];
};

export type OfficialSourceCaptureView = {
  sourceKind: "synthetic-alpha" | "persistent-preview";
  sourceLabel: string;
  status: "probe-only" | "persistent";
  statusReason: string;
  matterVersion: number | null;
  snapshotHash: string | null;
  requestId: string | null;
  runs: {
    runId: string;
    sourceId: string;
    publisher: string;
    sourceTier: string;
    targetUrl: string;
    status: string;
    attemptCount: number;
    authorizedAt: string | null;
    authorizationExpiresAt: string | null;
    finalUrl: string | null;
    retrievedAt: string | null;
    peerIp: string | null;
    contentMediaType: string | null;
    contentSha256: string | null;
    contentBytes: number | null;
    captureVerificationHash: string | null;
    parserKind: string | null;
    parsedOutputHash: string | null;
    parsedSummary: Record<string, unknown> | null;
    failureCode: string | null;
    completedAt: string | null;
    staleReason: string | null;
  }[];
  reviews: {
    reviewId: string;
    runId: string;
    decision: string;
    provisionLocator: string;
    reviewHash: string;
    reviewedBy: string;
    reviewedAt: string;
  }[];
};

export type OfficialSourceCaptureReceipt = {
  objectId: string;
  matterVersion: number;
  requestId: string | null;
};

export type SubmissionReviewView = {
  sourceKind: "synthetic-alpha" | "persistent-preview";
  sourceLabel: string;
  status: "blocked" | "reviewable" | "locked" | "exported";
  statusReason: string;
  matterVersion: number | null;
  stage: string | null;
  snapshotHash: string | null;
  requestId: string | null;
  workProducts: {
    workProductId: string;
    documentKind: string;
    audience: string;
    mediaType: string;
    artifactSha256: string;
    byteSize: number;
    pageCount: number | null;
    status: string;
    approvalHash: string | null;
  }[];
  bundles: {
    bundleId: string;
    lifecycle: string;
    validity: string;
    currency: string;
    inputHash: string;
    evidenceManifestHash: string;
    legalBundleHash: string;
    calculationOutputHash: string;
    finalTextHash: string;
    qaHash: string;
  }[];
  currentBundleId: string | null;
  currentComponents: {
    workProductId: string;
    sequence: number;
    documentKind: string;
    courtFilename: string;
    mediaType: string;
    artifactSha256: string;
    byteSize: number;
    approvalHash: string;
  }[];
  currentExport: {
    exportId: string;
    courtZipSha256: string;
    courtZipBytes: number;
    internalManifestSha256: string;
    componentCount: number;
    verificationHash: string;
    verifiedAt: string;
  } | null;
};

export type SubmissionExportDelivery = {
  blob: Blob;
  fileName: "法院提交材料.zip";
  artifactSha256: string;
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

type PersistentFormalCalculationSnapshot = {
  matter_id: string;
  matter_version: number;
  snapshot_hash: string;
  scenario: {
    scenario_id: string;
    obligation_id: string;
    version: number;
    start_date: string;
    end_date: string;
    currency: "CNY";
    allocation_policy: string;
    legal_bundle_id: string;
    legal_bundle_hash: string;
    approval_hash: string;
  } | null;
  run: {
    engine_version: string;
    legal_bundle_id: string;
    legal_bundle_hash: string;
    independent_check_hash: string;
    total_interest_accrued: string;
    total_interest_paid: string;
    remaining_principal: string;
    remaining_unpaid_interest: string;
    unapplied_payments: string;
    line_items: {
      line_sequence: number;
      period_start: string;
      period_end: string;
      opening_principal: string;
      annual_rate: string;
      day_count: number;
      accrued_interest: string;
      closing_principal: string;
      accrued_unpaid_interest: string;
      rule_segment_id: string;
      source_rule_version: string;
      evidence_ids: string[];
    }[];
    payment_allocations: {
      allocation_sequence: number;
      payment_event_id: string;
      effective_date: string;
      payment_amount: string;
      allocated_interest: string;
      allocated_principal: string;
      unapplied_amount: string;
      payment_application: string;
      evidence_ids: string[];
    }[];
  } | null;
};

type PersistentLegalReviewSnapshot = {
  matter_id: string;
  matter_version: number;
  snapshot_hash: string;
  sources: {
    snapshot_id: string;
    source_id: string;
    publisher: string;
    authority_level: string;
    official_url: string;
    provision_locator: string;
    verification_status: string;
    license_status: string;
    content_sha256: string;
  }[];
  rule_versions: {
    rule_version_id: string;
    rule_version: string;
    issue_key: string;
    trigger_event_kind: string;
    formula_kind: string;
    parameter_source_snapshot_id: string | null;
    parameter_evidence_locator: string | null;
    base_annual_rate: string | null;
    rate_multiplier: string | null;
    derived_annual_rate: string;
    required_fact_keys: string[];
    status: string;
  }[];
  legal_events: {
    legal_event_id: string;
    event_kind: string;
    local_date: string;
    status: string;
    evidence_ids: string[];
  }[];
  fact_bindings: {
    binding_id: string;
    fact_key: string;
    fact_id: string;
    status: string;
    approval_hash: string;
  }[];
  current_bundle: { bundle_id: string; version: number; bundle_hash: string; approval_hash: string } | null;
  bundle_segments: {
    segment_id: string;
    issue_key: string;
    rule_version_id: string;
    source_snapshot_id: string;
    parameter_source_snapshot_id: string | null;
    parameter_evidence_locator: string | null;
    start_date: string;
    end_date: string;
    annual_rate: string;
    applicability_anchor: string;
  }[];
};

type PersistentOfficialSourceCaptureSnapshot = {
  matter_id: string;
  matter_version: number;
  snapshot_hash: string;
  runs: {
    run_id: string;
    source_id: string;
    publisher?: string;
    source_tier?: string;
    target_url?: string;
    status: string;
    attempt_count?: number;
    authorized_at?: string | null;
    authorization_expires_at?: string | null;
    final_url?: string | null;
    retrieved_at?: string | null;
    peer_ip?: string | null;
    content_media_type?: string | null;
    content_sha256?: string | null;
    content_bytes?: number | null;
    capture_verification_hash?: string | null;
    parser_kind?: string | null;
    parsed_output_hash?: string | null;
    parsed_summary?: Record<string, unknown> | null;
    failure_code?: string | null;
    completed_at?: string | null;
    stale_reason?: string | null;
  }[];
  reviews: {
    review_id: string;
    run_id: string;
    decision: string;
    provision_locator: string;
    review_hash: string;
    reviewed_by: string;
    reviewed_at: string;
  }[];
};

type PersistentSubmissionSnapshot = {
  matter_id: string;
  matter_version: number;
  stage: string;
  snapshot_hash: string;
  work_products: {
    work_product_id: string;
    document_kind: string;
    audience: string;
    media_type: string;
    artifact_sha256: string;
    byte_size: number;
    page_count: number | null;
    status: string;
    approval_hash: string | null;
  }[];
  bundles: {
    bundle_id: string;
    lifecycle: string;
    validity: string;
    currency: string;
    input_hash: string;
    evidence_manifest_hash: string;
    legal_bundle_hash: string;
    calculation_output_hash: string;
    final_text_hash: string;
    qa_hash: string;
  }[];
  current_bundle: { bundle_id: string } | null;
  current_components: {
    work_product_id: string;
    sequence: number;
    document_kind: string;
    court_filename: string;
    media_type: string;
    artifact_sha256: string;
    byte_size: number;
    approval_hash: string;
  }[];
  current_export: {
    export_id: string;
    court_zip_sha256: string;
    court_zip_bytes: number;
    internal_manifest_sha256: string;
    component_count: number;
    verification_hash: string;
    verified_at: string;
  } | null;
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

export async function loadCalculationReview(
  config: CaseDataSourceConfig = caseDataSourceConfig,
  persistentObligationId: string | undefined = process.env.NEXT_PUBLIC_PERSISTENT_OBLIGATION_ID,
): Promise<CalculationReviewView> {
  if (config.kind === "persistent-disabled") throw new Error(config.reason);
  if (config.kind === "synthetic-alpha") {
    const response = await fetch(`${alphaCalculationApiBase}/v1/calculation-previews`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Alpha-Actor": "alpha_lead_lawyer" },
      body: JSON.stringify(alphaCalculationPreviewRequest),
    });
    const payload = (await response.json()) as CalculationPreview | ErrorEnvelope;
    if (!response.ok || !("independent_check_match" in payload)) {
      throw new Error(errorMessage(payload as ErrorEnvelope, "本机合成计算服务未返回可核验结果"));
    }
    return mapSyntheticCalculation(payload, response.headers.get("X-Request-ID"));
  }
  const obligationId = persistentObligationId?.trim() || "";
  if (!obligationId) {
    return emptyPersistentCalculation("尚未选择需要计算的债务单元；系统未显示任何演示金额。", null, null);
  }
  const response = await fetch(
    `${config.apiBase}/v1/matters/${config.matterId}/calculations/${encodeURIComponent(obligationId)}/current`,
    { credentials: "include", headers: { Accept: "application/json" }, cache: "no-store" },
  );
  const payload = (await response.json()) as PersistentFormalCalculationSnapshot | ErrorEnvelope;
  if (!response.ok || !("snapshot_hash" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "正式利息计算快照不可用"));
  }
  if (!payload.scenario || !payload.run) {
    return emptyPersistentCalculation(
      "当前债务单元尚无经律师批准并完成独立复算的正式计算。",
      payload.matter_version,
      payload.snapshot_hash,
      response.headers.get("X-Request-ID"),
      obligationId,
    );
  }
  return mapPersistentCalculation(payload, response.headers.get("X-Request-ID"));
}

export async function loadLegalReview(
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<LegalReviewView> {
  if (config.kind === "persistent-disabled") throw new Error(config.reason);
  if (config.kind === "synthetic-alpha") return syntheticLegalDiscoveryView();
  const response = await fetch(`${config.apiBase}/v1/matters/${config.matterId}/legal-review`, {
    credentials: "include",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  const payload = (await response.json()) as PersistentLegalReviewSnapshot | ErrorEnvelope;
  if (!response.ok || !("snapshot_hash" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "法律依据审查快照不可用"));
  }
  return mapPersistentLegalReview(payload, response.headers.get("X-Request-ID"));
}

export async function loadOfficialSourceCaptureReview(
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<OfficialSourceCaptureView> {
  if (config.kind === "persistent-disabled") throw new Error(config.reason);
  if (config.kind === "synthetic-alpha") return syntheticOfficialSourceProbeView();
  const response = await fetch(`${config.apiBase}/v1/matters/${config.matterId}/official-source-captures`, {
    credentials: "include",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  const payload = (await response.json()) as PersistentOfficialSourceCaptureSnapshot | ErrorEnvelope;
  if (!response.ok || !("runs" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "官方法源抓取快照不可用"));
  }
  return mapPersistentOfficialSourceCapture(payload, response.headers.get("X-Request-ID"));
}

export async function queueOfficialSourceCapture(
  input: { sourceId: string; targetUrl: string; expectedVersion: number },
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<OfficialSourceCaptureReceipt> {
  if (config.kind !== "persistent-preview") {
    throw new Error("只有已启用的本机持久化工作台可以建立正式法源抓取任务。");
  }
  const minimizedQuery = officialSourceMinimizedQuery(input.sourceId);
  const querySha256 = await sha256Text(minimizedQuery);
  const authorizationHash = await sha256Text([
    "official-source-capture-authorization-v1",
    config.matterId,
    String(input.expectedVersion),
    input.sourceId,
    input.targetUrl,
    querySha256,
    "PUBLIC_OFFICIAL_SOURCE_ONLY",
    "NO_CASE_MATERIAL_SENT",
  ].join("|"));
  let response: Response;
  try {
    response = await fetch(`${config.apiBase}/v1/matters/${config.matterId}/official-source-captures`, {
      method: "POST",
      credentials: "include",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify({
        expected_version: input.expectedVersion,
        source_id: input.sourceId,
        target_url: input.targetUrl,
        query_sha256: querySha256,
        authorization_hash: authorizationHash,
      }),
    });
  } catch {
    throw new Error("连接在法源抓取任务确认前中断。请先刷新状态；系统不会自动重试外部请求。");
  }
  const payload = (await response.json()) as
    | { object_id: string; matter_version: number; object_type: string }
    | ErrorEnvelope;
  if (!response.ok || !("object_id" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "官方法源抓取任务未建立"));
  }
  if (payload.object_type !== "OFFICIAL_SOURCE_CAPTURE_RUN") {
    throw new Error("法源抓取回执类型不一致，已停止后续处理。");
  }
  return {
    objectId: payload.object_id,
    matterVersion: payload.matter_version,
    requestId: response.headers.get("X-Request-ID"),
  };
}

export async function reviewOfficialSourceCapture(
  input: {
    runId: string;
    expectedVersion: number;
    decision: "APPROVE_FOR_REGISTRATION" | "REJECT";
    provisionLocator: string;
    contentSha256: string | null;
    parsedOutputHash: string | null;
  },
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<OfficialSourceCaptureReceipt> {
  if (config.kind !== "persistent-preview") {
    throw new Error("只有已启用的本机持久化工作台可以复核正式法源抓取结果。");
  }
  const provisionLocator = input.provisionLocator.trim();
  if (!provisionLocator) throw new Error("请填写可以回到官方原文核对的具体条文或记录位置。");
  const reviewHash = await sha256Text([
    "official-source-capture-review-v1",
    config.matterId,
    input.runId,
    String(input.expectedVersion),
    input.decision,
    provisionLocator,
    input.contentSha256 ?? "NO_CONTENT_HASH",
    input.parsedOutputHash ?? "NO_PARSED_OUTPUT_HASH",
  ].join("|"));
  let response: Response;
  try {
    response = await fetch(
      `${config.apiBase}/v1/matters/${config.matterId}/official-source-captures/${input.runId}/review`,
      {
        method: "POST",
        credentials: "include",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          expected_version: input.expectedVersion,
          decision: input.decision,
          provision_locator: provisionLocator,
          review_hash: reviewHash,
        }),
      },
    );
  } catch {
    throw new Error("连接在法源复核确认前中断。请先刷新状态；系统不会重复提交律师决定。");
  }
  const payload = (await response.json()) as
    | { object_id: string; matter_version: number; object_type: string }
    | ErrorEnvelope;
  if (!response.ok || !("object_id" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "官方法源复核未记录"));
  }
  if (payload.object_type !== "OFFICIAL_SOURCE_CAPTURE_REVIEW") {
    throw new Error("法源复核回执类型不一致，已停止后续处理。");
  }
  return {
    objectId: payload.object_id,
    matterVersion: payload.matter_version,
    requestId: response.headers.get("X-Request-ID"),
  };
}

export async function loadSubmissionReview(
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<SubmissionReviewView> {
  if (config.kind === "persistent-disabled") throw new Error(config.reason);
  if (config.kind === "synthetic-alpha") return syntheticSubmissionView();
  const response = await fetch(`${config.apiBase}/v1/matters/${config.matterId}/submission-snapshot`, {
    credentials: "include",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  const payload = (await response.json()) as PersistentSubmissionSnapshot | ErrorEnvelope;
  if (!response.ok || !("snapshot_hash" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "提交材料快照不可用"));
  }
  return mapPersistentSubmission(payload, response.headers.get("X-Request-ID"));
}

export async function fetchSubmissionExport(
  review: SubmissionReviewView,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<SubmissionExportDelivery> {
  if (config.kind !== "persistent-preview") {
    throw new Error("只有已启用的本机持久化工作台可以下载法院提交包。");
  }
  if (!review.currentExport || review.status !== "exported") {
    throw new Error("当前案件没有有效且已核验的法院提交包。");
  }
  const exportId = review.currentExport.exportId;
  const accessResponse = await fetch(
    `${config.apiBase}/v1/matters/${config.matterId}/submission-exports/${exportId}/access`,
    { method: "POST", credentials: "include", headers: { Accept: "application/json" } },
  );
  const accessPayload = (await accessResponse.json()) as
    | { access_token: string; export_id: string; expires_at: string }
    | ErrorEnvelope;
  if (!accessResponse.ok || !("access_token" in accessPayload)) {
    throw new Error(errorMessage(accessPayload as ErrorEnvelope, "无法取得法院提交包的短时下载许可"));
  }
  if (accessPayload.export_id !== exportId) {
    throw new Error("下载许可与当前提交包不一致，已停止读取。");
  }
  const contentResponse = await fetch(
    `${config.apiBase}/v1/matters/${config.matterId}/submission-exports/${exportId}/content`,
    {
      credentials: "include",
      headers: { Accept: "application/zip", Authorization: `Bearer ${accessPayload.access_token}` },
      cache: "no-store",
    },
  );
  if (!contentResponse.ok) {
    const payload = (await contentResponse.json().catch(() => ({}))) as ErrorEnvelope;
    throw new Error(errorMessage(payload, "法院提交包读取失败"));
  }
  if (contentResponse.headers.get("Content-Type")?.split(";", 1)[0] !== "application/zip") {
    throw new Error("提交包返回了非 ZIP 内容，已停止下载。");
  }
  const returnedHash = contentResponse.headers.get("X-Artifact-SHA256");
  if (returnedHash !== review.currentExport.courtZipSha256) {
    throw new Error("提交包的服务端哈希与当前快照不一致，已停止下载。");
  }
  const contentLength = Number(contentResponse.headers.get("Content-Length") || "0");
  if (contentLength > 256 * 1024 * 1024) {
    throw new Error("法院提交包超过本机下载大小上限。");
  }
  const blob = await contentResponse.blob();
  if (blob.size < 1 || blob.size > 256 * 1024 * 1024) {
    throw new Error("法院提交包为空或超过本机下载大小上限。");
  }
  return { blob, fileName: "法院提交材料.zip", artifactSha256: returnedHash };
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

function mapSyntheticCalculation(payload: CalculationPreview, requestId: string | null): CalculationReviewView {
  return {
    sourceKind: "synthetic-alpha",
    sourceLabel: "本机合成数据",
    status: "ready",
    emptyReason: null,
    matterVersion: null,
    snapshotHash: payload.output_hash,
    requestId,
    obligationId: "alpha_obligation_001",
    startDate: alphaCalculationPreviewRequest.start_date,
    endDate: alphaCalculationPreviewRequest.end_date,
    currency: "CNY",
    allocationPolicy: alphaCalculationPreviewRequest.allocation_policy,
    legalBundleId: payload.legal_bundle_id,
    legalBundleHash: payload.legal_bundle_hash,
    approvalHash: alphaCalculationPreviewRequest.approval_hash,
    engineVersion: payload.engine_version,
    independentCheckMatch: payload.independent_check_match,
    totalInterestAccrued: payload.total_interest_accrued,
    totalInterestPaid: payload.total_interest_paid,
    remainingPrincipal: payload.remaining_principal,
    remainingUnpaidInterest: payload.remaining_unpaid_interest,
    unappliedPayments: payload.unapplied_payments,
    lineItems: payload.line_items.map((item, index) => ({
      lineSequence: index + 1,
      periodStart: item.period_start,
      periodEnd: item.period_end,
      openingPrincipal: item.opening_principal,
      annualRate: item.annual_rate,
      dayCount: item.day_count,
      accruedInterest: item.accrued_interest,
      closingPrincipal: item.closing_principal,
      accruedUnpaidInterest: item.accrued_unpaid_interest,
      ruleSegmentId: item.rule_segment_id,
      sourceRuleVersion: item.source_rule_version,
      evidenceIds: item.evidence_ids,
    })),
    paymentAllocations: payload.payment_allocations.map((item, index) => ({
      allocationSequence: index + 1,
      paymentEventId: item.payment_event_id,
      effectiveDate: item.effective_date,
      paymentAmount: item.payment_amount,
      allocatedInterest: item.allocated_interest,
      allocatedPrincipal: item.allocated_principal,
      unappliedAmount: item.unapplied_amount,
      paymentApplication: "BY_POLICY",
      evidenceIds: item.evidence_ids,
    })),
  };
}

function mapPersistentCalculation(
  payload: PersistentFormalCalculationSnapshot,
  requestId: string | null,
): CalculationReviewView {
  const scenario = payload.scenario!;
  const run = payload.run!;
  return {
    sourceKind: "persistent-preview",
    sourceLabel: "持久化内部预览",
    status: "ready",
    emptyReason: null,
    matterVersion: payload.matter_version,
    snapshotHash: payload.snapshot_hash,
    requestId,
    obligationId: scenario.obligation_id,
    startDate: scenario.start_date,
    endDate: scenario.end_date,
    currency: scenario.currency,
    allocationPolicy: scenario.allocation_policy,
    legalBundleId: scenario.legal_bundle_id,
    legalBundleHash: scenario.legal_bundle_hash,
    approvalHash: scenario.approval_hash,
    engineVersion: run.engine_version,
    independentCheckMatch: Boolean(run.independent_check_hash),
    totalInterestAccrued: run.total_interest_accrued,
    totalInterestPaid: run.total_interest_paid,
    remainingPrincipal: run.remaining_principal,
    remainingUnpaidInterest: run.remaining_unpaid_interest,
    unappliedPayments: run.unapplied_payments,
    lineItems: run.line_items.map((item) => ({
      lineSequence: item.line_sequence,
      periodStart: item.period_start,
      periodEnd: item.period_end,
      openingPrincipal: item.opening_principal,
      annualRate: item.annual_rate,
      dayCount: item.day_count,
      accruedInterest: item.accrued_interest,
      closingPrincipal: item.closing_principal,
      accruedUnpaidInterest: item.accrued_unpaid_interest,
      ruleSegmentId: item.rule_segment_id,
      sourceRuleVersion: item.source_rule_version,
      evidenceIds: item.evidence_ids,
    })),
    paymentAllocations: run.payment_allocations.map((item) => ({
      allocationSequence: item.allocation_sequence,
      paymentEventId: item.payment_event_id,
      effectiveDate: item.effective_date,
      paymentAmount: item.payment_amount,
      allocatedInterest: item.allocated_interest,
      allocatedPrincipal: item.allocated_principal,
      unappliedAmount: item.unapplied_amount,
      paymentApplication: item.payment_application,
      evidenceIds: item.evidence_ids,
    })),
  };
}

function emptyPersistentCalculation(
  reason: string,
  matterVersion: number | null,
  snapshotHash: string | null,
  requestId: string | null = null,
  obligationId: string | null = null,
): CalculationReviewView {
  return {
    sourceKind: "persistent-preview",
    sourceLabel: "持久化内部预览",
    status: "empty",
    emptyReason: reason,
    matterVersion,
    snapshotHash,
    requestId,
    obligationId,
    startDate: null,
    endDate: null,
    currency: "CNY",
    allocationPolicy: null,
    legalBundleId: null,
    legalBundleHash: null,
    approvalHash: null,
    engineVersion: null,
    independentCheckMatch: false,
    totalInterestAccrued: null,
    totalInterestPaid: null,
    remainingPrincipal: null,
    remainingUnpaidInterest: null,
    unappliedPayments: null,
    lineItems: [],
    paymentAllocations: [],
  };
}

function syntheticLegalDiscoveryView(): LegalReviewView {
  const shared = {
    snapshotId: null,
    verificationStatus: "NOT_CAPTURED",
    licenseStatus: "DISCOVERY_ONLY",
    contentSha256: null,
  };
  return {
    sourceKind: "synthetic-alpha",
    sourceLabel: "官方来源发现清单",
    status: "discovery-only",
    statusReason: "这些链接已定位到官方发布站点，但尚未抓取、加密、计算哈希并由律师核验，不能进入正式规则包或利息计算。",
    matterVersion: null,
    snapshotHash: null,
    requestId: null,
    sources: [
      {
        ...shared,
        sourceId: "CN-CIVIL-CODE-680",
        publisher: "最高人民法院（公布民法典全文）",
        authorityLevel: "PRIMARY_LAW",
        officialUrl: "https://www.court.gov.cn/zixun/xiangqing/233181.html",
        provisionLocator: "《中华人民共和国民法典》第六百七十九条至第六百八十条",
      },
      {
        ...shared,
        sourceId: "SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
        publisher: "最高人民法院",
        authorityLevel: "JUDICIAL_INTERPRETATION",
        officialUrl: "https://www.court.gov.cn/zixun/xiangqing/282621.html",
        provisionLocator: "民间借贷司法解释第二十四条至第三十一条（2020年第二次修正）",
      },
      {
        ...shared,
        sourceId: "SPC-PRIVATE-LENDING-2020-FIRST-REVISION",
        publisher: "最高人民法院",
        authorityLevel: "JUDICIAL_INTERPRETATION",
        officialUrl: "https://www.court.gov.cn/zixun/xiangqing/249031.html",
        provisionLocator: "法释〔2020〕6号及修正后第二十五条至第三十二条",
      },
      {
        ...shared,
        sourceId: "SPC-PRIVATE-LENDING-2015-ORIGINAL",
        publisher: "最高人民法院公报",
        authorityLevel: "JUDICIAL_INTERPRETATION",
        officialUrl: "https://gongbao.court.gov.cn/Details/48786dea74c9545c2f4fb27254ca08.html",
        provisionLocator: "法释〔2015〕18号第二十六条、第三十一条",
      },
      {
        ...shared,
        sourceId: "CFETS-LPR-HISTORY",
        publisher: "全国银行间同业拆借中心（中国货币网）",
        authorityLevel: "OFFICIAL_RATE_DATA",
        officialUrl: "https://www.chinamoney.com.cn/r/cms/chinese/chinamoney/html/currency/lpr-shibor-history-download.html",
        provisionLocator: "一年期贷款市场报价利率历史数据",
      },
    ],
    ruleVersions: [],
    legalEvents: [],
    factBindings: [],
    currentBundle: null,
    bundleSegments: [],
  };
}

function syntheticOfficialSourceProbeView(): OfficialSourceCaptureView {
  const successfulProbe = (
    runId: string,
    sourceId: string,
    publisher: string,
    targetUrl: string,
    parserKind: string,
    parsedSummary: Record<string, unknown>,
  ): OfficialSourceCaptureView["runs"][number] => ({
    runId,
    sourceId,
    publisher,
    sourceTier: sourceId === "CFETS-LPR-HISTORY" ? "OFFICIAL_RATE_DATA" : "JUDICIAL_OR_PRIMARY_SOURCE",
    targetUrl,
    status: "PROBE_CAPTURE_AND_PARSE_OK",
    attemptCount: 1,
    authorizedAt: "2026-08-10T00:00:00+08:00",
    authorizationExpiresAt: null,
    finalUrl: targetUrl,
    retrievedAt: "2026-08-10T00:00:00+08:00",
    peerIp: "已验证为公网地址（未保留临时值）",
    contentMediaType: sourceId === "CFETS-LPR-HISTORY" ? "application/json" : "text/html",
    contentSha256: null,
    contentBytes: null,
    captureVerificationHash: null,
    parserKind,
    parsedOutputHash: null,
    parsedSummary,
    failureCode: null,
    completedAt: "2026-08-10T00:00:00+08:00",
    staleReason: "开发验证完成后临时加密对象已清除，不能登记为案件法源",
  });
  return {
    sourceKind: "synthetic-alpha",
    sourceLabel: "开发机公开网络验证记录",
    status: "probe-only",
    statusReason: "以下是对公开官方网站完成的开发机真实网络冒烟；临时对象已清除，未绑定任何案件、内容哈希或律师复核，不能进入正式规则包。",
    matterVersion: null,
    snapshotHash: null,
    requestId: null,
    runs: [
      successfulProbe(
        "probe-spc-second-revision",
        "SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
        "最高人民法院",
        "https://www.court.gov.cn/zixun/xiangqing/282621.html",
        "PRIVATE_LENDING_SECOND_REVISION",
        { located_articles: [25, 31], result: "精确条文定位通过" },
      ),
      successfulProbe(
        "probe-spc-first-revision",
        "SPC-PRIVATE-LENDING-2020-FIRST-REVISION",
        "最高人民法院",
        "https://www.court.gov.cn/zixun/xiangqing/249031.html",
        "PRIVATE_LENDING_FIRST_REVISION",
        { located_articles: [26, 32], result: "历史版本定位通过" },
      ),
      successfulProbe(
        "probe-civil-code",
        "CN-CIVIL-CODE-680",
        "最高人民法院（公布民法典全文）",
        "https://www.court.gov.cn/zixun/xiangqing/233181.html",
        "CIVIL_CODE_BORROWING",
        { located_articles: [679, 680], result: "标题、通过日期与条文顺序核对通过" },
      ),
      successfulProbe(
        "probe-cfets-lpr",
        "CFETS-LPR-HISTORY",
        "全国银行间同业拆借中心（中国货币网）",
        "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?lang=CN",
        "CFETS_LPR_JSON",
        { record_count: 12, period: "2025-08-20 至 2026-07-20", latest_one_year_lpr: "3.00%" },
      ),
      {
        runId: "probe-spc-2015-original",
        sourceId: "SPC-PRIVATE-LENDING-2015-ORIGINAL",
        publisher: "最高人民法院公报",
        sourceTier: "JUDICIAL_INTERPRETATION",
        targetUrl: "https://gongbao.court.gov.cn/Details/48786dea74c9545c2f4fb27254ca08.html",
        status: "PROBE_FAILED",
        attemptCount: 1,
        authorizedAt: "2026-08-10T00:00:00+08:00",
        authorizationExpiresAt: null,
        finalUrl: null,
        retrievedAt: null,
        peerIp: null,
        contentMediaType: null,
        contentSha256: null,
        contentBytes: null,
        captureVerificationHash: null,
        parserKind: "PRIVATE_LENDING_2015_ORIGINAL",
        parsedOutputHash: null,
        parsedSummary: { result: "已发现官方公报条目，但未取得可归档全文" },
        failureCode: "OFFICIAL_GAZETTE_HTTP_502",
        completedAt: null,
        staleReason: "不能使用发布说明或合成解析样本替代正式原文",
      },
    ],
    reviews: [],
  };
}

function mapPersistentOfficialSourceCapture(
  payload: PersistentOfficialSourceCaptureSnapshot,
  requestId: string | null,
): OfficialSourceCaptureView {
  return {
    sourceKind: "persistent-preview",
    sourceLabel: "案件正式法源抓取队列",
    status: "persistent",
    statusReason: "队列只访问律师明确授权的公开官方 URL，不发送案卷、当事人姓名或检索密钥；抓取和解析完成后仍须律师逐项复核。",
    matterVersion: payload.matter_version,
    snapshotHash: payload.snapshot_hash,
    requestId,
    runs: payload.runs.map((item) => ({
      runId: item.run_id,
      sourceId: item.source_id,
      publisher: item.publisher ?? item.source_id,
      sourceTier: item.source_tier ?? "OFFICIAL_SOURCE",
      targetUrl: item.target_url ?? item.final_url ?? "",
      status: item.status,
      attemptCount: item.attempt_count ?? 0,
      authorizedAt: item.authorized_at ?? null,
      authorizationExpiresAt: item.authorization_expires_at ?? null,
      finalUrl: item.final_url ?? null,
      retrievedAt: item.retrieved_at ?? null,
      peerIp: item.peer_ip ?? null,
      contentMediaType: item.content_media_type ?? null,
      contentSha256: item.content_sha256 ?? null,
      contentBytes: item.content_bytes ?? null,
      captureVerificationHash: item.capture_verification_hash ?? null,
      parserKind: item.parser_kind ?? null,
      parsedOutputHash: item.parsed_output_hash ?? null,
      parsedSummary: item.parsed_summary ?? null,
      failureCode: item.failure_code ?? null,
      completedAt: item.completed_at ?? null,
      staleReason: item.stale_reason ?? null,
    })),
    reviews: payload.reviews.map((item) => ({
      reviewId: item.review_id,
      runId: item.run_id,
      decision: item.decision,
      provisionLocator: item.provision_locator,
      reviewHash: item.review_hash,
      reviewedBy: item.reviewed_by,
      reviewedAt: item.reviewed_at,
    })),
  };
}

function officialSourceMinimizedQuery(sourceId: string): string {
  const queries: Record<string, string> = {
    "CN-CIVIL-CODE-680": "中华人民共和国民法典 第六百七十九条 第六百八十条",
    "SPC-PRIVATE-LENDING-2020-SECOND-REVISION": "民间借贷司法解释 2020年第二次修正 第二十五条 第三十一条",
    "SPC-PRIVATE-LENDING-2020-FIRST-REVISION": "民间借贷司法解释 2020年第一次修正 第二十六条 第三十二条",
    "SPC-PRIVATE-LENDING-2015-ORIGINAL": "法释2015 18号 第二十六条 第三十一条",
    "CFETS-LPR-HISTORY": "一年期贷款市场报价利率 历史数据",
  };
  const query = queries[sourceId];
  if (!query) throw new Error("该来源没有登记最小化公开检索模板，已停止外部请求。");
  return query;
}

function mapPersistentLegalReview(
  payload: PersistentLegalReviewSnapshot,
  requestId: string | null,
): LegalReviewView {
  return {
    sourceKind: "persistent-preview",
    sourceLabel: "持久化法律依据快照",
    status: "reviewable",
    statusReason: "仅展示已由服务端读取的来源、规则、关键事实绑定和当前规则包；批准动作仍受案件版本与角色权限控制。",
    matterVersion: payload.matter_version,
    snapshotHash: payload.snapshot_hash,
    requestId,
    sources: payload.sources.map((item) => ({
      snapshotId: item.snapshot_id,
      sourceId: item.source_id,
      publisher: item.publisher,
      authorityLevel: item.authority_level,
      officialUrl: item.official_url,
      provisionLocator: item.provision_locator,
      verificationStatus: item.verification_status,
      licenseStatus: item.license_status,
      contentSha256: item.content_sha256,
    })),
    ruleVersions: payload.rule_versions.map((item) => ({
      ruleVersionId: item.rule_version_id,
      ruleVersion: item.rule_version,
      issueKey: item.issue_key,
      triggerEventKind: item.trigger_event_kind,
      formulaKind: item.formula_kind,
      parameterSourceSnapshotId: item.parameter_source_snapshot_id,
      parameterEvidenceLocator: item.parameter_evidence_locator,
      baseAnnualRate: item.base_annual_rate,
      rateMultiplier: item.rate_multiplier,
      derivedAnnualRate: item.derived_annual_rate,
      requiredFactKeys: item.required_fact_keys,
      status: item.status,
    })),
    legalEvents: payload.legal_events.map((item) => ({
      legalEventId: item.legal_event_id,
      eventKind: item.event_kind,
      localDate: item.local_date,
      status: item.status,
      evidenceIds: item.evidence_ids,
    })),
    factBindings: payload.fact_bindings.map((item) => ({
      bindingId: item.binding_id,
      factKey: item.fact_key,
      factId: item.fact_id,
      status: item.status,
      approvalHash: item.approval_hash,
    })),
    currentBundle: payload.current_bundle
      ? {
          bundleId: payload.current_bundle.bundle_id,
          version: payload.current_bundle.version,
          bundleHash: payload.current_bundle.bundle_hash,
          approvalHash: payload.current_bundle.approval_hash,
        }
      : null,
    bundleSegments: payload.bundle_segments.map((item) => ({
      segmentId: item.segment_id,
      issueKey: item.issue_key,
      ruleVersionId: item.rule_version_id,
      sourceSnapshotId: item.source_snapshot_id,
      parameterSourceSnapshotId: item.parameter_source_snapshot_id,
      parameterEvidenceLocator: item.parameter_evidence_locator,
      startDate: item.start_date,
      endDate: item.end_date,
      annualRate: item.annual_rate,
      applicabilityAnchor: item.applicability_anchor,
    })),
  };
}

function syntheticSubmissionView(): SubmissionReviewView {
  return {
    sourceKind: "synthetic-alpha",
    sourceLabel: "本机合成模式",
    status: "blocked",
    statusReason: "合成模式不建立可提交文件、锁定版或导出回执；这里只展示正式流程的必备门禁。",
    matterVersion: null,
    stage: null,
    snapshotHash: null,
    requestId: null,
    workProducts: [],
    bundles: [],
    currentBundleId: null,
    currentComponents: [],
    currentExport: null,
  };
}

function mapPersistentSubmission(
  payload: PersistentSubmissionSnapshot,
  requestId: string | null,
): SubmissionReviewView {
  const currentBundle = payload.current_bundle
    ? payload.bundles.find((item) => item.bundle_id === payload.current_bundle?.bundle_id) ?? null
    : null;
  const status: SubmissionReviewView["status"] = payload.current_export
    ? "exported"
    : currentBundle?.lifecycle === "LOCKED"
      ? "locked"
      : "reviewable";
  return {
    sourceKind: "persistent-preview",
    sourceLabel: "持久化提交材料快照",
    status,
    statusReason: currentBundle
      ? "当前提交版已绑定证据、法律规则、利息计算、最终文本和逐份文件哈希；任何上游正式变化都会使其失效。"
      : "尚无当前锁定提交版。只有完成 QA 且全部依赖仍有效时才能锁定。",
    matterVersion: payload.matter_version,
    stage: payload.stage,
    snapshotHash: payload.snapshot_hash,
    requestId,
    workProducts: payload.work_products.map((item) => ({
      workProductId: item.work_product_id,
      documentKind: item.document_kind,
      audience: item.audience,
      mediaType: item.media_type,
      artifactSha256: item.artifact_sha256,
      byteSize: item.byte_size,
      pageCount: item.page_count,
      status: item.status,
      approvalHash: item.approval_hash,
    })),
    bundles: payload.bundles.map((item) => ({
      bundleId: item.bundle_id,
      lifecycle: item.lifecycle,
      validity: item.validity,
      currency: item.currency,
      inputHash: item.input_hash,
      evidenceManifestHash: item.evidence_manifest_hash,
      legalBundleHash: item.legal_bundle_hash,
      calculationOutputHash: item.calculation_output_hash,
      finalTextHash: item.final_text_hash,
      qaHash: item.qa_hash,
    })),
    currentBundleId: payload.current_bundle?.bundle_id ?? null,
    currentComponents: payload.current_components.map((item) => ({
      workProductId: item.work_product_id,
      sequence: item.sequence,
      documentKind: item.document_kind,
      courtFilename: item.court_filename,
      mediaType: item.media_type,
      artifactSha256: item.artifact_sha256,
      byteSize: item.byte_size,
      approvalHash: item.approval_hash,
    })),
    currentExport: payload.current_export
      ? {
          exportId: payload.current_export.export_id,
          courtZipSha256: payload.current_export.court_zip_sha256,
          courtZipBytes: payload.current_export.court_zip_bytes,
          internalManifestSha256: payload.current_export.internal_manifest_sha256,
          componentCount: payload.current_export.component_count,
          verificationHash: payload.current_export.verification_hash,
          verifiedAt: payload.current_export.verified_at,
        }
      : null,
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
