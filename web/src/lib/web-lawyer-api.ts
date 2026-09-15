/**
 * Narrow browser contracts for the first Web workflow:
 * authenticated lawyer -> case -> explicit PDF upload -> server receipt.
 *
 * This module deliberately does not use browser storage, local file paths, or
 * a bearer token.  `webApiFetch` keeps the request same-origin and supplies
 * the server-issued double-submit CSRF value for every write.
 */

import { webApiFetch } from "@/lib/web-api-client";

export type WebFactCorrectionDraft = Readonly<{
  proposalId: string; revision: number; stale: boolean;
  originalText: string; revisedText: string; reason: string;
  excerpts:readonly {pageId:string;text:string}[];
}>;

export type WebFactCorrectionSubmission = Readonly<{
  factId:string; proposalId:string;
  status:"CANDIDATE"|"CONFIRMED"|"DISPUTED"|"DENIED"|"INVALIDATED";
}>;

export async function readWebFactCorrectionSubmission(caseId:string,candidateId:string):Promise<{
  currentMatterVersion:number;submission:WebFactCorrectionSubmission|null;
}> {
  const response=await webApiFetch(`/api/v1/cases/${normalizeOpaqueId(caseId,"案件编号")}/fact-corrections/${normalizeOpaqueId(candidateId,"候选编号")}/submission`);
  const value=asRecord(await readJsonResponse(response,"读取事实送审状态"),"送审状态无效");
  if(value.court_ready!==false)throw protocolError("送审状态不能表示法院可提交");
  const currentMatterVersion=requiredPositiveInteger(value.current_matter_version,"案件版本无效",Number.MAX_SAFE_INTEGER);
  if(value.submission===null)return {currentMatterVersion,submission:null};
  const item=asRecord(value.submission,"事实关联无效");
  const status=item.status;
  if(status!=="CANDIDATE"&&status!=="CONFIRMED"&&status!=="DISPUTED"&&status!=="DENIED"&&status!=="INVALIDATED")throw protocolError("事实状态无效");
  return {currentMatterVersion,submission:{factId:normalizeOpaqueId(item.fact_id,"事实编号"),
    proposalId:normalizeOpaqueId(item.proposal_id,"送审稿编号"),status}};
}

function correctionSubmissionFact(value:Record<string,unknown>):string|null {
  if(value.court_ready!==false)throw protocolError("送审回执状态无效");
  if(value.receipt===null)return null;
  const receipt=asRecord(value.receipt,"送审回执无效");
  if(receipt.command_name!=="CREATE_FACT_CANDIDATE_FROM_CORRECTION"||receipt.object_type!=="FACT")throw protocolError("送审回执类型无效");
  return normalizeOpaqueId(receipt.object_id,"送审事实编号");
}

export async function recoverWebFactCorrectionSubmission(caseId:string,key:string):Promise<string|null> {
  const response=await webApiFetch(`/api/v1/cases/${normalizeOpaqueId(caseId,"案件编号")}/fact-correction-submission-receipt`,
    {headers:{"Idempotency-Key":normalizeIdempotencyKey(key)}});
  return correctionSubmissionFact(asRecord(await readJsonResponse(response,"查询事实送审结果"),"送审回执无效"));
}

export async function submitWebFactCorrection(caseId:string,candidateId:string,proposalId:string,version:number,key:string):Promise<string> {
  const response=await webApiFetch(`/api/v1/cases/${normalizeOpaqueId(caseId,"案件编号")}/fact-corrections/${normalizeOpaqueId(candidateId,"候选编号")}/proposals/${normalizeOpaqueId(proposalId,"修改稿编号")}/submit`,{
    method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":normalizeIdempotencyKey(key)},
    body:JSON.stringify({expected_matter_version:version}),
  });
  const fact=correctionSubmissionFact(asRecord(await readJsonResponse(response,"送入事实审批"),"送审回执无效"));
  if(fact===null)throw protocolError("送审未返回确定回执，请查询原请求");
  return fact;
}

export async function readWebFactCorrection(caseId: string, candidateId: string): Promise<{draft:WebFactCorrectionDraft|null;currentMatterVersion:number}> {
  const response = await webApiFetch(`/api/v1/cases/${normalizeOpaqueId(caseId, "案件编号")}/fact-corrections/${normalizeOpaqueId(candidateId, "候选编号")}`);
  const value = asRecord(await readJsonResponse(response, "读取修改稿"), "修改稿响应无效");
  const currentMatterVersion=requiredPositiveInteger(value.current_matter_version,"当前案件版本无效",Number.MAX_SAFE_INTEGER);
  if (value.draft === null) return {draft:null,currentMatterVersion};
  const draft = asRecord(value.draft, "修改稿无效");
  if (draft.court_ready !== false || draft.review_status !== "NEEDS_LAWYER_REVIEW" || typeof draft.stale !== "boolean") throw protocolError("修改稿审核状态无效");
  const proposal = asRecord(draft.proposal, "修改稿内容无效");
  const original = asRecord(proposal.original_candidate, "原候选无效");
  return {currentMatterVersion,draft:{ proposalId: normalizeOpaqueId(draft.proposal_id, "修改稿编号"),
    revision: requiredPositiveInteger(draft.revision_number, "修订号无效",999), stale:draft.stale,
    originalText:requiredText(original.fact_text,"原候选文字无效",4000),
    revisedText:requiredText(proposal.revised_text,"修改稿文字无效",4000),reason:requiredText(proposal.reason,"修改理由无效",2000),
    excerpts:parseArray(original.supporting_excerpts,"原始摘录",item=>{
      const excerpt=asRecord(item,"原始摘录无效");
      return {pageId:normalizeOpaqueId(excerpt.evidence_page_id,"摘录页编号"),text:requiredText(excerpt.text,"原始摘录内容无效",4000)};
    }) }};
}

export async function recoverWebFactCorrection(caseId: string, key: string): Promise<string | null> {
  const response = await webApiFetch(`/api/v1/cases/${normalizeOpaqueId(caseId,"案件编号")}/fact-correction-receipt`,
    {headers:{"Idempotency-Key":normalizeIdempotencyKey(key)}});
  const value=asRecord(await readJsonResponse(response,"查询修改稿保存结果"),"修改稿回执无效");
  if(value.receipt===null) return null;
  return normalizeOpaqueId(asRecord(value.receipt,"修改稿回执无效").proposal_id,"修改稿编号");
}

export async function saveWebFactCorrection(caseId:string,candidateId:string,version:number,revision:number,text:string,reason:string,key:string):Promise<void> {
  const response=await webApiFetch(`/api/v1/cases/${normalizeOpaqueId(caseId,"案件编号")}/fact-corrections/${normalizeOpaqueId(candidateId,"候选编号")}`,{
    method:"POST",headers:{"Content-Type":"application/json","Idempotency-Key":normalizeIdempotencyKey(key)},
    body:JSON.stringify({expected_matter_version:version,expected_revision:revision,revised_text:text,reason}),
  });
  const value=asRecord(await readJsonResponse(response,"保存修改稿"),"修改稿保存回执无效");
  if(value.court_ready!==false) throw protocolError("修改稿不得视为已批准");
  normalizeOpaqueId(asRecord(value.receipt,"修改稿回执无效").proposal_id,"修改稿编号");
}
import {
  buildWebAgentLedgerFollowupActionPayload,
  buildWebAgentLedgerPageQuery,
  buildWebAgentLedgerRecoveryPayload,
  webAgentLedgerExceptionCapacityMessage,
  type WebAgentLedgerFollowupActionCode,
  type WebAgentLedgerFollowupAutomationStatus,
  type WebManagedEvidenceSourceSelection,
} from "@/lib/web-agent-ledger-followup-contract";
import {
  buildActivePlanExecutionPayload,
  buildCaseAgentCompletionPayload,
} from "@/lib/web-active-plan-execution";

export type {
  WebAgentLedgerFollowupActionCode,
  WebAgentLedgerFollowupAutomationStatus,
  WebManagedEvidenceSourceSelection,
} from "@/lib/web-agent-ledger-followup-contract";

const CASE_TITLE_MIN_LENGTH = 2;
const CASE_TITLE_MAX_LENGTH = 160;
const FILE_NAME_MAX_LENGTH = 240;
const OPAQUE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/i;

/** Kept in sync with the server-side Web PDF staging limit. */
export const WEB_MAX_PDF_BYTES = 256 * 1024 * 1024;
export const WEB_MAX_IMAGE_BYTES = 64 * 1024 * 1024;
export const WEB_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024;
/** Kept in sync with the server-side common-material admission policy. */
export const WEB_MAX_COMMON_MATERIAL_BYTES = 100 * 1024 * 1024;

const COMMON_MATERIAL_CONTENT_TYPES = {
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".rtf": "application/rtf",
  ".txt": "text/plain",
  ".csv": "text/csv",
  ".html": "text/html",
  ".htm": "text/html",
  ".eml": "message/rfc822",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
} as const;

const LEGACY_COMMON_MATERIAL_SUFFIXES = new Set([".doc", ".xls", ".ppt", ".msg", ".ofd"]);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type WebLawyerSession = Readonly<{
  actor: Readonly<{
    roles: readonly string[];
    recoveryNamespace?: string;
  }>;
  capabilities: Readonly<{
    canCreateCase: boolean;
    canConfirmFact: boolean;
    canReviewEvidence: boolean;
    canReviewFacts: boolean;
    canReviewLegal: boolean;
    canReviewSubmission: boolean;
    canRunAgent: boolean;
    canDraftDefenceBrief: boolean;
    canRunCaseAgent: boolean;
    canExecuteActivePlan: boolean;
    canCompleteCaseAgentRun: boolean;
    canReviewCaseAgent: boolean;
    canReviewCaseAgentDocuments: boolean;
    canReviewDynamicCasePlan: boolean;
    canReviewAgentLedgerExtractions: boolean;
    canReviewAgentLedgerExceptionFollowups: boolean;
    canRunAgentLedgerExtraction: boolean;
    canRunCalculation: boolean;
    canUploadMaterial: boolean;
    canUploadCommonMaterial: boolean;
    canReviewCasePosture: boolean;
    canConfirmCasePosture: boolean;
  }>;
  expiresAt: string | null;
  /**
   * The browser always renders the same workbench.  This marker only tells
   * the UI whether it is attached to the firm-managed service or to the
   * deliberately limited offline development runtime.
   */
  workspaceMode: "FIRM_MANAGED" | "LOCAL_DEVELOPMENT";
}>;

export type WebLawyerCase = Readonly<{
  caseId: string;
  title: string;
  version: number;
  updatedAt: string | null;
  materialCount: number;
}>;

/**
 * A confirmed representation/procedure profile is a narrow case input, not a
 * preset litigation workflow.  The Agent uses it together with the admitted
 * materials and verified sources to propose work; it never turns a party
 * position directly into a fixed document list.
 */
export type WebCasePostureStatus = "NOT_CONFIRMED" | "CURRENT" | "STALE";

export type WebCasePostureProfile = Readonly<{
  profileId: string;
  profileVersion: number;
  representedPartyId: string;
  representedPartyDisplayLabel: string;
  representedPartyKind: string;
  proceedingId: string;
  forumType: string;
  positionId: string;
  engagementId: string;
  caseTypeCode: string;
  procedureStage: string;
  representedPosition: string;
  authorityScopeCode: string;
  engagementState: string;
  confirmedMatterVersion: number;
}>;

export type WebCasePostureOptions = Readonly<{
  partyKinds: readonly string[];
  forumTypes: readonly string[];
  caseTypes: readonly string[];
  procedureStages: readonly string[];
  partyPositions: readonly string[];
  authorityScopes: readonly string[];
  engagementStates: readonly string[];
}>;

export type WebCasePosture = Readonly<{
  status: WebCasePostureStatus;
  canConfirm: boolean;
  profile: WebCasePostureProfile | null;
  options: WebCasePostureOptions;
}>;

export type WebCasePostureCommandReceipt = Readonly<{
  action: "CONFIRM_PARTY" | "CONFIRM_PROCEEDING" | "CONFIRM_POSITION" | "CONFIRM_ENGAGEMENT" | "CONFIRM_CURRENT_PROFILE";
  matterVersion: number;
  objectType: string;
  objectId: string;
}>;

export type WebCasePostureCompleteReceipt = Readonly<{
  action: "CONFIRM_COMPLETE_POSTURE";
  matterVersion: number;
  partyId: string;
  proceedingId: string;
  positionId: string;
  engagementId: string;
  profileId: string;
}>;

export type WebMaterialUploadSlot = Readonly<{
  uploadId: string;
}>;

export type WebMaterialArchiveUploadSlot = Readonly<{
  archiveId: string;
}>;

export type WebCommonMaterialUploadSlot = Readonly<{
  uploadId: string;
}>;

export type WebMaterialArchiveReceipt = Readonly<{
  archiveId: string;
  displayName: string;
  sha256: string;
  byteSize: number;
  entryCount: number;
  expandedByteSize: number;
  processingStatus: "STORED_PENDING_PROCESSING";
}>;

export type WebMaterialUploadStatus = Readonly<{
  operationId: string;
  kind: "PDF" | "ZIP";
  state: "PROCESSING" | "COMPLETED" | "REJECTED" | "EXPIRED" | "RECONCILIATION_REQUIRED" | "STORED_PENDING_PROCESSING";
  receipt: WebMaterialReceipt | WebMaterialArchiveReceipt | null;
}>;

export type WebCommonMaterialAdmissionReceipt = Readonly<{
  materialObjectId: string;
  displayName: string;
  admittedFormat: "DOCX" | "XLSX" | "PPTX" | "RTF" | "TXT" | "CSV" | "HTML" | "EML" | "JPEG" | "PNG";
  mediaType: string;
  byteSize: number;
  sha256: string;
  route: "COMMON_DOCUMENT_READER" | "VISUAL_OCR";
  reviewStatus: "NEEDS_LAWYER_REVIEW";
  agentStatus: "AGENT_READY" | "INGESTED_PENDING_ADAPTER";
  agentSourceRef: string | null;
  matterVersion: number;
}>;

export type WebCommonMaterialUploadStatus = Readonly<{
  operationId: string;
  kind: "COMMON";
  state: "PROCESSING" | "COMPLETED" | "REJECTED" | "EXPIRED" | "RECONCILIATION_REQUIRED" | "ADMISSION_UNAVAILABLE";
  receipt: WebCommonMaterialAdmissionReceipt | null;
}>;

export type WebMaterialReceipt = Readonly<{
  evidenceFileId: string;
  displayName: string;
  sha256: string;
  pageCount: number;
  scanStatus: string;
  matterVersion: number;
  receivedAt: string | null;
}>;

export type WebCaseReview = Readonly<{
  matterId: string;
  title: string;
  stage: string;
  version: number;
  snapshotHash: string;
  facts: readonly WebCaseFact[];
  claims: readonly WebCaseClaim[];
  issues: readonly WebCaseIssue[];
  transactions: readonly WebCaseTransaction[];
  paymentClassifications: readonly WebPaymentClassification[];
}>;

export type WebCaseFact = Readonly<{
  factId: string;
  text: string;
  origin: string;
  status: string;
  evidenceCount: number;
  decisionHash: string | null;
  correctionCandidateId?:string|null;
  evidenceSources?:readonly {pageId:string;label:string;pageNumber:number|null}[];
}>;

export type WebCaseClaim = Readonly<{
  claimId: string;
  text: string;
  claimedAmount: string | null;
  currency: string | null;
  status: string;
  evidenceCount: number;
  response: Readonly<{ position: string; partialAmount: string | null; currency: string | null }> | null;
}>;

export type WebCaseIssue = Readonly<{
  issueId: string;
  question: string;
  status: string;
  claimIds: readonly string[];
  confirmedFactIds: readonly string[];
}>;

export type WebCaseTransaction = Readonly<{
  transactionId: string;
  localDate: string | null;
  datePrecision: string | null;
  amount: string | null;
  currency: string | null;
  direction: string;
  payerLabel: string | null;
  payeeLabel: string | null;
  channel: string | null;
  transactionReference: string | null;
  status: string;
  evidenceCount: number;
  confirmationHash: string | null;
}>;

export type WebPaymentClassification = Readonly<{
  classificationId: string;
  transactionId: string;
  nature: string;
  sameDaySequence: number | null;
  status: string;
  evidenceCount: number;
  allocations: readonly Readonly<{ obligationLabel: string; amount: string | null; currency: string | null }>[];
}>;

export type WebAgentLedgerExtractionExcerpt = Readonly<{
  evidencePageId: string;
  pageNumber: number;
  text: string;
}>;

export type WebAgentLedgerExtractionCandidate = Readonly<{
  sequence: number;
  candidateKind: "FACT" | "TRANSACTION";
  summary: string;
  confidence: number;
  reviewStatus: "LOW_RISK" | "EXCEPTION";
  reviewReasons: readonly string[];
  excerpts: readonly WebAgentLedgerExtractionExcerpt[];
}>;

export type WebAgentLedgerExceptionDecision =
  | "REJECT_AS_DUPLICATE"
  | "REQUEST_REEXTRACTION"
  | "REQUEST_MORE_EVIDENCE"
  | "DEFER_WITH_REASON";

export type WebAgentLedgerExceptionReason =
  | "DUPLICATE_CONFIRMED"
  | "SOURCE_QUALITY_INSUFFICIENT"
  | "EXTRACTION_CONFLICT"
  | "EVIDENCE_GAP"
  | "PARTY_DATE_AMOUNT_UNCLEAR"
  | "AWAITING_CLIENT_INPUT"
  | "AWAITING_EXTERNAL_RECORD"
  | "NEEDS_LEAD_REVIEW";

export type WebAgentLedgerExceptionReasonOption = Readonly<{
  code: WebAgentLedgerExceptionReason;
  label: string;
}>;

export type WebAgentLedgerExceptionAction = Readonly<{
  code: WebAgentLedgerExceptionDecision;
  label: string;
  consequence: string;
  requiresNote: boolean;
  reasons: readonly WebAgentLedgerExceptionReasonOption[];
}>;

export type WebAgentLedgerExceptionGroup = Readonly<{
  groupId: string;
  candidateKind: "FACT" | "TRANSACTION";
  candidateCount: number;
  summary: string;
  reviewReasons: readonly string[];
  sourceGuidance: string;
  riskLabel: string;
  status: "OPEN" | "DECIDED";
  decision: WebAgentLedgerExceptionDecision | null;
  decisionLabel: string | null;
  decisionReason: WebAgentLedgerExceptionReason | null;
  decisionReasonLabel: string | null;
  canDecide: boolean;
  allowedActions: readonly WebAgentLedgerExceptionAction[];
}>;

export type WebAgentLedgerExceptionMember = Readonly<{
  extractionCandidateId: string | null;
  sequence: number;
  candidateKind: "FACT" | "TRANSACTION";
  summary: string;
  confidence: number;
  reviewReasons: readonly string[];
  excerpts: readonly WebAgentLedgerExtractionExcerpt[];
}>;

export type WebAgentLedgerExceptionMemberPage = Readonly<{
  groupId: string;
  totalCount: number;
  offset: number;
  nextOffset: number | null;
  members: readonly WebAgentLedgerExceptionMember[];
}>;

export type WebAgentLedgerExtractionBatch = Readonly<{
  batchId: string;
  matterId: string;
  status: "REVIEW_READY" | "EXCEPTIONS_ONLY" | "EXCEPTIONS_PARTIALLY_RESOLVED" | "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN" | "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL" | "CONFIRMED" | "RESOLVED" | "STALE";
  currentMatterVersion: number;
  sourceMatterVersion: number;
  candidateCount: number;
  lowRiskCount: number;
  exceptionCount: number;
  stagedAt: string;
  confirmedAt: string | null;
  canConfirmLowRisk: boolean;
  exceptionReviewStatus: "NONE" | "OPEN" | "PARTIALLY_RESOLVED" | "RESOLVED";
  exceptionGroupCount: number;
  decidedExceptionGroupCount: number;
  exceptionGroups: readonly WebAgentLedgerExceptionGroup[];
  lowRiskCandidates: readonly WebAgentLedgerExtractionCandidate[];
  exceptionCandidates: readonly WebAgentLedgerExtractionCandidate[];
}>;

export type WebAgentLedgerExtractionConfirmationReceipt = Readonly<{
  batchId: string;
  matterVersion: number;
  confirmedFactCount: number;
  confirmedTransactionCount: number;
  confirmedTotalCount: number;
}>;

export type WebAgentLedgerExceptionDecisionReceipt = Readonly<{
  batchId: string;
  groupId: string;
  matterVersion: number;
  committedMatterVersion: number;
  decision: WebAgentLedgerExceptionDecision;
  exceptionReviewStatus: "OPEN" | "PARTIALLY_RESOLVED" | "RESOLVED";
  decidedExceptionGroupCount: number;
  exceptionGroupCount: number;
  batchResolved: boolean;
}>;

export type WebAgentLedgerFollowupAction = Readonly<{
  code: WebAgentLedgerFollowupActionCode;
  label: string;
  consequence: string;
  requiresReason: true;
}>;

export type WebAgentLedgerExceptionFollowup = Readonly<{
  followupId: string;
  kind: "REEXTRACTION" | "MORE_EVIDENCE" | "DEFERRED_REVIEW";
  state: "ACTIVE";
  headSequence: number;
  originBatchId: string;
  originGroupId: string;
  currentMatterVersion: number;
  createdMatterVersion: number;
  createdAt: string;
  reason: string;
  reasonNote: string | null;
  candidateCount: number;
  reviewReasons: readonly string[];
  evidencePageCount: number;
  acceptanceRequirements: readonly string[];
  automationStatus: WebAgentLedgerFollowupAutomationStatus | null;
  canAct: boolean;
  allowedActions: readonly WebAgentLedgerFollowupAction[];
}>;

export type WebAgentLedgerExceptionFollowupPage = Readonly<{
  totalCount: number;
  offset: number;
  nextOffset: number | null;
  controlHealth: "HEALTHY" | "RECOVERY_REQUIRED" | null;
  canRecover: boolean;
  followups: readonly WebAgentLedgerExceptionFollowup[];
}>;

export type WebManagedEvidenceSource = WebManagedEvidenceSourceSelection & Readonly<{
  displayLabel: string;
  createdAt: string;
}>;

export type WebManagedEvidenceSourcePage = Readonly<{
  totalCount: number;
  offset: number;
  nextOffset: number | null;
  sources: readonly WebManagedEvidenceSource[];
}>;

export type WebAgentLedgerFollowupEvidencePage = Readonly<{
  totalCount: number;
  offset: number;
  nextOffset: number | null;
  evidencePageIds: readonly string[];
}>;

export type WebAgentLedgerExceptionFollowupReceipt = Readonly<{
  followupId: string;
  action: WebAgentLedgerFollowupActionCode;
  terminalState: "SATISFIED" | "RESUMED" | "WITHDRAWN" | "SUPERSEDED";
  matterVersion: number;
}>;

export type WebAgentLedgerExceptionRecoveryReceipt = Readonly<{
  matterVersion: number;
  controlHealth: "HEALTHY";
  recoveryStarted: true;
}>;

export type WebLegalReview = Readonly<{
  matterId: string;
  matterVersion: number;
  snapshotHash: string;
  sources: readonly WebLegalSource[];
  ruleVersions: readonly WebLegalRule[];
  legalEvents: readonly WebLegalEvent[];
  factBindings: readonly WebLegalFactBinding[];
  currentBundle: Readonly<{ bundleId: string; version: number; bundleHash: string; approvedAt: string | null }> | null;
  bundleSegments: readonly WebLegalBundleSegment[];
  bundleReconfirmation: Readonly<{ version: number; reason: string; staleAt: string | null }> | null;
}>;

export type WebLegalSource = Readonly<{ snapshotId: string; sourceId: string; publisher: string; authorityLevel: string; officialUrl: string; provisionLocator: string; retrievedAt: string | null; contentSha256: string; verificationStatus: string; licenseStatus: string }>;
export type WebLegalRule = Readonly<{ ruleVersionId: string; ruleId: string; ruleVersion: string; issueKey: string; effectiveFrom: string | null; effectiveTo: string | null; triggerEventKind: string; formulaKind: string; baseAnnualRate: string | null; rateMultiplier: string | null; derivedAnnualRate: string | null; status: string }>;
export type WebLegalEvent = Readonly<{ legalEventId: string; eventKind: string; localDate: string | null; evidenceIds: readonly string[]; status: string }>;
export type WebLegalFactBinding = Readonly<{ bindingId: string; factKey: string; factId: string; status: string }>;
export type WebLegalBundleSegment = Readonly<{ segmentId: string; issueKey: string; ruleVersionId: string; triggerEventId: string; startDate: string | null; endDate: string | null; annualRate: string | null; applicabilityAnchor: string }>;
export type WebOfficialSourceCatalogueItem = Readonly<{ sourceId: "CN-CIVIL-CODE-680" | "SPC-PRIVATE-LENDING-2020-SECOND-REVISION" | "SPC-PRIVATE-LENDING-2020-FIRST-REVISION" | "SPC-PRIVATE-LENDING-2015-ORIGINAL" | "CFETS-LPR-HISTORY"; title: string; publisher: string; purpose: string }>;
export type WebOfficialSourceCapture = Readonly<{ runId: string; sourceId: string; publisher: string; status: string; authorizedAt: string | null; retrievedAt: string | null; officialUrl: string | null; provisions: readonly string[]; failureCode: string | null }>;
export type WebOfficialSourceCaptureStatus = Readonly<{ matterId: string; matterVersion: number; runs: readonly WebOfficialSourceCapture[]; reviewedRunIds: readonly string[] }>;
export type WebCaseReadiness = Readonly<{ matterId: string; matterVersion: number; checks: readonly { key: string; label: string; status: "READY" | "BLOCKED"; detail: string }[]; counts: Readonly<{ facts: number; claims: number; transactions: number; candidateItems: number; verifiedSources: number; approvedRules: number }>; nextAction: string }>;

export type WebFormalCalculation = Readonly<{
  matterId: string;
  matterVersion: number;
  snapshotHash: string;
  scenario: Readonly<{
    scenarioId: string;
    obligationId: string;
    version: number;
    startDate: string;
    endDate: string;
    currency: string;
    allocationPolicy: string;
    legalBundleId: string;
    legalBundleHash: string;
    transactionSnapshotHash: string;
    inputHash: string;
  }> | null;
  run: Readonly<{
    runId: string;
    scenarioId: string;
    scenarioVersion: number;
    engineVersion: string;
    legalBundleId: string;
    legalBundleHash: string;
    inputHash: string;
    outputHash: string;
    independentCheckHash: string;
    totalInterestAccrued: string | null;
    totalInterestPaid: string | null;
    remainingPrincipal: string | null;
    remainingUnpaidInterest: string | null;
    unappliedPayments: string | null;
    generatedAt: string | null;
    lineItems: readonly WebCalculationLineItem[];
    paymentAllocations: readonly WebPaymentAllocation[];
  }> | null;
}>;
export type WebCalculationLineItem = Readonly<{ lineSequence: number; periodStart: string; periodEnd: string; openingPrincipal: string | null; annualRate: string | null; dayCount: number; accruedInterest: string | null; closingPrincipal: string | null; accruedUnpaidInterest: string | null; ruleSegmentId: string; sourceRuleVersion: string; evidenceIds: readonly string[] }>;
export type WebPaymentAllocation = Readonly<{ allocationSequence: number; paymentEventId: string; effectiveDate: string; paymentAmount: string | null; allocatedInterest: string | null; allocatedPrincipal: string | null; unappliedAmount: string | null; paymentApplication: string; evidenceIds: readonly string[] }>;
export type WebSubmissionReview = Readonly<{
  matterId: string;
  matterVersion: number;
  stage: string;
  snapshotHash: string;
  workProducts: readonly WebSubmissionWorkProduct[];
  bundles: readonly WebSubmissionBundle[];
  currentBundle: WebSubmissionBundle | null;
  currentComponents: readonly WebSubmissionComponent[];
  currentExport: WebSubmissionExport | null;
  documentDraftsAvailable: boolean;
}>;
export type WebSubmissionWorkProduct = Readonly<{ workProductId: string; documentKind: string; audience: string; mediaType: string; artifactSha256: string; byteSize: number; pageCount: number; semanticTextSha256: string | null; status: string; approvedAt: string | null; staleAt: string | null; staleReason: string | null; createdAt: string | null }>;
export type WebSubmissionBundle = Readonly<{ bundleId: string; lifecycle: string; validity: string; finalTextHash: string | null; approvedMatterVersion: number; lockedAt: string | null; exportedAt: string | null; createdAt: string | null; exportProfile: string; currency: string; inputHash: string; requiredDocumentKinds: readonly string[]; evidenceManifestId: string; evidenceManifestHash: string; legalBundleId: string; legalBundleHash: string; calculationRunId: string; calculationOutputHash: string; finalTextApprovalId: string; qaHash: string; qaApprovedAt: string | null }>;
export type WebSubmissionComponent = Readonly<{ workProductId: string; sequence: number; documentKind: string; courtFilename: string; mediaType: string; artifactSha256: string; byteSize: number }>;
export type WebSubmissionExport = Readonly<{ exportId: string; bundleId: string; inputHash: string; courtZipSha256: string; courtZipBytes: number; internalManifestSha256: string; componentCount: number; verificationHash: string; verifiedAt: string | null; createdAt: string | null }>;
export type WebDocumentDraftPair = Readonly<{
  pairId: string;
  documentKind: string;
  editableMediaType: string;
  editableSha256: string;
  editableBytes: number;
  reviewPdfSha256: string;
  reviewPdfBytes: number;
  reviewPdfPageCount: number;
  reviewInputHash: string;
  status: string;
  approvedAt: string | null;
  createdAt: string | null;
}>;
export type WebDocumentDraftReview = Readonly<{ matterId: string; matterVersion: number; snapshotHash: string; pairs: readonly WebDocumentDraftPair[] }>;
export type WebLocalMaterialAnalysis = Readonly<{
  analysisId: string;
  mode: string;
  status: string;
  sourceVersion: number;
  generatedAt: string;
  summary: Readonly<{ fileCount: number; pageCount: number; textLayerPages: number; scannedPages: number; candidateCount: number; signalCounts: Readonly<Record<string, number>> }>;
  files: readonly Readonly<{ materialId: string; displayName: string; pageCount: number; textLayerPages: number; candidatePageCount: number }>[];
  candidates: readonly Readonly<{ candidateId: string; evidencePageId: string | null; kind: string; status: string; sourceFile: string; pageNumber: number; signals: readonly string[]; dates: readonly string[]; amounts: readonly string[]; snippet: string; humanAction: string }>[];
  limitations: readonly string[];
}>;

export type WebAgentMaterialRun = Readonly<{
  runId: string;
  matterId: string;
  matterVersion: number;
  status: "QUEUED" | "RUNNING" | "NEEDS_REVIEW" | "FAILED";
  progress: Readonly<{
    totalPages: number;
    processedPages: number;
    remainingPages: number;
    batchCount: number;
    completedBatchCount: number;
  }>;
  candidateCount: number;
  tasks: readonly Readonly<{ taskKind: string; status: string }>[];
  retryAllowed: boolean;
  failureState: string | null;
  createdAt: string;
  updatedAt: string;
  externalServiceNotice: string;
  representationProfile: WebRepresentationProfile;
}>;

export type WebRepresentationProfile = Readonly<{
  status: "UNCONFIRMED" | "CONFIRMED";
  activeProceedingRole: "PLAINTIFF" | "DEFENDANT" | "APPELLANT" | "APPELLEE" | "THIRD_PARTY" | "OTHER" | null;
  proceedingStage: string | null;
  caseType: string | null;
  version: number | null;
}>;

export type WebAgentMaterialCandidate = Readonly<{
  candidateId: string;
  evidencePageId: string;
  sourceLabel: string;
  pageNumber: number;
  kind: "RELEVANT_PAGE" | "UNRELATED_PAGE" | "OCR_REQUIRED" | "DUPLICATE_CANDIDATE" | "UNCERTAIN";
  confidence: number;
  reviewPriority: "LOW" | "MEDIUM" | "HIGH";
  reasonCodes: readonly string[];
  supportingExcerpt: string;
  duplicateOfPageId: string | null;
  status: "NEEDS_REVIEW";
}>;

export type WebAgentCandidateBatch = Readonly<{
  runId: string;
  totalCount: number;
  items: readonly WebAgentMaterialCandidate[];
  nextCursor: string | null;
  hasMore: boolean;
}>;

export type WebCaseAgentRun = Readonly<{
  runId: string;
  matterId: string;
  objective: string;
  status: "CREATED" | "PLANNING" | "WAITING_APPROVAL" | "EXECUTING" | "WAITING_INPUT" | "RECONCILIATION_REQUIRED" | "VERIFYING" | "READY_FOR_REVIEW" | "COMPLETED" | "PAUSED" | "STALE" | "CANCELLED" | "FAILED";
  phaseLabel: string;
  progress: Readonly<{ completed: number; total: number }>;
  currentWork: Readonly<{ title: string; detail: string; status: string }> | null;
  openDecisionCount: number;
  openApprovalCount: number;
  artifactCount: number;
  statusMessage: string;
  failureMessage: string | null;
  failureCode: string | null;
  version: number;
  snapshotMatterVersion: number;
  inputSnapshotStatus: "CURRENT" | "PLAN_CANDIDATE_REGISTERED" | "PLAN_ACTIVE" | "INPUTS_CHANGED";
  createdAt: string;
  updatedAt: string;
  actions: Readonly<{ canPause: boolean; canResume: boolean; canCancel: boolean }>;
  activePlanExecution: boolean;
  requiredDocumentDeliverables?: readonly WebCaseAgentRequestedDeliverable[];
}>;

export type WebCaseAgentCompletionReceipt = Readonly<{
  completionId: string;
  matterId: string;
  runId: string;
  reviewedRunVersion: number;
  completedRunVersion: number;
  runStatus: "COMPLETED";
  verificationStatus: "PASSED";
  reviewedArtifactCount: number;
}>;

export type WebCaseAgentDecision = Readonly<{
  decisionId: string;
  title: string;
  question: string;
  options: readonly Readonly<{ optionId: string; label: string; consequence: string; requiresNote: boolean }>[];
  allowNote: boolean;
  blocking: boolean;
  status: "OPEN" | "ANSWERED" | "EXPIRED" | "CANCELLED";
}>;

export type WebCaseAgentApproval = Readonly<{
  approvalId: string;
  actionLabel: string;
  reason: string;
  impact: string;
  status: "OPEN" | "APPROVED" | "REJECTED" | "EXPIRED" | "CANCELLED";
}>;

export type WebCaseAgentArtifact = Readonly<{
  artifactId: string;
  title: string;
  artifactType: string;
  status: "CANDIDATE" | "READY_FOR_REVIEW" | "APPROVED" | "SUPERSEDED" | "FAILED";
  reviewRequired: boolean;
  recoveryReviewOnly: boolean;
}>;

export type WebCaseAgentArtifactReviewSource = Readonly<{
  sourceKind: string;
  sourceId: string;
  label: string;
  evidencePageId: string | null;
}>;

export type WebCaseAgentArtifactReviewItem = Readonly<{
  itemId: string;
  title: string;
  detail: string;
  badge: string | null;
  confidence: number | null;
  sources: readonly WebCaseAgentArtifactReviewSource[];
  externalUrl: string | null;
}>;

export type WebCaseAgentArtifactReview = Readonly<{
  artifactId: string;
  artifactType: string;
  title: string;
  reviewNotice: string;
  sections: readonly Readonly<{
    sectionId: string;
    title: string;
    severity: "LOW" | "MEDIUM" | "HIGH";
    items: readonly WebCaseAgentArtifactReviewItem[];
  }>[];
}>;

export type WebCaseAgentDocumentSource = Readonly<{
  sourceRef: string;
  sourceKind: string;
  label: string;
}>;

export type WebCaseAgentDocumentReview = Readonly<{
  reviewVersion?: string | null;
  reviewArtifactId?: string | null;
  artifactId: string;
  title: string;
  deliverableKind: string;
  deliverableLabel: string;
  outputFormat: "DOCX" | "XLSX";
  reviewNotice: string;
  versionStatus: "CURRENT" | "UPDATE_REQUIRED" | "GENERATING" | "FAILED" | "UNKNOWN";
  revisionNumber: number;
  templateVersion: string;
  installedTemplateVersion: string;
  canRequestRevision: boolean;
  requestStatus: "READY" | "LEASED" | "PASSED" | "FAILED" | "UNKNOWN" | null;
  requestId: string | null;
  downloadReady: boolean;
  reviewPdfPageCount: number;
  totalItemCount: number;
  displayedItemCount: number;
  previewTruncated: boolean;
  sections: readonly Readonly<{
    sectionId: string;
    heading: string;
    paragraphs: readonly Readonly<{
      paragraphId: string;
      text: string;
      sources: readonly WebCaseAgentDocumentSource[];
    }>[];
  }>[];
  columns: readonly Readonly<{
    key: string;
    label: string;
    valueType: string;
  }>[];
  rows: readonly Readonly<{
    rowId: string;
    cells: readonly (string | number | boolean | null)[];
    sources: readonly WebCaseAgentDocumentSource[];
  }>[];
}>;

export type WebDynamicCasePlanSource = Readonly<{
  sourceKind: string;
  sourceId: string;
  label: string;
  locator: string | null;
}>;

export type WebDynamicCasePlanItem = Readonly<{
  itemId: string;
  sequence: number;
  category: "MATERIAL_REQUEST" | "RESEARCH_TASK" | "PROCEDURAL_TASK" | "CALCULATION" | "DOCUMENT_CANDIDATE" | "REVIEW" | "DEADLINE_RISK";
  status: "CANDIDATE" | "APPROVED" | "CHANGE_REQUESTED" | "REJECTED" | "SUPERSEDED";
  readiness: "ACTIONABLE" | "NEEDS_RESEARCH" | "NEEDS_INFORMATION";
  title: string;
  purpose: string;
  rationale: string;
  riskIfOmitted: string;
  prerequisiteCount: number;
  confidence: number;
  reviewGate: "LEAD_LAWYER_CONFIRMATION" | "EVIDENCE_REVIEW" | "LEGAL_AUTHORITY_REVIEW" | "PROCEDURE_REVIEW" | "CALCULATION_REVIEW";
  sources: readonly WebDynamicCasePlanSource[];
  sourceCounts: Readonly<{ fact: number; evidence: number; procedure: number; officialAuthority: number }>;
  deliveryTarget: "NOT_APPLICABLE" | "INTERNAL_WORK_PRODUCT" | "CLIENT_DELIVERABLE" | "COURT_SUBMISSION" | null;
  deliverableKind: string | null;
  requiredForDelivery: boolean;
}>;

export type WebDynamicCasePlan = Readonly<{
  planId: string;
  matterId: string;
  generatedMatterVersion: number;
  currentMatterVersion: number;
  status: "CANDIDATE" | "ACTIVE" | "STALE" | "SUPERSEDED";
  inputsCurrent: boolean;
  staleReasons: readonly string[];
  generatedAt: string;
  canActivate: boolean;
  activationBlockers: readonly string[];
  reviewedItemCount: number;
  items: readonly WebDynamicCasePlanItem[];
}>;

export type WebDynamicCasePlanDecision = Readonly<{
  decision: "APPROVE" | "MODIFY" | "REJECT";
  reasonCode: "VERIFIED_BY_COUNSEL" | "NOT_APPLICABLE" | "SUPERSEDED_BY_EVIDENCE" | "REQUIRES_FURTHER_RESEARCH" | "PROCEDURAL_POSTURE_CHANGED" | "INCORRECT_SOURCE_BINDING";
  readinessOverride?: "ACTIONABLE" | "NEEDS_RESEARCH" | "NEEDS_INFORMATION";
  requiredForDeliveryOverride?: boolean;
}>;

export class WebLawyerApiError extends Error {
  readonly status: number | null;
  readonly requestId: string | null;

  constructor(message: string, { status, requestId }: { status: number | null; requestId: string | null }) {
    super(message);
    this.name = "WebLawyerApiError";
    this.status = status;
    this.requestId = requestId;
  }
}

/** A missing or expired Web session is not an application outage. */
export function isWebLoginRequired(error: unknown): boolean {
  return error instanceof WebLawyerApiError && (error.status === 401 || error.status === 403);
}

/** These responses mean the server rejected the material before it was accepted. */
export function isWebMaterialRejected(error: unknown): boolean {
  return error instanceof WebLawyerApiError
    && [400, 413, 415, 422].includes(error.status ?? 0);
}

export async function readWebLawyerSession(signal?: AbortSignal): Promise<WebLawyerSession | null> {
  const response = await webApiFetch("/api/v1/session", { signal });
  if (response.status === 401 || response.status === 403) return null;
  const payload = await readJsonResponse(response, "读取登录状态");
  const record = asRecord(payload, "登录状态响应格式不正确");
  if (record.authenticated === false) return null;
  const actor = asRecord(record.actor, "登录状态未提供受管律师身份");
  const capabilities = asRecord(record.capabilities, "登录状态未提供受管权限");
  const canUploadMaterial = requiredBoolean(capabilities.can_upload_material, "登录状态中的材料权限格式不正确");
  const canConfirmFact = requiredBoolean(capabilities.can_confirm_fact, "登录状态中的事实确认权限格式不正确");
  const canRunCalculation = requiredBoolean(capabilities.can_run_calculation, "登录状态中的测算权限格式不正确");
  return {
    actor: { roles: requiredStringArray(actor.roles, "登录状态未提供受管律师角色", 16, 80),
      recoveryNamespace: typeof actor.recovery_namespace === "string" && /^[a-f0-9]{64}$/.test(actor.recovery_namespace) ? actor.recovery_namespace : undefined },
    capabilities: {
      canCreateCase: requiredBoolean(capabilities.can_create_case, "登录状态中的建案权限格式不正确"),
      canConfirmFact,
      canReviewEvidence: optionalBoolean(capabilities.can_review_evidence, canUploadMaterial, "登录状态中的证据审阅能力格式不正确"),
      canReviewFacts: optionalBoolean(capabilities.can_review_facts, canConfirmFact, "登录状态中的案情审阅能力格式不正确"),
      canReviewLegal: optionalBoolean(capabilities.can_review_legal, false, "登录状态中的法律审阅能力格式不正确"),
      canReviewSubmission: optionalBoolean(capabilities.can_review_submission, false, "登录状态中的应诉材料能力格式不正确"),
      canRunAgent: optionalBoolean(capabilities.can_run_agent, false, "登录状态中的 Agent 能力格式不正确"),
      canDraftDefenceBrief: optionalBoolean(capabilities.can_draft_defence_brief, false,
        "登录状态中的答辩状能力格式不正确"),
      canRunCaseAgent: optionalBoolean(capabilities.can_run_case_agent, false, "登录状态中的统一办案 Agent 能力格式不正确"),
      canExecuteActivePlan: optionalBoolean(capabilities.can_execute_active_plan, false, "登录状态中的已激活计划执行能力格式不正确"),
      canCompleteCaseAgentRun: optionalBoolean(capabilities.can_complete_case_agent_run, false, "登录状态中的 Agent 终审能力格式不正确"),
      canReviewCaseAgent: optionalBoolean(capabilities.can_review_case_agent, false, "登录状态中的 Agent 成果审阅能力格式不正确"),
      canReviewCaseAgentDocuments: optionalBoolean(capabilities.can_review_case_agent_documents, false, "登录状态中的 Agent 文书审阅与下载能力格式不正确"),
      canReviewDynamicCasePlan: optionalBoolean(capabilities.can_review_dynamic_case_plan, false, "登录状态中的动态办案计划能力格式不正确"),
      canReviewAgentLedgerExtractions: optionalBoolean(capabilities.can_review_agent_ledger_extractions, false, "登录状态中的材料提取批次复核能力格式不正确"),
      canReviewAgentLedgerExceptionFollowups: optionalBoolean(capabilities.can_review_agent_ledger_exception_followups, false, "登录状态中的异常后续工作能力格式不正确"),
      canRunAgentLedgerExtraction: optionalBoolean(capabilities.can_run_agent_ledger_extraction, false, "登录状态中的结构化台账提取能力格式不正确"),
      canRunCalculation,
      canUploadMaterial,
      canUploadCommonMaterial: optionalBoolean(
        capabilities.can_upload_common_material,
        false,
        "登录状态中的常见材料接收能力格式不正确",
      ),
      canReviewCasePosture: optionalBoolean(
        capabilities.can_review_case_posture,
        false,
        "登录状态中的代理情境查看能力格式不正确",
      ),
      canConfirmCasePosture: optionalBoolean(
        capabilities.can_confirm_case_posture,
        false,
        "登录状态中的代理情境确认能力格式不正确",
      ),
    },
    expiresAt: optionalText(record.expires_at, 64),
    workspaceMode: record.workspace_mode === "LOCAL_DEVELOPMENT" || record.workspace_mode === "LOCAL_WEB"
      ? "LOCAL_DEVELOPMENT"
      : "FIRM_MANAGED",
  };
}

export async function readWebMaterialAnalysis(caseId: string, signal?: AbortSignal): Promise<WebLocalMaterialAnalysis | null> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/analysis`, { signal });
  const payload = await readJsonResponse(response, "读取材料分析");
  const record = asRecord(payload, "材料分析响应格式不正确");
  if (record.status === "NOT_RUN" || record.analysis === null || record.analysis === undefined) return null;
  return parseLocalMaterialAnalysis(record.analysis);
}

export async function runWebMaterialAnalysis(caseId: string): Promise<WebLocalMaterialAnalysis> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/analysis`, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() }, body: JSON.stringify({}) });
  const payload = await readJsonResponse(response, "分析案件材料");
  return parseLocalMaterialAnalysis(asRecord(payload, "材料分析响应格式不正确").analysis);
}

/* ---------------------------------------------------------------- Agent 深度分析 */

export type WebAnalysisAgentStatus =
  | "NOT_RUN" | "RUNNING" | "COMPLETED" | "FAILED" | "BLOCKED"
  | "MODEL_NOT_CONFIGURED" | "STALE" | "DISABLED";

export type WebAnalysisAgentState = Readonly<{
  status: WebAnalysisAgentStatus;
  progress: number;
  stage: string;
  gateLevel: string;
  costCny: string;
  calls: number;
  error: string;
  engineNumbers: Readonly<Record<string, string>>;
  reportAvailable: boolean;
}>;

export type WebCaseAnalysisState = Readonly<{
  deterministicStatus: string;
  analysis: WebLocalMaterialAnalysis | null;
  agent: WebAnalysisAgentState;
}>;

export type WebAgentAnalysisRequest = Readonly<{
  caseNumber?: string;
  role?: "被告" | "原告";
  stage?: string;
  budgetCny?: number;
  caseConfig?: Readonly<Record<string, unknown>>;
  /**
   * 律师确认：扫描件页面以图像原样发送，图像内的身份证号/银行卡号无法在本机自动脱敏。
   * 默认 false（服务端 fail closed，检出即阻断并提示）。
   */
  allowImageIdentifiers?: boolean;
}>;

function parseAnalysisAgentState(value: unknown): WebAnalysisAgentState {
  const record = asRecord(value, "分析状态响应格式不正确");
  const engineRaw = record.engine_numbers;
  const engineNumbers: Record<string, string> = {};
  if (engineRaw !== null && engineRaw !== undefined) {
    for (const [key, item] of Object.entries(asRecord(engineRaw, "正式数字格式不正确"))) {
      if (typeof item === "string") engineNumbers[key] = item;
    }
  }
  const statusValue = requiredText(record.status, "分析状态格式不正确", 40);
  const allowed: WebAnalysisAgentStatus[] = ["NOT_RUN", "RUNNING", "COMPLETED", "FAILED",
    "BLOCKED", "MODEL_NOT_CONFIGURED", "STALE", "DISABLED"];
  const status = (allowed as string[]).includes(statusValue)
    ? (statusValue as WebAnalysisAgentStatus)
    : "FAILED";
  return {
    status,
    progress: optionalNonNegativeInteger(record.progress, 100) ?? 0,
    stage: optionalTextAllowEmpty(record.stage, 60),
    gateLevel: optionalTextAllowEmpty(record.gate_level, 40),
    costCny: optionalText(record.cost_cny, 40) ?? "0.000000",
    calls: optionalNonNegativeInteger(record.calls, 10_000) ?? 0,
    error: optionalTextAllowEmpty(record.error, 2_000),
    engineNumbers,
    reportAvailable: optionalBoolean(record.report_available, false, "报告状态格式不正确"),
  };
}

function parseCaseAnalysisState(payload: unknown, operation: string): WebCaseAnalysisState {
  const record = asRecord(payload, `${operation}响应格式不正确`);
  const rawAnalysis = record.analysis;
  const analysis = rawAnalysis === null || rawAnalysis === undefined
    ? null
    : parseLocalMaterialAnalysis(rawAnalysis);
  return {
    deterministicStatus: optionalText(record.status, 40) ?? "NOT_RUN",
    analysis,
    agent: parseAnalysisAgentState(record.agent ?? {}),
  };
}

export async function readWebCaseAnalysisState(caseId: string, signal?: AbortSignal): Promise<WebCaseAnalysisState> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/analysis`, { signal });
  return parseCaseAnalysisState(await readJsonResponse(response, "读取材料分析"), "读取材料分析");
}

export async function runWebCaseAgentAnalysis(
  caseId: string, request: WebAgentAnalysisRequest = {},
): Promise<WebCaseAnalysisState> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const body: Record<string, unknown> = {};
  if (request.caseNumber) body.case_number = request.caseNumber;
  if (request.role) body.role = request.role;
  if (request.stage) body.stage = request.stage;
  if (request.budgetCny !== undefined) body.budget_cny = request.budgetCny;
  if (request.caseConfig) body.case_config = request.caseConfig;
  if (request.allowImageIdentifiers) body.allow_image_identifiers = true;
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/analysis`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify(body),
  });
  return parseCaseAnalysisState(await readJsonResponse(response, "启动案件分析"), "启动案件分析");
}

export async function readWebAnalysisReport(caseId: string, signal?: AbortSignal): Promise<string> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/analysis/report`, { signal });
  if (!response.ok) throw await readJsonResponse(response, "读取分析报告").then(() => new Error("读取分析报告失败"));
  return response.text();
}

/* ------------------------------------------------- 案件计算参数（律师确认，正式数字来源） */

export type WebCaseParameterDebt = Readonly<{
  debtId: string;
  principal: string;
  disbursedOn: string;
  dueOn: string;
  /** 约定月利率，小数形式（0.015 = 月利率 1.5%）。 */
  agreedMonthlyRate: string;
  /** 缺少出借凭证：引擎会挂起该笔，不计入正式数字。 */
  evidencePending: boolean;
}>;

export type WebCaseParameterPayment = Readonly<{
  paymentId: string;
  paidOn: string;
  amount: string;
  /** 还本 / 付息 / 代付 进入计算；争议 / 排除 只登记不计算。 */
  classification: string;
  debtId: string;
  memo: string;
}>;

export const WEB_PAYMENT_CLASSES: readonly string[] = ["还本", "付息", "代付", "争议", "排除"];

export type WebCaseParameters = Readonly<{
  /** 司法保护上限：LPR 四倍对应的月利率（小数形式）。 */
  lpr4xMonthlyRate: string;
  /** 利息暂计截止日（YYYY-MM-DD）。 */
  interestCutoff: string;
  debts: readonly WebCaseParameterDebt[];
  /** 律师逐笔确认性质的付款；未确认的付款不进入正式数字。 */
  payments: readonly WebCaseParameterPayment[];
}>;

/** 表单参数 → 引擎 case_config（shadow-case-config-v1）。模型不得写入该结构。 */
export function toWebCaseConfigPayload(
  parameters: WebCaseParameters,
): Record<string, unknown> {
  return {
    schema: "shadow-case-config-v1",
    lpr_4x_monthly_rate: parameters.lpr4xMonthlyRate,
    interest_cutoff: parameters.interestCutoff,
    debts: parameters.debts.map((debt) => ({
      debt_id: debt.debtId,
      principal: debt.principal,
      disbursed_on: debt.disbursedOn,
      ...(debt.dueOn ? { due_on: debt.dueOn } : {}),
      agreed_monthly_rate: debt.agreedMonthlyRate,
      evidence_pending: debt.evidencePending,
    })),
    payments: parameters.payments.map((payment, index) => ({
      payment_id: payment.paymentId || `P${index + 1}`,
      paid_on: payment.paidOn,
      amount: payment.amount,
      classification: payment.classification,
      ...(payment.debtId ? { debt_id: payment.debtId } : {}),
      ...(payment.memo ? { memo: payment.memo } : {}),
    })),
  };
}

function parseWebCaseParameters(value: unknown): WebCaseParameters {
  const record = asRecord(value, "案件计算参数格式不正确");
  const debtsRaw = record.debts;
  const debts: WebCaseParameterDebt[] = [];
  if (Array.isArray(debtsRaw)) {
    for (const item of debtsRaw) {
      const row = asRecord(item, "债务参数格式不正确");
      debts.push({
        debtId: optionalText(row.debt_id, 40) ?? "",
        principal: optionalText(row.principal, 40) ?? "",
        disbursedOn: optionalText(row.disbursed_on, 20) ?? "",
        dueOn: optionalText(row.due_on, 20) ?? "",
        agreedMonthlyRate: optionalText(row.agreed_monthly_rate, 40) ?? "",
        evidencePending: optionalBoolean(row.evidence_pending, false, "挂起标记格式不正确"),
      });
    }
  }
  const paymentsRaw = record.payments;
  const payments: WebCaseParameterPayment[] = [];
  if (Array.isArray(paymentsRaw)) {
    for (const item of paymentsRaw) {
      const row = asRecord(item, "付款参数格式不正确");
      const classification = optionalText(row.classification, 20) ?? "";
      payments.push({
        paymentId: optionalText(row.payment_id, 40) ?? "",
        paidOn: optionalText(row.paid_on, 20) ?? "",
        amount: optionalText(row.amount, 40) ?? "",
        classification: WEB_PAYMENT_CLASSES.includes(classification) ? classification : "争议",
        debtId: optionalText(row.debt_id, 40) ?? "",
        memo: optionalText(row.memo, 200) ?? "",
      });
    }
  }
  return {
    lpr4xMonthlyRate: optionalText(record.lpr_4x_monthly_rate, 40) ?? "",
    interestCutoff: optionalText(record.interest_cutoff, 20) ?? "",
    debts,
    payments,
  };
}

export async function readWebCaseParameters(
  caseId: string,
  signal?: AbortSignal,
): Promise<WebCaseParameters | null> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/analysis/config`, { signal });
  const payload = asRecord(await readJsonResponse(response, "读取案件计算参数"), "案件计算参数响应格式不正确");
  if (payload.case_config === null || payload.case_config === undefined) return null;
  return parseWebCaseParameters(payload.case_config);
}

export async function saveWebCaseParameters(
  caseId: string,
  parameters: WebCaseParameters,
): Promise<WebCaseParameters | null> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/analysis/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({ case_config: toWebCaseConfigPayload(parameters) }),
  });
  const payload = asRecord(await readJsonResponse(response, "保存案件计算参数"), "案件计算参数响应格式不正确");
  if (payload.case_config === null || payload.case_config === undefined) return null;
  return parseWebCaseParameters(payload.case_config);
}

export async function exportWebAnalysis(caseId: string, format: "md" | "docx"): Promise<Blob> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/analysis/export?format=${format}`, {});
  if (!response.ok) throw await readJsonResponse(response, "导出分析报告").then(() => new Error("导出分析报告失败"));
  return response.blob();
}

/* ------------------------------------------------------ 答辩状草稿（律师工作稿） */

/** 与服务端 GROUNDS 一一对应；律师勾选后才进入文书。 */
export const WEB_BRIEF_GROUNDS: ReadonlyArray<Readonly<{
  id: string; title: string; description: string;
}>> = [
  { id: "cap", title: "利息按司法保护上限核减",
    description: "主张原告请求的利息超出司法保护上限部分不应支持；上限参数由律师在决策包页面填写。" },
  { id: "offset", title: "已付款项予以冲抵",
    description: "主张被告已支付款项应在计算中冲抵；每笔付款的性质需律师确认后才进入计算。" },
  { id: "lawyer_fee", title: "律师费承担条款不予支持",
    description: "对原告主张由其负担律师费的请求提出异议。" },
  { id: "limitation", title: "诉讼时效抗辩",
    description: "主张原告的请求已超过诉讼时效期间。" },
  { id: "delivery", title: "出借事实与款项交付证据不足",
    description: "主张原告提交的材料不足以证明借贷合意与款项实际交付。" },
  { id: "amount", title: "本金数额与证据不符",
    description: "主张原告请求的本金数额与其提交的材料不能对应。" },
] as const;

export const WEB_BRIEF_CLAIMS: ReadonlyArray<Readonly<{ id: string; label: string }>> = [
  { id: "principal", label: "借款本金" },
  { id: "interest", label: "利息" },
  { id: "lawyer_fee", label: "律师费" },
  { id: "costs", label: "诉讼费用" },
] as const;

export const WEB_BRIEF_STANCES: readonly string[] = ["不认可", "部分认可", "认可", "不发表意见"];

export type WebBriefSelections = Readonly<{
  respondent: string;
  claimant: string;
  court: string;
  caseNumber: string;
  grounds: Readonly<Record<string, boolean>>;
  stances: Readonly<Record<string, string>>;
  authorities: readonly string[];
  notes: string;
}>;

export type WebBriefStatus =
  | "NOT_RUN" | "RUNNING" | "COMPLETED" | "FAILED" | "BLOCKED"
  | "MODEL_NOT_CONFIGURED" | "STALE" | "DISABLED";

export type WebBriefState = Readonly<{
  status: WebBriefStatus;
  progress: number;
  stage: string;
  gateLevel: string;
  costCny: string;
  calls: number;
  error: string;
  engineNumbers: Readonly<Record<string, string>>;
  markdownAvailable: boolean;
  stale: boolean;
  runId: string;
  generatedAt: string;
}>;

export type WebBriefPayload = Readonly<{
  selections: WebBriefSelections;
  state: WebBriefState;
  markdown: string;
}>;

export function emptyWebBriefSelections(): WebBriefSelections {
  return {
    respondent: "", claimant: "", court: "", caseNumber: "",
    grounds: Object.fromEntries(WEB_BRIEF_GROUNDS.map((ground) => [ground.id, false])),
    stances: Object.fromEntries(WEB_BRIEF_CLAIMS.map((claim) => [claim.id, "不发表意见"])),
    authorities: [],
    notes: "",
  };
}

function parseWebBriefSelections(value: unknown): WebBriefSelections {
  const record = asRecord(value ?? {}, "答辩状选择格式不正确");
  const groundsRaw = asRecord(record.grounds ?? {}, "答辩状主张格式不正确");
  const stancesRaw = asRecord(record.stances ?? {}, "答辩状态度格式不正确");
  const authoritiesRaw = record.authorities;
  const authorities: string[] = [];
  if (Array.isArray(authoritiesRaw)) {
    for (const item of authoritiesRaw) {
      if (typeof item === "string" && item.trim()) authorities.push(item.trim().slice(0, 200));
    }
  }
  const statuses = WEB_BRIEF_STANCES as readonly string[];
  return {
    respondent: optionalTextAllowEmpty(record.respondent, 120),
    claimant: optionalTextAllowEmpty(record.claimant, 120),
    court: optionalTextAllowEmpty(record.court, 120),
    caseNumber: optionalTextAllowEmpty(record.case_number, 120),
    grounds: Object.fromEntries(WEB_BRIEF_GROUNDS.map((ground) => [
      ground.id, optionalBoolean(groundsRaw[ground.id], false, "主张勾选格式不正确"),
    ])),
    stances: Object.fromEntries(WEB_BRIEF_CLAIMS.map((claim) => {
      const raw = typeof stancesRaw[claim.id] === "string" ? String(stancesRaw[claim.id]) : "";
      return [claim.id, statuses.includes(raw) ? raw : "不发表意见"];
    })),
    authorities,
    notes: optionalTextAllowEmpty(record.notes, 2_000),
  };
}

function parseWebBriefPayload(payload: unknown, operation: string): WebBriefPayload {
  const record = asRecord(payload, `${operation}响应格式不正确`);
  const stateRaw = asRecord(record.state ?? {}, "答辩状状态格式不正确");
  const allowed: WebBriefStatus[] = ["NOT_RUN", "RUNNING", "COMPLETED", "FAILED", "BLOCKED",
    "MODEL_NOT_CONFIGURED", "STALE", "DISABLED"];
  const statusValue = requiredText(stateRaw.status, "答辩状状态格式不正确", 40);
  const engineRaw = stateRaw.engine_numbers;
  const engineNumbers: Record<string, string> = {};
  if (engineRaw !== null && engineRaw !== undefined) {
    for (const [key, item] of Object.entries(asRecord(engineRaw, "正式数字格式不正确"))) {
      if (typeof item === "string") engineNumbers[key] = item;
    }
  }
  return {
    selections: parseWebBriefSelections(record.selections),
    state: {
      status: (allowed as string[]).includes(statusValue) ? (statusValue as WebBriefStatus) : "FAILED",
      progress: optionalNonNegativeInteger(stateRaw.progress, 100) ?? 0,
      stage: optionalTextAllowEmpty(stateRaw.stage, 60),
      gateLevel: optionalTextAllowEmpty(stateRaw.gate_level, 40),
      costCny: optionalText(stateRaw.cost_cny, 40) ?? "0.000000",
      calls: optionalNonNegativeInteger(stateRaw.calls, 10_000) ?? 0,
      error: optionalTextAllowEmpty(stateRaw.error, 2_000),
      engineNumbers,
      markdownAvailable: optionalBoolean(stateRaw.markdown_available, false, "草稿状态格式不正确"),
      stale: optionalBoolean(stateRaw.stale, false, "失效标记格式不正确"),
      runId: optionalTextAllowEmpty(stateRaw.run_id, 80),
      generatedAt: optionalTextAllowEmpty(stateRaw.generated_at, 60),
    },
    markdown: optionalMultilineText(record.markdown, 400_000),
  };
}

function toWebBriefPayload(selections: WebBriefSelections): Record<string, unknown> {
  return {
    respondent: selections.respondent,
    claimant: selections.claimant,
    court: selections.court,
    case_number: selections.caseNumber,
    grounds: selections.grounds,
    stances: selections.stances,
    authorities: selections.authorities,
    notes: selections.notes,
  };
}

export async function readWebBrief(caseId: string, signal?: AbortSignal): Promise<WebBriefPayload> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/brief`, { signal });
  return parseWebBriefPayload(await readJsonResponse(response, "读取答辩状"), "读取答辩状");
}

export async function saveWebBriefSelections(
  caseId: string, selections: WebBriefSelections,
): Promise<WebBriefPayload> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/brief`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({ selections: toWebBriefPayload(selections) }),
  });
  return parseWebBriefPayload(await readJsonResponse(response, "保存答辩状选择"), "保存答辩状选择");
}

export async function generateWebBrief(
  caseId: string, request: Readonly<{ caseNumber?: string; budgetCny?: number }> = {},
): Promise<WebBriefPayload> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const body: Record<string, unknown> = {};
  if (request.caseNumber) body.case_number = request.caseNumber;
  if (request.budgetCny !== undefined) body.budget_cny = request.budgetCny;
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/brief/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify(body),
  });
  return parseWebBriefPayload(await readJsonResponse(response, "生成答辩状"), "生成答辩状");
}

export async function exportWebBrief(caseId: string, format: "md" | "docx"): Promise<Blob> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/brief/export?format=${format}`, {});
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as Record<string, unknown> | null;
    const message = payload && typeof payload.message === "string" ? payload.message : "导出答辩状失败";
    throw new WebLawyerApiError(message, { status: response.status, requestId: null });
  }
  return response.blob();
}

export async function readCurrentWebAgentMaterialRun(caseId: string, signal?: AbortSignal): Promise<WebAgentMaterialRun | null> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/agent-runs/current`, { signal });
  const payload = await readJsonResponse(response, "读取整案材料整理状态");
  const run = asRecord(payload, "整案材料整理响应格式不正确").run;
  return run === null || run === undefined ? null : parseWebAgentMaterialRun(run);
}

export async function readCurrentWebCaseAgentRun(caseId: string, signal?: AbortSignal): Promise<WebCaseAgentRun | null> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/case-agent-runs/current`, { signal });
  const payload = await readJsonResponse(response, "读取办案 Agent 状态");
  const run = asRecord(payload, "办案 Agent 响应格式不正确").run;
  return run === null || run === undefined ? null : parseWebCaseAgentRun(run);
}

export async function readWebCaseAgentRun(
  caseId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<WebCaseAgentRun> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}`,
    { signal },
  );
  const payload = asRecord(
    await readJsonResponse(response, "读取指定办案 Agent 状态"),
    "办案 Agent 响应格式不正确",
  );
  const run = parseWebCaseAgentRun(payload.run);
  if (run.matterId !== normalizedCaseId || run.runId !== normalizedRunId) {
    throw protocolError("办案 Agent 任务与请求范围不一致");
  }
  return run;
}

export type WebCaseAgentRequestedDeliverable =
  | "CASE_REVIEW_MEMO"
  | "DEFENCE_STATEMENT"
  | "EVIDENCE_CATALOGUE"
  | "SUPPLEMENTARY_EVIDENCE_CHECKLIST"
  | "PAYMENT_LEDGER";

const CASE_AGENT_DELIVERABLE_CATALOGUE = new Set<WebCaseAgentRequestedDeliverable>([
  "CASE_REVIEW_MEMO",
  "DEFENCE_STATEMENT",
  "EVIDENCE_CATALOGUE",
  "SUPPLEMENTARY_EVIDENCE_CHECKLIST",
  "PAYMENT_LEDGER",
]);

function normalizeWebCaseAgentDeliverables(
  values: readonly WebCaseAgentRequestedDeliverable[],
): readonly WebCaseAgentRequestedDeliverable[] {
  const normalized = [...new Set(values)].sort();
  if (
    normalized.length === 0
    || normalized.length > 5
    || normalized.some((value) => !CASE_AGENT_DELIVERABLE_CATALOGUE.has(value as WebCaseAgentRequestedDeliverable))
  ) {
    throw protocolError("办案 Agent 成果类型不在当前服务目录中");
  }
  return normalized as readonly WebCaseAgentRequestedDeliverable[];
}

export async function createWebCaseAgentRun(
  caseId: string,
  expectedVersion: number,
  objective: string,
  successCriteria: readonly string[],
  constraints: readonly string[],
  requestedDeliverables: readonly WebCaseAgentRequestedDeliverable[] = [
    "CASE_REVIEW_MEMO",
    "PAYMENT_LEDGER",
  ],
): Promise<WebCaseAgentRun> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedDeliverables = normalizeWebCaseAgentDeliverables(requestedDeliverables);
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/case-agent-runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseAgentIdempotencyKey() },
    body: JSON.stringify({
      objective: requiredText(objective, "请说明办案目标", 4_000),
      success_criteria: successCriteria.map((item) => requiredText(item, "请填写完成标准", 1_000)),
      constraints: constraints.map((item) => requiredText(item, "办案约束格式不正确", 1_000)),
      requested_deliverables: normalizedDeliverables,
      expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER),
    }),
  });
  const payload = await readJsonResponse(response, "交代办案目标");
  return parseWebCaseAgentRun(asRecord(payload, "办案 Agent 响应格式不正确").run);
}

export async function executeWebActivePlan(
  caseId: string,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<WebCaseAgentRun> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/case-agent-runs/execute-active-plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey) },
    body: JSON.stringify(buildActivePlanExecutionPayload(
      requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER),
    )),
  });
  const payload = await readJsonResponse(response, "执行已激活办案计划");
  const run = parseWebCaseAgentRun(asRecord(payload, "已激活计划执行响应格式不正确").run);
  if (!run.activePlanExecution || run.matterId !== normalizedCaseId) {
    throw protocolError("服务端未返回已激活计划的执行任务");
  }
  return run;
}

export async function reconcileWebActivePlanExecution(
  caseId: string,
  planId: string,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<WebCaseAgentRun | null> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedPlanId = normalizeOpaqueId(planId, "动态计划编号");
  const params = new URLSearchParams({
    plan_id: normalizedPlanId,
    expected_version: String(requiredPositiveInteger(
      expectedVersion,
      "案件版本格式不正确",
      Number.MAX_SAFE_INTEGER,
    )),
  });
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/case-agent-runs/active-plan-execution-intent?${params.toString()}`,
    { headers: { "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey) } },
  );
  const payload = asRecord(
    await readJsonResponse(response, "核验已激活计划执行请求"),
    "已激活计划执行核验响应格式不正确",
  );
  if (payload.run === null || payload.run === undefined) return null;
  const run = parseWebCaseAgentRun(payload.run);
  if (!run.activePlanExecution || run.matterId !== normalizedCaseId) {
    throw protocolError("服务端核验结果不是原已激活计划执行任务");
  }
  return run;
}

export async function completeWebCaseAgentRun(
  caseId: string,
  runId: string,
  expectedRunVersion: number,
  idempotencyKey: string,
  documentReviewVersions: Readonly<Record<string, string>> = {},
): Promise<Readonly<{ receipt: WebCaseAgentCompletionReceipt; run: WebCaseAgentRun }>> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey) },
    body: JSON.stringify(buildCaseAgentCompletionPayload(
      requiredPositiveInteger(expectedRunVersion, "任务版本格式不正确", Number.MAX_SAFE_INTEGER),
      documentReviewVersions,
    )),
  });
  const payload = asRecord(await readJsonResponse(response, "确认 Agent 成果终审"), "Agent 终审响应格式不正确");
  const run = parseWebCaseAgentRun(payload.run);
  const receipt = parseWebCaseAgentCompletionReceipt(
    payload.receipt,
    normalizedCaseId,
    normalizedRunId,
    expectedRunVersion,
  );
  if (
    run.runId !== normalizedRunId
    || run.matterId !== normalizedCaseId
    || run.status !== "COMPLETED"
    || run.version !== receipt.completedRunVersion
    || run.artifactCount !== receipt.reviewedArtifactCount
  ) {
    throw protocolError("Agent 终审回执与当前任务不一致");
  }
  return { receipt, run };
}

export async function reconcileWebCaseAgentCompletion(
  caseId: string,
  runId: string,
  expectedRunVersion: number,
  idempotencyKey: string,
): Promise<WebCaseAgentCompletionReceipt | null> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const normalizedVersion = requiredPositiveInteger(
    expectedRunVersion,
    "任务版本格式不正确",
    Number.MAX_SAFE_INTEGER,
  );
  const params = new URLSearchParams({
    expected_run_version: String(normalizedVersion),
  });
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}/completion-intent?${params.toString()}`,
    { headers: { "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey) } },
  );
  const payload = asRecord(
    await readJsonResponse(response, "核验 Agent 终审请求"),
    "Agent 终审核验响应格式不正确",
  );
  if (payload.receipt === null || payload.receipt === undefined) return null;
  return parseWebCaseAgentCompletionReceipt(
    payload.receipt,
    normalizedCaseId,
    normalizedRunId,
    normalizedVersion,
  );
}

function parseWebCaseAgentCompletionReceipt(
  value: unknown,
  expectedMatterId: string,
  expectedRunId: string,
  expectedRunVersion: number,
): WebCaseAgentCompletionReceipt {
  const rawReceipt = asRecord(value, "Agent 终审回执格式不正确");
  const runStatus = requiredText(rawReceipt.run_status, "Agent 终审状态格式不正确", 20);
  const verificationStatus = requiredText(rawReceipt.verification_status, "Agent 终审核验状态格式不正确", 20);
  if (runStatus !== "COMPLETED" || verificationStatus !== "PASSED") {
    throw protocolError("Agent 终审回执未证明已通过服务器核验");
  }
  const receipt: WebCaseAgentCompletionReceipt = {
    completionId: normalizeOpaqueId(rawReceipt.completion_id, "Agent 终审回执编号"),
    matterId: normalizeOpaqueId(rawReceipt.matter_id, "Agent 终审案件编号"),
    runId: normalizeOpaqueId(rawReceipt.run_id, "Agent 终审任务编号"),
    reviewedRunVersion: requiredPositiveInteger(rawReceipt.reviewed_run_version, "Agent 终审审阅版本格式不正确", Number.MAX_SAFE_INTEGER),
    completedRunVersion: requiredPositiveInteger(rawReceipt.completed_run_version, "Agent 终审完成版本格式不正确", Number.MAX_SAFE_INTEGER),
    runStatus: "COMPLETED",
    verificationStatus: "PASSED",
    reviewedArtifactCount: requiredNonNegativeInteger(rawReceipt.reviewed_artifact_count, "Agent 终审成果数格式不正确"),
  };
  if (
    receipt.matterId !== expectedMatterId
    || receipt.runId !== expectedRunId
    || receipt.reviewedRunVersion !== expectedRunVersion
    || receipt.completedRunVersion !== expectedRunVersion + 1
  ) {
    throw protocolError("Agent 终审回执与原请求不一致");
  }
  return receipt;
}

export async function commandWebCaseAgentRun(
  caseId: string,
  runId: string,
  command: "pause" | "resume" | "cancel",
  expectedRunVersion: number,
): Promise<WebCaseAgentRun> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}/${command}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseAgentIdempotencyKey() },
    body: JSON.stringify({ expected_run_version: requiredPositiveInteger(expectedRunVersion, "任务版本格式不正确", Number.MAX_SAFE_INTEGER) }),
  });
  const payload = await readJsonResponse(response, command === "pause" ? "暂停办案任务" : command === "resume" ? "继续办案任务" : "取消办案任务");
  return parseWebCaseAgentRun(asRecord(payload, "办案 Agent 响应格式不正确").run);
}

/**
 * Continues the same verified material-reading run.  The browser supplies no
 * source range, candidate IDs, prompt, model, or cost setting; all of those
 * stay server-owned and are revalidated before the continuation is appended.
 */
export async function continueWebCaseAgentAnalysis(
  caseId: string,
  runId: string,
  expectedRunVersion: number,
): Promise<WebCaseAgentRun> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}/continue-analysis`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseAgentIdempotencyKey() },
      body: JSON.stringify({
        expected_run_version: requiredPositiveInteger(
          expectedRunVersion,
          "任务版本格式不正确",
          Number.MAX_SAFE_INTEGER,
        ),
      }),
    },
  );
  const payload = await readJsonResponse(response, "开始形成风险与补证清单");
  const run = parseWebCaseAgentRun(asRecord(payload, "办案 Agent 响应格式不正确").run);
  if (run.matterId !== normalizedCaseId || run.runId !== normalizedRunId) {
    throw protocolError("服务端返回的办案研判不属于当前案件");
  }
  return run;
}

export async function readWebCaseAgentInbox(caseId: string, runId: string, signal?: AbortSignal): Promise<Readonly<{
  decisions: readonly WebCaseAgentDecision[]; approvals: readonly WebCaseAgentApproval[]; artifacts: readonly WebCaseAgentArtifact[];
}>> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const root = `/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}`;
  const [decisionsResponse, approvalsResponse, artifactsResponse] = await Promise.all([
    webApiFetch(`${root}/decisions`, { signal }), webApiFetch(`${root}/approvals`, { signal }), webApiFetch(`${root}/artifacts`, {
      signal,
      headers: { "X-Lawcase-Artifact-View": "sealed-recovery-v1" },
    }),
  ]);
  const [decisionsPayload, approvalsPayload, artifactsPayload] = await Promise.all([
    readJsonResponse(decisionsResponse, "读取需要决定的事项"), readJsonResponse(approvalsResponse, "读取待审批事项"), readJsonResponse(artifactsResponse, "读取 Agent 成果"),
  ]);
  return {
    decisions: parseArray(asRecord(decisionsPayload, "决定事项格式不正确").items, "决定事项", parseWebCaseAgentDecision),
    approvals: parseArray(asRecord(approvalsPayload, "审批事项格式不正确").items, "审批事项", parseWebCaseAgentApproval),
    artifacts: parseArray(asRecord(artifactsPayload, "Agent 成果格式不正确").items, "Agent 成果", parseWebCaseAgentArtifact),
  };
}

export async function readWebCaseAgentArtifactReview(
  caseId: string,
  runId: string,
  artifactId: string,
  signal?: AbortSignal,
): Promise<WebCaseAgentArtifactReview> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const normalizedArtifactId = normalizeOpaqueId(artifactId, "Agent 成果编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}/artifacts/${normalizedArtifactId}/review`,
    { signal },
  );
  const payload = await readJsonResponse(response, "读取 Agent 分析成果");
  return parseWebCaseAgentArtifactReview(
    asRecord(payload, "Agent 分析成果响应格式不正确").review,
  );
}

export async function readWebCaseAgentDocumentReview(
  caseId: string,
  runId: string,
  artifactId: string,
  signal?: AbortSignal,
): Promise<WebCaseAgentDocumentReview> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const normalizedArtifactId = normalizeOpaqueId(artifactId, "文书成果编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}/artifacts/${normalizedArtifactId}/document-review`,
    { signal },
  );
  const payload = await readJsonResponse(response, "读取 Agent 文书候选");
  return parseWebCaseAgentDocumentReview(
    asRecord(payload, "Agent 文书候选响应格式不正确").review,
  );
}

export type WebDocumentParagraphChange = Readonly<{
  section_index: number; paragraph_index: number; expected_text_hash: string;
  replacement_text: string; reason: string; source_refs: readonly string[];
}>;

const documentGenerationStatuses = ["UNAVAILABLE", "NOT_AUTHORIZED", "QUEUED", "GENERATING", "RECOVERING", "UNKNOWN", "UNKNOWN_REGISTERED", "UNKNOWN_FILES_VERIFIED", "FAILED", "GENERATED_REVIEW_COPY"] as const;
export type WebDocumentGenerationStatus = typeof documentGenerationStatuses[number];
export type WebDocumentContentProposal = Readonly<{
  proposalId: string; revisionNumber: number; basedOnCurrentVersion: boolean;
  generationStatus: WebDocumentGenerationStatus;
  changes: readonly Readonly<{ before: string; after: string; reason: string }>[];
}>;

export type WebDocumentContentProposalPage = Readonly<{
  items: readonly Readonly<{ proposalId: string; revisionNumber: number; createdAt: string }>[];
  nextAfter: string | null;
}>;

export async function listWebDocumentContentProposals(
  caseId: string, runId: string, artifactId: string, after: string | null = null,
): Promise<WebDocumentContentProposalPage> {
  const query = after === null ? "" : `?after=${normalizeOpaqueId(after, "修改记录游标")}`;
  const response = await webApiFetch(contentProposalPath(caseId, runId, artifactId) + query);
  const record = asRecord(await readJsonResponse(response, "读取修改记录列表"), "修改列表不正确");
  const items = parseArray(record.items, "修改记录", (value) => {
    const item = asRecord(value, "修改记录不正确");
    if (item.status !== "NEEDS_SOURCE_AND_LAWYER_REVIEW" || item.court_ready !== false) throw protocolError("修改记录状态不正确");
    const createdAt = requiredText(item.created_at, "保存时间不正确", 80);
    if (!Number.isFinite(Date.parse(createdAt))) throw protocolError("保存时间不正确");
    return { proposalId: normalizeOpaqueId(requiredText(item.proposal_id, "修改编号不正确", 80), "修改编号"),
      revisionNumber: requiredPositiveInteger(item.expected_revision_number, "文书版本不正确", 999), createdAt };
  });
  if (items.length > 20 || new Set(items.map((item) => item.proposalId)).size !== items.length) throw protocolError("修改列表数量或编号不正确");
  const nextAfter = record.next_after === null ? null : normalizeOpaqueId(requiredText(record.next_after, "修改记录游标不正确", 80), "修改记录游标");
  if (nextAfter !== null && (items.length !== 20 || nextAfter !== items.at(-1)?.proposalId || nextAfter === after)) throw protocolError("修改记录分页不正确");
  return { items, nextAfter };
}

function contentProposalPath(caseId: string, runId: string, artifactId: string): string {
  return `/api/v1/cases/${normalizeOpaqueId(caseId, "案件编号")}/case-agent-runs/${normalizeOpaqueId(runId, "办案任务编号")}/artifacts/${normalizeOpaqueId(artifactId, "文书成果编号")}/document-content-proposals`;
}

export async function saveWebDocumentContentProposal(
  caseId: string, runId: string, artifactId: string, revisionNumber: number,
  changes: readonly WebDocumentParagraphChange[], idempotencyKey: string,
  recoveryNamespace?: string,
): Promise<string> {
  const response = await webApiFetch(contentProposalPath(caseId, runId, artifactId), {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey) },
    body: JSON.stringify({ expected_revision_number: requiredPositiveInteger(revisionNumber, "文书版本不正确", 999), changes, recovery_namespace: recoveryNamespace }),
  });
  const record = asRecord(await readJsonResponse(response, "保存文书修改"), "修改回执不正确");
  if (record.status !== "NEEDS_SOURCE_AND_LAWYER_REVIEW" || record.court_ready !== false) throw protocolError("修改回执状态不正确");
  return normalizeOpaqueId(requiredText(record.proposal_id, "修改编号不正确", 80), "修改编号");
}

export async function resolveWebDocumentContentProposal(
  caseId: string, runId: string, artifactId: string, idempotencyKey: string,
): Promise<string | null> {
  const path = contentProposalPath(caseId, runId, artifactId).replace(/document-content-proposals$/, "document-content-proposal-status");
  const response = await webApiFetch(path, { headers: { "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey) } });
  const record = asRecord(await readJsonResponse(response, "核对修改保存结果"), "保存核对回执不正确");
  if (record.court_ready !== false) throw protocolError("保存核对状态不正确");
  if (record.status === "UNCONFIRMED" && record.proposal_id === null) return null;
  if (record.status !== "RECORDED") throw protocolError("保存核对状态不正确");
  return normalizeOpaqueId(requiredText(record.proposal_id, "修改编号不正确", 80), "修改编号");
}

export async function readWebDocumentContentProposal(
  caseId: string, runId: string, artifactId: string, proposalId: string,
): Promise<WebDocumentContentProposal> {
  const response = await webApiFetch(`${contentProposalPath(caseId, runId, artifactId)}/${normalizeOpaqueId(proposalId, "修改编号")}`);
  const record = asRecord(await readJsonResponse(response, "读取文书修改"), "修改记录不正确");
  if (record.proposal_id !== proposalId || record.status !== "NEEDS_SOURCE_AND_LAWYER_REVIEW" || record.court_ready !== false || typeof record.based_on_current_version !== "boolean") throw protocolError("修改记录状态不正确");
  const changes = parseArray(record.changes, "修改内容", (value) => {
    const item = asRecord(value, "修改内容不正确");
    return { before: requiredText(item.before, "原文不正确", 20_000), after: requiredText(item.after, "修改正文不正确", 8_000), reason: requiredText(item.reason, "修改理由不正确", 1_000) };
  });
  if (!changes.length || changes.length > 50) throw protocolError("修改数量不正确");
  const generationStatus = record.generation_status === undefined ? "UNAVAILABLE" : record.generation_status;
  if (typeof generationStatus !== "string" || !documentGenerationStatuses.some((status) => status === generationStatus)) throw protocolError("文书生成状态不正确");
  return { proposalId, revisionNumber: requiredPositiveInteger(record.expected_revision_number, "修改版本不正确", 999), basedOnCurrentVersion: record.based_on_current_version, generationStatus: generationStatus as WebDocumentGenerationStatus, changes };
}

export async function authorizeWebDocumentContentGeneration(
  caseId: string, runId: string, artifactId: string, proposalId: string,
  revisionNumber: number, reviewNote: string, idempotencyKey: string, recoveryNamespace: string,
): Promise<string> {
  if (!/^[a-f0-9]{64}$/.test(recoveryNamespace)) throw protocolError("登录恢复标识不正确");
  const response = await webApiFetch(`${contentProposalPath(caseId, runId, artifactId)}/${normalizeOpaqueId(proposalId, "修改编号")}/generation-reviews`, {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey) },
    body: JSON.stringify({ expected_revision_number: requiredPositiveInteger(revisionNumber, "文书版本不正确", 999),
      review_note: requiredText(reviewNote.trim(), "请填写复核说明", 2000), recovery_namespace: recoveryNamespace }),
  });
  const record = asRecord(await readJsonResponse(response, "授权生成文书"), "生成授权记录不正确");
  if (record.status !== "AUTHORIZED_NOT_GENERATED" || record.court_ready !== false) throw protocolError("生成授权状态不正确");
  return normalizeOpaqueId(requiredText(record.review_id, "生成授权编号不正确", 80), "生成授权编号");
}

export async function requestWebCaseAgentDocumentRevision(
  caseId: string,
  runId: string,
  artifactId: string,
  expectedRevisionNumber: number,
  idempotencyKey: string,
): Promise<WebCaseAgentDocumentReview> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const normalizedArtifactId = normalizeOpaqueId(artifactId, "文书成果编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}/artifacts/${normalizedArtifactId}/document-revisions`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey),
      },
      body: JSON.stringify({
        expected_revision_number: requiredPositiveInteger(
          expectedRevisionNumber,
          "文书版本格式不正确",
          999,
        ),
      }),
    },
  );
  const payload = await readJsonResponse(response, "更新 Agent 文书模板版本");
  return parseWebCaseAgentDocumentReview(
    asRecord(payload, "Agent 文书更新响应格式不正确").review,
  );
}

export function webCaseAgentDocumentDownloadPath(
  caseId: string,
  runId: string,
  artifactId: string,
  fileRole: "editable" | "pdf-preview",
): string {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const normalizedArtifactId = normalizeOpaqueId(artifactId, "文书成果编号");
  if (fileRole !== "editable" && fileRole !== "pdf-preview") {
    throw protocolError("文书下载类型不受支持");
  }
  return `/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}/artifacts/${normalizedArtifactId}/document-files/${fileRole}`;
}

export async function downloadWebCaseAgentDocumentFile(
  caseId: string,
  runId: string,
  artifactId: string,
  fileRole: "editable" | "pdf-preview",
  outputFormat: "DOCX" | "XLSX",
  expectedReviewVersion?: string,
): Promise<Readonly<{ fileName: string; byteSize: number }>> {
  const path = webCaseAgentDocumentDownloadPath(caseId, runId, artifactId, fileRole);
  if (expectedReviewVersion !== undefined && !/^[a-f0-9]{64}$/.test(expectedReviewVersion)) throw protocolError("文书审阅版本无效");
  const response = await webApiFetch(path, expectedReviewVersion ? { headers: { "X-Document-Review-Version": expectedReviewVersion } } : undefined);
  if (!response.ok) {
    await readJsonResponse(response, fileRole === "pdf-preview" ? "下载 PDF 审阅稿" : "下载可编辑文书");
    throw protocolError("文书下载响应格式不正确");
  }
  const expectedMediaType = fileRole === "pdf-preview"
    ? "application/pdf"
    : outputFormat === "DOCX"
      ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  if (expectedReviewVersion && response.headers.get("X-Document-Review-Version") !== expectedReviewVersion) {
    throw protocolError("下载版本与已审阅文书不一致，本次不保存文件");
  }
  const mediaType = (response.headers.get("content-type") ?? "").split(";", 1)[0]?.trim().toLowerCase();
  if (mediaType !== expectedMediaType) {
    throw protocolError("服务端返回的文书文件格式与已核验成果不一致");
  }
  const blob = await response.blob();
  if (blob.size < 1) throw protocolError("服务端返回的文书文件为空");
  const fallbackName = fileRole === "pdf-preview"
    ? "agent-document-review.pdf"
    : `agent-document-candidate.${outputFormat === "DOCX" ? "docx" : "xlsx"}`;
  const fileName = safeContentDispositionFileName(response.headers.get("content-disposition")) ?? fallbackName;
  if (typeof document === "undefined" || typeof URL === "undefined") {
    throw new Error("当前环境无法启动浏览器文书下载");
  }
  const objectUrl = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = fileName;
    anchor.rel = "noreferrer";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }
  return { fileName, byteSize: blob.size };
}

export async function decideWebCaseAgentItem(caseId: string, runId: string, decisionId: string, expectedRunVersion: number, optionId: string | null, note: string | null): Promise<WebCaseAgentRun> {
  return postWebCaseAgentReview(caseId, runId, `decisions/${normalizeOpaqueId(decisionId, "决定事项编号")}`, expectedRunVersion, { option_id: optionId, note }, "提交律师决定");
}

export async function approveWebCaseAgentItem(caseId: string, runId: string, approvalId: string, expectedRunVersion: number, approved: boolean, note: string | null): Promise<WebCaseAgentRun> {
  return postWebCaseAgentReview(caseId, runId, `approvals/${normalizeOpaqueId(approvalId, "审批事项编号")}`, expectedRunVersion, { approved, note }, "提交律师审批");
}

async function postWebCaseAgentReview(caseId: string, runId: string, suffix: string, expectedRunVersion: number, fields: Record<string, unknown>, operation: string): Promise<WebCaseAgentRun> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "办案任务编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/case-agent-runs/${normalizedRunId}/${suffix}`, {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseAgentIdempotencyKey() },
    body: JSON.stringify({ expected_run_version: requiredPositiveInteger(expectedRunVersion, "任务版本格式不正确", Number.MAX_SAFE_INTEGER), ...fields }),
  });
  const payload = await readJsonResponse(response, operation);
  return parseWebCaseAgentRun(asRecord(payload, "办案 Agent 响应格式不正确").run);
}

export async function readWebRepresentationProfile(caseId: string, signal?: AbortSignal): Promise<WebRepresentationProfile> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/representation-profile`, { signal });
  const payload = await readJsonResponse(response, "读取代理身份与程序阶段");
  return parseWebRepresentationProfile(asRecord(payload, "代理身份响应格式不正确").profile);
}

export async function readWebCasePosture(caseId: string, signal?: AbortSignal): Promise<WebCasePosture> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/case-posture`, { signal });
  const payload = await readJsonResponse(response, "读取本案代理情境");
  const record = asRecord(payload, "代理情境响应格式不正确");
  return {
    ...parseWebCasePostureState(asRecord(record.posture, "代理情境响应未提供状态")),
    options: parseWebCasePostureOptions(asRecord(record.options, "代理情境响应未提供受控选项")),
  };
}

/**
 * Confirms the complete profile with one user action. The API stores derived
 * stage keys server-side; callers must retain the supplied key for an
 * identical retry so an interrupted response can resume safely.
 */
export async function confirmWebCasePosture(input: {
  caseId: string;
  expectedVersion: number;
  idempotencyKey: string;
  partyKind: string;
  displayLabel: string;
  forumType: string;
  caseTypeCode: string;
  procedureStage: string;
  positionCode: string;
  authorityScopeCode: string;
  engagementState: string;
}): Promise<WebCasePostureCompleteReceipt> {
  const normalizedCaseId = normalizeOpaqueId(input.caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/case-posture/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": normalizeIdempotencyKey(input.idempotencyKey) },
    body: JSON.stringify({
      expected_version: requiredPositiveInteger(input.expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER),
      party_kind: normalizePostureCode(input.partyKind, "当事人类型"),
      display_label: normalizePostureDisplayLabel(input.displayLabel),
      forum_type: normalizePostureCode(input.forumType, "受理机构"),
      case_type_code: normalizePostureCode(input.caseTypeCode, "案件类型"),
      procedure_stage: normalizePostureCode(input.procedureStage, "程序阶段"),
      position_code: normalizePostureCode(input.positionCode, "当事人地位"),
      authority_scope_code: normalizePostureCode(input.authorityScopeCode, "代理权限"),
      engagement_state: normalizePostureCode(input.engagementState, "委托状态"),
    }),
  });
  const payload = await readJsonResponse(response, "确认本案代理情境");
  const record = asRecord(payload, "代理情境确认回执格式不正确");
  return parseWebCasePostureCompleteReceipt(record.receipt ?? record);
}

export async function confirmWebCasePostureParty(input: {
  caseId: string;
  expectedVersion: number;
  partyKind: string;
  displayLabel: string;
}): Promise<WebCasePostureCommandReceipt> {
  return postWebCasePostureCommand(input.caseId, "parties", input.expectedVersion, {
    party_kind: normalizePostureCode(input.partyKind, "当事人类型"),
    display_label: normalizePostureDisplayLabel(input.displayLabel),
  }, "确认被代理当事人");
}

export async function confirmWebCasePostureProceeding(input: {
  caseId: string;
  expectedVersion: number;
  forumType: string;
  caseTypeCode: string;
  procedureStage: string;
}): Promise<WebCasePostureCommandReceipt> {
  return postWebCasePostureCommand(input.caseId, "proceedings", input.expectedVersion, {
    forum_type: normalizePostureCode(input.forumType, "受理机构"),
    case_type_code: normalizePostureCode(input.caseTypeCode, "案件类型"),
    procedure_stage: normalizePostureCode(input.procedureStage, "程序阶段"),
  }, "确认当前程序");
}

export async function confirmWebCasePosturePosition(input: {
  caseId: string;
  expectedVersion: number;
  proceedingId: string;
  partyId: string;
  positionCode: string;
}): Promise<WebCasePostureCommandReceipt> {
  return postWebCasePostureCommand(input.caseId, "positions", input.expectedVersion, {
    proceeding_id: normalizeOpaqueId(input.proceedingId, "程序编号"),
    party_id: normalizeOpaqueId(input.partyId, "当事人编号"),
    position_code: normalizePostureCode(input.positionCode, "当事人地位"),
  }, "确认当事人地位");
}

export async function confirmWebCasePostureEngagement(input: {
  caseId: string;
  expectedVersion: number;
  proceedingId: string;
  representedPartyId: string;
  authorityScopeCode: string;
  engagementState: string;
}): Promise<WebCasePostureCommandReceipt> {
  return postWebCasePostureCommand(input.caseId, "engagements", input.expectedVersion, {
    proceeding_id: normalizeOpaqueId(input.proceedingId, "程序编号"),
    represented_party_id: normalizeOpaqueId(input.representedPartyId, "被代理当事人编号"),
    authority_scope_code: normalizePostureCode(input.authorityScopeCode, "代理权限"),
    engagement_state: normalizePostureCode(input.engagementState, "委托状态"),
  }, "确认委托范围");
}

export async function confirmWebCasePostureProfile(input: {
  caseId: string;
  expectedVersion: number;
  representedPartyId: string;
  proceedingId: string;
  positionId: string;
  engagementId: string;
}): Promise<WebCasePostureCommandReceipt> {
  return postWebCasePostureCommand(input.caseId, "profile", input.expectedVersion, {
    represented_party_id: normalizeOpaqueId(input.representedPartyId, "被代理当事人编号"),
    proceeding_id: normalizeOpaqueId(input.proceedingId, "程序编号"),
    position_id: normalizeOpaqueId(input.positionId, "当事人地位编号"),
    engagement_id: normalizeOpaqueId(input.engagementId, "委托编号"),
  }, "确认本案代理情境");
}

async function postWebCasePostureCommand(
  caseId: string,
  suffix: "parties" | "proceedings" | "positions" | "engagements" | "profile",
  expectedVersion: number,
  fields: Record<string, string>,
  operation: string,
): Promise<WebCasePostureCommandReceipt> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/case-posture/${suffix}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({
      expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER),
      ...fields,
    }),
  });
  const payload = await readJsonResponse(response, operation);
  const record = asRecord(payload, `${operation}回执格式不正确`);
  return parseWebCasePostureCommandReceipt(record.receipt ?? record);
}

export async function queueCurrentEvidenceWebAgentRun(caseId: string, expectedVersion: number): Promise<WebAgentMaterialRun> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/agent-runs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": createWebAgentIdempotencyKey(),
    },
    body: JSON.stringify({
      expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER),
      scope: "ALL_CURRENT_EVIDENCE",
      material_review_authorized: true,
    }),
  });
  const payload = await readJsonResponse(response, "开始整案材料整理");
  return parseWebAgentMaterialRun(asRecord(payload, "整案材料整理响应格式不正确").run);
}

export async function readWebAgentCandidateBatch(caseId: string, runId: string, signal?: AbortSignal): Promise<WebAgentCandidateBatch> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "材料整理任务编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/agent-runs/${normalizedRunId}/candidate-batches?limit=100`, { signal });
  const payload = await readJsonResponse(response, "读取材料整理候选");
  return parseWebAgentCandidateBatch(asRecord(payload, "材料整理候选响应格式不正确").candidates);
}

export async function readWebDynamicCasePlan(caseId: string, signal?: AbortSignal): Promise<WebDynamicCasePlan | null> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/dynamic-case-plan`, { signal });
  const payload = await readJsonResponse(response, "读取动态办案计划");
  const plan = asRecord(payload, "动态办案计划响应格式不正确").plan;
  return plan === null || plan === undefined ? null : parseWebDynamicCasePlan(plan);
}

export async function decideWebDynamicCasePlanItem(
  caseId: string,
  planId: string,
  itemId: string,
  expectedVersion: number,
  decision: WebDynamicCasePlanDecision,
  idempotencyKey: string,
): Promise<{ matterVersion: number; decisionStatus: "APPROVED" | "CHANGE_REQUESTED" | "REJECTED"; requiresReplanning: boolean }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedPlanId = normalizeOpaqueId(planId, "办案计划编号");
  const normalizedItemId = normalizeOpaqueId(itemId, "计划建议编号");
  const body: Record<string, unknown> = {
    expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER),
    decision: decision.decision,
    reason_code: decision.reasonCode,
  };
  if (decision.decision === "MODIFY") {
    if (decision.readinessOverride === undefined && decision.requiredForDeliveryOverride === undefined) {
      throw new Error("修改计划建议时必须选择至少一项结构化调整。");
    }
    if (decision.readinessOverride !== undefined) body.readiness_override = decision.readinessOverride;
    if (decision.requiredForDeliveryOverride !== undefined) body.required_for_delivery_override = decision.requiredForDeliveryOverride;
  }
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/dynamic-case-plans/${normalizedPlanId}/items/${normalizedItemId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey) },
    body: JSON.stringify(body),
  });
  const payload = await readJsonResponse(response, "复核动态办案建议");
  const receipt = asRecord(asRecord(payload, "动态办案建议回执格式不正确").receipt, "动态办案建议回执格式不正确");
  const status = requiredText(receipt.decision_status, "动态办案建议决定格式不正确", 20);
  if (!(["APPROVED", "CHANGE_REQUESTED", "REJECTED"] as const).includes(status as "APPROVED" | "CHANGE_REQUESTED" | "REJECTED")) {
    throw protocolError("动态办案建议决定格式不正确");
  }
  if (normalizeOpaqueId(receipt.plan_id, "办案计划编号") !== normalizedPlanId || normalizeOpaqueId(receipt.item_id, "计划建议编号") !== normalizedItemId) {
    throw protocolError("动态办案建议回执与请求不一致");
  }
  return {
    matterVersion: requiredPositiveInteger(receipt.matter_version, "动态办案建议回执未提供案件版本", Number.MAX_SAFE_INTEGER),
    decisionStatus: status as "APPROVED" | "CHANGE_REQUESTED" | "REJECTED",
    requiresReplanning: requiredBoolean(receipt.requires_replanning, "动态办案建议重研判状态格式不正确"),
  };
}

export async function activateWebDynamicCasePlan(
  caseId: string,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<{ planId: string; matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/dynamic-case-plan/activate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey) },
    body: JSON.stringify({
      expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER),
    }),
  });
  const payload = await readJsonResponse(response, "确认整案办案计划");
  const receipt = asRecord(asRecord(payload, "办案计划激活回执格式不正确").receipt, "办案计划激活回执格式不正确");
  if (requiredText(receipt.status, "办案计划激活状态格式不正确", 20) !== "ACTIVE") {
    throw protocolError("办案计划激活状态格式不正确");
  }
  return {
    planId: normalizeOpaqueId(receipt.plan_id, "办案计划编号"),
    matterVersion: requiredPositiveInteger(receipt.matter_version, "办案计划激活案件版本格式不正确", Number.MAX_SAFE_INTEGER),
  };
}

export function createWebDynamicCasePlanIdempotencyKey(): string {
  if (typeof crypto === "undefined" || typeof crypto.randomUUID !== "function") {
    throw new Error("当前浏览器无法生成安全的办案计划请求编号。请使用受支持的现代浏览器。");
  }
  return normalizeIdempotencyKey(`dynamic-plan-${crypto.randomUUID()}`);
}

export async function readWebAgentLedgerExtractionBatches(
  caseId: string,
  signal?: AbortSignal,
): Promise<readonly WebAgentLedgerExtractionBatch[]> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/agent-ledger-extractions`,
    { signal },
  );
  const payload = await readJsonResponse(response, "读取 Agent 材料提取批次");
  const batches = asRecord(payload, "材料提取批次响应格式不正确").batches;
  const parsed = parseArray(batches, "材料提取批次", parseWebAgentLedgerExtractionBatch);
  if (
    parsed.length > 100
    || parsed.some((batch) => batch.matterId !== normalizedCaseId)
    || new Set(parsed.map((batch) => batch.batchId)).size !== parsed.length
  ) {
    throw protocolError("材料提取批次与当前案件不一致");
  }
  return parsed;
}

export async function confirmWebAgentLedgerExtractionLowRisk(
  caseId: string,
  batchId: string,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<WebAgentLedgerExtractionConfirmationReceipt> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedBatchId = normalizeOpaqueId(batchId, "材料提取批次编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/agent-ledger-extractions/${normalizedBatchId}/confirm-low-risk`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey),
      },
      body: JSON.stringify({
        expected_version: requiredPositiveInteger(
          expectedVersion,
          "案件版本格式不正确",
          Number.MAX_SAFE_INTEGER,
        ),
      }),
    },
  );
  const payload = await readJsonResponse(response, "确认低风险材料提取组");
  const receipt = asRecord(
    asRecord(payload, "低风险材料提取确认回执格式不正确").receipt,
    "低风险材料提取确认回执格式不正确",
  );
  const factCount = requiredNonNegativeInteger(
    receipt.confirmed_fact_count,
    "已确认事实数量格式不正确",
  );
  const transactionCount = requiredNonNegativeInteger(
    receipt.confirmed_transaction_count,
    "已确认交易数量格式不正确",
  );
  const totalCount = requiredPositiveInteger(
    receipt.confirmed_total_count,
    "已确认候选数量格式不正确",
    500,
  );
  if (factCount + transactionCount !== totalCount) {
    throw protocolError("低风险材料提取确认数量不一致");
  }
  const receiptBatchId = normalizeOpaqueId(receipt.batch_id, "材料提取批次编号");
  const matterVersion = requiredPositiveInteger(
      receipt.matter_version,
      "低风险材料提取确认案件版本格式不正确",
      Number.MAX_SAFE_INTEGER,
    );
  if (receiptBatchId !== normalizedBatchId || matterVersion !== expectedVersion + 1) {
    throw protocolError("低风险材料提取确认回执与请求不一致");
  }
  return {
    batchId: receiptBatchId,
    matterVersion,
    confirmedFactCount: factCount,
    confirmedTransactionCount: transactionCount,
    confirmedTotalCount: totalCount,
  };
}

export async function readWebAgentLedgerExceptionGroupMembers(
  caseId: string,
  batchId: string,
  groupId: string,
  offset: number,
  limit = 50,
  signal?: AbortSignal,
): Promise<WebAgentLedgerExceptionMemberPage> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedBatchId = normalizeOpaqueId(batchId, "材料提取批次编号");
  const normalizedGroupId = normalizeOpaqueId(groupId, "异常组编号");
  if (!Number.isInteger(offset) || offset < 0 || offset > 1_000_000) {
    throw new Error("异常组分页位置无效。");
  }
  if (!Number.isInteger(limit) || limit < 1 || limit > 50) {
    throw new Error("异常组分页大小无效。");
  }
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/agent-ledger-extractions/${normalizedBatchId}/exception-groups/${normalizedGroupId}/members?offset=${offset}&limit=${limit}`,
    { signal },
  );
  const payload = await readJsonResponse(response, "读取异常组完整成员");
  const page = parseWebAgentLedgerExceptionMemberPage(
    asRecord(payload, "异常组成员响应格式不正确").page,
  );
  if (page.groupId !== normalizedGroupId || page.offset !== offset) {
    throw protocolError("异常组成员响应与请求不一致");
  }
  return page;
}

export async function decideWebAgentLedgerExceptionGroup(
  caseId: string,
  batchId: string,
  groupId: string,
  expectedVersion: number,
  decision: WebAgentLedgerExceptionDecision,
  reason: WebAgentLedgerExceptionReason,
  reasonNote: string | null,
  idempotencyKey: string,
): Promise<WebAgentLedgerExceptionDecisionReceipt> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedBatchId = normalizeOpaqueId(batchId, "材料提取批次编号");
  const normalizedGroupId = normalizeOpaqueId(groupId, "异常组编号");
  const version = requiredPositiveInteger(
    expectedVersion,
    "案件版本格式不正确",
    Number.MAX_SAFE_INTEGER,
  );
  const normalizedNote = reasonNote === null ? null : reasonNote.trim();
  if (normalizedNote !== null && (normalizedNote.length === 0 || normalizedNote.length > 500)) {
    throw new Error("处置说明应为 1–500 个字符。");
  }
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/agent-ledger-extractions/${normalizedBatchId}/exception-groups/${normalizedGroupId}/decision`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey),
      },
      body: JSON.stringify({
        expected_version: version,
        decision,
        reason,
        reason_note: normalizedNote,
      }),
    },
  );
  const payload = await readJsonResponse(response, "处置材料提取异常组");
  const receipt = parseWebAgentLedgerExceptionDecisionReceipt(
    asRecord(payload, "异常组处置回执格式不正确").receipt,
  );
  if (
    receipt.batchId !== normalizedBatchId
    || receipt.groupId !== normalizedGroupId
    || receipt.decision !== decision
    || ![version, version + 1].includes(receipt.committedMatterVersion)
    || receipt.matterVersion < receipt.committedMatterVersion
  ) {
    throw protocolError("异常组处置回执与请求不一致");
  }
  return receipt;
}

export function createWebAgentLedgerExtractionIdempotencyKey(): string {
  if (typeof crypto === "undefined" || typeof crypto.randomUUID !== "function") {
    throw new Error("当前浏览器无法生成安全的批次确认请求编号。请使用受支持的现代浏览器。");
  }
  return normalizeIdempotencyKey(`ledger-extraction-${crypto.randomUUID()}`);
}

export async function readWebAgentLedgerExceptionFollowupPage(
  caseId: string,
  offset = 0,
  limit = 50,
  signal?: AbortSignal,
): Promise<WebAgentLedgerExceptionFollowupPage> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const query = buildWebAgentLedgerPageQuery(offset, limit);
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/agent-ledger-exception-followups?${query}`,
    { signal },
  );
  const payload = await readJsonResponse(response, "读取异常后续工作");
  const page = parseWebAgentLedgerExceptionFollowupPage(
    asRecord(payload, "异常后续工作响应格式不正确").page,
  );
  if (page.offset !== offset || page.followups.length > limit) {
    throw protocolError("异常后续工作分页与请求不一致");
  }
  return page;
}

export async function readWebAgentLedgerEligibleEvidenceSources(
  caseId: string,
  followupId: string,
  offset = 0,
  limit = 50,
  signal?: AbortSignal,
): Promise<WebManagedEvidenceSourcePage> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedFollowupId = normalizeOpaqueId(followupId, "异常后续工作编号");
  const query = buildWebAgentLedgerPageQuery(offset, limit);
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/agent-ledger-exception-followups/${normalizedFollowupId}/eligible-managed-evidence-sources?${query}`,
    { signal },
  );
  const payload = asRecord(
    await readJsonResponse(response, "读取可用补证材料"),
    "可用补证材料响应格式不正确",
  );
  if (normalizeOpaqueId(payload.followup_id, "异常后续工作编号") !== normalizedFollowupId) {
    throw protocolError("可用补证材料与当前后续工作不一致");
  }
  const page = parseWebManagedEvidenceSourcePage(payload.page);
  if (page.offset !== offset || page.sources.length > limit) {
    throw protocolError("可用补证材料分页与请求不一致");
  }
  return page;
}

export async function readWebAgentLedgerFollowupEvidencePage(
  caseId: string,
  followupId: string,
  offset = 0,
  limit = 50,
  signal?: AbortSignal,
): Promise<WebAgentLedgerFollowupEvidencePage> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedFollowupId = normalizeOpaqueId(followupId, "异常后续工作编号");
  const query = buildWebAgentLedgerPageQuery(offset, limit);
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/agent-ledger-exception-followups/${normalizedFollowupId}/evidence-pages?${query}`,
    { signal },
  );
  const payload = asRecord(
    await readJsonResponse(response, "读取异常后续工作来源页"),
    "异常后续工作来源页响应格式不正确",
  );
  if (normalizeOpaqueId(payload.followup_id, "异常后续工作编号") !== normalizedFollowupId) {
    throw protocolError("来源页与当前后续工作不一致");
  }
  const page = parseWebAgentLedgerFollowupEvidencePage(payload.page);
  if (page.offset !== offset || page.evidencePageIds.length > limit) {
    throw protocolError("来源页分页与请求不一致");
  }
  return page;
}

export async function resolveWebAgentLedgerExceptionFollowup(
  caseId: string,
  followupId: string,
  expectedVersion: number,
  action: WebAgentLedgerFollowupActionCode,
  reasonNote: string,
  sources: readonly WebManagedEvidenceSourceSelection[],
  idempotencyKey: string,
): Promise<WebAgentLedgerExceptionFollowupReceipt> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedFollowupId = normalizeOpaqueId(followupId, "异常后续工作编号");
  const version = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  if (!isAgentLedgerFollowupActionCode(action)) throw new Error("异常后续工作动作无效。");
  const normalizedNote = reasonNote.trim();
  if (!normalizedNote || normalizedNote.length > 500 || new TextEncoder().encode(normalizedNote).length > 2_000) {
    throw new Error("操作说明应为 1–500 个有效字符。");
  }
  const normalizedSources = sources.map((source) => ({
    objectType: source.objectType,
    objectId: normalizeOpaqueId(source.objectId, "补证材料编号"),
  }));
  const sourceIdentities = normalizedSources.map((source) => `${source.objectType}:${source.objectId}`);
  if (
    normalizedSources.length > 100
    || new Set(sourceIdentities).size !== sourceIdentities.length
    || (action === "CONFIRM_MORE_EVIDENCE") !== (normalizedSources.length > 0)
  ) {
    throw new Error("补证材料选择与当前动作不一致。");
  }
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/agent-ledger-exception-followups/${normalizedFollowupId}/action`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey),
      },
      body: JSON.stringify(buildWebAgentLedgerFollowupActionPayload({
        expectedVersion: version,
        action,
        reasonNote: normalizedNote,
        sources: normalizedSources,
      })),
    },
  );
  const payload = await readJsonResponse(response, "提交异常后续工作动作");
  const receipt = parseWebAgentLedgerExceptionFollowupReceipt(
    asRecord(payload, "异常后续工作回执格式不正确").receipt,
  );
  if (
    receipt.followupId !== normalizedFollowupId
    || receipt.action !== action
    || receipt.matterVersion !== version + 1
  ) {
    throw protocolError("异常后续工作回执与请求不一致");
  }
  return receipt;
}

export async function recoverWebAgentLedgerExceptionFollowups(
  caseId: string,
  expectedVersion: number,
  idempotencyKey: string,
): Promise<WebAgentLedgerExceptionRecoveryReceipt> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const version = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/agent-ledger-exception-followups/recover-control`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey),
      },
      body: JSON.stringify(buildWebAgentLedgerRecoveryPayload(version)),
    },
  );
  const payload = asRecord(
    await readJsonResponse(response, "启动异常后续工作恢复"),
    "异常后续工作恢复回执格式不正确",
  );
  const receipt = asRecord(payload.receipt, "异常后续工作恢复回执格式不正确");
  if (
    receipt.control_health !== "HEALTHY"
    || receipt.recovery_started !== true
    || requiredPositiveInteger(receipt.matter_version, "恢复案件版本格式不正确", Number.MAX_SAFE_INTEGER) !== version
  ) {
    throw protocolError("异常后续工作恢复回执与请求不一致");
  }
  return { matterVersion: version, controlHealth: "HEALTHY", recoveryStarted: true };
}

export function createWebAgentLedgerFollowupIdempotencyKey(): string {
  if (typeof crypto === "undefined" || typeof crypto.randomUUID !== "function") {
    throw new Error("当前浏览器无法生成安全的异常后续工作请求编号。");
  }
  return normalizeIdempotencyKey(`ledger-followup-${crypto.randomUUID()}`);
}

export async function decideWebCaseFact(
  caseId: string,
  factId: string,
  expectedVersion: number,
  status: "CONFIRMED" | "DISPUTED" | "DENIED" | "INVALIDATED",
  idempotencyKey: string,
): Promise<{ matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedFactId = normalizeOpaqueId(factId, "事实编号");
  const version = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/facts/${normalizedFactId}/decision`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": normalizeIdempotencyKey(idempotencyKey),
    },
    body: JSON.stringify({ expected_version: version, status }),
  });
  const payload = await readJsonResponse(response, "确认事实");
  const record = asRecord(payload, "事实确认回执格式不正确");
  const receipt = asRecord(record.receipt ?? record, "事实确认回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "事实确认回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function recoverWebCaseFactDecision(caseId:string,factId:string,key:string):Promise<{matterVersion:number}|null> {
  const response=await webApiFetch(`/api/v1/cases/${normalizeOpaqueId(caseId,"案件编号")}/facts/${normalizeOpaqueId(factId,"事实编号")}/decision-receipt`,
    {headers:{"Idempotency-Key":normalizeIdempotencyKey(key)}});
  const value=asRecord(await readJsonResponse(response,"查询原事实决定"),"事实决定回执无效");
  if(value.court_ready!==false)throw protocolError("事实决定不代表可提交法院");
  if(value.receipt===null)return null;
  const receipt=asRecord(value.receipt,"事实决定回执无效");
  if(receipt.object_id!==factId||receipt.object_type!=="FACT")throw protocolError("事实决定回执对象不一致");
  return {matterVersion:requiredPositiveInteger(receipt.matter_version,"案件版本无效",Number.MAX_SAFE_INTEGER)};
}

export async function confirmWebCaseClaimScope(caseId: string, claimId: string, expectedVersion: number): Promise<{ matterVersion: number }> {
  return postWebLedgerConfirmation(caseId, `claims/${normalizeOpaqueId(claimId, "诉请编号")}/confirm-scope`, expectedVersion, "确认诉请范围");
}

export async function setWebCaseClaimResponse(
  caseId: string,
  claimId: string,
  expectedVersion: number,
  input: Readonly<{
    position: "ADMIT" | "PARTIALLY_ADMIT" | "DISPUTE" | "OUTSIDE_SCOPE";
    confirmedFactIds: readonly string[];
    partialAmount?: string | null;
    currency?: string | null;
  }>,
): Promise<{ matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedClaimId = normalizeOpaqueId(claimId, "诉请编号");
  const version = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const confirmedFactIds = normalizeDistinctDecisionIds(input.confirmedFactIds, "回应依据事实", 200);
  const partialAmount = input.partialAmount?.trim() || null;
  const currency = input.currency?.trim().toUpperCase() || null;
  if (input.position === "PARTIALLY_ADMIT") {
    if (partialAmount === null || currency === null) throw new Error("部分承认时请同时填写金额和币种。");
    if (!/^(?:0|[1-9]\d{0,15})(?:\.\d{1,2})?$/.test(partialAmount)) throw new Error("回应金额最多保留两位小数。");
  } else if (partialAmount !== null || currency !== null) {
    throw new Error("只有部分承认可以填写回应金额。");
  }
  if (currency !== null && !/^[A-Z]{3}$/.test(currency)) throw new Error("回应币种必须使用三位大写代码。");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/claims/${normalizedClaimId}/response`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({
      expected_version: version,
      position: input.position,
      confirmed_fact_ids: confirmedFactIds,
      partial_amount: partialAmount,
      currency,
    }),
  });
  const payload = await readJsonResponse(response, "保存本方回应");
  const record = asRecord(payload, "本方回应回执格式不正确");
  const receipt = asRecord(record.receipt ?? record, "本方回应回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "本方回应回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function approveWebCurrentLegalBundle(input: {
  caseId: string;
  expectedVersion: number;
  ruleVersionId: string;
  triggerEventId: string;
  endDate: string;
}): Promise<{ matterVersion: number }> {
  const caseId = normalizeOpaqueId(input.caseId, "案件编号");
  const ruleVersionId = normalizeOpaqueId(input.ruleVersionId, "规则版本编号");
  const triggerEventId = normalizeOpaqueId(input.triggerEventId, "关键日期编号");
  const endDate = input.endDate.trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(endDate)) throw new Error("请填写依据适用终点。");
  const response = await webApiFetch("/api/v1/cases/" + caseId + "/legal-bundles/current", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({
      expected_version: requiredPositiveInteger(input.expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER),
      rule_version_id: ruleVersionId,
      trigger_event_id: triggerEventId,
      end_date: endDate,
    }),
  });
  const payload = asRecord(await readJsonResponse(response, "确认本案适用依据"), "本案适用依据回执格式不正确");
  const receipt = asRecord(payload.receipt ?? payload, "本案适用依据回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "本案适用依据回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function createWebCaseClaimCandidate(
  caseId: string,
  expectedVersion: number,
  input: Readonly<{
    text: string;
    claimedAmount?: string | null;
    currency?: string | null;
    confirmedFactIds: readonly string[];
  }>,
): Promise<{ matterVersion: number; objectId: string }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const version = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const text = normalizeLawyerDecisionText(input.text, "诉请候选", 8_000);
  const confirmedFactIds = normalizeDistinctDecisionIds(input.confirmedFactIds, "诉请事实", 30);
  const amount = input.claimedAmount?.trim() || null;
  const currency = input.currency?.trim().toUpperCase() || null;
  if ((amount === null) !== (currency === null)) {
    throw new Error("诉请金额和币种必须同时填写或同时留空。");
  }
  if (amount !== null && !/^(?:0|[1-9]\d{0,15})(?:\.\d{1,2})?$/.test(amount)) {
    throw new Error("诉请金额最多保留两位小数。");
  }
  if (currency !== null && !/^[A-Z]{3}$/.test(currency)) {
    throw new Error("诉请币种必须使用三位大写代码。");
  }
  return postWebLedgerCandidate(
    normalizedCaseId,
    "claims/candidates",
    {
      expected_version: version,
      original_claim_text: text,
      claimed_amount: amount,
      currency,
      confirmed_fact_ids: confirmedFactIds,
    },
    "建立诉请候选",
  );
}

export async function createWebCaseDisputeIssueCandidate(
  caseId: string,
  expectedVersion: number,
  input: Readonly<{
    question: string;
    claimIds: readonly string[];
    confirmedFactIds: readonly string[];
  }>,
): Promise<{ matterVersion: number; objectId: string }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const version = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  return postWebLedgerCandidate(
    normalizedCaseId,
    "issues/candidates",
    {
      expected_version: version,
      question: normalizeLawyerDecisionText(input.question, "争点候选", 2_000),
      claim_ids: normalizeDistinctDecisionIds(input.claimIds, "争点诉请", 30),
      confirmed_fact_ids: normalizeDistinctDecisionIds(input.confirmedFactIds, "争点事实", 100),
    },
    "建立争点候选",
  );
}

export async function confirmWebCaseDisputeIssue(caseId: string, issueId: string, expectedVersion: number): Promise<{ matterVersion: number }> {
  return postWebLedgerConfirmation(caseId, `issues/${normalizeOpaqueId(issueId, "争点编号")}/confirm`, expectedVersion, "确认争点");
}

export async function confirmWebCaseTransaction(caseId: string, transactionId: string, expectedVersion: number): Promise<{ matterVersion: number }> {
  return postWebLedgerConfirmation(caseId, `transactions/${normalizeOpaqueId(transactionId, "交易编号")}/confirm`, expectedVersion, "确认收付款记录");
}

export async function createWebPaymentClassificationCandidate(input: {
  caseId: string;
  transactionId: string;
  expectedVersion: number;
  obligationLabel: string;
  nature: "DISBURSEMENT" | "REPAYMENT_UNSPECIFIED" | "INTEREST_PAYMENT" | "PRINCIPAL_REPAYMENT";
  sameDaySequence: number | null;
}): Promise<{ matterVersion: number }> {
  const caseId = normalizeOpaqueId(input.caseId, "案件编号");
  const transactionId = normalizeOpaqueId(input.transactionId, "收付款记录编号");
  const obligationLabel = normalizeCalculationObligationId(input.obligationLabel);
  const response = await webApiFetch(`/api/v1/cases/${caseId}/transactions/${transactionId}/payment-classifications`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({
      expected_version: input.expectedVersion,
      obligation_label: obligationLabel,
      nature: input.nature,
      same_day_sequence: input.sameDaySequence,
    }),
  });
  const payload = asRecord(await readJsonResponse(response, "保存款项归属"), "款项归属回执格式不正确");
  const receipt = asRecord(payload.receipt ?? payload, "款项归属回执格式不正确");
  return {
    matterVersion: requiredPositiveInteger(receipt.matter_version, "款项归属回执未提供案件版本", Number.MAX_SAFE_INTEGER),
  };
}

export async function confirmWebPaymentClassification(caseId: string, classificationId: string, expectedVersion: number): Promise<{ matterVersion: number }> {
  return postWebLedgerConfirmation(caseId, `payment-classifications/${normalizeOpaqueId(classificationId, "款项归属编号")}/confirm`, expectedVersion, "确认款项归属");
}

async function postWebLedgerCandidate(
  caseId: string,
  path: string,
  body: Record<string, unknown>,
  operation: string,
): Promise<{ matterVersion: number; objectId: string }> {
  const response = await webApiFetch(`/api/v1/cases/${caseId}/${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": createWebCaseIdempotencyKey(),
    },
    body: JSON.stringify(body),
  });
  const payload = await readJsonResponse(response, operation);
  const record = asRecord(payload, `${operation}回执格式不正确`);
  const receipt = asRecord(record.receipt ?? record, `${operation}回执格式不正确`);
  return {
    matterVersion: requiredPositiveInteger(receipt.matter_version, `${operation}回执未提供案件版本`, Number.MAX_SAFE_INTEGER),
    objectId: normalizeOpaqueId(receipt.object_id, `${operation}对象编号`),
  };
}

async function postWebLedgerConfirmation(caseId: string, path: string, expectedVersion: number, operation: string): Promise<{ matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const version = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": createWebCaseIdempotencyKey(),
    },
    body: JSON.stringify({ expected_version: version }),
  });
  const payload = await readJsonResponse(response, operation);
  const record = asRecord(payload, `${operation}回执格式不正确`);
  const receipt = asRecord(record.receipt ?? record, `${operation}回执格式不正确`);
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, `${operation}回执未提供案件版本`, Number.MAX_SAFE_INTEGER) };
}

export async function listWebLawyerCases(signal?: AbortSignal): Promise<WebLawyerCase[]> {
  const response = await webApiFetch("/api/v1/cases", { signal });
  const payload = await readJsonResponse(response, "读取案件列表");
  const record = asRecord(payload, "案件列表响应格式不正确");
  if (!Array.isArray(record.cases)) {
    throw protocolError("案件列表未提供 cases 数组");
  }
  const cases = record.cases.map((item) => parseCase(item));
  if (new Set(cases.map((item) => item.caseId)).size !== cases.length) {
    throw protocolError("案件列表包含重复案件编号");
  }
  return cases;
}

export async function readWebCaseReview(caseId: string, signal?: AbortSignal): Promise<WebCaseReview> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/review`, { signal });
  const payload = await readJsonResponse(response, "读取案件要点");
  const record = asRecord(payload, "案件要点响应格式不正确");
  return parseCaseReview(asRecord(record.review ?? record, "案件要点响应格式不正确"));
}

export async function readWebLegalReview(caseId: string, signal?: AbortSignal): Promise<WebLegalReview> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/legal-review`, { signal });
  const payload = await readJsonResponse(response, "读取法律依据");
  const record = asRecord(payload, "法律依据响应格式不正确");
  return parseLegalReview(asRecord(record.review ?? record, "法律依据响应格式不正确"));
}

export async function readWebOfficialSourceCaptures(caseId: string, signal?: AbortSignal): Promise<{ catalogue: readonly WebOfficialSourceCatalogueItem[]; captures: WebOfficialSourceCaptureStatus }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/official-source-captures`, { signal });
  const payload = asRecord(await readJsonResponse(response, "读取官方依据核对状态"), "官方依据核对状态格式不正确");
  const catalogue = parseArray(payload.catalogue, "官方依据目录", parseOfficialSourceCatalogueItem);
  const captures = parseOfficialSourceCaptureStatus(asRecord(payload.captures, "官方依据任务状态格式不正确"));
  return { catalogue, captures };
}

export async function queueWebOfficialSourceCapture(caseId: string, sourceId: WebOfficialSourceCatalogueItem["sourceId"], expectedVersion: number): Promise<{ matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/official-source-captures`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({ expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER), source_id: sourceId }),
  });
  const payload = asRecord(await readJsonResponse(response, "提交官方依据核对"), "官方依据核对回执格式不正确");
  const receipt = asRecord(payload.receipt ?? payload, "官方依据核对回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "官方依据核对回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function reviewWebOfficialSourceCapture(input: { caseId: string; runId: string; expectedVersion: number; decision: "APPROVE_FOR_REGISTRATION" | "REJECT"; provisionLocator: string }): Promise<{ matterVersion: number }> {
  const caseId = normalizeOpaqueId(input.caseId, "案件编号");
  const runId = normalizeOpaqueId(input.runId, "官方依据任务编号");
  const provisionLocator = normalizeLawyerDecisionText(input.provisionLocator, "依据定位", 1_000);
  const response = await webApiFetch(`/api/v1/cases/${caseId}/official-source-captures/${runId}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({ expected_version: requiredPositiveInteger(input.expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER), decision: input.decision, provision_locator: provisionLocator }),
  });
  const payload = asRecord(await readJsonResponse(response, "核对官方依据"), "官方依据核对回执格式不正确");
  const receipt = asRecord(payload.receipt ?? payload, "官方依据核对回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "官方依据核对回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function registerWebOfficialSourceCapture(caseId: string, runId: string, expectedVersion: number): Promise<{ matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedRunId = normalizeOpaqueId(runId, "官方依据任务编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/official-source-captures/${normalizedRunId}/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({ expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER) }),
  });
  const payload = asRecord(await readJsonResponse(response, "登记本案依据"), "登记本案依据回执格式不正确");
  const receipt = asRecord(payload.receipt ?? payload, "登记本案依据回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "登记本案依据回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function confirmWebLegalEvent(input: {
  caseId: string;
  expectedVersion: number;
  eventKind: "CONTRACT_SIGNED" | "DISBURSEMENT" | "PAYMENT" | "DEFAULT" | "CLAIM_FILED" | "CASE_ACCEPTED" | "JUDGMENT";
  localDate: string;
  evidencePageIds: readonly string[];
}): Promise<{ matterVersion: number }> {
  const caseId = normalizeOpaqueId(input.caseId, "案件编号");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(input.localDate)) throw protocolError("关键日期格式不正确");
  if (!Array.isArray(input.evidencePageIds) || input.evidencePageIds.length < 1 || input.evidencePageIds.length > 4) {
    throw protocolError("请选择 1 至 4 页材料作为日期依据");
  }
  const evidencePageIds = input.evidencePageIds.map((value) => normalizeOpaqueId(value, "材料页编号"));
  if (new Set(evidencePageIds).size !== evidencePageIds.length) throw protocolError("日期依据不能重复");
  const response = await webApiFetch(`/api/v1/cases/${caseId}/legal-events`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({
      expected_version: requiredPositiveInteger(input.expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER),
      event_kind: input.eventKind,
      local_date: input.localDate,
      evidence_page_ids: evidencePageIds,
    }),
  });
  const payload = asRecord(await readJsonResponse(response, "确认关键日期"), "关键日期确认回执格式不正确");
  const receipt = asRecord(payload.receipt ?? payload, "关键日期确认回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "关键日期确认回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function readWebCaseReadiness(caseId: string, signal?: AbortSignal): Promise<WebCaseReadiness> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/readiness`, { signal });
  const payload = await readJsonResponse(response, "读取办案前置状态");
  const record = asRecord(payload, "办案前置状态响应格式不正确");
  const readiness = asRecord(record.readiness ?? record, "办案前置状态响应格式不正确");
  const checksValue = readiness.checks;
  if (!Array.isArray(checksValue) || checksValue.length > 20) throw protocolError("办案前置状态清单格式不正确");
  const checks = checksValue.map((item) => {
    const check = asRecord(item, "办案前置状态项格式不正确");
    const status = requiredText(check.status, "办案前置状态项缺少状态", 16);
    if (status !== "READY" && status !== "BLOCKED") throw protocolError("办案前置状态项状态无效");
    return { key: requiredText(check.key, "办案前置状态项缺少标识", 64), label: requiredText(check.label, "办案前置状态项缺少名称", 120), status, detail: requiredText(check.detail, "办案前置状态项缺少说明", 500) } as const;
  });
  const counts = asRecord(readiness.counts, "办案前置状态数量格式不正确");
  return {
    matterId: normalizeOpaqueId(readiness.matter_id, "案件编号"),
    matterVersion: requiredPositiveInteger(readiness.matter_version, "办案前置状态未提供案件版本", Number.MAX_SAFE_INTEGER),
    checks,
    counts: {
      facts: requiredNonNegativeInteger(counts.facts, "事实数量格式不正确"),
      claims: requiredNonNegativeInteger(counts.claims, "诉请数量格式不正确"),
      transactions: requiredNonNegativeInteger(counts.transactions, "交易数量格式不正确"),
      candidateItems: requiredNonNegativeInteger(counts.candidate_items, "待确认数量格式不正确"),
      verifiedSources: requiredNonNegativeInteger(counts.verified_sources, "法源数量格式不正确"),
      approvedRules: requiredNonNegativeInteger(counts.approved_rules, "规则数量格式不正确"),
    },
    nextAction: requiredText(readiness.next_action, "办案前置状态未提供下一步", 500),
  };
}

export async function readWebCurrentFormalCalculation(caseId: string, obligationId: string, signal?: AbortSignal): Promise<WebFormalCalculation> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedObligationId = normalizeCalculationObligationId(obligationId);
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/calculations/${encodeURIComponent(normalizedObligationId)}/current`, { signal });
  const payload = await readJsonResponse(response, "读取利息测算");
  const record = asRecord(payload, "利息测算响应格式不正确");
  return parseFormalCalculation(asRecord(record.calculation ?? record, "利息测算响应格式不正确"));
}

export async function createWebFormalCalculation(input: {
  caseId: string;
  obligationId: string;
  expectedVersion: number;
  startDate: string;
  endDate: string;
  allocationPolicy: "INTEREST_THEN_PRINCIPAL" | "PRINCIPAL_THEN_INTEREST";
}): Promise<{ matterVersion: number; objectId: string }> {
  const normalizedCaseId = normalizeOpaqueId(input.caseId, "案件编号");
  const obligationId = normalizeCalculationObligationId(input.obligationId);
  const expectedVersion = requiredPositiveInteger(input.expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const startDate = normalizeIsoDate(input.startDate, "计算开始日期");
  const endDate = normalizeIsoDate(input.endDate, "计算结束日期");
  if (endDate <= startDate) throw new Error("计算结束日期必须晚于开始日期。");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/formal-calculations`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({ expected_version: expectedVersion, obligation_id: obligationId, start_date: startDate, end_date: endDate, allocation_policy: input.allocationPolicy }),
  });
  const payload = await readJsonResponse(response, "建立确定性利息测算");
  const record = asRecord(payload, "利息测算回执格式不正确");
  const receipt = asRecord(record.receipt ?? record, "利息测算回执格式不正确");
  return {
    matterVersion: requiredPositiveInteger(receipt.matter_version, "利息测算回执未提供案件版本", Number.MAX_SAFE_INTEGER),
    objectId: normalizeOpaqueId(receipt.object_id, "利息测算运行编号"),
  };
}

export async function readWebSubmissionReview(caseId: string, signal?: AbortSignal): Promise<WebSubmissionReview> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/submission-review`, { signal });
  const payload = await readJsonResponse(response, "读取应诉材料");
  const record = asRecord(payload, "应诉材料响应格式不正确");
  return parseSubmissionReview(asRecord(record.review ?? record, "应诉材料响应格式不正确"));
}

export async function readWebDocumentDraftReview(caseId: string, signal?: AbortSignal): Promise<WebDocumentDraftReview> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/document-drafts`, { signal });
  const payload = await readJsonResponse(response, "读取文书候选");
  const record = asRecord(payload, "文书候选响应格式不正确");
  return {
    matterId: normalizeOpaqueId(record.matter_id, "案件编号"),
    matterVersion: requiredPositiveInteger(record.matter_version, "文书候选未提供有效案件版本", Number.MAX_SAFE_INTEGER),
    snapshotHash: requiredSha(record.snapshot_hash, "文书候选快照哈希格式不正确"),
    pairs: parseArray(record.pairs, "文书候选", parseDocumentDraftPair),
  };
}

export async function createWebDocumentDraft(input: { caseId: string; expectedVersion: number; documentKind: "CASE_REVIEW_MEMO" | "PAYMENT_LEDGER" }): Promise<{ pairId: string; matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(input.caseId, "案件编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/document-drafts`, {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({ expected_version: requiredPositiveInteger(input.expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER), document_kind: input.documentKind }),
  });
  const payload = await readJsonResponse(response, "生成文书候选");
  const record = asRecord(payload, "文书候选生成回执格式不正确");
  const receipt = asRecord(record.receipt ?? record, "文书候选生成回执格式不正确");
  return { pairId: normalizeOpaqueId(receipt.pair_id, "文书候选编号"), matterVersion: requiredPositiveInteger(receipt.matter_version, "文书候选回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

/**
 * Return a same-origin, server-authorized review/download route.  This is not
 * an object-store URL: the server resolves the current lawyer, case, draft
 * pair, purpose and verified private artifact for every request.
 */
export function webDocumentDraftDeliveryUrl(input: {
  caseId: string;
  pairId: string;
  purpose: "REVIEW_PDF" | "DOWNLOAD_EDITABLE";
}): string {
  const caseId = normalizeOpaqueId(input.caseId, "案件编号");
  const pairId = normalizeOpaqueId(input.pairId, "文书候选编号");
  if (input.purpose !== "REVIEW_PDF" && input.purpose !== "DOWNLOAD_EDITABLE") {
    throw new Error("文书交付用途格式不正确");
  }
  return `/api/v1/cases/${caseId}/document-drafts/${pairId}/delivery?purpose=${input.purpose}`;
}

export async function approveWebDocumentDraft(caseId: string, pairId: string, expectedVersion: number): Promise<{ matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedPairId = normalizeOpaqueId(pairId, "文书候选编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/document-drafts/${normalizedPairId}/approve`, {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({ expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER) }),
  });
  const payload = await readJsonResponse(response, "批准文书候选");
  const record = asRecord(payload, "文书候选审批回执格式不正确");
  const receipt = asRecord(record.receipt ?? record, "文书候选审批回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "文书候选审批回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function approveWebSubmissionWorkProduct(
  caseId: string,
  workProductId: string,
  expectedVersion: number,
): Promise<{ matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedWorkProductId = normalizeOpaqueId(workProductId, "应诉文书编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/submission-work-products/${normalizedWorkProductId}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
      body: JSON.stringify({ expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER) }),
    },
  );
  const payload = await readJsonResponse(response, "批准应诉文书");
  const record = asRecord(payload, "应诉文书审批回执格式不正确");
  const receipt = asRecord(record.receipt ?? record, "应诉文书审批回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "应诉文书审批回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function lockWebSubmissionBundle(
  caseId: string,
  bundleId: string,
  expectedVersion: number,
): Promise<{ matterVersion: number }> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedBundleId = normalizeOpaqueId(bundleId, "应诉材料包编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/submission-bundles/lock`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({ bundle_id: normalizedBundleId, expected_version: requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER) }),
  });
  const payload = await readJsonResponse(response, "锁定应诉材料包");
  const record = asRecord(payload, "应诉材料包锁定回执格式不正确");
  const receipt = asRecord(record.receipt ?? record, "应诉材料包锁定回执格式不正确");
  return { matterVersion: requiredPositiveInteger(receipt.matter_version, "应诉材料包锁定回执未提供案件版本", Number.MAX_SAFE_INTEGER) };
}

export async function createWebLawyerCase(title: string, idempotencyKey: string): Promise<WebLawyerCase> {
  const normalizedTitle = normalizeCaseTitle(title);
  const normalizedIdempotencyKey = normalizeIdempotencyKey(idempotencyKey);
  const response = await webApiFetch("/api/v1/cases", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": normalizedIdempotencyKey,
    },
    body: JSON.stringify({ title: normalizedTitle }),
  });
  const payload = await readJsonResponse(response, "新建案件");
  const record = asRecord(payload, "新建案件响应格式不正确");
  return parseCase(record.case ?? record);
}

/**
 * Creates a server-side, case-scoped receiving slot.  The filename is only a
 * client-provided display label; the server remains authoritative for
 * sanitisation, MIME verification, anti-malware scanning and object storage.
 */
export async function createWebMaterialUploadSlot(
  caseId: string,
  expectedVersion: number,
  file: File,
): Promise<WebMaterialUploadSlot> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedExpectedVersion = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const clientFilename = normalizeClientFilename(file.name);
  const sizeLimit = isImageMaterialCandidate(file) ? WEB_MAX_IMAGE_BYTES : WEB_MAX_PDF_BYTES;
  if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > sizeLimit) {
    throw new Error("文件大小不符合要求，未创建材料接收位。");
  }
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/material-uploads`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({
      client_filename: clientFilename,
      content_length: file.size,
      content_type: webMaterialContentType(file),
      expected_version: normalizedExpectedVersion,
    }),
  });
  const payload = await readJsonResponse(response, "建立材料接收位");
  const record = asRecord(payload, "材料接收位响应格式不正确");
  const upload = asRecord(record.upload ?? record, "材料接收位响应格式不正确");
  return { uploadId: normalizeOpaqueId(upload.upload_id, "材料接收编号") };
}

/**
 * Transfers raw PDF bytes only after the server granted a receiving slot.
 * There is intentionally no browser-side retry: a transport failure after
 * bytes leave the browser has an unknown outcome and must be reconciled by
 * the server-side audit trail instead of duplicated locally.
 */
export async function uploadWebMaterialPdf(
  caseId: string,
  uploadId: string,
  file: File,
): Promise<WebMaterialReceipt> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedUploadId = normalizeOpaqueId(uploadId, "材料接收编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/material-uploads/${normalizedUploadId}/content`,
    {
      method: "PUT",
      headers: { "Content-Type": webMaterialContentType(file) },
      body: file,
    },
  );
  const payload = await readJsonResponse(response, "上传 PDF 材料");
  const record = asRecord(payload, "材料接收回执格式不正确");
  return parseMaterialReceipt(record.receipt ?? record);
}

export async function createWebMaterialArchiveSlot(
  caseId: string,
  expectedVersion: number,
  file: File,
): Promise<WebMaterialArchiveUploadSlot> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedExpectedVersion = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const clientFilename = normalizeClientFilename(file.name);
  if (!isZipCandidate(file) || !Number.isSafeInteger(file.size) || file.size <= 0 || file.size > WEB_MAX_ARCHIVE_BYTES) {
    throw new Error("ZIP 材料包大小或格式不符合要求，未创建接收位。");
  }
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/material-archives`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({
      client_filename: clientFilename,
      content_length: file.size,
      content_type: "application/zip",
      expected_version: normalizedExpectedVersion,
    }),
  });
  const payload = await readJsonResponse(response, "建立 ZIP 材料接收位");
  const record = asRecord(payload, "ZIP 材料接收位响应格式不正确");
  const upload = asRecord(record.upload ?? record, "ZIP 材料接收位响应格式不正确");
  return { archiveId: normalizeOpaqueId(upload.archive_id, "ZIP 材料接收编号") };
}

export async function uploadWebMaterialArchive(
  caseId: string,
  archiveId: string,
  file: File,
): Promise<WebMaterialArchiveReceipt> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedArchiveId = normalizeOpaqueId(archiveId, "ZIP 材料接收编号");
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/material-archives/${normalizedArchiveId}/content`,
    { method: "PUT", headers: { "Content-Type": "application/zip" }, body: file },
  );
  const payload = await readJsonResponse(response, "上传 ZIP 材料包");
  const record = asRecord(payload, "ZIP 材料接收回执格式不正确");
  return parseMaterialArchiveReceipt(record.receipt ?? record);
}

/**
 * Reserves a case-scoped receiving slot for a supported common material.
 * The browser keeps one idempotency key for this reservation and uses a
 * second, distinct key for the irreversible byte hand-off below.
 */
export async function createWebCommonMaterialUploadSlot(
  caseId: string,
  expectedVersion: number,
  file: File,
): Promise<WebCommonMaterialUploadSlot> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedExpectedVersion = requiredPositiveInteger(expectedVersion, "案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const clientFilename = normalizeClientFilename(file.name);
  const contentType = commonMaterialContentType(file);
  if (!contentType || !isCommonMaterialCandidate(file) || !Number.isSafeInteger(file.size) || file.size <= 0 || file.size > WEB_MAX_COMMON_MATERIAL_BYTES) {
    throw new Error("常见材料大小或格式不符合要求，未创建接收位。");
  }
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/common-material-uploads`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": createWebCaseIdempotencyKey() },
    body: JSON.stringify({
      client_filename: clientFilename,
      content_length: file.size,
      content_type: contentType,
      expected_version: normalizedExpectedVersion,
    }),
  });
  const payload = await readJsonResponse(response, "建立常见材料接收位");
  const record = asRecord(payload, "常见材料接收位响应格式不正确");
  const upload = asRecord(record.upload ?? record, "常见材料接收位响应格式不正确");
  return { uploadId: normalizeOpaqueId(upload.upload_id, "常见材料接收编号") };
}

/**
 * Transfers one common-material body after a slot is granted.  There is no
 * automatic retry: an interrupted transfer has an unknown server outcome and
 * must be reconciled through the status endpoint before another upload.
 */
export async function uploadWebCommonMaterial(
  caseId: string,
  uploadId: string,
  file: File,
): Promise<WebCommonMaterialAdmissionReceipt> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedUploadId = normalizeOpaqueId(uploadId, "常见材料接收编号");
  const contentType = commonMaterialContentType(file);
  if (!contentType || !isCommonMaterialCandidate(file)) {
    throw new Error("常见材料格式不符合要求，未开始正文传输。");
  }
  const response = await webApiFetch(
    `/api/v1/cases/${normalizedCaseId}/common-material-uploads/${normalizedUploadId}/content`,
    {
      method: "PUT",
      headers: { "Content-Type": contentType, "Idempotency-Key": createWebCaseIdempotencyKey() },
      body: file,
    },
  );
  const payload = await readJsonResponse(response, "上传常见材料");
  const record = asRecord(payload, "常见材料接收回执格式不正确");
  return parseCommonMaterialAdmissionReceipt(record.receipt ?? record);
}

export async function readWebMaterialUploadStatus(caseId: string, uploadId: string): Promise<WebMaterialUploadStatus> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedUploadId = normalizeOpaqueId(uploadId, "材料接收编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/material-uploads/${normalizedUploadId}`);
  const payload = await readJsonResponse(response, "核验 PDF 接收状态");
  return parseMaterialUploadStatus(asRecord(payload, "PDF 接收状态响应格式不正确").status, "PDF");
}

export async function readWebMaterialArchiveStatus(caseId: string, archiveId: string): Promise<WebMaterialUploadStatus> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedArchiveId = normalizeOpaqueId(archiveId, "ZIP 材料接收编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/material-archives/${normalizedArchiveId}`);
  const payload = await readJsonResponse(response, "核验 ZIP 接收状态");
  return parseMaterialUploadStatus(asRecord(payload, "ZIP 接收状态响应格式不正确").status, "ZIP");
}

export async function readWebCommonMaterialUploadStatus(
  caseId: string,
  uploadId: string,
): Promise<WebCommonMaterialUploadStatus> {
  const normalizedCaseId = normalizeOpaqueId(caseId, "案件编号");
  const normalizedUploadId = normalizeOpaqueId(uploadId, "常见材料接收编号");
  const response = await webApiFetch(`/api/v1/cases/${normalizedCaseId}/common-material-uploads/${normalizedUploadId}`);
  const payload = await readJsonResponse(response, "核验常见材料接收状态");
  return parseCommonMaterialUploadStatus(asRecord(payload, "常见材料接收状态响应格式不正确").status);
}

export function normalizeCaseTitle(value: string): string {
  const normalized = value.trim().replace(/\s+/g, " ");
  if (normalized.length < CASE_TITLE_MIN_LENGTH || normalized.length > CASE_TITLE_MAX_LENGTH || containsControlCharacter(normalized)) {
    throw new Error(`案件名称须为 ${CASE_TITLE_MIN_LENGTH} 至 ${CASE_TITLE_MAX_LENGTH} 个可见字符。`);
  }
  return normalized;
}

export function webMaterialContentType(file: File): string {
  if (isImageMaterialCandidate(file)) {
    return file.name.toLowerCase().endsWith(".png") ? "image/png" : "image/jpeg";
  }
  return "application/pdf";
}

export function isPdfCandidate(file: File): boolean {
  const filename = file.name.toLowerCase();
  const type = file.type.toLowerCase();
  return filename.endsWith(".pdf") && (type === "" || type === "application/pdf" || type === "application/x-pdf");
}

export function isImageMaterialCandidate(file: File): boolean {
  const filename = file.name.toLowerCase();
  const type = file.type.toLowerCase();
  const suffixOk = filename.endsWith(".jpg") || filename.endsWith(".jpeg") || filename.endsWith(".png");
  const typeOk = type === "" || type === "image/jpeg" || type === "image/png";
  return suffixOk && typeOk;
}

export function isZipCandidate(file: File): boolean {
  const filename = file.name.toLowerCase();
  const type = file.type.toLowerCase();
  return filename.endsWith(".zip") && (type === "" || type === "application/zip" || type === "application/x-zip-compressed");
}

/** Supported by the current safe-admission slice; server byte inspection remains authoritative. */
export function isCommonMaterialCandidate(file: File): boolean {
  return commonMaterialContentType(file) !== null;
}

/** Old binary Office/mail/OFD formats have no safe reader in this release. */
export function isLegacyCommonMaterialCandidate(file: File): boolean {
  return LEGACY_COMMON_MATERIAL_SUFFIXES.has(fileSuffix(file.name));
}

function commonMaterialContentType(file: File): string | null {
  const suffix = fileSuffix(file.name) as keyof typeof COMMON_MATERIAL_CONTENT_TYPES;
  return COMMON_MATERIAL_CONTENT_TYPES[suffix] ?? null;
}

function fileSuffix(filename: string): string {
  const normalized = filename.trim().toLowerCase();
  const index = normalized.lastIndexOf(".");
  return index > 0 ? normalized.slice(index) : "";
}

/**
 * Generates a browser-held idempotency key for one case-creation intent.
 * The key is not an authentication credential and is never persisted after
 * the page session; its only purpose is to make an interrupted click safe to
 * repeat against the server's command ledger.
 */
export function createWebCaseIdempotencyKey(): string {
  if (typeof crypto === "undefined" || typeof crypto.randomUUID !== "function") {
    throw new Error("当前浏览器无法生成安全的建案请求编号。请使用受支持的现代浏览器。");
  }
  return normalizeIdempotencyKey(`web-case-${crypto.randomUUID()}`);
}

function parseCase(value: unknown): WebLawyerCase {
  const record = asRecord(value, "案件记录格式不正确");
  return {
    caseId: normalizeOpaqueId(record.case_id, "案件编号"),
    title: requiredText(record.title, "案件记录未提供名称", CASE_TITLE_MAX_LENGTH),
    version: requiredPositiveInteger(record.version, "案件记录未提供有效版本", Number.MAX_SAFE_INTEGER),
    updatedAt: optionalText(record.updated_at, 64),
    materialCount: requiredNonNegativeInteger(record.material_count, "案件记录未提供有效材料数量"),
  };
}

function parseLocalMaterialAnalysis(value: unknown): WebLocalMaterialAnalysis {
  const record = asRecord(value, "材料分析结果格式不正确");
  const summary = asRecord(record.summary, "材料分析摘要格式不正确");
  const files = parseArray(record.files, "材料分析文件", (item) => {
    const row = asRecord(item, "材料分析文件格式不正确");
    return { materialId: normalizeOpaqueId(row.material_id, "材料编号"), displayName: requiredText(row.display_name, "材料名称格式不正确", FILE_NAME_MAX_LENGTH), pageCount: requiredNonNegativeInteger(row.page_count, "材料页数格式不正确"), textLayerPages: requiredNonNegativeInteger(row.text_layer_pages, "文本页数格式不正确"), candidatePageCount: requiredNonNegativeInteger(row.candidate_page_count, "候选页数格式不正确") };
  });
  const candidates = parseArray(record.candidates, "材料分析候选", (item) => {
    const row = asRecord(item, "材料分析候选格式不正确");
    return { candidateId: normalizeOpaqueId(row.candidate_id, "分析候选编号"), evidencePageId: optionalOpaqueId(row.evidence_page_id, "证据页面编号"), kind: requiredText(row.kind, "候选类型格式不正确", 80), status: requiredText(row.status, "候选状态格式不正确", 40), sourceFile: requiredText(row.source_file, "候选来源格式不正确", FILE_NAME_MAX_LENGTH), pageNumber: requiredPositiveInteger(row.page_number, "候选页码格式不正确", 100_000), signals: boundedStringArray(row.signals, "候选信号格式不正确", 20, 80), dates: boundedStringArray(row.dates, "候选日期格式不正确", 20, 80), amounts: boundedStringArray(row.amounts, "候选金额格式不正确", 20, 80), snippet: requiredText(row.snippet, "候选摘要格式不正确", 1_000), humanAction: requiredText(row.human_action, "候选人工动作格式不正确", 500) };
  });
  const signals = asRecord(summary.signal_counts, "分析信号数量格式不正确");
  const signalCounts: Record<string, number> = {};
  for (const [key, value] of Object.entries(signals)) signalCounts[requiredText(key, "分析信号格式不正确", 80)] = requiredNonNegativeInteger(value, "分析信号数量格式不正确");
  return { analysisId: normalizeOpaqueId(record.analysis_id, "分析编号"), mode: requiredText(record.mode, "分析模式格式不正确", 100), status: requiredText(record.status, "分析状态格式不正确", 40), sourceVersion: requiredPositiveInteger(record.source_version, "分析来源版本格式不正确", Number.MAX_SAFE_INTEGER), generatedAt: requiredText(record.generated_at, "分析时间格式不正确", 80), summary: { fileCount: requiredNonNegativeInteger(summary.file_count, "分析文件数格式不正确"), pageCount: requiredNonNegativeInteger(summary.page_count, "分析页数格式不正确"), textLayerPages: requiredNonNegativeInteger(summary.text_layer_pages, "文本页数格式不正确"), scannedPages: requiredNonNegativeInteger(summary.scanned_pages, "扫描页数格式不正确"), candidateCount: requiredNonNegativeInteger(summary.candidate_count, "候选数格式不正确"), signalCounts }, files, candidates, limitations: requiredStringArray(record.limitations, "分析限制说明格式不正确", 20, 500) };
}

function parseWebAgentMaterialRun(value: unknown): WebAgentMaterialRun {
  const record = asRecord(value, "整案材料整理任务格式不正确");
  const status = requiredText(record.status, "材料整理状态格式不正确", 40);
  if (!["QUEUED", "RUNNING", "NEEDS_REVIEW", "FAILED"].includes(status)) throw protocolError("材料整理状态格式不正确");
  if (record.scope !== "ALL_CURRENT_EVIDENCE") throw protocolError("材料整理范围格式不正确");
  const progress = asRecord(record.progress, "材料整理进度格式不正确");
  const totalPages = requiredPositiveInteger(progress.total_pages, "材料整理总页数格式不正确", 1_000_000);
  const processedPages = requiredNonNegativeInteger(progress.processed_pages, "已整理页数格式不正确");
  const remainingPages = requiredNonNegativeInteger(progress.remaining_pages, "待整理页数格式不正确");
  if (processedPages + remainingPages !== totalPages) throw protocolError("材料整理页数进度不一致");
  const batchCount = requiredPositiveInteger(progress.batch_count, "材料整理批次数格式不正确", 1_000_000);
  const completedBatchCount = requiredNonNegativeInteger(progress.completed_batch_count, "已完成批次数格式不正确");
  if (completedBatchCount > batchCount) throw protocolError("材料整理批次进度不一致");
  const retryAllowed = requiredBoolean(record.retry_allowed, "材料整理重试状态格式不正确");
  const failureState = optionalText(record.failure_state, 80);
  if (failureState === "PROVIDER_RESULT_UNKNOWN" && retryAllowed) throw protocolError("结果待核验的材料整理任务不能重试");
  return {
    runId: normalizeOpaqueId(record.run_id, "材料整理任务编号"),
    matterId: normalizeOpaqueId(record.matter_id, "案件编号"),
    matterVersion: requiredPositiveInteger(record.matter_version, "材料整理案件版本格式不正确", Number.MAX_SAFE_INTEGER),
    status: status as WebAgentMaterialRun["status"],
    progress: { totalPages, processedPages, remainingPages, batchCount, completedBatchCount },
    candidateCount: requiredNonNegativeInteger(record.candidate_count, "材料整理候选数量格式不正确"),
    tasks: parseArray(record.tasks, "材料整理步骤", (item) => {
      const task = asRecord(item, "材料整理步骤格式不正确");
      return { taskKind: requiredText(task.task_kind, "材料整理步骤名称格式不正确", 80), status: requiredText(task.status, "材料整理步骤状态格式不正确", 40) };
    }),
    retryAllowed,
    failureState,
    createdAt: requiredText(record.created_at, "材料整理创建时间格式不正确", 80),
    updatedAt: requiredText(record.updated_at, "材料整理更新时间格式不正确", 80),
    externalServiceNotice: requiredText(record.external_service_notice, "外部 AI 服务说明格式不正确", 500),
    representationProfile: parseWebRepresentationProfile(record.representation_profile),
  };
}

function parseWebCaseAgentRun(value: unknown): WebCaseAgentRun {
  const record = asRecord(value, "办案 Agent 任务格式不正确");
  const status = requiredText(record.status, "办案 Agent 状态格式不正确", 40);
  const statuses: readonly WebCaseAgentRun["status"][] = ["CREATED", "PLANNING", "WAITING_APPROVAL", "EXECUTING", "WAITING_INPUT", "RECONCILIATION_REQUIRED", "VERIFYING", "READY_FOR_REVIEW", "COMPLETED", "PAUSED", "STALE", "CANCELLED", "FAILED"];
  if (!statuses.includes(status as WebCaseAgentRun["status"])) throw protocolError("办案 Agent 状态格式不正确");
  const progress = asRecord(record.progress, "办案 Agent 进度格式不正确");
  const completed = requiredNonNegativeInteger(progress.completed, "已完成工作数量格式不正确");
  const total = requiredNonNegativeInteger(progress.total, "总工作数量格式不正确");
  if (completed > total) throw protocolError("办案 Agent 进度不一致");
  const current = record.current_work === null || record.current_work === undefined ? null : asRecord(record.current_work, "当前工作格式不正确");
  const actions = asRecord(record.actions, "办案 Agent 操作状态格式不正确");
  const inputSnapshotStatus = requiredText(record.input_snapshot_status, "办案 Agent 输入快照状态格式不正确", 80);
  if (!(["CURRENT", "PLAN_CANDIDATE_REGISTERED", "PLAN_ACTIVE", "INPUTS_CHANGED"] as const).includes(inputSnapshotStatus as WebCaseAgentRun["inputSnapshotStatus"])) {
    throw protocolError("办案 Agent 输入快照状态格式不正确");
  }
  return {
    runId: normalizeOpaqueId(record.run_id, "办案任务编号"), matterId: normalizeOpaqueId(record.matter_id, "案件编号"),
    objective: requiredText(record.objective, "办案目标格式不正确", 4_000), status: status as WebCaseAgentRun["status"],
    phaseLabel: requiredText(record.phase_label, "办案阶段格式不正确", 120), progress: { completed, total },
    currentWork: current === null ? null : { title: requiredText(current.title, "当前工作标题格式不正确", 240), detail: requiredText(current.detail, "当前工作说明格式不正确", 1_000), status: requiredText(current.status, "当前工作状态格式不正确", 40) },
    openDecisionCount: requiredNonNegativeInteger(record.open_decision_count, "待决定数量格式不正确"),
    openApprovalCount: requiredNonNegativeInteger(record.open_approval_count, "待审批数量格式不正确"),
    artifactCount: requiredNonNegativeInteger(record.artifact_count, "成果数量格式不正确"),
    statusMessage: requiredText(record.status_message, "办案 Agent 状态说明格式不正确", 1_000), failureMessage: optionalText(record.failure_message, 1_000),
    failureCode: optionalText(record.failure_code, 80),
    version: requiredPositiveInteger(record.version, "办案任务版本格式不正确", Number.MAX_SAFE_INTEGER),
    snapshotMatterVersion: requiredPositiveInteger(record.snapshot_matter_version, "办案任务案件快照版本格式不正确", Number.MAX_SAFE_INTEGER),
    inputSnapshotStatus: inputSnapshotStatus as WebCaseAgentRun["inputSnapshotStatus"],
    createdAt: requiredText(record.created_at, "任务建立时间格式不正确", 80), updatedAt: requiredText(record.updated_at, "任务更新时间格式不正确", 80),
    actions: { canPause: requiredBoolean(actions.can_pause, "暂停状态格式不正确"), canResume: requiredBoolean(actions.can_resume, "继续状态格式不正确"), canCancel: requiredBoolean(actions.can_cancel, "取消状态格式不正确") },
    activePlanExecution: requiredBoolean(record.active_plan_execution, "已激活计划执行标记格式不正确"),
    requiredDocumentDeliverables: record.required_document_deliverables === undefined ? [] : parseArray(record.required_document_deliverables, "本次文书清单", (item) => {
      if (!CASE_AGENT_DELIVERABLE_CATALOGUE.has(item as WebCaseAgentRequestedDeliverable)) throw protocolError("本次文书类型不在服务目录中");
      return item as WebCaseAgentRequestedDeliverable;
    }),
  };
}

function parseWebCaseAgentDecision(value: unknown): WebCaseAgentDecision {
  const record = asRecord(value, "决定事项格式不正确");
  const status = requiredText(record.status, "决定事项状态格式不正确", 20);
  if (!["OPEN", "ANSWERED", "EXPIRED", "CANCELLED"].includes(status)) throw protocolError("决定事项状态格式不正确");
  return {
    decisionId: normalizeOpaqueId(record.decision_id, "决定事项编号"), title: requiredText(record.title, "决定事项标题格式不正确", 240),
    question: requiredText(record.question, "决定事项问题格式不正确", 1_000),
    options: parseArray(record.options, "决定选项", (item) => { const option = asRecord(item, "决定选项格式不正确"); return { optionId: normalizeOpaqueId(option.option_id, "决定选项编号"), label: requiredText(option.label, "决定选项名称格式不正确", 160), consequence: requiredText(option.consequence, "决定选项影响格式不正确", 500), requiresNote: requiredBoolean(option.requires_note, "决定选项说明要求格式不正确") }; }),
    allowNote: requiredBoolean(record.allow_note, "决定备注状态格式不正确"), blocking: requiredBoolean(record.blocking, "决定阻断状态格式不正确"), status: status as WebCaseAgentDecision["status"],
  };
}

function parseWebCaseAgentApproval(value: unknown): WebCaseAgentApproval {
  const record = asRecord(value, "审批事项格式不正确");
  const status = requiredText(record.status, "审批事项状态格式不正确", 20);
  if (!["OPEN", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"].includes(status)) throw protocolError("审批事项状态格式不正确");
  return { approvalId: normalizeOpaqueId(record.approval_id, "审批事项编号"), actionLabel: requiredText(record.action_label, "审批动作格式不正确", 240), reason: requiredText(record.reason, "审批理由格式不正确", 1_000), impact: requiredText(record.impact, "审批影响格式不正确", 1_000), status: status as WebCaseAgentApproval["status"] };
}

function parseWebCaseAgentArtifact(value: unknown): WebCaseAgentArtifact {
  const record = asRecord(value, "Agent 成果格式不正确");
  const status = requiredText(record.status, "Agent 成果状态格式不正确", 30);
  if (!["CANDIDATE", "READY_FOR_REVIEW", "APPROVED", "SUPERSEDED", "FAILED"].includes(status)) throw protocolError("Agent 成果状态格式不正确");
  return { artifactId: normalizeOpaqueId(record.artifact_id, "Agent 成果编号"), title: requiredText(record.title, "Agent 成果名称格式不正确", 240), artifactType: requiredText(record.artifact_type, "Agent 成果类型格式不正确", 80), status: status as WebCaseAgentArtifact["status"], reviewRequired: requiredBoolean(record.review_required, "Agent 成果复核状态格式不正确"), recoveryReviewOnly: requiredBoolean(record.recovery_review_only, "Agent 成果恢复状态格式不正确") };
}

function parseWebCaseAgentArtifactReview(value: unknown): WebCaseAgentArtifactReview {
  const record = asRecord(value, "Agent 分析成果格式不正确");
  return {
    artifactId: normalizeOpaqueId(record.artifact_id, "Agent 成果编号"),
    artifactType: requiredText(record.artifact_type, "Agent 成果类型格式不正确", 80),
    title: requiredText(record.title, "Agent 成果标题格式不正确", 240),
    reviewNotice: requiredText(record.review_notice, "Agent 成果复核说明格式不正确", 1_000),
    sections: parseArray(record.sections, "Agent 成果分组", (sectionValue) => {
      const section = asRecord(sectionValue, "Agent 成果分组格式不正确");
      const severity = requiredText(section.severity, "Agent 成果风险级别格式不正确", 20);
      if (!["LOW", "MEDIUM", "HIGH"].includes(severity)) throw protocolError("Agent 成果风险级别格式不正确");
      return {
        sectionId: requiredText(section.section_id, "Agent 成果分组编号格式不正确", 200),
        title: requiredText(section.title, "Agent 成果分组标题格式不正确", 240),
        severity: severity as "LOW" | "MEDIUM" | "HIGH",
        items: parseArray(section.items, "Agent 成果项目", (itemValue) => {
          const item = asRecord(itemValue, "Agent 成果项目格式不正确");
          const confidence = item.confidence === null || item.confidence === undefined
            ? null
            : typeof item.confidence === "number" && Number.isFinite(item.confidence) && item.confidence >= 0 && item.confidence <= 1
              ? item.confidence
              : (() => { throw protocolError("Agent 成果置信度格式不正确"); })();
          return {
            itemId: requiredText(item.item_id, "Agent 成果项目编号格式不正确", 200),
            title: requiredText(item.title, "Agent 成果项目标题格式不正确", 500),
            detail: requiredMultilineText(item.detail, "Agent 成果项目内容格式不正确", 8_000),
            badge: optionalText(item.badge, 240),
            confidence,
            externalUrl: optionalHttpsUrl(item.external_url, "Agent 成果公开链接格式不正确"),
            sources: parseArray(item.sources, "Agent 成果来源", (sourceValue) => {
              const source = asRecord(sourceValue, "Agent 成果来源格式不正确");
              return {
                sourceKind: requiredText(source.source_kind, "Agent 成果来源类型格式不正确", 80),
                sourceId: normalizeOpaqueId(source.source_id, "Agent 成果来源编号"),
                label: requiredText(source.label, "Agent 成果来源名称格式不正确", 120),
                evidencePageId: source.evidence_page_id === null || source.evidence_page_id === undefined
                  ? null
                  : normalizeOpaqueId(source.evidence_page_id, "证据页面编号"),
              };
            }),
          };
        }),
      };
    }),
  };
}

function parseWebCaseAgentDocumentReview(value: unknown): WebCaseAgentDocumentReview {
  const record = asRecord(value, "Agent 文书候选格式不正确");
  const outputFormat = requiredText(record.output_format, "文书格式不正确", 20);
  if (outputFormat !== "DOCX" && outputFormat !== "XLSX") throw protocolError("文书格式不正确");
  const source = (value: unknown): WebCaseAgentDocumentSource => {
    const item = asRecord(value, "文书来源格式不正确");
    return {
      sourceRef: requiredText(item.source_ref, "文书来源编号格式不正确", 200),
      sourceKind: requiredText(item.source_kind, "文书来源类型格式不正确", 80),
      label: requiredText(item.label, "文书来源名称格式不正确", 240),
    };
  };
  const sections = parseArray(record.sections, "Word 文书章节", (sectionValue) => {
    const section = asRecord(sectionValue, "Word 文书章节格式不正确");
    return {
      sectionId: requiredText(section.section_id, "Word 文书章节编号格式不正确", 200),
      heading: requiredText(section.heading, "Word 文书章节标题格式不正确", 240),
      paragraphs: parseArray(section.paragraphs, "Word 文书段落", (paragraphValue) => {
        const paragraph = asRecord(paragraphValue, "Word 文书段落格式不正确");
        const sources = parseArray(paragraph.sources, "Word 文书段落来源", source);
        if (sources.length < 1) throw protocolError("Word 文书段落必须保留来源");
        return {
          paragraphId: requiredText(paragraph.paragraph_id, "Word 文书段落编号格式不正确", 200),
          text: requiredText(paragraph.text, "Word 文书段落内容格式不正确", 20_000),
          sources,
        };
      }),
    };
  });
  const columns = parseArray(record.columns, "Excel 文书列", (columnValue) => {
    const column = asRecord(columnValue, "Excel 文书列格式不正确");
    return {
      key: requiredText(column.key, "Excel 文书列编号格式不正确", 80),
      label: requiredText(column.label, "Excel 文书列名格式不正确", 160),
      valueType: requiredText(column.value_type, "Excel 文书列类型格式不正确", 20),
    };
  });
  const rows = parseArray(record.rows, "Excel 文书行", (rowValue) => {
    const row = asRecord(rowValue, "Excel 文书行格式不正确");
    if (!Array.isArray(row.cells) || row.cells.length !== columns.length) throw protocolError("Excel 文书单元格数量不正确");
    const cells = row.cells.map((cell) => {
      if (cell === null || typeof cell === "boolean") return cell;
      if (typeof cell === "number" && Number.isFinite(cell)) return cell;
      if (typeof cell === "string" && cell.length <= 20_000) return cell;
      throw protocolError("Excel 文书单元格格式不正确");
    });
    const sources = parseArray(row.sources, "Excel 文书行来源", source);
    if (sources.length < 1) throw protocolError("Excel 文书行必须保留来源");
    return {
      rowId: requiredText(row.row_id, "Excel 文书行编号格式不正确", 200),
      cells,
      sources,
    };
  });
  const versionStatus = requiredText(record.version_status, "文书版本状态格式不正确", 24);
  if (!["CURRENT", "UPDATE_REQUIRED", "GENERATING", "FAILED", "UNKNOWN"].includes(versionStatus)) throw protocolError("文书版本状态格式不正确");
  const downloadReady = requiredBoolean(record.download_ready, "文书下载状态格式不正确");
  if (downloadReady !== (versionStatus === "CURRENT")) throw protocolError("文书版本与下载状态不一致");
  const totalItemCount = downloadReady
    ? requiredPositiveInteger(record.total_item_count, "文书项目总数格式不正确", 100_000)
    : requiredNonNegativeInteger(record.total_item_count, "文书项目总数格式不正确");
  const displayedItemCount = downloadReady
    ? requiredPositiveInteger(record.displayed_item_count, "文书预览项目数格式不正确", 500)
    : requiredNonNegativeInteger(record.displayed_item_count, "文书预览项目数格式不正确");
  const actualDisplayed = outputFormat === "DOCX"
    ? sections.reduce((sum, section) => sum + section.paragraphs.length, 0)
    : rows.length;
  if (displayedItemCount !== actualDisplayed || displayedItemCount > totalItemCount) throw protocolError("文书预览项目数量不一致");
  const previewTruncated = requiredBoolean(record.preview_truncated, "文书预览截断状态格式不正确");
  if (previewTruncated !== (displayedItemCount < totalItemCount)) throw protocolError("文书预览截断状态不一致");
  if (downloadReady) {
    if (outputFormat === "DOCX" ? (sections.length < 1 || columns.length > 0 || rows.length > 0) : (sections.length > 0 || columns.length < 1 || rows.length < 1)) {
      throw protocolError("文书预览内容与文件格式不一致");
    }
  } else if (sections.length > 0 || columns.length > 0 || rows.length > 0 || totalItemCount !== 0 || displayedItemCount !== 0 || previewTruncated) {
    throw protocolError("未完成更新的文书不应暴露旧版内容");
  }
  const requestStatusValue = record.request_status;
  const requestStatus = requestStatusValue === null || requestStatusValue === undefined
    ? null
    : requiredText(requestStatusValue, "文书更新任务状态格式不正确", 20);
  if (requestStatus !== null && !["READY", "LEASED", "PASSED", "FAILED", "UNKNOWN"].includes(requestStatus)) throw protocolError("文书更新任务状态格式不正确");
  const canRequestRevision = requiredBoolean(record.can_request_revision, "文书更新权限格式不正确");
  if (canRequestRevision && !["UPDATE_REQUIRED", "FAILED", "UNKNOWN"].includes(versionStatus)) throw protocolError("文书更新权限与版本状态不一致");
  return {
    artifactId: normalizeOpaqueId(record.artifact_id, "文书成果编号"),
    title: requiredText(record.title, "文书标题格式不正确", 240),
    deliverableKind: requiredText(record.deliverable_kind, "文书成果类型格式不正确", 120),
    deliverableLabel: requiredText(record.deliverable_label, "文书成果名称格式不正确", 240),
    outputFormat,
    reviewNotice: requiredText(record.review_notice, "文书复核说明格式不正确", 1_000),
    versionStatus: versionStatus as WebCaseAgentDocumentReview["versionStatus"],
    reviewArtifactId: record.review_artifact_id === null || record.review_artifact_id === undefined
      ? null : normalizeOpaqueId(record.review_artifact_id, "文书终审成果编号"),
    reviewVersion: record.review_version === null || record.review_version === undefined
      ? null : /^[a-f0-9]{64}$/.test(String(record.review_version)) && typeof record.review_version === "string"
        ? record.review_version : (() => { throw protocolError("文书审阅版本标识不正确"); })(),
    revisionNumber: requiredPositiveInteger(record.revision_number, "文书版本号格式不正确", 1_000),
    templateVersion: requiredText(record.template_version, "文书模板版本格式不正确", 64),
    installedTemplateVersion: requiredText(record.installed_template_version, "当前文书模板版本格式不正确", 64),
    canRequestRevision,
    requestStatus: requestStatus as WebCaseAgentDocumentReview["requestStatus"],
    requestId: record.request_id === null || record.request_id === undefined
      ? null
      : normalizeOpaqueId(record.request_id, "文书更新任务编号"),
    downloadReady,
    reviewPdfPageCount: downloadReady
      ? requiredPositiveInteger(record.review_pdf_page_count, "文书预览页数格式不正确", 10_000)
      : requiredNonNegativeInteger(record.review_pdf_page_count, "文书预览页数格式不正确"),
    totalItemCount,
    displayedItemCount,
    previewTruncated,
    sections,
    columns,
    rows,
  };
}

function parseWebRepresentationProfile(value: unknown): WebRepresentationProfile {
  const record = asRecord(value, "代理身份与程序阶段格式不正确");
  const status = requiredText(record.status, "代理身份状态格式不正确", 20);
  if (status !== "UNCONFIRMED" && status !== "CONFIRMED") throw protocolError("代理身份状态格式不正确");
  if (status === "UNCONFIRMED") {
    if ([record.active_proceeding_role, record.proceeding_stage, record.case_type, record.version].some((item) => item !== null && item !== undefined)) throw protocolError("未确认的代理身份不应包含正式值");
    return { status, activeProceedingRole: null, proceedingStage: null, caseType: null, version: null };
  }
  const role = requiredText(record.active_proceeding_role, "当前程序角色格式不正确", 40);
  if (!["PLAINTIFF", "DEFENDANT", "APPELLANT", "APPELLEE", "THIRD_PARTY", "OTHER"].includes(role)) throw protocolError("当前程序角色格式不正确");
  return {
    status,
    activeProceedingRole: role as NonNullable<WebRepresentationProfile["activeProceedingRole"]>,
    proceedingStage: requiredText(record.proceeding_stage, "程序阶段格式不正确", 80),
    caseType: requiredText(record.case_type, "案件类型格式不正确", 160),
    version: requiredPositiveInteger(record.version, "代理身份版本格式不正确", Number.MAX_SAFE_INTEGER),
  };
}

function parseWebAgentCandidateBatch(value: unknown): WebAgentCandidateBatch {
  const record = asRecord(value, "材料整理候选批次格式不正确");
  const items = parseArray(record.items, "材料整理候选", (item) => {
    const candidate = asRecord(item, "材料整理候选格式不正确");
    const kind = requiredText(candidate.kind, "材料整理候选类型格式不正确", 80);
    if (!["RELEVANT_PAGE", "UNRELATED_PAGE", "OCR_REQUIRED", "DUPLICATE_CANDIDATE", "UNCERTAIN"].includes(kind)) throw protocolError("材料整理候选类型格式不正确");
    const priority = requiredText(candidate.review_priority, "材料整理候选优先级格式不正确", 20);
    if (!["LOW", "MEDIUM", "HIGH"].includes(priority)) throw protocolError("材料整理候选优先级格式不正确");
    if (candidate.status !== "NEEDS_REVIEW") throw protocolError("材料整理候选状态格式不正确");
    if (typeof candidate.confidence !== "number" || !Number.isFinite(candidate.confidence) || candidate.confidence < 0 || candidate.confidence > 1) throw protocolError("材料整理候选置信度格式不正确");
    return {
      candidateId: normalizeOpaqueId(candidate.candidate_id, "材料整理候选编号"),
      evidencePageId: normalizeOpaqueId(candidate.evidence_page_id, "证据页面编号"),
      sourceLabel: requiredText(candidate.source_label, "材料来源格式不正确", FILE_NAME_MAX_LENGTH),
      pageNumber: requiredPositiveInteger(candidate.page_number, "材料页码格式不正确", 100_000),
      kind: kind as WebAgentMaterialCandidate["kind"],
      confidence: candidate.confidence,
      reviewPriority: priority as WebAgentMaterialCandidate["reviewPriority"],
      reasonCodes: boundedStringArray(candidate.reason_codes, "材料整理候选原因格式不正确", 20, 80),
      supportingExcerpt: requiredText(candidate.supporting_excerpt, "材料整理候选摘录格式不正确", 2_000),
      duplicateOfPageId: optionalOpaqueId(candidate.duplicate_of_page_id, "疑似重复页面编号"),
      status: "NEEDS_REVIEW" as const,
    };
  });
  const totalCount = requiredNonNegativeInteger(record.total_count, "材料整理候选总数格式不正确");
  if (totalCount < items.length) throw protocolError("材料整理候选总数格式不正确");
  const nextCursor = optionalText(record.next_cursor, 1_024);
  const hasMore = requiredBoolean(record.has_more, "材料整理候选分页状态格式不正确");
  if (hasMore !== (nextCursor !== null)) throw protocolError("材料整理候选分页状态格式不正确");
  return { runId: normalizeOpaqueId(record.run_id, "材料整理任务编号"), totalCount, items, nextCursor, hasMore };
}

function parseWebDynamicCasePlan(value: unknown): WebDynamicCasePlan {
  const record = asRecord(value, "动态办案计划格式不正确");
  const status = requiredText(record.status, "动态办案计划状态格式不正确", 20);
  if (!(["CANDIDATE", "ACTIVE", "STALE", "SUPERSEDED"] as const).includes(status as WebDynamicCasePlan["status"])) {
    throw protocolError("动态办案计划状态格式不正确");
  }
  const generatedMatterVersion = requiredPositiveInteger(record.generated_matter_version, "计划生成案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const currentMatterVersion = requiredPositiveInteger(record.current_matter_version, "当前案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const inputsCurrent = requiredBoolean(record.inputs_current, "计划输入状态格式不正确");
  const staleReasons = boundedStringArray(record.stale_reasons, "计划失效原因格式不正确", 20, 240);
  if ((status === "STALE") !== (!inputsCurrent && staleReasons.length > 0)) throw protocolError("动态办案计划失效状态不一致");
  if (generatedMatterVersion > currentMatterVersion) throw protocolError("动态办案计划案件版本不一致");
  const items = parseArray(record.items, "动态办案建议", parseWebDynamicCasePlanItem);
  if (items.length > 200) throw protocolError("动态办案建议数量超出限制");
  const sequences = items.map((item) => item.sequence);
  if (new Set(items.map((item) => item.itemId)).size !== items.length || new Set(sequences).size !== sequences.length || sequences.some((item, index) => index > 0 && item <= sequences[index - 1])) {
    throw protocolError("动态办案建议顺序不一致");
  }
  const canActivate = requiredBoolean(record.can_activate, "计划激活状态格式不正确");
  const activationBlockers = boundedStringArray(record.activation_blockers, "计划激活阻断原因格式不正确", 20, 240);
  const reviewedItemCount = requiredNonNegativeInteger(record.reviewed_item_count, "计划已复核事项数量格式不正确");
  if (reviewedItemCount > items.length) throw protocolError("计划已复核事项数量格式不正确");
  if (canActivate !== (status === "CANDIDATE" && inputsCurrent && activationBlockers.length === 0)) {
    throw protocolError("计划激活状态与阻断原因不一致");
  }
  return {
    planId: normalizeOpaqueId(record.plan_id, "办案计划编号"),
    matterId: normalizeOpaqueId(record.matter_id, "案件编号"),
    generatedMatterVersion,
    currentMatterVersion,
    status: status as WebDynamicCasePlan["status"],
    inputsCurrent,
    staleReasons,
    generatedAt: requiredText(record.generated_at, "计划生成时间格式不正确", 80),
    canActivate,
    activationBlockers,
    reviewedItemCount,
    items,
  };
}

function parseWebDynamicCasePlanItem(value: unknown): WebDynamicCasePlanItem {
  const record = asRecord(value, "动态办案建议格式不正确");
  const category = requiredText(record.category, "建议分类格式不正确", 40);
  const categories = ["MATERIAL_REQUEST", "RESEARCH_TASK", "PROCEDURAL_TASK", "CALCULATION", "DOCUMENT_CANDIDATE", "REVIEW", "DEADLINE_RISK"] as const;
  if (!categories.includes(category as WebDynamicCasePlanItem["category"])) throw protocolError("建议分类格式不正确");
  const status = requiredText(record.status, "建议状态格式不正确", 20);
  if (!(["CANDIDATE", "APPROVED", "CHANGE_REQUESTED", "REJECTED", "SUPERSEDED"] as const).includes(status as WebDynamicCasePlanItem["status"])) throw protocolError("建议状态格式不正确");
  const readiness = requiredText(record.readiness, "建议就绪状态格式不正确", 30);
  if (!(["ACTIONABLE", "NEEDS_RESEARCH", "NEEDS_INFORMATION"] as const).includes(readiness as WebDynamicCasePlanItem["readiness"])) throw protocolError("建议就绪状态格式不正确");
  const reviewGate = requiredText(record.review_gate, "建议复核门槛格式不正确", 40);
  if (!(["LEAD_LAWYER_CONFIRMATION", "EVIDENCE_REVIEW", "LEGAL_AUTHORITY_REVIEW", "PROCEDURE_REVIEW", "CALCULATION_REVIEW"] as const).includes(reviewGate as WebDynamicCasePlanItem["reviewGate"])) throw protocolError("建议复核门槛格式不正确");
  if (typeof record.confidence !== "number" || !Number.isFinite(record.confidence) || record.confidence < 0 || record.confidence > 1) throw protocolError("建议置信度格式不正确");
  const sourceCounts = asRecord(record.source_counts, "建议来源数量格式不正确");
  const sources = parseArray(record.sources, "建议来源", (item): WebDynamicCasePlanSource => {
    const source = asRecord(item, "建议来源格式不正确");
    return { sourceKind: requiredText(source.source_kind, "来源类型格式不正确", 80), sourceId: normalizeOpaqueId(source.source_id, "来源编号"), label: requiredText(source.label, "来源名称格式不正确", 500), locator: optionalText(source.locator, 500) };
  });
  const deliveryTarget = optionalText(record.delivery_target, 80);
  if (deliveryTarget !== null && !(["NOT_APPLICABLE", "INTERNAL_WORK_PRODUCT", "CLIENT_DELIVERABLE", "COURT_SUBMISSION"] as const).includes(deliveryTarget as NonNullable<WebDynamicCasePlanItem["deliveryTarget"]>)) throw protocolError("交付目标格式不正确");
  return {
    itemId: normalizeOpaqueId(record.item_id, "计划建议编号"),
    sequence: requiredPositiveInteger(record.sequence, "建议顺序格式不正确", 10_000),
    category: category as WebDynamicCasePlanItem["category"],
    status: status as WebDynamicCasePlanItem["status"],
    readiness: readiness as WebDynamicCasePlanItem["readiness"],
    title: requiredText(record.title, "建议标题格式不正确", 240),
    purpose: requiredText(record.purpose, "建议目的格式不正确", 2_000),
    rationale: requiredText(record.rationale, "建议理由格式不正确", 4_000),
    riskIfOmitted: requiredText(record.risk_if_omitted, "遗漏风险格式不正确", 2_000),
    prerequisiteCount: requiredNonNegativeInteger(record.prerequisite_count, "前置条件数量格式不正确"),
    confidence: record.confidence,
    reviewGate: reviewGate as WebDynamicCasePlanItem["reviewGate"],
    sources,
    sourceCounts: {
      fact: requiredNonNegativeInteger(sourceCounts.fact, "事实来源数量格式不正确"),
      evidence: requiredNonNegativeInteger(sourceCounts.evidence, "证据来源数量格式不正确"),
      procedure: requiredNonNegativeInteger(sourceCounts.procedure, "程序来源数量格式不正确"),
      officialAuthority: requiredNonNegativeInteger(sourceCounts.official_authority, "法源数量格式不正确"),
    },
    deliveryTarget: deliveryTarget as WebDynamicCasePlanItem["deliveryTarget"],
    deliverableKind: optionalText(record.deliverable_kind, 120),
    requiredForDelivery: requiredBoolean(record.required_for_delivery, "交付必要性格式不正确"),
  };
}

function parseWebAgentLedgerExtractionBatch(value: unknown): WebAgentLedgerExtractionBatch {
  const record = asRecord(value, "材料提取批次格式不正确");
  const status = requiredText(record.status, "材料提取批次状态格式不正确", 40);
  if (!(["REVIEW_READY", "EXCEPTIONS_ONLY", "EXCEPTIONS_PARTIALLY_RESOLVED", "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN", "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL", "CONFIRMED", "RESOLVED", "STALE"] as const).includes(status as WebAgentLedgerExtractionBatch["status"])) {
    throw protocolError("材料提取批次状态格式不正确");
  }
  const lowRiskCandidates = parseArray(
    record.low_risk_candidates,
    "低风险材料提取候选",
    parseWebAgentLedgerExtractionCandidate,
  );
  const exceptionCandidates = parseArray(
    record.exception_candidates,
    "例外材料提取候选",
    parseWebAgentLedgerExtractionCandidate,
  );
  if (
    lowRiskCandidates.length > 500
    || exceptionCandidates.length > 500
    || lowRiskCandidates.some((item) => item.reviewStatus !== "LOW_RISK")
    || exceptionCandidates.some((item) => item.reviewStatus !== "EXCEPTION")
  ) {
    throw protocolError("材料提取候选分组不一致");
  }
  const candidateCount = requiredNonNegativeInteger(record.candidate_count, "材料提取候选总数格式不正确");
  const lowRiskCount = requiredNonNegativeInteger(record.low_risk_count, "低风险候选数量格式不正确");
  const exceptionCount = requiredNonNegativeInteger(record.exception_count, "例外候选数量格式不正确");
  if (
    candidateCount > 500
    || candidateCount !== lowRiskCount + exceptionCount
    || lowRiskCount !== lowRiskCandidates.length
    || exceptionCount !== exceptionCandidates.length
  ) {
    throw protocolError("材料提取候选数量不一致");
  }
  const sequences = [...lowRiskCandidates, ...exceptionCandidates].map((item) => item.sequence);
  if (new Set(sequences).size !== sequences.length) {
    throw protocolError("材料提取候选顺序重复");
  }
  const canConfirmLowRisk = requiredBoolean(record.can_confirm_low_risk, "低风险整组确认状态格式不正确");
  if (canConfirmLowRisk && (status !== "REVIEW_READY" || lowRiskCount === 0)) {
    throw protocolError("低风险整组确认状态不一致");
  }
  const confirmedAt = optionalText(record.confirmed_at, 80);
  const lowRiskConfirmed = status === "CONFIRMED"
    || status === "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN"
    || status === "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL"
    || (status === "RESOLVED" && lowRiskCount > 0);
  if (lowRiskConfirmed !== (confirmedAt !== null)) {
    throw protocolError("材料提取批次确认状态不一致");
  }
  const exceptionReviewStatus = requiredText(record.exception_review_status, "异常组复核状态格式不正确", 30);
  if (!(["NONE", "OPEN", "PARTIALLY_RESOLVED", "RESOLVED"] as const).includes(exceptionReviewStatus as WebAgentLedgerExtractionBatch["exceptionReviewStatus"])) {
    throw protocolError("异常组复核状态格式不正确");
  }
  const exceptionGroupCount = requiredNonNegativeInteger(record.exception_group_count, "异常组数量格式不正确");
  const decidedExceptionGroupCount = requiredNonNegativeInteger(record.decided_exception_group_count, "已处置异常组数量格式不正确");
  const exceptionGroups = parseArray(record.exception_groups, "异常组", parseWebAgentLedgerExceptionGroup);
  const expectedExceptionStatus = exceptionGroupCount === 0
    ? "NONE"
    : decidedExceptionGroupCount === 0
      ? "OPEN"
      : decidedExceptionGroupCount < exceptionGroupCount
        ? "PARTIALLY_RESOLVED"
        : "RESOLVED";
  if (
    (status === "CONFIRMED" && exceptionCount !== 0)
    || (status === "RESOLVED" && exceptionReviewStatus !== "RESOLVED")
    || (["LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN", "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL"] as const).includes(status as "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN" | "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL") && (lowRiskCount === 0 || exceptionCount === 0)
    || exceptionGroupCount !== exceptionGroups.length
    || exceptionGroupCount > 500
    || decidedExceptionGroupCount > exceptionGroupCount
    || decidedExceptionGroupCount !== exceptionGroups.filter((item) => item.status === "DECIDED").length
    || exceptionGroups.reduce((sum, item) => sum + item.candidateCount, 0) !== exceptionCount
    || exceptionReviewStatus !== expectedExceptionStatus
    || (status === "EXCEPTIONS_ONLY" && exceptionReviewStatus !== "OPEN")
    || (status === "EXCEPTIONS_PARTIALLY_RESOLVED" && exceptionReviewStatus !== "PARTIALLY_RESOLVED")
    || (status === "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN" && exceptionReviewStatus !== "OPEN")
    || (status === "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL" && exceptionReviewStatus !== "PARTIALLY_RESOLVED")
  ) {
    throw protocolError("材料提取批次异常处置状态不一致");
  }
  return {
    batchId: normalizeOpaqueId(record.batch_id, "材料提取批次编号"),
    matterId: normalizeOpaqueId(record.matter_id, "案件编号"),
    status: status as WebAgentLedgerExtractionBatch["status"],
    currentMatterVersion: requiredPositiveInteger(record.current_matter_version, "当前案件版本格式不正确", Number.MAX_SAFE_INTEGER),
    sourceMatterVersion: requiredPositiveInteger(record.source_matter_version, "材料提取来源版本格式不正确", Number.MAX_SAFE_INTEGER),
    candidateCount,
    lowRiskCount,
    exceptionCount,
    stagedAt: requiredText(record.staged_at, "材料提取时间格式不正确", 80),
    confirmedAt,
    canConfirmLowRisk,
    exceptionReviewStatus: exceptionReviewStatus as WebAgentLedgerExtractionBatch["exceptionReviewStatus"],
    exceptionGroupCount,
    decidedExceptionGroupCount,
    exceptionGroups,
    lowRiskCandidates,
    exceptionCandidates,
  };
}

function parseWebAgentLedgerExtractionCandidate(value: unknown): WebAgentLedgerExtractionCandidate {
  const record = asRecord(value, "材料提取候选格式不正确");
  const candidateKind = requiredText(record.candidate_kind, "材料提取候选类型格式不正确", 20);
  if (candidateKind !== "FACT" && candidateKind !== "TRANSACTION") {
    throw protocolError("材料提取候选类型格式不正确");
  }
  const reviewStatus = requiredText(record.review_status, "材料提取候选复核状态格式不正确", 20);
  if (reviewStatus !== "LOW_RISK" && reviewStatus !== "EXCEPTION") {
    throw protocolError("材料提取候选复核状态格式不正确");
  }
  if (
    typeof record.confidence !== "number"
    || !Number.isFinite(record.confidence)
    || record.confidence < 0
    || record.confidence > 1
  ) {
    throw protocolError("材料提取候选置信度格式不正确");
  }
  const reviewReasons = boundedStringArray(record.review_reasons, "材料提取复核原因格式不正确", 15, 240);
  if (
    (reviewStatus === "LOW_RISK" && reviewReasons.length > 0)
    || (reviewStatus === "EXCEPTION" && reviewReasons.length === 0)
  ) {
    throw protocolError("材料提取候选复核原因不一致");
  }
  const excerpts = parseArray(record.excerpts, "材料提取来源摘录", (item): WebAgentLedgerExtractionExcerpt => {
    const excerpt = asRecord(item, "材料提取来源摘录格式不正确");
    return {
      evidencePageId: normalizeOpaqueId(excerpt.evidence_page_id, "证据页面编号"),
      pageNumber: requiredPositiveInteger(excerpt.page_number, "证据页码格式不正确", 100_000),
      text: requiredEvidenceExcerptText(
        excerpt.text,
        "材料提取来源摘录格式不正确",
        2_000,
      ),
    };
  });
  if (
    excerpts.length < 1
    || excerpts.length > 20
    || new Set(excerpts.map((item) => item.evidencePageId)).size !== excerpts.length
  ) {
    throw protocolError("材料提取来源摘录不完整或重复");
  }
  return {
    sequence: requiredPositiveInteger(record.sequence, "材料提取候选顺序格式不正确", 500),
    candidateKind,
    summary: requiredText(record.summary, "材料提取候选摘要格式不正确", 2_000),
    confidence: record.confidence,
    reviewStatus,
    reviewReasons,
    excerpts,
  };
}

function parseWebAgentLedgerExceptionGroup(value: unknown): WebAgentLedgerExceptionGroup {
  const record = asRecord(value, "材料提取异常组格式不正确");
  const candidateKind = requiredText(record.candidate_kind, "异常组类型格式不正确", 20);
  if (candidateKind !== "FACT" && candidateKind !== "TRANSACTION") {
    throw protocolError("异常组类型格式不正确");
  }
  const status = requiredText(record.status, "异常组状态格式不正确", 20);
  if (status !== "OPEN" && status !== "DECIDED") {
    throw protocolError("异常组状态格式不正确");
  }
  const decision = optionalText(record.decision, 40);
  const decisionLabel = optionalText(record.decision_label, 120);
  const decisionReason = optionalText(record.decision_reason, 50);
  const decisionReasonLabel = optionalText(record.decision_reason_label, 160);
  if (decision !== null && !isAgentLedgerExceptionDecision(decision)) {
    throw protocolError("异常组既有处置格式不正确");
  }
  if (decisionReason !== null && !isAgentLedgerExceptionReason(decisionReason)) {
    throw protocolError("异常组既有处置原因格式不正确");
  }
  if (
    (status === "OPEN" && [decision, decisionLabel, decisionReason, decisionReasonLabel].some((item) => item !== null))
    || (status === "DECIDED" && [decision, decisionLabel, decisionReason, decisionReasonLabel].some((item) => item === null))
  ) {
    throw protocolError("异常组既有处置状态不完整");
  }
  const allowedActions = parseArray(record.allowed_actions, "异常组可选动作", parseWebAgentLedgerExceptionAction);
  const reviewReasons = boundedStringArray(record.review_reasons, "异常组复核原因格式不正确", 15, 240);
  const candidateCount = requiredPositiveInteger(record.candidate_count, "异常组候选数量格式不正确", 500);
  const canDecide = requiredBoolean(record.can_decide, "异常组处置权限格式不正确");
  if (
    !reviewReasons.length
    || !allowedActions.length
    || allowedActions.length > 4
    || new Set(allowedActions.map((item) => item.code)).size !== allowedActions.length
    || (status === "DECIDED" && canDecide)
    || (decision !== null && !allowedActions.some((item) => item.code === decision))
  ) {
    throw protocolError("异常组处置策略不一致");
  }
  return {
    groupId: normalizeOpaqueId(record.group_id, "异常组编号"),
    candidateKind,
    candidateCount,
    summary: requiredText(record.summary, "异常组摘要格式不正确", 1_000),
    reviewReasons,
    sourceGuidance: requiredText(record.source_guidance, "异常组来源提示格式不正确", 240),
    riskLabel: requiredText(record.risk_label, "异常组风险格式不正确", 160),
    status,
    decision,
    decisionLabel,
    decisionReason,
    decisionReasonLabel,
    canDecide,
    allowedActions,
  };
}

function parseWebAgentLedgerExceptionAction(value: unknown): WebAgentLedgerExceptionAction {
  const record = asRecord(value, "异常组处置动作格式不正确");
  const code = requiredText(record.code, "异常组处置动作格式不正确", 40);
  if (!isAgentLedgerExceptionDecision(code)) {
    throw protocolError("异常组处置动作格式不正确");
  }
  const reasons = parseArray(record.reasons, "异常组处置原因", (item): WebAgentLedgerExceptionReasonOption => {
    const reason = asRecord(item, "异常组处置原因格式不正确");
    const reasonCode = requiredText(reason.code, "异常组处置原因格式不正确", 50);
    if (!isAgentLedgerExceptionReason(reasonCode)) {
      throw protocolError("异常组处置原因格式不正确");
    }
    return {
      code: reasonCode,
      label: requiredText(reason.label, "异常组处置原因说明格式不正确", 160),
    };
  });
  if (!reasons.length || reasons.length > 3 || new Set(reasons.map((item) => item.code)).size !== reasons.length) {
    throw protocolError("异常组处置原因不完整或重复");
  }
  return {
    code,
    label: requiredText(record.label, "异常组处置动作说明格式不正确", 120),
    consequence: requiredText(record.consequence, "异常组处置后果格式不正确", 300),
    requiresNote: requiredBoolean(record.requires_note, "异常组处置说明要求格式不正确"),
    reasons,
  };
}

function parseWebAgentLedgerExceptionMemberPage(value: unknown): WebAgentLedgerExceptionMemberPage {
  const record = asRecord(value, "异常组成员分页格式不正确");
  const totalCount = requiredPositiveInteger(record.total_count, "异常组成员总数格式不正确", 500);
  const offset = requiredNonNegativeInteger(record.offset, "异常组成员分页位置格式不正确");
  const members = parseArray(record.members, "异常组成员", (item): WebAgentLedgerExceptionMember => {
    const member = asRecord(item, "异常组成员格式不正确");
    const candidateKind = requiredText(member.candidate_kind, "异常组成员类型格式不正确", 20);
    if (candidateKind !== "FACT" && candidateKind !== "TRANSACTION") {
      throw protocolError("异常组成员类型格式不正确");
    }
    if (typeof member.confidence !== "number" || !Number.isFinite(member.confidence) || member.confidence < 0 || member.confidence > 1) {
      throw protocolError("异常组成员来源匹配度格式不正确");
    }
    const excerpts = parseArray(member.excerpts, "异常组成员来源", (raw): WebAgentLedgerExtractionExcerpt => {
      const excerpt = asRecord(raw, "异常组成员来源格式不正确");
      return {
        evidencePageId: normalizeOpaqueId(excerpt.evidence_page_id, "证据页面编号"),
        pageNumber: requiredPositiveInteger(excerpt.page_number, "证据页码格式不正确", 100_000),
        text: requiredEvidenceExcerptText(
          excerpt.text,
          "异常组成员来源摘录格式不正确",
          2_000,
        ),
      };
    });
    const reviewReasons = boundedStringArray(member.review_reasons, "异常组成员复核原因格式不正确", 15, 240);
    if (!reviewReasons.length || !excerpts.length || excerpts.length > 20) {
      throw protocolError("异常组成员依据不完整");
    }
    return {
      sequence: requiredPositiveInteger(member.sequence, "异常组成员顺序格式不正确", 500),
      extractionCandidateId: member.extraction_candidate_id == null ? null : normalizeOpaqueId(member.extraction_candidate_id, "原候选编号"),
      candidateKind,
      summary: requiredText(member.summary, "异常组成员摘要格式不正确", 1_000),
      confidence: member.confidence,
      reviewReasons,
      excerpts,
    };
  });
  const nextOffset = optionalNonNegativeInteger(record.next_offset, 500);
  if (
    offset >= totalCount
    || !members.length
    || members.length > 50
    || members.some((item, index) => item.sequence !== offset + index + 1)
    || nextOffset !== (offset + members.length < totalCount ? offset + members.length : null)
  ) {
    throw protocolError("异常组成员分页不连续");
  }
  return {
    groupId: normalizeOpaqueId(record.group_id, "异常组编号"),
    totalCount,
    offset,
    nextOffset,
    members,
  };
}

function parseWebAgentLedgerExceptionDecisionReceipt(value: unknown): WebAgentLedgerExceptionDecisionReceipt {
  const record = asRecord(value, "异常组处置回执格式不正确");
  const decision = requiredText(record.decision, "异常组处置动作格式不正确", 40);
  if (!isAgentLedgerExceptionDecision(decision)) {
    throw protocolError("异常组处置动作格式不正确");
  }
  const exceptionReviewStatus = requiredText(record.exception_review_status, "异常组复核状态格式不正确", 30);
  if (!(exceptionReviewStatus === "OPEN" || exceptionReviewStatus === "PARTIALLY_RESOLVED" || exceptionReviewStatus === "RESOLVED")) {
    throw protocolError("异常组复核状态格式不正确");
  }
  const exceptionGroupCount = requiredPositiveInteger(record.exception_group_count, "异常组数量格式不正确", 500);
  const decidedExceptionGroupCount = requiredPositiveInteger(record.decided_exception_group_count, "已处置异常组数量格式不正确", 500);
  const batchResolved = requiredBoolean(record.batch_resolved, "提取批次解决状态格式不正确");
  const matterVersion = requiredPositiveInteger(record.matter_version, "当前案件版本格式不正确", Number.MAX_SAFE_INTEGER);
  const committedMatterVersion = requiredPositiveInteger(record.committed_matter_version, "异常组决定提交版本格式不正确", Number.MAX_SAFE_INTEGER);
  if (
    decidedExceptionGroupCount > exceptionGroupCount
    || (batchResolved && exceptionReviewStatus !== "RESOLVED")
    || matterVersion < committedMatterVersion
  ) {
    throw protocolError("异常组处置回执数量不一致");
  }
  return {
    batchId: normalizeOpaqueId(record.batch_id, "材料提取批次编号"),
    groupId: normalizeOpaqueId(record.group_id, "异常组编号"),
    matterVersion,
    committedMatterVersion,
    decision,
    exceptionReviewStatus,
    decidedExceptionGroupCount,
    exceptionGroupCount,
    batchResolved,
  };
}

function parseWebAgentLedgerExceptionFollowupPage(value: unknown): WebAgentLedgerExceptionFollowupPage {
  const record = asRecord(value, "异常后续工作分页格式不正确");
  const totalCount = requiredSafeNonNegativeInteger(record.total_count, "异常后续工作总数格式不正确");
  const offset = requiredSafeNonNegativeInteger(record.offset, "异常后续工作分页位置格式不正确");
  const nextOffset = optionalNonNegativeInteger(record.next_offset, Number.MAX_SAFE_INTEGER);
  const controlHealthValue = record.control_health;
  const controlHealth = controlHealthValue === null
    ? null
    : requiredText(controlHealthValue, "异常后续工作控制状态格式不正确", 32);
  if (controlHealth !== null && controlHealth !== "HEALTHY" && controlHealth !== "RECOVERY_REQUIRED") {
    throw protocolError("异常后续工作控制状态格式不正确");
  }
  const canRecover = requiredBoolean(record.can_recover, "异常后续工作恢复权限格式不正确");
  const followups = parseArray(record.followups, "异常后续工作", parseWebAgentLedgerExceptionFollowup);
  const expectedNext = offset + followups.length < totalCount ? offset + followups.length : null;
  if (
    offset > totalCount
    || followups.length > 50
    || (totalCount === 0 && (offset !== 0 || followups.length !== 0 || controlHealth !== null || canRecover))
    || (totalCount > 0 && (offset >= totalCount || followups.length === 0 || controlHealth === null))
    || nextOffset !== expectedNext
    || new Set(followups.map((item) => item.followupId)).size !== followups.length
    || (canRecover && controlHealth !== "RECOVERY_REQUIRED")
  ) {
    throw protocolError("异常后续工作分页与控制状态不一致");
  }
  return { totalCount, offset, nextOffset, controlHealth, canRecover, followups };
}

function parseWebAgentLedgerExceptionFollowup(value: unknown): WebAgentLedgerExceptionFollowup {
  const record = asRecord(value, "异常后续工作格式不正确");
  const kind = requiredText(record.kind, "异常后续工作类型格式不正确", 32);
  if (kind !== "REEXTRACTION" && kind !== "MORE_EVIDENCE" && kind !== "DEFERRED_REVIEW") {
    throw protocolError("异常后续工作类型格式不正确");
  }
  if (record.state !== "ACTIVE") throw protocolError("异常后续工作状态格式不正确");
  const automationValue = record.automation_status;
  const automationStatus = automationValue === null
    ? null
    : requiredText(automationValue, "重新提取状态格式不正确", 40);
  if (automationStatus !== null && !isAgentLedgerFollowupAutomationStatus(automationStatus)) {
    throw protocolError("重新提取状态格式不正确");
  }
  if ((kind === "REEXTRACTION") !== (automationStatus !== null)) {
    throw protocolError("重新提取状态与后续工作类型不一致");
  }
  const acceptanceRequirements = boundedStringArray(
    record.acceptance_requirements,
    "补证验收要求格式不正确",
    10,
    300,
  );
  if ((kind === "MORE_EVIDENCE") !== (acceptanceRequirements.length > 0)) {
    throw protocolError("补证验收要求与后续工作类型不一致");
  }
  const allowedActions = parseArray(record.allowed_actions, "异常后续工作可选动作", parseWebAgentLedgerFollowupAction);
  const expectedActions: Readonly<Record<typeof kind, readonly WebAgentLedgerFollowupActionCode[]>> = {
    REEXTRACTION: ["WITHDRAW", "SUPERSEDE"],
    MORE_EVIDENCE: ["CONFIRM_MORE_EVIDENCE", "WITHDRAW", "SUPERSEDE"],
    DEFERRED_REVIEW: ["RESUME", "WITHDRAW", "SUPERSEDE"],
  };
  if (
    allowedActions.length !== expectedActions[kind].length
    || new Set(allowedActions.map((item) => item.code)).size !== allowedActions.length
    || expectedActions[kind].some((code) => !allowedActions.some((item) => item.code === code))
  ) {
    throw protocolError("异常后续工作动作策略不完整");
  }
  const evidencePageCount = requiredPositiveInteger(
    record.evidence_page_count,
    "异常后续工作证据页数量格式不正确",
    Number.MAX_SAFE_INTEGER,
  );
  const createdAt = requiredText(record.created_at, "异常后续工作创建时间格式不正确", 80);
  if (!Number.isFinite(Date.parse(createdAt))) throw protocolError("异常后续工作创建时间格式不正确");
  return {
    followupId: normalizeOpaqueId(record.followup_id, "异常后续工作编号"),
    kind,
    state: "ACTIVE",
    headSequence: requiredPositiveInteger(record.head_sequence, "异常后续工作序号格式不正确", Number.MAX_SAFE_INTEGER),
    originBatchId: normalizeOpaqueId(record.origin_batch_id, "异常后续工作来源批次编号"),
    originGroupId: normalizeOpaqueId(record.origin_group_id, "异常后续工作来源组编号"),
    currentMatterVersion: requiredPositiveInteger(record.current_matter_version, "当前案件版本格式不正确", Number.MAX_SAFE_INTEGER),
    createdMatterVersion: requiredPositiveInteger(record.created_matter_version, "异常后续工作创建版本格式不正确", Number.MAX_SAFE_INTEGER),
    createdAt,
    reason: requiredText(record.reason, "异常后续工作原因格式不正确", 240),
    reasonNote: optionalText(record.reason_note, 500),
    candidateCount: requiredPositiveInteger(record.candidate_count, "异常后续工作候选数量格式不正确", 500),
    reviewReasons: requiredStringArray(record.review_reasons, "异常后续工作复核原因格式不正确", 15, 240),
    evidencePageCount,
    acceptanceRequirements,
    automationStatus,
    canAct: requiredBoolean(record.can_act, "异常后续工作操作权限格式不正确"),
    allowedActions,
  };
}

function parseWebAgentLedgerFollowupAction(value: unknown): WebAgentLedgerFollowupAction {
  const record = asRecord(value, "异常后续工作动作格式不正确");
  const code = requiredText(record.code, "异常后续工作动作编码格式不正确", 40);
  if (!isAgentLedgerFollowupActionCode(code) || record.requires_reason !== true) {
    throw protocolError("异常后续工作动作策略格式不正确");
  }
  return {
    code,
    label: requiredText(record.label, "异常后续工作动作名称格式不正确", 120),
    consequence: requiredText(record.consequence, "异常后续工作动作后果格式不正确", 300),
    requiresReason: true,
  };
}

function parseWebManagedEvidenceSource(value: unknown): WebManagedEvidenceSource {
  const record = asRecord(value, "可用补证材料格式不正确");
  const objectType = requiredText(record.object_type, "可用补证材料类型格式不正确", 32);
  if (objectType !== "EVIDENCE_FILE" && objectType !== "MATERIAL_OBJECT") {
    throw protocolError("可用补证材料类型格式不正确");
  }
  const createdAt = requiredText(record.created_at, "可用补证材料时间格式不正确", 80);
  if (!Number.isFinite(Date.parse(createdAt))) throw protocolError("可用补证材料时间格式不正确");
  return {
    objectType,
    objectId: normalizeOpaqueId(record.object_id, "可用补证材料编号"),
    displayLabel: requiredText(record.display_label, "可用补证材料名称格式不正确", 500),
    createdAt,
  };
}

function parseWebManagedEvidenceSourcePage(value: unknown): WebManagedEvidenceSourcePage {
  const record = asRecord(value, "可用补证材料分页格式不正确");
  const totalCount = requiredSafeNonNegativeInteger(record.total_count, "可用补证材料总数格式不正确");
  const offset = requiredSafeNonNegativeInteger(record.offset, "可用补证材料分页位置格式不正确");
  const nextOffset = optionalNonNegativeInteger(record.next_offset, Number.MAX_SAFE_INTEGER);
  const sources = parseArray(record.sources, "可用补证材料", parseWebManagedEvidenceSource);
  const expectedNext = offset + sources.length < totalCount ? offset + sources.length : null;
  const identities = sources.map((source) => `${source.objectType}:${source.objectId}`);
  if (
    offset > totalCount
    || sources.length > 50
    || (totalCount === 0 && (offset !== 0 || sources.length !== 0))
    || (totalCount > 0 && (offset >= totalCount || sources.length === 0))
    || nextOffset !== expectedNext
    || new Set(identities).size !== identities.length
  ) {
    throw protocolError("可用补证材料分页不连续");
  }
  return { totalCount, offset, nextOffset, sources };
}

function parseWebAgentLedgerFollowupEvidencePage(value: unknown): WebAgentLedgerFollowupEvidencePage {
  const record = asRecord(value, "异常后续工作来源页分页格式不正确");
  const totalCount = requiredPositiveInteger(
    record.total_count,
    "异常后续工作来源页总数格式不正确",
    Number.MAX_SAFE_INTEGER,
  );
  const offset = requiredSafeNonNegativeInteger(record.offset, "异常后续工作来源页位置格式不正确");
  const nextOffset = optionalNonNegativeInteger(record.next_offset, Number.MAX_SAFE_INTEGER);
  const evidencePageIds = parseIdArray(record.evidence_page_ids, "异常后续工作来源页");
  const expectedNext = offset + evidencePageIds.length < totalCount
    ? offset + evidencePageIds.length
    : null;
  if (
    offset >= totalCount
    || evidencePageIds.length < 1
    || evidencePageIds.length > 50
    || new Set(evidencePageIds).size !== evidencePageIds.length
    || nextOffset !== expectedNext
  ) {
    throw protocolError("异常后续工作来源页分页不连续");
  }
  return { totalCount, offset, nextOffset, evidencePageIds };
}

function parseWebAgentLedgerExceptionFollowupReceipt(value: unknown): WebAgentLedgerExceptionFollowupReceipt {
  const record = asRecord(value, "异常后续工作回执格式不正确");
  const action = requiredText(record.action, "异常后续工作回执动作格式不正确", 40);
  if (!isAgentLedgerFollowupActionCode(action)) throw protocolError("异常后续工作回执动作格式不正确");
  const terminalState = requiredText(record.terminal_state, "异常后续工作回执状态格式不正确", 32);
  const expectedTerminal: Readonly<Record<WebAgentLedgerFollowupActionCode, WebAgentLedgerExceptionFollowupReceipt["terminalState"]>> = {
    CONFIRM_MORE_EVIDENCE: "SATISFIED",
    RESUME: "RESUMED",
    WITHDRAW: "WITHDRAWN",
    SUPERSEDE: "SUPERSEDED",
  };
  if (terminalState !== expectedTerminal[action]) throw protocolError("异常后续工作回执状态与动作不一致");
  return {
    followupId: normalizeOpaqueId(record.followup_id, "异常后续工作编号"),
    action,
    terminalState,
    matterVersion: requiredPositiveInteger(record.matter_version, "异常后续工作回执案件版本格式不正确", Number.MAX_SAFE_INTEGER),
  };
}

function isAgentLedgerFollowupActionCode(value: string): value is WebAgentLedgerFollowupActionCode {
  return (["CONFIRM_MORE_EVIDENCE", "RESUME", "WITHDRAW", "SUPERSEDE"] as const).includes(value as WebAgentLedgerFollowupActionCode);
}

function isAgentLedgerFollowupAutomationStatus(value: string): value is WebAgentLedgerFollowupAutomationStatus {
  return (["WAITING_FOR_PLAN", "WAITING_FOR_REPLAN", "QUEUED", "RUNNING", "VERIFYING", "BLOCKED", "RECOVERY_REQUIRED"] as const).includes(value as WebAgentLedgerFollowupAutomationStatus);
}

function isAgentLedgerExceptionDecision(value: string): value is WebAgentLedgerExceptionDecision {
  return (["REJECT_AS_DUPLICATE", "REQUEST_REEXTRACTION", "REQUEST_MORE_EVIDENCE", "DEFER_WITH_REASON"] as const).includes(value as WebAgentLedgerExceptionDecision);
}

function isAgentLedgerExceptionReason(value: string): value is WebAgentLedgerExceptionReason {
  return (["DUPLICATE_CONFIRMED", "SOURCE_QUALITY_INSUFFICIENT", "EXTRACTION_CONFLICT", "EVIDENCE_GAP", "PARTY_DATE_AMOUNT_UNCLEAR", "AWAITING_CLIENT_INPUT", "AWAITING_EXTERNAL_RECORD", "NEEDS_LEAD_REVIEW"] as const).includes(value as WebAgentLedgerExceptionReason);
}

function createWebAgentIdempotencyKey(): string {
  if (typeof crypto === "undefined" || typeof crypto.randomUUID !== "function") {
    throw new Error("当前浏览器无法生成安全的材料整理请求编号。请使用受支持的现代浏览器。");
  }
  return normalizeIdempotencyKey(`web-agent-${crypto.randomUUID()}`);
}

export function createWebCaseAgentIdempotencyKey(): string {
  if (typeof crypto === "undefined" || typeof crypto.randomUUID !== "function") {
    throw new Error("当前浏览器无法生成安全的办案任务请求编号。请使用受支持的现代浏览器。");
  }
  return normalizeIdempotencyKey(`case-agent-${crypto.randomUUID()}`);
}

function safeContentDispositionFileName(value: string | null): string | null {
  if (!value) return null;
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(value);
  const ascii = /filename="([A-Za-z0-9._-]{1,120})"/i.exec(value);
  let candidate: string | null = null;
  if (utf8) {
    try {
      candidate = decodeURIComponent(utf8[1]);
    } catch {
      candidate = null;
    }
  }
  candidate ??= ascii?.[1] ?? null;
  if (
    candidate === null
    || candidate.length < 1
    || candidate.length > 120
    || /[\r\n\0/\\]/.test(candidate)
  ) return null;
  return candidate;
}

function parseMaterialReceipt(value: unknown): WebMaterialReceipt {
  const record = asRecord(value, "材料接收回执格式不正确");
  const sha256 = requiredText(record.sha256, "材料接收回执未提供 SHA-256", 64);
  if (!SHA256_PATTERN.test(sha256)) {
    throw protocolError("材料接收回执中的 SHA-256 格式不正确");
  }
  return {
    evidenceFileId: normalizeOpaqueId(record.evidence_file_id, "证据文件编号"),
    displayName: requiredText(record.display_name, "材料接收回执未提供文件名称", FILE_NAME_MAX_LENGTH),
    sha256: sha256.toLowerCase(),
    pageCount: requiredPositiveInteger(record.page_count, "材料接收回执未提供有效页数", 100_000),
    scanStatus: requiredText(record.scan_status, "材料接收回执未提供扫描状态", 80),
    matterVersion: requiredPositiveInteger(record.matter_version, "材料接收回执未提供案件版本", Number.MAX_SAFE_INTEGER),
    receivedAt: optionalText(record.received_at, 64),
  };
}

function parseMaterialArchiveReceipt(value: unknown): WebMaterialArchiveReceipt {
  const record = asRecord(value, "ZIP 材料接收回执格式不正确");
  const sha256 = requiredText(record.sha256, "ZIP 材料接收回执未提供 SHA-256", 64).toLowerCase();
  if (!SHA256_PATTERN.test(sha256)) throw protocolError("ZIP 材料接收回执中的 SHA-256 格式不正确");
  const processingStatus = requiredText(record.processing_status, "ZIP 材料包未提供处理状态", 64);
  if (processingStatus !== "STORED_PENDING_PROCESSING") throw protocolError("ZIP 材料包处理状态不受支持");
  return {
    archiveId: normalizeOpaqueId(record.archive_id, "ZIP 材料接收编号"),
    displayName: requiredText(record.display_name, "ZIP 材料接收回执未提供文件名称", FILE_NAME_MAX_LENGTH),
    sha256,
    byteSize: requiredPositiveInteger(record.byte_size, "ZIP 材料大小格式不正确", WEB_MAX_ARCHIVE_BYTES),
    entryCount: requiredPositiveInteger(record.entry_count, "ZIP 材料文件数量格式不正确", 1_000),
    expandedByteSize: requiredPositiveInteger(record.expanded_byte_size, "ZIP 解压大小格式不正确", 1_073_741_824),
    processingStatus: "STORED_PENDING_PROCESSING",
  };
}

function parseWebCasePostureState(value: Record<string, unknown>): Omit<WebCasePosture, "options"> {
  const status = requiredText(value.status, "代理情境未提供状态", 32) as WebCasePostureStatus;
  if (status !== "NOT_CONFIRMED" && status !== "CURRENT" && status !== "STALE") {
    throw protocolError("代理情境状态不受支持");
  }
  const profileValue = value.profile;
  if ((status === "NOT_CONFIRMED") !== (profileValue === null || profileValue === undefined)) {
    throw protocolError("代理情境状态与档案不一致");
  }
  return {
    status,
    canConfirm: requiredBoolean(value.can_confirm, "代理情境未提供确认权限"),
    profile: profileValue === null || profileValue === undefined ? null : parseWebCasePostureProfile(profileValue),
  };
}

function parseWebCasePostureProfile(value: unknown): WebCasePostureProfile {
  const record = asRecord(value, "代理情境档案格式不正确");
  return {
    profileId: normalizeOpaqueId(record.profile_id, "代理情境编号"),
    profileVersion: requiredPositiveInteger(record.profile_version, "代理情境版本格式不正确", Number.MAX_SAFE_INTEGER),
    representedPartyId: normalizeOpaqueId(record.represented_party_id, "被代理当事人编号"),
    representedPartyDisplayLabel: requiredText(record.represented_party_display_label, "代理情境未提供当事人名称", 200),
    representedPartyKind: normalizePostureCode(record.represented_party_kind, "当事人类型"),
    proceedingId: normalizeOpaqueId(record.proceeding_id, "程序编号"),
    forumType: normalizePostureCode(record.forum_type, "受理机构"),
    positionId: normalizeOpaqueId(record.position_id, "当事人地位编号"),
    engagementId: normalizeOpaqueId(record.engagement_id, "委托编号"),
    caseTypeCode: normalizePostureCode(record.case_type_code, "案件类型"),
    procedureStage: normalizePostureCode(record.procedure_stage, "程序阶段"),
    representedPosition: normalizePostureCode(record.represented_position, "当事人地位"),
    authorityScopeCode: normalizePostureCode(record.authority_scope_code, "代理权限"),
    engagementState: normalizePostureCode(record.engagement_state, "委托状态"),
    confirmedMatterVersion: requiredPositiveInteger(record.confirmed_matter_version, "代理情境未提供案件版本", Number.MAX_SAFE_INTEGER),
  };
}

function parseWebCasePostureOptions(value: Record<string, unknown>): WebCasePostureOptions {
  return {
    partyKinds: parsePostureCodes(value.party_kinds, "当事人类型"),
    forumTypes: parsePostureCodes(value.forum_types, "受理机构"),
    caseTypes: parsePostureCodes(value.case_types, "案件类型"),
    procedureStages: parsePostureCodes(value.procedure_stages, "程序阶段"),
    partyPositions: parsePostureCodes(value.party_positions, "当事人地位"),
    authorityScopes: parsePostureCodes(value.authority_scopes, "代理权限"),
    engagementStates: parsePostureCodes(value.engagement_states, "委托状态"),
  };
}

function parsePostureCodes(value: unknown, label: string): readonly string[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 100) {
    throw protocolError(`${label}受控选项格式不正确`);
  }
  const values = value.map((item) => normalizePostureCode(item, label));
  if (new Set(values).size !== values.length) throw protocolError(`${label}受控选项重复`);
  return values;
}

function parseWebCasePostureCommandReceipt(value: unknown): WebCasePostureCommandReceipt {
  const record = asRecord(value, "代理情境确认回执格式不正确");
  const action = requiredText(record.action, "代理情境确认回执未提供动作", 64) as WebCasePostureCommandReceipt["action"];
  const allowed: readonly WebCasePostureCommandReceipt["action"][] = [
    "CONFIRM_PARTY",
    "CONFIRM_PROCEEDING",
    "CONFIRM_POSITION",
    "CONFIRM_ENGAGEMENT",
    "CONFIRM_CURRENT_PROFILE",
  ];
  if (!allowed.includes(action)) throw protocolError("代理情境确认回执动作不受支持");
  return {
    action,
    matterVersion: requiredPositiveInteger(record.matter_version, "代理情境确认回执未提供案件版本", Number.MAX_SAFE_INTEGER),
    objectType: requiredText(record.object_type, "代理情境确认回执未提供对象类型", 80),
    objectId: normalizeOpaqueId(record.object_id, "代理情境确认对象编号"),
  };
}

function parseWebCasePostureCompleteReceipt(value: unknown): WebCasePostureCompleteReceipt {
  const record = asRecord(value, "完整代理情境确认回执格式不正确");
  if (requiredText(record.action, "完整代理情境确认回执未提供动作", 64) !== "CONFIRM_COMPLETE_POSTURE") {
    throw protocolError("完整代理情境确认回执动作不匹配");
  }
  if (record.refresh_posture_state !== true) throw protocolError("完整代理情境确认回执未要求刷新状态");
  return {
    action: "CONFIRM_COMPLETE_POSTURE",
    matterVersion: requiredPositiveInteger(record.matter_version, "完整代理情境确认回执未提供案件版本", Number.MAX_SAFE_INTEGER),
    partyId: normalizeOpaqueId(record.party_id, "被代理当事人编号"),
    proceedingId: normalizeOpaqueId(record.proceeding_id, "程序编号"),
    positionId: normalizeOpaqueId(record.position_id, "当事人地位编号"),
    engagementId: normalizeOpaqueId(record.engagement_id, "委托编号"),
    profileId: normalizeOpaqueId(record.profile_id, "代理情境编号"),
  };
}

function normalizePostureCode(value: unknown, label: string): string {
  const normalized = requiredText(value, `${label}格式不正确`, 64);
  if (!/^[A-Z][A-Z0-9._-]*$/.test(normalized)) {
    throw protocolError(`${label}格式不正确`);
  }
  return normalized;
}

function normalizePostureDisplayLabel(value: string): string {
  const normalized = value.trim().replace(/\s+/g, " ");
  if (normalized.length < 1 || normalized.length > 200 || containsControlCharacter(normalized)) {
    throw new Error("被代理当事人名称格式不正确。");
  }
  return normalized;
}

function parseCommonMaterialAdmissionReceipt(value: unknown): WebCommonMaterialAdmissionReceipt {
  const record = asRecord(value, "常见材料接收回执格式不正确");
  const sha256 = requiredText(record.sha256, "常见材料接收回执未提供 SHA-256", 64).toLowerCase();
  if (!SHA256_PATTERN.test(sha256)) throw protocolError("常见材料接收回执中的 SHA-256 格式不正确");
  const materialObjectId = normalizeOpaqueId(record.material_object_id, "材料对象编号");
  const admittedFormat = requiredText(record.admitted_format, "常见材料接收回执未提供格式", 16) as WebCommonMaterialAdmissionReceipt["admittedFormat"];
  const formats: readonly WebCommonMaterialAdmissionReceipt["admittedFormat"][] = ["DOCX", "XLSX", "PPTX", "RTF", "TXT", "CSV", "HTML", "EML", "JPEG", "PNG"];
  if (!formats.includes(admittedFormat)) throw protocolError("常见材料接收回执格式不受支持");
  const route = requiredText(record.route, "常见材料接收回执未提供处理路径", 64) as WebCommonMaterialAdmissionReceipt["route"];
  if (route !== "COMMON_DOCUMENT_READER" && route !== "VISUAL_OCR") throw protocolError("常见材料处理路径不受支持");
  const reviewStatus = requiredText(record.review_status, "常见材料接收回执未提供复核状态", 64);
  if (reviewStatus !== "NEEDS_LAWYER_REVIEW") throw protocolError("常见材料不得绕过律师复核");
  const agentStatus = requiredText(record.agent_status, "常见材料接收回执未提供 Agent 状态", 64) as WebCommonMaterialAdmissionReceipt["agentStatus"];
  if (agentStatus !== "AGENT_READY" && agentStatus !== "INGESTED_PENDING_ADAPTER") {
    throw protocolError("常见材料 Agent 状态不受支持");
  }
  const agentSourceRef = record.agent_source_ref === null || record.agent_source_ref === undefined
    ? null
    : requiredText(record.agent_source_ref, "常见材料 Agent 来源格式不正确", 128);
  if (
    record.formal_fact !== false
    || record.formal_transaction !== false
    || record.legal_conclusion !== false
    || record.evidence_decision !== false
    || record.court_ready !== false
  ) {
    throw protocolError("常见材料接收回执不能包含正式结论");
  }
  if (agentStatus === "AGENT_READY") {
    if ((admittedFormat === "DOCX" || admittedFormat === "XLSX") && agentSourceRef !== `material-object:${materialObjectId}`) {
      throw protocolError("常见材料 Agent 来源与材料对象不匹配");
    }
    if ((admittedFormat === "JPEG" || admittedFormat === "PNG") && !isEvidencePageSourceRef(agentSourceRef)) {
      throw protocolError("图像材料未提供有效证据页来源");
    }
    if (!["DOCX", "XLSX", "JPEG", "PNG"].includes(admittedFormat)) {
      throw protocolError("该常见材料格式不应标记为 Agent 可读取");
    }
  } else if (agentSourceRef !== null) {
    throw protocolError("待接通材料不能提供 Agent 来源");
  }
  return {
    materialObjectId,
    displayName: requiredText(record.display_name, "常见材料接收回执未提供文件名称", FILE_NAME_MAX_LENGTH),
    admittedFormat,
    mediaType: requiredText(record.media_type, "常见材料接收回执未提供媒体类型", 128),
    byteSize: requiredPositiveInteger(record.byte_size, "常见材料大小格式不正确", WEB_MAX_COMMON_MATERIAL_BYTES),
    sha256,
    route,
    reviewStatus: "NEEDS_LAWYER_REVIEW",
    agentStatus,
    agentSourceRef,
    matterVersion: requiredPositiveInteger(record.matter_version, "常见材料接收回执未提供案件版本", Number.MAX_SAFE_INTEGER),
  };
}

function parseCommonMaterialUploadStatus(value: unknown): WebCommonMaterialUploadStatus {
  const record = asRecord(value, "常见材料接收状态格式不正确");
  if (requiredText(record.kind, "常见材料接收状态未提供类型", 16) !== "COMMON") {
    throw protocolError("常见材料接收状态类型不匹配");
  }
  if (record.retry_allowed !== false) throw protocolError("常见材料接收状态不允许浏览器自动重传");
  const state = requiredText(record.state, "常见材料接收状态未提供状态", 64) as WebCommonMaterialUploadStatus["state"];
  const allowed: readonly WebCommonMaterialUploadStatus["state"][] = ["PROCESSING", "COMPLETED", "REJECTED", "EXPIRED", "RECONCILIATION_REQUIRED", "ADMISSION_UNAVAILABLE"];
  if (!allowed.includes(state)) throw protocolError("常见材料接收状态不受支持");
  const receiptValue = record.receipt;
  const receipt = receiptValue === null || receiptValue === undefined ? null : parseCommonMaterialAdmissionReceipt(receiptValue);
  if ((state === "COMPLETED") !== (receipt !== null)) throw protocolError("常见材料完成状态与回执不一致");
  return {
    operationId: normalizeOpaqueId(record.operation_id, "常见材料接收编号"),
    kind: "COMMON",
    state,
    receipt,
  };
}

function isEvidencePageSourceRef(value: string | null): boolean {
  return value !== null && value.startsWith("evidence-page:") && UUID_PATTERN.test(value.slice("evidence-page:".length));
}

function parseMaterialUploadStatus(value: unknown, expectedKind: "PDF" | "ZIP"): WebMaterialUploadStatus {
  const record = asRecord(value, "材料接收状态格式不正确");
  const kind = requiredText(record.kind, "材料接收状态未提供类型", 8);
  if (kind !== expectedKind) throw protocolError("材料接收状态类型不匹配");
  const state = requiredText(record.state, "材料接收状态未提供状态", 64) as WebMaterialUploadStatus["state"];
  const allowed: readonly WebMaterialUploadStatus["state"][] = ["PROCESSING", "COMPLETED", "REJECTED", "EXPIRED", "RECONCILIATION_REQUIRED", "STORED_PENDING_PROCESSING"];
  if (!allowed.includes(state)) throw protocolError("材料接收状态不受支持");
  const receiptValue = record.receipt;
  const receipt = receiptValue === null || receiptValue === undefined
    ? null
    : expectedKind === "PDF" ? parseMaterialReceipt(receiptValue) : parseMaterialArchiveReceipt(receiptValue);
  if ((state === "COMPLETED" && receipt === null) || (state === "STORED_PENDING_PROCESSING" && receipt === null)) {
    throw protocolError("材料接收完成状态未提供回执");
  }
  return {
    operationId: normalizeOpaqueId(record.operation_id, "材料接收编号"),
    kind: expectedKind,
    state,
    receipt,
  };
}

function parseCaseReview(value: Record<string, unknown>): WebCaseReview {
  const facts = parseArray(value.facts, "事实", parseFact);
  const claims = parseArray(value.claims, "诉请", parseClaim);
  const issues = parseArray(value.issues, "争点", parseIssue);
  const transactions = parseArray(value.transactions, "交易", parseTransaction);
  const paymentClassifications = parseArray(value.payment_classifications ?? [], "款项归属", parsePaymentClassification);
  const snapshotHash = requiredText(value.snapshot_hash, "案件要点未提供快照哈希", 64);
  if (!SHA256_PATTERN.test(snapshotHash)) throw protocolError("案件要点快照哈希格式不正确");
  return {
    matterId: normalizeOpaqueId(value.matter_id, "案件编号"),
    title: requiredText(value.title, "案件要点未提供案件名称", CASE_TITLE_MAX_LENGTH),
    stage: requiredText(value.stage, "案件要点未提供阶段", 80),
    version: requiredPositiveInteger(value.version, "案件要点未提供有效版本", Number.MAX_SAFE_INTEGER),
    snapshotHash: snapshotHash.toLowerCase(),
    facts,
    claims,
    issues,
    transactions,
    paymentClassifications,
  };
}

function parseFact(value: unknown): WebCaseFact {
  const record = asRecord(value, "事实记录格式不正确");
  return {
    factId: normalizeOpaqueId(record.fact_id, "事实编号"),
    text: requiredText(record.text, "事实记录未提供内容", 8_000),
    origin: requiredText(record.origin, "事实记录未提供来源", 80),
    status: requiredText(record.status, "事实记录未提供状态", 40),
    evidenceCount: requiredNonNegativeInteger(record.evidence_count, "事实证据数量格式不正确"),
    decisionHash: optionalSha(record.decision_hash),
    correctionCandidateId:record.correction_candidate_id==null?null:normalizeOpaqueId(record.correction_candidate_id,"纠正候选编号"),
    evidenceSources:parseArray(record.evidence_sources??[],"事实原件来源",item=>{
      const source=asRecord(item,"事实原件来源无效");
      return {pageId:normalizeOpaqueId(source.evidence_page_id,"事实证据定位"),label:requiredText(source.label,"原件名称无效",1024),
        pageNumber:source.page_number===null?null:requiredPositiveInteger(source.page_number,"原件页码无效",Number.MAX_SAFE_INTEGER)};
    }),
  };
}

function parseClaim(value: unknown): WebCaseClaim {
  const record = asRecord(value, "诉请记录格式不正确");
  const response = record.response === null || record.response === undefined
    ? null
    : asRecord(record.response, "诉请回应格式不正确");
  return {
    claimId: normalizeOpaqueId(record.claim_id, "诉请编号"),
    text: requiredText(record.text, "诉请记录未提供内容", 8_000),
    claimedAmount: optionalAmount(record.claimed_amount),
    currency: optionalText(record.currency, 12),
    status: requiredText(record.status, "诉请记录未提供状态", 40),
    evidenceCount: requiredNonNegativeInteger(record.evidence_count, "诉请证据数量格式不正确"),
    response: response === null ? null : {
      position: requiredText(response.position, "诉请回应未提供立场", 64),
      partialAmount: optionalAmount(response.partial_amount),
      currency: optionalText(response.currency, 12),
    },
  };
}

function parseIssue(value: unknown): WebCaseIssue {
  const record = asRecord(value, "争点记录格式不正确");
  return {
    issueId: normalizeOpaqueId(record.issue_id, "争点编号"),
    question: requiredText(record.question, "争点记录未提供问题", 2_000),
    status: requiredText(record.status, "争点记录未提供状态", 40),
    claimIds: parseIdArray(record.claim_ids, "争点诉请编号"),
    confirmedFactIds: parseIdArray(record.confirmed_fact_ids, "争点事实编号"),
  };
}

function parseTransaction(value: unknown): WebCaseTransaction {
  const record = asRecord(value, "交易记录格式不正确");
  return {
    transactionId: normalizeOpaqueId(record.transaction_id, "交易编号"),
    localDate: optionalText(record.local_date, 32),
    datePrecision: optionalText(record.date_precision, 32),
    amount: optionalAmount(record.amount),
    currency: optionalText(record.currency, 12),
    direction: requiredText(record.direction, "交易记录未提供方向", 40),
    payerLabel: optionalText(record.payer_label, 255),
    payeeLabel: optionalText(record.payee_label, 255),
    channel: optionalText(record.channel, 64),
    transactionReference: optionalText(record.transaction_reference, 255),
    status: requiredText(record.status, "交易记录未提供状态", 40),
    evidenceCount: requiredNonNegativeInteger(record.evidence_count, "交易证据数量格式不正确"),
    confirmationHash: optionalSha(record.confirmation_hash),
  };
}

function parsePaymentClassification(value: unknown): WebPaymentClassification {
  const record = asRecord(value, "款项归属记录格式不正确");
  return {
    classificationId: normalizeOpaqueId(record.classification_id, "款项归属编号"),
    transactionId: normalizeOpaqueId(record.transaction_id, "收付款记录编号"),
    nature: requiredText(record.nature, "款项性质格式不正确", 64),
    sameDaySequence: record.same_day_sequence === null || record.same_day_sequence === undefined
      ? null
      : requiredPositiveInteger(record.same_day_sequence, "同日顺序格式不正确", 999),
    status: requiredText(record.status, "款项归属状态格式不正确", 40),
    evidenceCount: requiredNonNegativeInteger(record.evidence_count, "款项归属证据数量格式不正确"),
    allocations: parseArray(record.allocations ?? [], "款项归属明细", (item) => {
      const allocation = asRecord(item, "款项归属明细格式不正确");
      return {
        obligationLabel: requiredText(allocation.obligation_label, "归属事项格式不正确", 160),
        amount: optionalAmount(allocation.amount),
        currency: optionalText(allocation.currency, 12),
      };
    }),
  };
}

function parseLegalReview(value: Record<string, unknown>): WebLegalReview {
  const snapshotHash = requiredText(value.snapshot_hash, "法律依据未提供快照哈希", 64).toLowerCase();
  if (!SHA256_PATTERN.test(snapshotHash)) throw protocolError("法律依据快照哈希格式不正确");
  return {
    matterId: normalizeOpaqueId(value.matter_id, "案件编号"),
    matterVersion: requiredPositiveInteger(value.matter_version, "法律依据未提供有效案件版本", Number.MAX_SAFE_INTEGER),
    snapshotHash,
    sources: parseArray(value.sources, "法源", parseLegalSource),
    ruleVersions: parseArray(value.rule_versions, "规则版本", parseLegalRule),
    legalEvents: parseArray(value.legal_events, "法律事件", parseLegalEvent),
    factBindings: parseArray(value.fact_bindings, "法律事实绑定", parseLegalFactBinding),
    currentBundle: value.current_bundle === null || value.current_bundle === undefined ? null : parseLegalBundle(value.current_bundle),
    bundleSegments: parseArray(value.bundle_segments, "规则段", parseLegalBundleSegment),
    bundleReconfirmation: value.bundle_reconfirmation === null || value.bundle_reconfirmation === undefined ? null : parseLegalBundleReconfirmation(value.bundle_reconfirmation),
  };
}

function parseLegalSource(value: unknown): WebLegalSource {
  const record = asRecord(value, "法源记录格式不正确");
  const officialUrl = requiredText(record.official_url, "法源记录未提供官方地址", 2_048);
  if (!officialUrl.startsWith("https://")) throw protocolError("法源地址必须是 HTTPS");
  return { snapshotId: normalizeOpaqueId(record.snapshot_id, "法源快照编号"), sourceId: requiredText(record.source_id, "法源标识格式不正确", 160), publisher: requiredText(record.publisher, "法源发布机构格式不正确", 255), authorityLevel: requiredText(record.authority_level, "法源层级格式不正确", 64), officialUrl, provisionLocator: requiredText(record.provision_locator, "法源定位格式不正确", 1_000), retrievedAt: optionalText(record.retrieved_at, 80), contentSha256: optionalSha(record.content_sha256) ?? (() => { throw protocolError("法源内容哈希格式不正确"); })(), verificationStatus: requiredText(record.verification_status, "法源核验状态格式不正确", 64), licenseStatus: requiredText(record.license_status, "法源许可状态格式不正确", 64) };
}

function parseLegalRule(value: unknown): WebLegalRule {
  const record = asRecord(value, "规则版本格式不正确");
  return { ruleVersionId: normalizeOpaqueId(record.rule_version_id, "规则版本编号"), ruleId: requiredText(record.rule_id, "规则标识格式不正确", 160), ruleVersion: requiredText(record.rule_version, "规则版本格式不正确", 80), issueKey: requiredText(record.issue_key, "规则争点格式不正确", 255), effectiveFrom: optionalText(record.effective_from, 32), effectiveTo: optionalText(record.effective_to, 32), triggerEventKind: requiredText(record.trigger_event_kind, "触发事件格式不正确", 80), formulaKind: requiredText(record.formula_kind, "计算公式格式不正确", 80), baseAnnualRate: optionalAmount(record.base_annual_rate), rateMultiplier: optionalAmount(record.rate_multiplier), derivedAnnualRate: optionalRate(record.derived_annual_rate), status: requiredText(record.status, "规则状态格式不正确", 64) };
}

function parseLegalEvent(value: unknown): WebLegalEvent {
  const record = asRecord(value, "法律事件格式不正确");
  return { legalEventId: normalizeOpaqueId(record.legal_event_id, "法律事件编号"), eventKind: requiredText(record.event_kind, "法律事件类型格式不正确", 80), localDate: optionalText(record.local_date, 32), evidenceIds: parseIdArray(record.evidence_ids, "法律事件证据编号"), status: requiredText(record.status, "法律事件状态格式不正确", 64) };
}

function parseLegalFactBinding(value: unknown): WebLegalFactBinding {
  const record = asRecord(value, "法律事实绑定格式不正确");
  return { bindingId: normalizeOpaqueId(record.binding_id, "法律事实绑定编号"), factKey: requiredText(record.fact_key, "法律事实键格式不正确", 160), factId: normalizeOpaqueId(record.fact_id, "法律事实编号"), status: requiredText(record.status, "法律事实绑定状态格式不正确", 64) };
}

function parseLegalBundle(value: unknown): WebLegalReview["currentBundle"] {
  const record = asRecord(value, "法律规则包格式不正确");
  return { bundleId: normalizeOpaqueId(record.bundle_id, "法律规则包编号"), version: requiredPositiveInteger(record.version, "法律规则包版本格式不正确", Number.MAX_SAFE_INTEGER), bundleHash: requiredText(record.bundle_hash, "法律规则包哈希格式不正确", 64), approvedAt: optionalText(record.approved_at, 80) };
}

function parseLegalBundleReconfirmation(value: unknown): NonNullable<WebLegalReview["bundleReconfirmation"]> {
  const record = asRecord(value, "法律规则包复核状态格式不正确");
  return {
    version: requiredPositiveInteger(record.version, "法律规则包版本格式不正确", Number.MAX_SAFE_INTEGER),
    reason: requiredText(record.reason, "法律规则包失效原因格式不正确", 500),
    staleAt: optionalText(record.stale_at, 80),
  };
}

function parseLegalBundleSegment(value: unknown): WebLegalBundleSegment {
  const record = asRecord(value, "法律规则段格式不正确");
  return { segmentId: normalizeOpaqueId(record.segment_id, "法律规则段编号"), issueKey: requiredText(record.issue_key, "规则段争点格式不正确", 255), ruleVersionId: normalizeOpaqueId(record.rule_version_id, "规则段版本编号"), triggerEventId: normalizeOpaqueId(record.trigger_event_id, "规则段触发事件编号"), startDate: optionalText(record.start_date, 32), endDate: optionalText(record.end_date, 32), annualRate: optionalRate(record.annual_rate), applicabilityAnchor: requiredText(record.applicability_anchor, "规则段适用锚点格式不正确", 120) };
}

function parseOfficialSourceCatalogueItem(value: unknown): WebOfficialSourceCatalogueItem {
  const record = asRecord(value, "官方依据目录项格式不正确");
  const sourceId = requiredText(record.source_id, "官方依据标识格式不正确", 160);
  if (![
    "CN-CIVIL-CODE-680",
    "SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
    "SPC-PRIVATE-LENDING-2020-FIRST-REVISION",
    "SPC-PRIVATE-LENDING-2015-ORIGINAL",
    "CFETS-LPR-HISTORY",
  ].includes(sourceId)) throw protocolError("官方依据标识不在可用目录中");
  return {
    sourceId: sourceId as WebOfficialSourceCatalogueItem["sourceId"],
    title: requiredText(record.title, "官方依据名称格式不正确", 160),
    publisher: requiredText(record.publisher, "官方发布机构格式不正确", 160),
    purpose: requiredText(record.purpose, "官方依据用途格式不正确", 240),
  };
}

function parseOfficialSourceCaptureStatus(value: Record<string, unknown>): WebOfficialSourceCaptureStatus {
  const reviewedRunIds = parseIdArray(value.reviewed_run_ids ?? [], "已核对官方依据任务编号");
  return {
    matterId: normalizeOpaqueId(value.matter_id, "案件编号"),
    matterVersion: requiredPositiveInteger(value.matter_version, "官方依据任务未提供案件版本", Number.MAX_SAFE_INTEGER),
    runs: parseArray(value.runs, "官方依据任务", (item) => {
      const record = asRecord(item, "官方依据任务格式不正确");
      return {
        runId: normalizeOpaqueId(record.run_id, "官方依据任务编号"),
        sourceId: requiredText(record.source_id, "官方依据标识格式不正确", 160),
        publisher: requiredText(record.publisher, "官方发布机构格式不正确", 255),
        status: requiredText(record.status, "官方依据任务状态格式不正确", 64),
        authorizedAt: optionalText(record.authorized_at, 80),
        retrievedAt: optionalText(record.retrieved_at, 80),
        officialUrl: record.official_url === null || record.official_url === undefined ? null : (() => {
          const url = requiredText(record.official_url, "官方原文地址格式不正确", 2_048);
          if (!url.startsWith("https://")) throw protocolError("官方原文地址必须是 HTTPS");
          return url;
        })(),
        provisions: parseArray(record.provisions ?? [], "可核对条款", (provision) => requiredText(provision, "条款名称格式不正确", 120)),
        failureCode: optionalText(record.failure_code, 120),
      };
    }),
    reviewedRunIds,
  };
}

function parseFormalCalculation(value: Record<string, unknown>): WebFormalCalculation {
  const snapshotHash = requiredText(value.snapshot_hash, "利息测算未提供快照哈希", 64).toLowerCase();
  if (!SHA256_PATTERN.test(snapshotHash)) throw protocolError("利息测算快照哈希格式不正确");
  const scenario = value.scenario === null || value.scenario === undefined ? null : parseCalculationScenario(value.scenario);
  const run = value.run === null || value.run === undefined ? null : parseCalculationRun(value.run);
  if ((scenario === null) !== (run === null)) throw protocolError("利息测算情景与结果不完整");
  return { matterId: normalizeOpaqueId(value.matter_id, "案件编号"), matterVersion: requiredPositiveInteger(value.matter_version, "利息测算未提供有效案件版本", Number.MAX_SAFE_INTEGER), snapshotHash, scenario, run };
}

function parseCalculationScenario(value: unknown): NonNullable<WebFormalCalculation["scenario"]> {
  const record = asRecord(value, "利息测算情景格式不正确");
  return {
    scenarioId: normalizeOpaqueId(record.scenario_id, "计算情景编号"),
    obligationId: normalizeCalculationObligationId(record.obligation_id),
    version: requiredPositiveInteger(record.version, "计算情景版本格式不正确", Number.MAX_SAFE_INTEGER),
    startDate: normalizeIsoDate(requiredText(record.start_date, "计算开始日期格式不正确", 32), "计算开始日期"),
    endDate: normalizeIsoDate(requiredText(record.end_date, "计算结束日期格式不正确", 32), "计算结束日期"),
    currency: requiredText(record.currency, "计算币种格式不正确", 12),
    allocationPolicy: requiredText(record.allocation_policy, "还款抵扣口径格式不正确", 64),
    legalBundleId: normalizeOpaqueId(record.legal_bundle_id, "法律规则包编号"),
    legalBundleHash: requiredSha(record.legal_bundle_hash, "法律规则包哈希格式不正确"),
    transactionSnapshotHash: requiredSha(record.transaction_snapshot_hash, "交易快照哈希格式不正确"),
    inputHash: requiredSha(record.input_hash, "计算输入哈希格式不正确"),
  };
}

function parseCalculationRun(value: unknown): NonNullable<WebFormalCalculation["run"]> {
  const record = asRecord(value, "利息测算结果格式不正确");
  return {
    runId: normalizeOpaqueId(record.run_id, "计算运行编号"),
    scenarioId: normalizeOpaqueId(record.scenario_id, "计算情景编号"),
    scenarioVersion: requiredPositiveInteger(record.scenario_version, "计算情景版本格式不正确", Number.MAX_SAFE_INTEGER),
    engineVersion: requiredText(record.engine_version, "计算引擎版本格式不正确", 80),
    legalBundleId: normalizeOpaqueId(record.legal_bundle_id, "法律规则包编号"),
    legalBundleHash: requiredSha(record.legal_bundle_hash, "法律规则包哈希格式不正确"),
    inputHash: requiredSha(record.input_hash, "计算输入哈希格式不正确"),
    outputHash: requiredSha(record.output_hash, "计算输出哈希格式不正确"),
    independentCheckHash: requiredSha(record.independent_check_hash, "独立复核哈希格式不正确"),
    totalInterestAccrued: optionalAmount(record.total_interest_accrued),
    totalInterestPaid: optionalAmount(record.total_interest_paid),
    remainingPrincipal: optionalAmount(record.remaining_principal),
    remainingUnpaidInterest: optionalAmount(record.remaining_unpaid_interest),
    unappliedPayments: optionalAmount(record.unapplied_payments),
    generatedAt: optionalText(record.generated_at, 80),
    lineItems: parseArray(record.line_items, "计算明细", parseCalculationLineItem),
    paymentAllocations: parseArray(record.payment_allocations, "还款抵扣", parsePaymentAllocation),
  };
}

function parseCalculationLineItem(value: unknown): WebCalculationLineItem {
  const record = asRecord(value, "计算明细格式不正确");
  return {
    lineSequence: requiredPositiveInteger(record.line_sequence, "计算明细序号格式不正确", 1_000_000),
    periodStart: normalizeIsoDate(requiredText(record.period_start, "计算明细开始日期格式不正确", 32), "计算明细开始日期"),
    periodEnd: normalizeIsoDate(requiredText(record.period_end, "计算明细结束日期格式不正确", 32), "计算明细结束日期"),
    openingPrincipal: optionalAmount(record.opening_principal),
    annualRate: optionalAmount(record.annual_rate),
    dayCount: requiredNonNegativeInteger(record.day_count, "计算天数格式不正确"),
    accruedInterest: optionalAmount(record.accrued_interest),
    closingPrincipal: optionalAmount(record.closing_principal),
    accruedUnpaidInterest: optionalAmount(record.accrued_unpaid_interest),
    ruleSegmentId: normalizeOpaqueId(record.rule_segment_id, "规则段编号"),
    sourceRuleVersion: requiredText(record.source_rule_version, "规则版本格式不正确", 80),
    evidenceIds: parseIdArray(record.evidence_ids, "计算明细证据编号"),
  };
}

function parsePaymentAllocation(value: unknown): WebPaymentAllocation {
  const record = asRecord(value, "还款抵扣格式不正确");
  return {
    allocationSequence: requiredPositiveInteger(record.allocation_sequence, "还款抵扣序号格式不正确", 1_000_000),
    paymentEventId: normalizeOpaqueId(record.payment_event_id, "还款事件编号"),
    effectiveDate: normalizeIsoDate(requiredText(record.effective_date, "还款日期格式不正确", 32), "还款日期"),
    paymentAmount: optionalAmount(record.payment_amount),
    allocatedInterest: optionalAmount(record.allocated_interest),
    allocatedPrincipal: optionalAmount(record.allocated_principal),
    unappliedAmount: optionalAmount(record.unapplied_amount),
    paymentApplication: requiredText(record.payment_application, "还款用途格式不正确", 64),
    evidenceIds: parseIdArray(record.evidence_ids, "还款证据编号"),
  };
}

function parseSubmissionReview(value: Record<string, unknown>): WebSubmissionReview {
  const snapshotHash = requiredSha(value.snapshot_hash, "应诉材料快照哈希格式不正确");
  return {
    matterId: normalizeOpaqueId(value.matter_id, "案件编号"),
    matterVersion: requiredPositiveInteger(value.matter_version, "应诉材料未提供有效案件版本", Number.MAX_SAFE_INTEGER),
    stage: requiredText(value.stage, "应诉材料未提供案件阶段", 80),
    snapshotHash,
    workProducts: parseArray(value.work_products, "应诉文书", parseSubmissionWorkProduct),
    bundles: parseArray(value.bundles, "应诉材料包", parseSubmissionBundle),
    currentBundle: value.current_bundle === null || value.current_bundle === undefined ? null : parseSubmissionBundle(value.current_bundle),
    currentComponents: parseArray(value.current_components, "应诉材料包组成", parseSubmissionComponent),
    currentExport: value.current_export === null || value.current_export === undefined ? null : parseSubmissionExport(value.current_export),
    documentDraftsAvailable: requiredBoolean(value.document_drafts_available, "文书候选能力状态格式不正确"),
  };
}

function parseDocumentDraftPair(value: unknown): WebDocumentDraftPair {
  const record = asRecord(value, "文书候选格式不正确");
  return {
    pairId: normalizeOpaqueId(record.pair_id, "文书候选编号"),
    documentKind: requiredText(record.document_kind, "文书候选类型格式不正确", 80),
    editableMediaType: requiredText(record.editable_media_type, "可编辑文书格式不正确", 160),
    editableSha256: requiredSha(record.editable_sha256, "可编辑文书哈希格式不正确"),
    editableBytes: requiredPositiveInteger(record.editable_bytes, "可编辑文书大小格式不正确", 64 * 1024 * 1024),
    reviewPdfSha256: requiredSha(record.review_pdf_sha256, "审阅 PDF 哈希格式不正确"),
    reviewPdfBytes: requiredPositiveInteger(record.review_pdf_bytes, "审阅 PDF 大小格式不正确", 128 * 1024 * 1024),
    reviewPdfPageCount: requiredPositiveInteger(record.review_pdf_page_count, "审阅 PDF 页数格式不正确", 100_000),
    reviewInputHash: requiredSha(record.review_input_hash, "文书候选核验哈希格式不正确"),
    status: requiredText(record.status, "文书候选状态格式不正确", 32),
    approvedAt: optionalText(record.approved_at, 80),
    createdAt: optionalText(record.created_at, 80),
  };
}

function parseSubmissionWorkProduct(value: unknown): WebSubmissionWorkProduct {
  const record = asRecord(value, "应诉文书格式不正确");
  return { workProductId: normalizeOpaqueId(record.work_product_id, "应诉文书编号"), documentKind: requiredText(record.document_kind, "应诉文书类型格式不正确", 80), audience: requiredText(record.audience, "应诉文书用途格式不正确", 40), mediaType: requiredText(record.media_type, "应诉文书格式不正确", 40), artifactSha256: requiredSha(record.artifact_sha256, "应诉文书哈希格式不正确"), byteSize: requiredPositiveInteger(record.byte_size, "应诉文书大小格式不正确", 256 * 1024 * 1024), pageCount: requiredPositiveInteger(record.page_count, "应诉文书页数格式不正确", 100_000), semanticTextSha256: optionalSha(record.semantic_text_sha256), status: requiredText(record.status, "应诉文书状态格式不正确", 40), approvedAt: optionalText(record.approved_at, 80), staleAt: optionalText(record.stale_at, 80), staleReason: optionalText(record.stale_reason, 500), createdAt: optionalText(record.created_at, 80) };
}

function parseSubmissionBundle(value: unknown): WebSubmissionBundle {
  const record = asRecord(value, "应诉材料包格式不正确");
  const requiredKinds = record.required_document_kinds;
  if (!Array.isArray(requiredKinds) || requiredKinds.length > 100) throw protocolError("应诉材料包必需文书清单格式不正确");
  return { bundleId: normalizeOpaqueId(record.bundle_id, "应诉材料包编号"), lifecycle: requiredText(record.lifecycle, "应诉材料包生命周期格式不正确", 40), validity: requiredText(record.validity, "应诉材料包有效性格式不正确", 40), finalTextHash: optionalSha(record.final_text_hash), approvedMatterVersion: requiredPositiveInteger(record.approved_matter_version, "应诉材料包批准版本格式不正确", Number.MAX_SAFE_INTEGER), lockedAt: optionalText(record.locked_at, 80), exportedAt: optionalText(record.exported_at, 80), createdAt: optionalText(record.created_at, 80), exportProfile: requiredText(record.export_profile, "应诉导出配置格式不正确", 80), currency: requiredText(record.currency, "应诉材料币种格式不正确", 12), inputHash: requiredSha(record.input_hash, "应诉材料输入哈希格式不正确"), requiredDocumentKinds: requiredKinds.map((kind) => requiredText(kind, "必需文书类型格式不正确", 80)), evidenceManifestId: normalizeOpaqueId(record.evidence_manifest_id, "证据清单编号"), evidenceManifestHash: requiredSha(record.evidence_manifest_hash, "证据清单哈希格式不正确"), legalBundleId: normalizeOpaqueId(record.legal_bundle_id, "法律规则包编号"), legalBundleHash: requiredSha(record.legal_bundle_hash, "法律规则包哈希格式不正确"), calculationRunId: normalizeOpaqueId(record.calculation_run_id, "利息测算运行编号"), calculationOutputHash: requiredSha(record.calculation_output_hash, "利息测算输出哈希格式不正确"), finalTextApprovalId: normalizeOpaqueId(record.final_text_approval_id, "最终文本批准编号"), qaHash: requiredSha(record.qa_hash, "应诉质量核对哈希格式不正确"), qaApprovedAt: optionalText(record.qa_approved_at, 80) };
}

function parseSubmissionComponent(value: unknown): WebSubmissionComponent {
  const record = asRecord(value, "应诉材料包组成格式不正确");
  return { workProductId: normalizeOpaqueId(record.work_product_id, "组成文书编号"), sequence: requiredPositiveInteger(record.sequence, "组成顺序格式不正确", 100_000), documentKind: requiredText(record.document_kind, "组成文书类型格式不正确", 80), courtFilename: requiredText(record.court_filename, "法院文件名格式不正确", FILE_NAME_MAX_LENGTH), mediaType: requiredText(record.media_type, "组成文书格式不正确", 40), artifactSha256: requiredSha(record.artifact_sha256, "组成文书哈希格式不正确"), byteSize: requiredPositiveInteger(record.byte_size, "组成文书大小格式不正确", 256 * 1024 * 1024) };
}

function parseSubmissionExport(value: unknown): WebSubmissionExport {
  const record = asRecord(value, "法院提交导出格式不正确");
  return { exportId: normalizeOpaqueId(record.export_id, "法院导出编号"), bundleId: normalizeOpaqueId(record.bundle_id, "导出材料包编号"), inputHash: requiredSha(record.input_hash, "导出输入哈希格式不正确"), courtZipSha256: requiredSha(record.court_zip_sha256, "法院压缩包哈希格式不正确"), courtZipBytes: requiredPositiveInteger(record.court_zip_bytes, "法院压缩包大小格式不正确", 256 * 1024 * 1024), internalManifestSha256: requiredSha(record.internal_manifest_sha256, "内部清单哈希格式不正确"), componentCount: requiredPositiveInteger(record.component_count, "法院导出组成数量格式不正确", 100), verificationHash: requiredSha(record.verification_hash, "法院导出核验哈希格式不正确"), verifiedAt: optionalText(record.verified_at, 80), createdAt: optionalText(record.created_at, 80) };
}

function parseArray<T>(value: unknown, label: string, parser: (item: unknown) => T): T[] {
  if (!Array.isArray(value) || value.length > 100_000) throw protocolError(`${label}列表格式不正确`);
  return value.map(parser);
}

function parseIdArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.length > 10_000) throw protocolError(`${label}列表格式不正确`);
  return value.map((item) => normalizeOpaqueId(item, label));
}

function requiredNonNegativeInteger(value: unknown, message: string): number {
  if (!Number.isInteger(value) || (value as number) < 0 || (value as number) > 1_000_000) throw protocolError(message);
  return value as number;
}

function requiredSafeNonNegativeInteger(value: unknown, message: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw protocolError(message);
  return value as number;
}

function optionalAmount(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const text = typeof value === "string" ? value : String(value);
  if (!/^(?:0|[1-9]\d{0,15})(?:\.\d{1,4})?$/.test(text)) throw protocolError("金额格式不正确");
  return text;
}

function optionalRate(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const text = typeof value === "string" ? value : String(value);
  if (!/^(?:0|[1-9]\d{0,7})(?:\.\d{1,12})?$/.test(text)) throw protocolError("年利率格式不正确");
  return text;
}

function optionalSha(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const text = requiredText(value, "哈希格式不正确", 64).toLowerCase();
  if (!SHA256_PATTERN.test(text)) throw protocolError("哈希格式不正确");
  return text;
}

function requiredSha(value: unknown, message: string): string {
  const text = requiredText(value, message, 64).toLowerCase();
  if (!SHA256_PATTERN.test(text)) throw protocolError(message);
  return text;
}

function normalizeCalculationObligationId(value: unknown): string {
  if (typeof value !== "string") throw new Error("金额核对事项格式不正确。");
  const normalized = value.trim();
  if (normalized.length < 1 || normalized.length > 160 || containsControlCharacter(normalized) || normalized.includes("/")) {
    throw new Error("金额核对事项格式不正确。");
  }
  return normalized;
}

function normalizeIsoDate(value: string, label: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error(`${label}格式不正确。`);
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== value) throw new Error(`${label}格式不正确。`);
  return value;
}

async function readJsonResponse(response: Response, operation: string): Promise<unknown> {
  const requestId = normalizedRequestId(response.headers.get("x-request-id"));
  if (!response.ok) {
    let message = messageForStatus(operation, response.status);
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.toLowerCase().includes("application/json")) {
      try {
        const payload: unknown = await response.json();
        if (payload !== null && typeof payload === "object" && !Array.isArray(payload)) {
          const error = (payload as Record<string, unknown>).error;
          if (error !== null && typeof error === "object" && !Array.isArray(error)) {
            const code = (error as Record<string, unknown>).code;
            message = webDocumentDraftErrorMessage(code)
              ?? webAgentLedgerExceptionCapacityMessage(code)
              ?? message;
          }
        }
      } catch {
        // An invalid error body never turns a deterministic HTTP rejection
        // into an unknown write outcome; the status-owned fallback remains.
      }
    }
    throw new WebLawyerApiError(message, {
      status: response.status,
      requestId,
    });
  }
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new WebLawyerApiError(`${operation}未返回可核验的 JSON 回执。`, {
      status: response.status,
      requestId,
    });
  }
  try {
    return await response.json();
  } catch {
    throw new WebLawyerApiError(`${operation}返回内容无法解析。`, {
      status: response.status,
      requestId,
    });
  }
}

function messageForStatus(operation: string, status: number): string {
  if (status === 401 || status === 403) return "登录状态或案件权限已失效。";
  if ([400, 413, 415, 422].includes(status)) return `${operation}未被服务端接收；请核对文件类型、大小与当前案件状态。`;
  if (status === 409) {
    return operation === "上传 PDF"
      ? "材料接收结果正在服务端核验；请勿重复上传，先刷新案件材料记录。"
      : `${operation}时案件状态已变化；请刷新后再继续。`;
  }
  if (status === 429) return `${operation}请求过于频繁；请稍后再试。`;
  return `${operation}暂未完成。`;
}

function webDocumentDraftErrorMessage(code: unknown): string | null {
  if (code === "DOCUMENT_DRAFT_RENDERER_UNAVAILABLE") {
    return "隔离文书渲染服务暂不可用；本次没有生成候选。";
  }
  if (code === "DOCUMENT_DRAFT_RENDERER_RESULT_UNKNOWN") {
    return "文书候选生成结果尚未确认；系统未自动重试，请先刷新候选台账。";
  }
  return null;
}

function asRecord(value: unknown, message: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw protocolError(message);
  }
  return value as Record<string, unknown>;
}

function requiredText(value: unknown, message: string, maxLength: number): string {
  if (typeof value !== "string") throw protocolError(message);
  const normalized = value.trim();
  if (normalized.length === 0 || normalized.length > maxLength || containsControlCharacter(normalized)) {
    throw protocolError(message);
  }
  return normalized;
}

/**
 * 多行文本且允许为空：文书正文/报告含换行，不能套用单行文本的校验。
 * 其余控制字符仍然是协议违规。
 */
function optionalMultilineText(value: unknown, maxLength: number): string {
  if (value === null || value === undefined) return "";
  if (typeof value !== "string") throw protocolError("服务端返回的文本字段格式不正确");
  const normalized = value.trim();
  if (normalized.length > maxLength
      || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(normalized)) {
    throw protocolError("服务端返回的文本字段格式不正确");
  }
  return normalized;
}

function requiredMultilineText(value: unknown, message: string, maxLength: number): string {
  if (typeof value !== "string") throw protocolError(message);
  const normalized = value.trim();
  // Artifact details deliberately preserve line breaks: the server composes
  // lawyer-facing evidence gaps, adversarial routes and blockers as separate
  // readable lines. All other control characters remain protocol violations.
  if (
    normalized.length === 0
    || normalized.length > maxLength
    || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(normalized)
  ) {
    throw protocolError(message);
  }
  return normalized;
}

function normalizeLawyerDecisionText(value: string, label: string, maxLength: number): string {
  const normalized = value.trim();
  if (
    normalized.length === 0
    || normalized.length > maxLength
    || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(normalized)
  ) {
    throw new Error(`${label}内容无效。`);
  }
  return normalized;
}

function normalizeDistinctDecisionIds(values: readonly string[], label: string, maximum: number): string[] {
  if (values.length === 0 || values.length > maximum) {
    throw new Error(`${label}至少选择一项，且不能超过 ${maximum} 项。`);
  }
  const normalized = values.map((value) => normalizeOpaqueId(value, `${label}编号`));
  if (new Set(normalized).size !== normalized.length) {
    throw new Error(`${label}不能重复选择。`);
  }
  return normalized;
}

function requiredEvidenceExcerptText(value: unknown, message: string, maxLength: number): string {
  if (typeof value !== "string") throw protocolError(message);
  const normalized = value.replace(/\r\n?/g, "\n").trim();
  if (
    normalized.length === 0
    || normalized.length > maxLength
    || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(normalized)
  ) {
    throw protocolError(message);
  }
  return normalized;
}

function optionalText(value: unknown, maxLength: number): string | null {
  if (value === null || value === undefined) return null;
  return requiredText(value, "服务端返回的文本字段格式不正确", maxLength);
}

/**
 * 空字符串是合法缺省值：尚未分析时 stage/gate_level/error 都是空的。
 * 这里只拒绝超长与非文本，避免把「没有值」误判成协议错误。
 */
function optionalTextAllowEmpty(value: unknown, maxLength: number): string {
  if (value === null || value === undefined) return "";
  if (typeof value !== "string") throw protocolError("服务端返回的文本字段格式不正确");
  const normalized = value.trim();
  if (normalized.length > maxLength || containsControlCharacter(normalized)) {
    throw protocolError("服务端返回的文本字段格式不正确");
  }
  return normalized;
}

function optionalHttpsUrl(value: unknown, message: string): string | null {
  if (value === null || value === undefined) return null;
  const text = requiredText(value, message, 2_048);
  let parsed: URL;
  try { parsed = new URL(text); }
  catch { throw protocolError(message); }
  if (parsed.protocol !== "https:" || !parsed.hostname || parsed.username || parsed.password || parsed.hash) {
    throw protocolError(message);
  }
  return text;
}

function requiredStringArray(value: unknown, message: string, maxItems: number, itemMaxLength: number): string[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > maxItems) {
    throw protocolError(message);
  }
  const values = value.map((item) => requiredText(item, message, itemMaxLength));
  if (new Set(values).size !== values.length) throw protocolError(message);
  return values;
}

function boundedStringArray(value: unknown, message: string, maxItems: number, itemMaxLength: number): string[] {
  if (!Array.isArray(value) || value.length > maxItems) throw protocolError(message);
  const values = value.map((item) => requiredText(item, message, itemMaxLength));
  if (new Set(values).size !== values.length) throw protocolError(message);
  return values;
}

function optionalBoolean(value: unknown, fallback: boolean, message: string): boolean {
  if (value === null || value === undefined) return fallback;
  return requiredBoolean(value, message);
}

function requiredBoolean(value: unknown, message: string): boolean {
  if (typeof value !== "boolean") throw protocolError(message);
  return value;
}

function requiredPositiveInteger(value: unknown, message: string, maxValue: number): number {
  if (!Number.isInteger(value) || (value as number) < 1 || (value as number) > maxValue) {
    throw protocolError(message);
  }
  return value as number;
}

function optionalNonNegativeInteger(value: unknown, maxValue: number): number | null {
  if (value === null || value === undefined) return null;
  if (!Number.isInteger(value) || (value as number) < 0 || (value as number) > maxValue) {
    throw protocolError("服务端返回的材料数量格式不正确");
  }
  return value as number;
}

function normalizeOpaqueId(value: unknown, label: string): string {
  if (typeof value !== "string" || !OPAQUE_ID_PATTERN.test(value)) {
    throw protocolError(`${label}格式不正确`);
  }
  return value;
}

function optionalOpaqueId(value: unknown, label: string): string | null {
  if (value === null || value === undefined) return null;
  return normalizeOpaqueId(value, label);
}

function normalizeIdempotencyKey(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9._~-]{16,128}$/.test(value)) {
    throw new Error("建案请求编号无效，操作已停止。");
  }
  return value;
}

function normalizeClientFilename(value: string): string {
  const normalized = value.trim();
  if (
    normalized.length === 0
    || normalized.length > FILE_NAME_MAX_LENGTH
    || containsControlCharacter(normalized)
    || normalized.includes("/")
    || normalized.includes("\\")
  ) {
    throw new Error("文件名称无效，未创建材料接收位。");
  }
  return normalized;
}

function containsControlCharacter(value: string): boolean {
  return /[\u0000-\u001f\u007f]/.test(value);
}

function normalizedRequestId(value: string | null): string | null {
  if (value === null) return null;
  const normalized = value.trim();
  if (normalized.length === 0 || normalized.length > 128 || containsControlCharacter(normalized)) return null;
  return normalized;
}

function protocolError(message: string): WebLawyerApiError {
  return new WebLawyerApiError(message, { status: null, requestId: null });
}
