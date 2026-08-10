import {
  alphaCalculationApiBase,
  alphaCalculationPreviewRequest,
  type CalculationPreview,
} from "@/lib/synthetic-calculation";
import { persistentApiFetch } from "@/lib/persistent-api-client";
import { syntheticMatter } from "@/lib/synthetic-matter";

export type CaseDataSourceConfig =
  | { kind: "synthetic-alpha"; label: "本机合成数据" }
  | { kind: "persistent-preview"; label: "持久化内部预览"; apiBase: string | null; matterId: string }
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
  factPage: { loadedCount: number; totalCount: number; nextCursor: string | null; hasMore: boolean };
  transactionPage: { loadedCount: number; totalCount: number; nextCursor: string | null; hasMore: boolean };
};

export type EvidenceReviewPage = {
  pageId: string;
  fileId: string;
  originalLabel: string;
  pageNumber: number;
  decisionId: string | null;
  disposition: "INCLUDE" | "EXCLUDE" | null;
  reason: string | null;
  pendingDecision: {
    decisionId: string;
    disposition: "INCLUDE" | "EXCLUDE";
    reason: string;
    status: "CANDIDATE";
  } | null;
  annotations: { annotationId: string; x0: number; y0: number; x1: number; y1: number; label: string; status: string }[];
  syntheticPreview: { date: string; amount: string; counterpart: string; confidence: string; note: string } | null;
};

export type EvidenceReviewView = {
  sourceKind: "synthetic-alpha" | "persistent-preview";
  sourceLabel: string;
  matterVersion: number | null;
  snapshotHash: string;
  manifestReadinessHash: string;
  requestId: string | null;
  totalPages: number;
  unresolvedPageCount: number;
  pendingDecisionCount: number;
  unresolvedDuplicateCount: number;
  originals: { fileId: string; originalLabel: string; originalFileSha256: string; pageCount: number }[];
  pages: EvidenceReviewPage[];
  pagePage: { loadedCount: number; totalCount: number; nextCursor: string | null; hasMore: boolean };
  duplicateGroups: {
    groupId: string;
    status: string;
    canonicalPageId: string | null;
    pageIds: string[];
    pageLabels: Record<string, string>;
  }[];
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

export type EvidenceMutationReceipt = {
  objectId: string;
  matterVersion: number;
  requestId: string | null;
};

export type LocalFolderSelection = {
  selectedRoot: string;
  displayName: string;
  rootFingerprint: string;
};

export type LocalFolderGrant = {
  grantId: string;
  displayName: string;
  rootFingerprint: string;
  expiresAt: string;
};

export type LocalFolderScanSummary = {
  scanId: string;
  manifestHash: string;
  baseScanId: string | null;
  status: "CANDIDATE" | "APPROVED";
  totalFiles: number;
  totalBytes: number;
  skippedSymlinks: number;
  newCount: number;
  modifiedCount: number;
  movedCount: number;
  missingCount: number;
  unchangedCount: number;
  duplicateContentCount: number;
  scannedAt: string;
  approvedAt: string | null;
};

export type LocalFolderIntakeView = {
  matterVersion: number;
  summaryHash: string;
  approvedScan: LocalFolderScanSummary | null;
  candidateScan: LocalFolderScanSummary | null;
  displayedScanId: string | null;
  files: {
    relativePath: string;
    previousRelativePath: string | null;
    byteSize: number;
    fileSha256: string;
    detectedKind: string;
    changeKind: "NEW" | "MODIFIED" | "MOVED" | "MISSING" | "UNCHANGED";
    present: boolean;
  }[];
  filePage: { loadedCount: number; totalCount: number; nextCursor: string | null; hasMore: boolean };
  intakeRun: {
    runId: string;
    scanId: string;
    scanManifestHash: string;
    status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "PARTIAL";
    totalItems: number;
    queuedItems: number;
    runningItems: number;
    registeredItems: number;
    reviewRequiredItems: number;
    blockedItems: number;
    failedItems: number;
    createdAt: string;
    completedAt: string | null;
  } | null;
};

export type OriginalPagePreviewDelivery = {
  pageId: string;
  blob: Blob;
  contentSha256: string;
  width: number;
  height: number;
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
    licenseBasis: string | null;
    licenseReviewHash: string | null;
    captureRunId: string | null;
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

type PersistentReviewSummary = {
  matter_id: string;
  title: string;
  stage: string;
  version: number;
  summary_hash: string;
  fact_count: number;
  candidate_fact_count: number;
  transaction_count: number;
  claims: { claim_id: string; original_claim_text: string; claimed_amount: string | null; currency: string | null; status: string; response: { position: string; partial_amount: string | null; currency: string | null } | null }[];
  issues: { issue_id: string; question: string; status: string; claim_count: number; fact_count: number }[];
};

type PersistentFactPage = {
  matter_id: string;
  matter_version: number;
  total_count: number;
  candidate_count: number;
  items: { fact_id: string; original_text: string; origin: string; status: string; evidence_count: number }[];
  next_cursor: string | null;
  has_more: boolean;
};

type PersistentTransactionPage = {
  matter_id: string;
  matter_version: number;
  total_count: number;
  items: { transaction_id: string; local_date: string | null; amount: string; currency: string; status: string; evidence_count: number; classification_nature: string | null; classification_status: string | null }[];
  next_cursor: string | null;
  has_more: boolean;
};

type PersistentEvidencePageItem = {
  evidence_page_id: string;
  evidence_file_id: string;
  original_label: string;
  page_number: number;
  decision: { decision_id: string; disposition: "INCLUDE" | "EXCLUDE"; reason: string; status: "APPROVED" } | null;
  pending_decision: { decision_id: string; disposition: "INCLUDE" | "EXCLUDE"; reason: string; status: "CANDIDATE" } | null;
  annotations: { annotation_id: string; x0: string; y0: string; x1: string; y1: string; label: string; status: string }[];
};

type PersistentEvidencePagePage = {
  matter_id: string;
  matter_version: number;
  total_count: number;
  items: PersistentEvidencePageItem[];
  next_cursor: string | null;
  has_more: boolean;
};

type PersistentEvidenceReviewSummary = {
  matter_id: string;
  version: number;
  summary_hash: string;
  manifest_readiness_hash: string;
  total_pages: number;
  unresolved_page_count: number;
  pending_decision_count: number;
  unresolved_duplicate_count: number;
  original_files: { evidence_file_id: string; original_label: string; original_file_sha256: string; page_count: number }[];
  duplicate_groups: {
    duplicate_group_id: string;
    status: string;
    canonical_page_id: string | null;
    members: { evidence_page_id: string; evidence_file_id: string; original_label: string; page_number: number }[];
  }[];
  locked_manifest: { manifest_id: string; content_hash: string; total_pages: number; included_pages: number; excluded_pages: number } | null;
  derivatives: { derivative_id: string; artifact_type: string; artifact_sha256: string; page_count: number; status: string }[];
  derivative_runs: { run_id: string; manifest_id: string; status: string; attempt_count: number; failure_code: string | null }[];
};

type PersistentLocalFolderScanSummary = {
  scan_id: string;
  manifest_hash: string;
  base_scan_id: string | null;
  status: "CANDIDATE" | "APPROVED";
  total_files: number;
  total_bytes: number;
  skipped_symlinks: number;
  new_count: number;
  modified_count: number;
  moved_count: number;
  missing_count: number;
  unchanged_count: number;
  duplicate_content_count: number;
  scanned_at: string;
  approved_at: string | null;
};

type PersistentLocalFolderIntakeSummary = {
  matter_id: string;
  matter_version: number;
  summary_hash: string;
  approved_scan: PersistentLocalFolderScanSummary | null;
  candidate_scan: PersistentLocalFolderScanSummary | null;
};

type PersistentLocalFolderFilePage = {
  matter_id: string;
  matter_version: number;
  scan_id: string;
  total_count: number;
  items: {
    relative_path: string;
    previous_relative_path: string | null;
    byte_size: number;
    file_sha256: string;
    detected_kind: string;
    change_kind: "NEW" | "MODIFIED" | "MOVED" | "MISSING" | "UNCHANGED";
    present: boolean;
  }[];
  next_cursor: string | null;
  has_more: boolean;
};

type PersistentEvidenceIntakeSummary = {
  matter_id: string;
  matter_version: number;
  run: {
    run_id: string;
    scan_id: string;
    scan_manifest_hash: string;
    status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "PARTIAL";
    total_items: number;
    queued_items: number;
    running_items: number;
    registered_items: number;
    review_required_items: number;
    blocked_items: number;
    failed_items: number;
    created_at: string;
    completed_at: string | null;
  } | null;
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
    license_basis?: string | null;
    license_review_hash?: string | null;
    capture_run_id?: string | null;
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
  if (!matterId) {
    return { kind: "persistent-disabled", label: "持久化模式未启用", reason: "缺少案件标识，未回退到合成数据。" };
  }
  if (apiBase && !isAllowedPreviewOrigin(apiBase)) {
    return { kind: "persistent-disabled", label: "持久化模式未启用", reason: "持久化服务地址不符合本机或 HTTPS 安全边界。" };
  }
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(matterId)) {
    return { kind: "persistent-disabled", label: "持久化模式未启用", reason: "案件标识不是有效 UUID，已停止读取。" };
  }
  return { kind: "persistent-preview", label: "持久化内部预览", apiBase: apiBase || null, matterId };
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
  const response = await persistentApiFetch(config, `/v1/matters/${config.matterId}/review-summary`, {
    headers: { Accept: "application/json" },
  });
  const payload = (await response.json()) as PersistentReviewSummary | ErrorEnvelope;
  if (!response.ok || !("summary_hash" in payload)) throw new Error(errorMessage(payload as ErrorEnvelope, "持久化案件摘要不可用"));
  const versionQuery = { limit: "50", expected_version: String(payload.version) };
  const [factResponse, transactionResponse] = await Promise.all([
    persistentApiFetch(config, `/v1/matters/${config.matterId}/fact-pages`, { headers: { Accept: "application/json" } }, "desktop-session", versionQuery),
    persistentApiFetch(config, `/v1/matters/${config.matterId}/transaction-pages`, { headers: { Accept: "application/json" } }, "desktop-session", versionQuery),
  ]);
  const [factPayload, transactionPayload] = await Promise.all([
    factResponse.json() as Promise<PersistentFactPage | ErrorEnvelope>,
    transactionResponse.json() as Promise<PersistentTransactionPage | ErrorEnvelope>,
  ]);
  if (!factResponse.ok || !("items" in factPayload)) throw new Error(errorMessage(factPayload as ErrorEnvelope, "事实分页不可用"));
  if (!transactionResponse.ok || !("items" in transactionPayload)) throw new Error(errorMessage(transactionPayload as ErrorEnvelope, "交易分页不可用"));
  return mapPersistentReview(
    payload,
    factPayload,
    transactionPayload,
    response.headers.get("X-Request-ID"),
  );
}

export async function loadMoreCaseFacts(
  review: CaseReviewView,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<CaseReviewView> {
  if (review.sourceKind !== "persistent-preview" || !review.factPage.hasMore || !review.factPage.nextCursor) return review;
  const persistent = requirePersistentReviewConfig(review, config);
  const page = await loadPersistentLedgerPage<PersistentFactPage>(
    persistent,
    "fact-pages",
    review.factPage.nextCursor,
    review.matterVersion,
    "事实后续页不可用",
  );
  const mapped = mapPersistentFacts(page.items);
  return {
    ...review,
    facts: mergeById(review.facts, mapped.facts, (item) => item.factId),
    pendingFacts: mergeById(review.pendingFacts, mapped.pendingFacts, (item) => item.factId),
    factPage: { loadedCount: review.factPage.loadedCount + page.items.length, totalCount: page.total_count, nextCursor: page.next_cursor, hasMore: page.has_more },
  };
}

export async function loadMoreCaseTransactions(
  review: CaseReviewView,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<CaseReviewView> {
  if (review.sourceKind !== "persistent-preview" || !review.transactionPage.hasMore || !review.transactionPage.nextCursor) return review;
  const persistent = requirePersistentReviewConfig(review, config);
  const page = await loadPersistentLedgerPage<PersistentTransactionPage>(
    persistent,
    "transaction-pages",
    review.transactionPage.nextCursor,
    review.matterVersion,
    "交易后续页不可用",
  );
  return {
    ...review,
    transactions: mergeById(review.transactions, mapPersistentTransactions(page.items), (item) => item.transactionId),
    transactionPage: { loadedCount: review.transactionPage.loadedCount + page.items.length, totalCount: page.total_count, nextCursor: page.next_cursor, hasMore: page.has_more },
  };
}

async function loadPersistentLedgerPage<T extends { matter_version: number; items: unknown[] }>(
  config: Extract<CaseDataSourceConfig, { kind: "persistent-preview" }>,
  projection: "fact-pages" | "transaction-pages",
  cursor: string,
  matterVersion: number | null,
  fallback: string,
): Promise<T> {
  if (matterVersion === null) throw new Error("当前案件版本无效，不能继续加载。");
  const query = {
    limit: "50",
    expected_version: String(matterVersion),
    cursor,
  };
  const response = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/${projection}`,
    { headers: { Accept: "application/json" } },
    "desktop-session",
    query,
  );
  const payload = (await response.json()) as T | ErrorEnvelope;
  if (!response.ok || !("items" in payload)) throw new Error(errorMessage(payload as ErrorEnvelope, fallback));
  if (payload.matter_version !== matterVersion) throw new Error("案件在加载后续记录期间已变化，请重新载入。");
  return payload;
}

function requirePersistentReviewConfig(
  review: CaseReviewView,
  config: CaseDataSourceConfig,
): Extract<CaseDataSourceConfig, { kind: "persistent-preview" }> {
  if (review.sourceKind !== "persistent-preview" || config.kind !== "persistent-preview") {
    throw new Error("当前案件不是可继续加载的持久化案件。");
  }
  return config;
}

function mergeById<T>(existing: T[], incoming: T[], identifier: (item: T) => string): T[] {
  const seen = new Set(existing.map(identifier));
  return existing.concat(incoming.filter((item) => !seen.has(identifier(item))));
}

export async function loadEvidenceReview(config: CaseDataSourceConfig = caseDataSourceConfig): Promise<EvidenceReviewView> {
  if (config.kind === "persistent-disabled") throw new Error(config.reason);
  if (config.kind === "synthetic-alpha") return mapSyntheticEvidence();
  const response = await persistentApiFetch(config, `/v1/matters/${config.matterId}/evidence-review-summary`, {
    headers: { Accept: "application/json" },
  });
  const summary = (await response.json()) as PersistentEvidenceReviewSummary | ErrorEnvelope;
  if (!response.ok || !("summary_hash" in summary)) throw new Error(errorMessage(summary as ErrorEnvelope, "持久化证据摘要不可用"));
  const pageResponse = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/evidence-pages`,
    { headers: { Accept: "application/json" } },
    "desktop-session",
    { limit: "50", expected_version: String(summary.version) },
  );
  const page = (await pageResponse.json()) as PersistentEvidencePagePage | ErrorEnvelope;
  if (!pageResponse.ok || !("items" in page)) throw new Error(errorMessage(page as ErrorEnvelope, "证据来源页不可用"));
  if (page.matter_version !== summary.version) throw new Error("案件在载入证据页期间已变化，请重新载入。");
  return mapPersistentEvidence(summary, page, response.headers.get("X-Request-ID"));
}

export async function loadMoreEvidencePages(
  review: EvidenceReviewView,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceReviewView> {
  if (review.sourceKind !== "persistent-preview" || !review.pagePage.hasMore || !review.pagePage.nextCursor) return review;
  if (config.kind !== "persistent-preview" || review.matterVersion === null) {
    throw new Error("当前证据快照不是可继续载入的持久化案件。");
  }
  const response = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/evidence-pages`,
    { headers: { Accept: "application/json" } },
    "desktop-session",
    { limit: "50", expected_version: String(review.matterVersion), cursor: review.pagePage.nextCursor },
  );
  const payload = (await response.json()) as PersistentEvidencePagePage | ErrorEnvelope;
  if (!response.ok || !("items" in payload)) throw new Error(errorMessage(payload as ErrorEnvelope, "证据后续页不可用"));
  if (payload.matter_version !== review.matterVersion) throw new Error("案件在载入后续证据页期间已变化，请重新载入。");
  const incoming = payload.items.map(mapPersistentEvidencePage);
  return {
    ...review,
    pages: mergeById(review.pages, incoming, (item) => item.pageId),
    pagePage: {
      loadedCount: Math.min(payload.total_count, review.pagePage.loadedCount + payload.items.length),
      totalCount: payload.total_count,
      nextCursor: payload.next_cursor,
      hasMore: payload.has_more,
    },
  };
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
  const response = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/calculations/${encodeURIComponent(obligationId)}/current`,
    { headers: { Accept: "application/json" } },
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
  const response = await persistentApiFetch(config, `/v1/matters/${config.matterId}/legal-review`, {
    headers: { Accept: "application/json" },
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
  const response = await persistentApiFetch(config, `/v1/matters/${config.matterId}/official-source-captures`, {
    headers: { Accept: "application/json" },
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
    response = await persistentApiFetch(config, `/v1/matters/${config.matterId}/official-source-captures`, {
      method: "POST",
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
    response = await persistentApiFetch(
      config,
      `/v1/matters/${config.matterId}/official-source-captures/${input.runId}/review`,
      {
        method: "POST",
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

export async function registerReviewedOfficialSourceCapture(
  input: {
    runId: string;
    expectedVersion: number;
    contentSha256: string;
    parsedOutputHash: string;
    captureReviewHash: string;
    licenseBasis: string;
  },
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<OfficialSourceCaptureReceipt> {
  if (config.kind !== "persistent-preview") {
    throw new Error("只有已启用的本机持久化工作台可以登记正式法源快照。");
  }
  const licenseBasis = input.licenseBasis.trim();
  if (!licenseBasis) throw new Error("请填写本次公开访问、加密保存和案内使用的许可依据。");
  if (!input.contentSha256 || !input.parsedOutputHash) {
    throw new Error("本次抓取缺少内容或解析哈希，不能登记正式法源。");
  }
  const licenseReviewHash = await sha256Text([
    "official-source-license-review-v1",
    config.matterId,
    input.runId,
    input.contentSha256,
    licenseBasis,
  ].join("|"));
  const registrationHash = await sha256Text([
    "reviewed-official-source-registration-v1",
    config.matterId,
    String(input.expectedVersion),
    input.runId,
    input.contentSha256,
    input.parsedOutputHash,
    input.captureReviewHash,
    licenseReviewHash,
  ].join("|"));
  let response: Response;
  try {
    response = await persistentApiFetch(
      config,
      `/v1/matters/${config.matterId}/official-source-captures/${input.runId}/register`,
      {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          expected_version: input.expectedVersion,
          license_basis: licenseBasis,
          license_review_hash: licenseReviewHash,
          registration_hash: registrationHash,
        }),
      },
    );
  } catch {
    throw new Error("连接在正式法源登记确认前中断。请先刷新来源快照；系统不会重复登记。");
  }
  const payload = (await response.json()) as
    | { object_id: string; matter_version: number; object_type: string }
    | ErrorEnvelope;
  if (!response.ok || !("object_id" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "正式法源快照未登记"));
  }
  if (payload.object_type !== "OFFICIAL_LEGAL_SOURCE_SNAPSHOT") {
    throw new Error("正式法源登记回执类型不一致，已停止后续处理。");
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
  const response = await persistentApiFetch(config, `/v1/matters/${config.matterId}/submission-snapshot`, {
    headers: { Accept: "application/json" },
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
  const accessResponse = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/submission-exports/${exportId}/access`,
    { method: "POST", headers: { Accept: "application/json" } },
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
  const contentResponse = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/submission-exports/${exportId}/content`,
    {
      headers: { Accept: "application/zip", Authorization: `Bearer ${accessPayload.access_token}` },
    },
    "provided-bearer",
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
  const accessResponse = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/evidence-derivatives/${derivative.derivativeId}/access`,
    {
      method: "POST",
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
  const contentResponse = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/evidence-derivatives/${derivative.derivativeId}/content`,
    {
      headers: { Accept: "application/pdf", Authorization: `Bearer ${accessPayload.access_token}` },
    },
    "provided-bearer",
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

export async function inspectLocalFolderSelection(
  selectedRoot: string,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<LocalFolderSelection> {
  if (config.kind !== "persistent-preview") throw new Error("只有本机持久化工作台可以选择案卷文件夹。");
  const normalizedRoot = selectedRoot.trim();
  if (!normalizedRoot) throw new Error("本机没有返回已选择的案卷文件夹。");
  const response = await persistentApiFetch(config, `/v1/matters/${config.matterId}/local-folder-selections/inspect`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ selected_root: normalizedRoot }),
  });
  const payload = (await response.json()) as
    | { display_name: string; root_fingerprint: string }
    | ErrorEnvelope;
  if (!response.ok || !("root_fingerprint" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "无法核验所选案卷文件夹"));
  }
  return {
    selectedRoot: normalizedRoot,
    displayName: payload.display_name,
    rootFingerprint: payload.root_fingerprint,
  };
}

export async function issueLocalFolderGrant(
  selection: LocalFolderSelection,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<LocalFolderGrant> {
  if (config.kind !== "persistent-preview") throw new Error("只有本机持久化工作台可以授权案卷文件夹。");
  const response = await persistentApiFetch(config, `/v1/matters/${config.matterId}/local-folder-grants`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      selected_root: selection.selectedRoot,
      confirmed_root_fingerprint: selection.rootFingerprint,
    }),
  });
  const payload = (await response.json()) as
    | { grant_id: string; display_name: string; root_fingerprint: string; expires_at: string }
    | ErrorEnvelope;
  if (!response.ok || !("grant_id" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, "案卷文件夹授权未建立"));
  }
  if (payload.root_fingerprint !== selection.rootFingerprint) {
    throw new Error("案卷文件夹在确认前发生变化，已停止授权。");
  }
  return {
    grantId: payload.grant_id,
    displayName: payload.display_name,
    rootFingerprint: payload.root_fingerprint,
    expiresAt: payload.expires_at,
  };
}

export async function loadLocalFolderIntake(
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<LocalFolderIntakeView> {
  if (config.kind !== "persistent-preview") throw new Error("只有本机持久化工作台可以读取案卷盘点。");
  const [response, runResponse] = await Promise.all([
    persistentApiFetch(config, `/v1/matters/${config.matterId}/local-folder-intake`, {
      headers: { Accept: "application/json" },
    }),
    persistentApiFetch(config, `/v1/matters/${config.matterId}/evidence-intake-runs/current`, {
      headers: { Accept: "application/json" },
    }),
  ]);
  const summary = (await response.json()) as PersistentLocalFolderIntakeSummary | ErrorEnvelope;
  if (!response.ok || !("summary_hash" in summary)) throw new Error(errorMessage(summary as ErrorEnvelope, "案卷盘点摘要不可用"));
  const runSummary = (await runResponse.json()) as PersistentEvidenceIntakeSummary | ErrorEnvelope;
  if (!runResponse.ok || !("run" in runSummary)) throw new Error(errorMessage(runSummary as ErrorEnvelope, "材料接收状态不可用"));
  if (runSummary.matter_version !== summary.matter_version) throw new Error("案件在载入材料接收状态期间已变化，请重新载入。");
  const displayed = summary.candidate_scan ?? summary.approved_scan;
  if (!displayed) {
    return {
      matterVersion: summary.matter_version,
      summaryHash: summary.summary_hash,
      approvedScan: null,
      candidateScan: null,
      displayedScanId: null,
      files: [],
      filePage: { loadedCount: 0, totalCount: 0, nextCursor: null, hasMore: false },
      intakeRun: mapEvidenceIntakeRun(runSummary.run),
    };
  }
  const page = await loadPersistentLocalFolderFilePage(config, displayed.scan_id, summary.matter_version, null);
  return mapLocalFolderIntake(summary, page, runSummary);
}

export async function loadMoreLocalFolderFiles(
  intake: LocalFolderIntakeView,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<LocalFolderIntakeView> {
  if (!intake.displayedScanId || !intake.filePage.hasMore || !intake.filePage.nextCursor) return intake;
  if (config.kind !== "persistent-preview") throw new Error("只有本机持久化工作台可以继续读取案卷盘点。");
  const page = await loadPersistentLocalFolderFilePage(
    config,
    intake.displayedScanId,
    intake.matterVersion,
    intake.filePage.nextCursor,
  );
  return {
    ...intake,
    files: mergeById(intake.files, mapLocalFolderFiles(page.items), (item) => `${item.changeKind}:${item.relativePath}`),
    filePage: {
      loadedCount: Math.min(page.total_count, intake.filePage.loadedCount + page.items.length),
      totalCount: page.total_count,
      nextCursor: page.next_cursor,
      hasMore: page.has_more,
    },
  };
}

export async function createLocalFolderScan(
  intake: LocalFolderIntakeView,
  folderGrantId: string,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceMutationReceipt> {
  if (config.kind !== "persistent-preview") throw new Error("只有本机持久化工作台可以建立案卷盘点候选。");
  return postEvidenceMutation({
    config,
    path: "local-folder-scans",
    body: { expected_version: intake.matterVersion, folder_grant_id: folderGrantId },
    expectedObjectType: "LOCAL_FOLDER_SCAN",
    fallback: "案卷文件盘点候选未建立",
    interrupted: "连接在案卷盘点回执前中断。请刷新盘点状态；系统不会盲目重复扫描写入。",
  });
}

export async function approveLocalFolderScan(
  intake: LocalFolderIntakeView,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceMutationReceipt> {
  if (config.kind !== "persistent-preview" || !intake.candidateScan) {
    throw new Error("当前没有可批准的案卷盘点候选。");
  }
  const scan = intake.candidateScan;
  const approvalHash = await sha256Text([
    "local-folder-scan-approval-v1",
    config.matterId,
    String(intake.matterVersion),
    intake.summaryHash,
    scan.scanId,
    scan.manifestHash,
    String(scan.totalFiles),
    String(scan.totalBytes),
    String(scan.newCount),
    String(scan.modifiedCount),
    String(scan.movedCount),
    String(scan.missingCount),
    String(scan.duplicateContentCount),
  ].join("|"));
  return postEvidenceMutation({
    config,
    path: `local-folder-scans/${scan.scanId}/approve`,
    body: { expected_version: intake.matterVersion, manifest_hash: scan.manifestHash, approval_hash: approvalHash },
    expectedObjectType: "LOCAL_FOLDER_SCAN",
    fallback: "案卷盘点范围未获批准",
    interrupted: "连接在案卷范围批准确认前中断。请刷新盘点状态；系统不会重复提交律师批准。",
  });
}

export async function enqueueEvidenceIntakeRun(
  intake: LocalFolderIntakeView,
  folderGrantId: string,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceMutationReceipt> {
  if (config.kind !== "persistent-preview" || !intake.approvedScan || intake.candidateScan) {
    throw new Error("只有当前已批准且没有待确认变化的案卷范围可以进入材料接收。");
  }
  if (intake.intakeRun) throw new Error("当前已批准案卷范围已经建立材料接收任务。");
  const scan = intake.approvedScan;
  const approvalHash = await sha256Text([
    "evidence-intake-run-approval-v1",
    config.matterId,
    String(intake.matterVersion),
    scan.scanId,
    scan.manifestHash,
    String(scan.totalFiles),
    String(scan.totalBytes),
  ].join("|"));
  return postEvidenceMutation({
    config,
    path: "evidence-intake-runs",
    body: {
      expected_version: intake.matterVersion,
      scan_id: scan.scanId,
      scan_manifest_hash: scan.manifestHash,
      approval_hash: approvalHash,
      folder_grant_id: folderGrantId,
    },
    expectedObjectType: "EVIDENCE_INTAKE_RUN",
    fallback: "材料接收任务未建立",
    interrupted: "连接在材料接收任务回执前中断。请刷新接收状态；系统不会重复建立任务。",
  });
}

async function loadPersistentLocalFolderFilePage(
  config: Extract<CaseDataSourceConfig, { kind: "persistent-preview" }>,
  scanId: string,
  matterVersion: number,
  cursor: string | null,
): Promise<PersistentLocalFolderFilePage> {
  const query: Record<string, string> = { limit: "100", expected_version: String(matterVersion) };
  if (cursor) query.cursor = cursor;
  const response = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/local-folder-scans/${scanId}/files`,
    { headers: { Accept: "application/json" } },
    "desktop-session",
    query,
  );
  const payload = (await response.json()) as PersistentLocalFolderFilePage | ErrorEnvelope;
  if (!response.ok || !("items" in payload)) throw new Error(errorMessage(payload as ErrorEnvelope, "案卷文件清单不可用"));
  if (payload.matter_version !== matterVersion || payload.scan_id !== scanId) {
    throw new Error("案卷盘点在载入文件期间已变化，请重新载入。");
  }
  return payload;
}

export async function fetchOriginalPagePreview(
  pageId: string,
  folderGrantId: string,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<OriginalPagePreviewDelivery> {
  if (config.kind !== "persistent-preview") throw new Error("只有本机持久化工作台可以预览原始证据页。");
  const accessResponse = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/evidence-pages/${pageId}/original-preview/access`,
    {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ folder_grant_id: folderGrantId }),
    },
  );
  const accessPayload = (await accessResponse.json()) as
    | { access_token: string; evidence_page_id: string }
    | ErrorEnvelope;
  if (!accessResponse.ok || !("access_token" in accessPayload)) {
    throw new Error(errorMessage(accessPayload as ErrorEnvelope, "无法取得原始证据页的短时预览许可"));
  }
  if (accessPayload.evidence_page_id !== pageId) throw new Error("原始证据页预览许可与当前页面不一致。");
  const contentResponse = await persistentApiFetch(
    config,
    `/v1/matters/${config.matterId}/evidence-pages/${pageId}/original-preview/content`,
    {
      headers: { Accept: "image/png", Authorization: `Bearer ${accessPayload.access_token}` },
    },
    "provided-bearer",
  );
  if (!contentResponse.ok) {
    const payload = (await contentResponse.json().catch(() => ({}))) as ErrorEnvelope;
    throw new Error(errorMessage(payload, "原始证据页预览失败"));
  }
  if (contentResponse.headers.get("Content-Type")?.split(";", 1)[0] !== "image/png") {
    throw new Error("原始证据页返回了非 PNG 内容，已停止预览。");
  }
  const contentLength = Number(contentResponse.headers.get("Content-Length") || "0");
  if (contentLength > 30 * 1024 * 1024) throw new Error("原始证据页预览超过本机大小上限。");
  const blob = await contentResponse.blob();
  if (blob.size < 24 || blob.size > 30 * 1024 * 1024) throw new Error("原始证据页预览为空或超过本机大小上限。");
  const returnedHash = contentResponse.headers.get("X-Artifact-SHA256");
  const actualHash = await sha256Bytes(await blob.arrayBuffer());
  if (!returnedHash || returnedHash !== actualHash) throw new Error("原始证据页预览哈希核验失败，已停止显示。");
  const width = Number(contentResponse.headers.get("X-Image-Width") || "0");
  const height = Number(contentResponse.headers.get("X-Image-Height") || "0");
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1) {
    throw new Error("原始证据页预览尺寸无效，已停止显示。");
  }
  return { pageId, blob, contentSha256: actualHash, width, height };
}

export async function proposeEvidencePageDecision(
  input: { review: EvidenceReviewView; pageId: string; disposition: "INCLUDE" | "EXCLUDE"; reason: string },
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceMutationReceipt> {
  const { matterVersion, persistentConfig } = requireEvidenceMutationContext(input.review, config);
  const reason = input.reason.trim();
  if (!reason) throw new Error("请填写本页纳入或排除的具体理由。");
  return postEvidenceMutation({
    config: persistentConfig,
    path: `evidence-pages/${input.pageId}/decisions`,
    body: { expected_version: matterVersion, disposition: input.disposition, reason },
    expectedObjectType: "EVIDENCE_PAGE_DECISION",
    fallback: "页级处置候选未建立",
    interrupted: "连接在页级处置候选确认前中断。请先刷新证据快照；系统不会盲目重复提交。",
  });
}

export async function approveEvidencePageDecision(
  input: { review: EvidenceReviewView; page: EvidenceReviewPage },
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceMutationReceipt> {
  const { matterVersion, persistentConfig } = requireEvidenceMutationContext(input.review, config);
  const pending = input.page.pendingDecision;
  if (!pending) throw new Error("当前页没有可批准的处置候选。");
  const approvalHash = await sha256Text([
    "evidence-page-decision-approval-v1",
    persistentConfig.matterId,
    String(matterVersion),
    input.review.snapshotHash,
    input.page.pageId,
    pending.decisionId,
    pending.disposition,
    pending.reason,
  ].join("|"));
  return postEvidenceMutation({
    config: persistentConfig,
    path: `evidence-page-decisions/${pending.decisionId}/approve`,
    body: { expected_version: matterVersion, approval_hash: approvalHash },
    expectedObjectType: "EVIDENCE_PAGE_DECISION",
    fallback: "页级处置未获批准",
    interrupted: "连接在页级处置批准确认前中断。请先刷新证据快照；系统不会重复提交律师决定。",
  });
}

export async function approveEvidenceAnnotation(
  input: { review: EvidenceReviewView; page: EvidenceReviewPage; annotationId: string },
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceMutationReceipt> {
  const { matterVersion, persistentConfig } = requireEvidenceMutationContext(input.review, config);
  const annotation = input.page.annotations.find((item) => item.annotationId === input.annotationId);
  if (!annotation || annotation.status !== "CANDIDATE") throw new Error("当前红框不是可批准候选。");
  const approvalHash = await sha256Text([
    "evidence-annotation-approval-v1",
    persistentConfig.matterId,
    String(matterVersion),
    input.review.snapshotHash,
    input.page.pageId,
    annotation.annotationId,
    annotation.label,
    String(annotation.x0),
    String(annotation.y0),
    String(annotation.x1),
    String(annotation.y1),
  ].join("|"));
  return postEvidenceMutation({
    config: persistentConfig,
    path: `evidence-annotations/${annotation.annotationId}/approve`,
    body: { expected_version: matterVersion, approval_hash: approvalHash },
    expectedObjectType: "EVIDENCE_ANNOTATION",
    fallback: "红框候选未获批准",
    interrupted: "连接在红框批准确认前中断。请先刷新证据快照；系统不会重复提交律师决定。",
  });
}

export async function proposeEvidenceAnnotation(
  input: {
    review: EvidenceReviewView;
    pageId: string;
    x0: number;
    y0: number;
    x1: number;
    y1: number;
    label: string;
  },
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceMutationReceipt> {
  const { matterVersion, persistentConfig } = requireEvidenceMutationContext(input.review, config);
  const label = input.label.trim();
  if (!label) throw new Error("请填写红框所标识内容的简短说明。");
  const coordinates = [input.x0, input.y0, input.x1, input.y1];
  if (coordinates.some((value) => !Number.isFinite(value) || value < 0 || value > 1)) {
    throw new Error("红框坐标不在当前原始页范围内。");
  }
  if (input.x1 - input.x0 < 0.005 || input.y1 - input.y0 < 0.005) {
    throw new Error("红框范围太小，请重新拖选需要标识的区域。");
  }
  return postEvidenceMutation({
    config: persistentConfig,
    path: `evidence-pages/${input.pageId}/annotations`,
    body: {
      expected_version: matterVersion,
      x0: input.x0.toFixed(9),
      y0: input.y0.toFixed(9),
      x1: input.x1.toFixed(9),
      y1: input.y1.toFixed(9),
      label,
    },
    expectedObjectType: "EVIDENCE_ANNOTATION",
    fallback: "红框候选未建立",
    interrupted: "连接在红框候选确认前中断。请先刷新证据快照；系统不会盲目重复提交。",
  });
}

export async function resolveEvidenceDuplicateGroup(
  input: { review: EvidenceReviewView; groupId: string; sameSourcePage: boolean; canonicalPageId: string | null },
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceMutationReceipt> {
  const { matterVersion, persistentConfig } = requireEvidenceMutationContext(input.review, config);
  const group = input.review.duplicateGroups.find((item) => item.groupId === input.groupId);
  if (!group || group.status !== "CANDIDATE") throw new Error("当前重复页组不是可裁决候选。");
  if (input.sameSourcePage && !input.canonicalPageId) throw new Error("判定为同一来源页时必须选择唯一保留页。");
  if (input.canonicalPageId && !group.pageIds.includes(input.canonicalPageId)) throw new Error("唯一保留页不属于当前重复页组。");
  const approvalHash = await sha256Text([
    "evidence-duplicate-resolution-approval-v1",
    persistentConfig.matterId,
    String(matterVersion),
    input.review.snapshotHash,
    group.groupId,
    [...group.pageIds].sort().join(","),
    input.sameSourcePage ? "SAME_SOURCE_PAGE" : "DISTINCT_PAGES",
    input.canonicalPageId ?? "NO_CANONICAL_PAGE",
  ].join("|"));
  return postEvidenceMutation({
    config: persistentConfig,
    path: `evidence-duplicate-groups/${group.groupId}/resolve`,
    body: {
      expected_version: matterVersion,
      approval_hash: approvalHash,
      same_source_page: input.sameSourcePage,
      canonical_page_id: input.canonicalPageId,
    },
    expectedObjectType: "EVIDENCE_DUPLICATE_GROUP",
    fallback: "重复页结论未记录",
    interrupted: "连接在重复页裁决确认前中断。请先刷新证据快照；系统不会重复提交律师决定。",
  });
}

export async function lockEvidenceManifest(
  review: EvidenceReviewView,
  config: CaseDataSourceConfig = caseDataSourceConfig,
): Promise<EvidenceMutationReceipt> {
  const { matterVersion, persistentConfig } = requireEvidenceMutationContext(review, config);
  if (review.lockedManifest) throw new Error("当前证据清单已经锁定。");
  if (review.totalPages < 1) throw new Error("当前案件没有可锁定的来源页。");
  if (review.unresolvedPageCount > 0 || review.pendingDecisionCount > 0) {
    throw new Error("仍有来源页未批准或存在待批准的新处置，不能锁定证据清单。");
  }
  if (review.unresolvedDuplicateCount > 0) {
    throw new Error("仍有重复页候选未裁决，不能锁定证据清单。");
  }
  const approvalHash = await sha256Text([
    "evidence-manifest-lock-approval-v2",
    persistentConfig.matterId,
    String(matterVersion),
    review.snapshotHash,
    review.manifestReadinessHash,
  ].join("|"));
  return postEvidenceMutation({
    config: persistentConfig,
    path: "evidence-manifests/lock",
    body: { expected_version: matterVersion, approval_hash: approvalHash, readiness_hash: review.manifestReadinessHash },
    expectedObjectType: "EVIDENCE_MANIFEST",
    fallback: "证据清单未锁定",
    interrupted: "连接在证据清单锁定确认前中断。请先刷新证据快照；系统不会重复锁定。",
  });
}

function requireEvidenceMutationContext(
  review: EvidenceReviewView,
  config: CaseDataSourceConfig,
): {
  matterVersion: number;
  persistentConfig: Extract<CaseDataSourceConfig, { kind: "persistent-preview" }>;
} {
  if (config.kind !== "persistent-preview") throw new Error("只有持久化工作台可以记录正式证据决定。");
  if (review.sourceKind !== "persistent-preview" || review.matterVersion === null) {
    throw new Error("当前证据快照不是可写入的持久化版本。");
  }
  return { matterVersion: review.matterVersion, persistentConfig: config };
}

async function postEvidenceMutation(input: {
  config: Extract<CaseDataSourceConfig, { kind: "persistent-preview" }>;
  path: string;
  body: Record<string, unknown>;
  expectedObjectType: string;
  fallback: string;
  interrupted: string;
}): Promise<EvidenceMutationReceipt> {
  let response: Response;
  try {
    response = await persistentApiFetch(input.config, `/v1/matters/${input.config.matterId}/${input.path}`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify(input.body),
    });
  } catch {
    throw new Error(input.interrupted);
  }
  const payload = (await response.json()) as
    | { object_id: string; matter_version: number; object_type: string }
    | ErrorEnvelope;
  if (!response.ok || !("object_id" in payload)) {
    throw new Error(errorMessage(payload as ErrorEnvelope, input.fallback));
  }
  if (payload.object_type !== input.expectedObjectType) {
    throw new Error("证据命令回执类型不一致，已停止后续处理。");
  }
  return {
    objectId: payload.object_id,
    matterVersion: payload.matter_version,
    requestId: response.headers.get("X-Request-ID"),
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
    response = await persistentApiFetch(
      config,
      `/v1/matters/${config.matterId}/evidence-manifests/${review.lockedManifest.manifestId}/derivative-runs`,
      {
        method: "POST",
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
    factPage: { loadedCount: payload.facts.length + payload.pending_facts.length, totalCount: payload.facts.length + payload.pending_facts.length, nextCursor: null, hasMore: false },
    transactionPage: { loadedCount: payload.transactions.length, totalCount: payload.transactions.length, nextCursor: null, hasMore: false },
  };
}

function mapPersistentReview(
  summary: PersistentReviewSummary,
  factPage: PersistentFactPage,
  transactionPage: PersistentTransactionPage,
  requestId: string | null,
): CaseReviewView {
  if (factPage.matter_version !== summary.version || transactionPage.matter_version !== summary.version) {
    throw new Error("案件在读取分页期间已变化，请重新载入当前案件。");
  }
  const mappedFacts = mapPersistentFacts(factPage.items);
  return {
    sourceKind: "persistent-preview",
    sourceLabel: "持久化内部预览",
    matterTitle: summary.title,
    matterVersion: summary.version,
    snapshotHash: summary.summary_hash,
    transactionSnapshotHash: null,
    requestId,
    facts: mappedFacts.facts,
    claims: summary.claims.map((item) => ({ claimId: item.claim_id, text: item.original_claim_text, amount: item.claimed_amount, currency: item.currency, position: item.response?.position ?? "待律师回应", responseAmount: item.response?.partial_amount ?? null })),
    issues: summary.issues.map((item) => ({ issueId: item.issue_id, question: item.question, claimCount: item.claim_count, factCount: item.fact_count, status: item.status })),
    transactions: mapPersistentTransactions(transactionPage.items),
    pendingFacts: mappedFacts.pendingFacts,
    factPage: { loadedCount: factPage.items.length, totalCount: factPage.total_count, nextCursor: factPage.next_cursor, hasMore: factPage.has_more },
    transactionPage: { loadedCount: transactionPage.items.length, totalCount: transactionPage.total_count, nextCursor: transactionPage.next_cursor, hasMore: transactionPage.has_more },
  };
}

function mapPersistentFacts(items: PersistentFactPage["items"]) {
  return {
    facts: items.filter((item) => item.status !== "CANDIDATE").map((item) => ({ factId: item.fact_id, text: item.original_text, origin: item.origin, status: item.status, evidenceCount: item.evidence_count })),
    pendingFacts: items.filter((item) => item.status === "CANDIDATE").map((item) => ({ factId: item.fact_id, text: item.original_text, origin: item.origin, evidenceCount: item.evidence_count })),
  };
}

function mapPersistentTransactions(items: PersistentTransactionPage["items"]): CaseReviewView["transactions"] {
  return items.map((item) => ({
    transactionId: item.transaction_id,
    date: item.local_date,
    amount: item.amount,
    currency: item.currency,
    nature: item.classification_nature ?? "待分类",
    application: paymentApplication(item.classification_nature ?? undefined),
    status: item.classification_status ?? item.status,
  }));
}

function mapSyntheticEvidence(): EvidenceReviewView {
  const pages: EvidenceReviewPage[] = syntheticMatter.evidence.map((item) => ({
    pageId: `synthetic-page-${item.page}`,
    fileId: "synthetic-wechat-ledger",
    originalLabel: "[合成] 微信交易记录.pdf",
    pageNumber: item.page,
    decisionId: item.confidence === "已核验" ? `synthetic-decision-${item.page}` : null,
    disposition: item.confidence === "已核验" ? "INCLUDE" : null,
    reason: item.confidence === "已核验" ? "[合成] 与目标主体相关。" : null,
    pendingDecision: null,
    annotations: item.confidence === "已核验" ? [{ annotationId: `synthetic-annotation-${item.page}`, x0: 0.08, y0: 0.32, x1: 0.92, y1: 0.52, label: "[合成] 相关交易行", status: "APPROVED" }] : [],
    syntheticPreview: { date: item.date, amount: item.amount, counterpart: item.counterpart, confidence: item.confidence, note: item.note },
  }));
  return {
    sourceKind: "synthetic-alpha",
    sourceLabel: "本机合成数据",
    matterVersion: null,
    snapshotHash: "synthetic-evidence-manifest-preview",
    manifestReadinessHash: "synthetic-evidence-readiness-preview",
    requestId: null,
    totalPages: pages.length,
    unresolvedPageCount: pages.filter((page) => !page.decisionId).length,
    pendingDecisionCount: 0,
    unresolvedDuplicateCount: 1,
    originals: [{ fileId: "synthetic-wechat-ledger", originalLabel: "[合成] 微信交易记录.pdf", originalFileSha256: "synthetic-only", pageCount: syntheticMatter.evidence.length }],
    pages,
    pagePage: { loadedCount: pages.length, totalCount: pages.length, nextCursor: null, hasMore: false },
    duplicateGroups: [{
      groupId: "synthetic-duplicate-17-18",
      status: "CANDIDATE",
      canonicalPageId: null,
      pageIds: ["synthetic-page-17", "synthetic-page-18"],
      pageLabels: { "synthetic-page-17": "[合成] 微信交易记录.pdf · 第 17 页", "synthetic-page-18": "[合成] 微信交易记录.pdf · 第 18 页" },
    }],
    lockedManifest: null,
    derivatives: [],
    derivativeRuns: [],
  };
}

function mapPersistentEvidence(
  summary: PersistentEvidenceReviewSummary,
  page: PersistentEvidencePagePage,
  requestId: string | null,
): EvidenceReviewView {
  return {
    sourceKind: "persistent-preview",
    sourceLabel: "持久化内部预览",
    matterVersion: summary.version,
    snapshotHash: summary.summary_hash,
    manifestReadinessHash: summary.manifest_readiness_hash,
    requestId,
    totalPages: summary.total_pages,
    unresolvedPageCount: summary.unresolved_page_count,
    pendingDecisionCount: summary.pending_decision_count,
    unresolvedDuplicateCount: summary.unresolved_duplicate_count,
    originals: summary.original_files.map((item) => ({ fileId: item.evidence_file_id, originalLabel: item.original_label, originalFileSha256: item.original_file_sha256, pageCount: item.page_count })),
    pages: page.items.map(mapPersistentEvidencePage),
    pagePage: { loadedCount: page.items.length, totalCount: page.total_count, nextCursor: page.next_cursor, hasMore: page.has_more },
    duplicateGroups: summary.duplicate_groups.map((item) => ({
      groupId: item.duplicate_group_id,
      status: item.status,
      canonicalPageId: item.canonical_page_id,
      pageIds: item.members.map((member) => member.evidence_page_id),
      pageLabels: Object.fromEntries(item.members.map((member) => [member.evidence_page_id, `${member.original_label} · 第 ${member.page_number} 页`])),
    })),
    lockedManifest: summary.locked_manifest ? { manifestId: summary.locked_manifest.manifest_id, contentHash: summary.locked_manifest.content_hash, totalPages: summary.locked_manifest.total_pages, includedPages: summary.locked_manifest.included_pages, excludedPages: summary.locked_manifest.excluded_pages } : null,
    derivatives: summary.derivatives.map((item) => ({ derivativeId: item.derivative_id, artifactType: item.artifact_type, artifactSha256: item.artifact_sha256, pageCount: item.page_count, status: item.status })),
    derivativeRuns: summary.derivative_runs.map((item) => ({ runId: item.run_id, manifestId: item.manifest_id, status: item.status, attemptCount: item.attempt_count, failureCode: item.failure_code })),
  };
}

function mapPersistentEvidencePage(item: PersistentEvidencePageItem): EvidenceReviewPage {
  return {
    pageId: item.evidence_page_id,
    fileId: item.evidence_file_id,
    originalLabel: item.original_label,
    pageNumber: item.page_number,
    decisionId: item.decision?.decision_id ?? null,
    disposition: item.decision?.disposition ?? null,
    reason: item.decision?.reason ?? null,
    pendingDecision: item.pending_decision ? {
      decisionId: item.pending_decision.decision_id,
      disposition: item.pending_decision.disposition,
      reason: item.pending_decision.reason,
      status: item.pending_decision.status,
    } : null,
    annotations: item.annotations.map((annotation) => ({ annotationId: annotation.annotation_id, x0: Number(annotation.x0), y0: Number(annotation.y0), x1: Number(annotation.x1), y1: Number(annotation.y1), label: annotation.label, status: annotation.status })),
    syntheticPreview: null,
  };
}

function mapLocalFolderIntake(
  summary: PersistentLocalFolderIntakeSummary,
  page: PersistentLocalFolderFilePage,
  runSummary: PersistentEvidenceIntakeSummary,
): LocalFolderIntakeView {
  const displayed = summary.candidate_scan ?? summary.approved_scan;
  return {
    matterVersion: summary.matter_version,
    summaryHash: summary.summary_hash,
    approvedScan: summary.approved_scan ? mapLocalFolderScanSummary(summary.approved_scan) : null,
    candidateScan: summary.candidate_scan ? mapLocalFolderScanSummary(summary.candidate_scan) : null,
    displayedScanId: displayed?.scan_id ?? null,
    files: mapLocalFolderFiles(page.items),
    filePage: {
      loadedCount: page.items.length,
      totalCount: page.total_count,
      nextCursor: page.next_cursor,
      hasMore: page.has_more,
    },
    intakeRun: mapEvidenceIntakeRun(runSummary.run),
  };
}

function mapEvidenceIntakeRun(run: PersistentEvidenceIntakeSummary["run"]): LocalFolderIntakeView["intakeRun"] {
  if (!run) return null;
  return {
    runId: run.run_id,
    scanId: run.scan_id,
    scanManifestHash: run.scan_manifest_hash,
    status: run.status,
    totalItems: run.total_items,
    queuedItems: run.queued_items,
    runningItems: run.running_items,
    registeredItems: run.registered_items,
    reviewRequiredItems: run.review_required_items,
    blockedItems: run.blocked_items,
    failedItems: run.failed_items,
    createdAt: run.created_at,
    completedAt: run.completed_at,
  };
}

function mapLocalFolderScanSummary(item: PersistentLocalFolderScanSummary): LocalFolderScanSummary {
  return {
    scanId: item.scan_id,
    manifestHash: item.manifest_hash,
    baseScanId: item.base_scan_id,
    status: item.status,
    totalFiles: item.total_files,
    totalBytes: item.total_bytes,
    skippedSymlinks: item.skipped_symlinks,
    newCount: item.new_count,
    modifiedCount: item.modified_count,
    movedCount: item.moved_count,
    missingCount: item.missing_count,
    unchangedCount: item.unchanged_count,
    duplicateContentCount: item.duplicate_content_count,
    scannedAt: item.scanned_at,
    approvedAt: item.approved_at,
  };
}

function mapLocalFolderFiles(items: PersistentLocalFolderFilePage["items"]): LocalFolderIntakeView["files"] {
  return items.map((item) => ({
    relativePath: item.relative_path,
    previousRelativePath: item.previous_relative_path,
    byteSize: item.byte_size,
    fileSha256: item.file_sha256,
    detectedKind: item.detected_kind,
    changeKind: item.change_kind,
    present: item.present,
  }));
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
    licenseBasis: null,
    licenseReviewHash: null,
    captureRunId: null,
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
      licenseBasis: item.license_basis ?? null,
      licenseReviewHash: item.license_review_hash ?? null,
      captureRunId: item.capture_run_id ?? null,
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
    if (url.username || url.password || url.search || url.hash || (url.pathname !== "/" && url.pathname !== "")) return false;
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

async function sha256Bytes(value: ArrayBuffer): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error("当前浏览器无法核验本机预览哈希。");
  const digest = await globalThis.crypto.subtle.digest("SHA-256", value);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
