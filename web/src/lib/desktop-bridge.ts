import { invoke } from "@tauri-apps/api/core";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const TAURI_BOOTSTRAP_WAIT_MS = 900;
const TAURI_BOOTSTRAP_POLL_MS = 45;

/**
 * Tauri injects its native bridge shortly after the document is created.  The
 * packaged application is still recognisable from its reserved local origin
 * during that small interval, so the UI can show a neutral loading state
 * instead of briefly rendering the browser-only demonstration case.
 */
export function isDesktopNativeShell(): boolean {
  if (typeof window === "undefined") return false;
  if (window.__TAURI_INTERNALS__ !== undefined) return true;
  return window.location.protocol === "tauri:" || window.location.hostname === "tauri.localhost";
}

async function waitForDesktopNativeBridge(): Promise<boolean> {
  if (typeof window === "undefined" || !isDesktopNativeShell()) return false;
  if (window.__TAURI_INTERNALS__ !== undefined) return true;

  const deadline = Date.now() + TAURI_BOOTSTRAP_WAIT_MS;
  while (Date.now() < deadline) {
    await new Promise<void>((resolve) => window.setTimeout(resolve, TAURI_BOOTSTRAP_POLL_MS));
    if (window.__TAURI_INTERNALS__ !== undefined) return true;
  }
  return false;
}

export type SelectedCaseFolder = {
  selectedRoot: string;
};

export type DesktopRuntimeStatus = {
  phase: "STARTING" | "READY" | "BLOCKED" | "STOPPED";
  message: string;
  apiBase: string | null;
  processId: number | null;
  identityPhase: "NOT_ENROLLED" | "BLOCKED" | "ENROLLED" | "LOCAL" | "UNAVAILABLE" | "UNKNOWN";
  enrollmentTrustPhase: "NOT_CONFIGURED" | "BLOCKED" | "READY" | "UNAVAILABLE" | "UNKNOWN";
  sessionPhase: "NOT_AVAILABLE" | "STARTING" | "READY" | "EXPIRED" | "UNAVAILABLE" | "UNKNOWN";
  sessionExpiresAt: string | null;
  persistencePhase: "NOT_CONFIGURED" | "CONFIGURED" | "LOCAL_CONFIGURED" | "UNAVAILABLE" | "UNKNOWN";
  evidenceIntakeWorkerPhase: "NOT_CONFIGURED" | "ASSEMBLED" | "UNAVAILABLE" | "UNKNOWN";
  officialSourceCaptureWorkerPhase: "NOT_CONFIGURED" | "ASSEMBLED" | "UNAVAILABLE" | "UNKNOWN";
  /**
   * A local, single-lawyer workspace is intentionally separate from the
   * optional firm-managed deployment.  Older packages do not expose these
   * fields, so callers must treat their absence as unavailable rather than
   * assuming a local case can be opened.
   */
  workspaceMode?: "LOCAL_STANDALONE" | "FIRM_MANAGED" | "SYNTHETIC_ALPHA" | "UNAVAILABLE";
  localWorkspacePhase?: "NOT_CONFIGURED" | "READY" | "UNAVAILABLE" | "UNKNOWN";
};

/**
 * Opaque result of a native folder selection.  The selected absolute path is
 * deliberately kept in the native shell and never enters the WebView.
 */
export type LocalCaseFolderSelection = {
  selectionId: string;
  displayName: string;
  rootFingerprint: string;
  selectedAt: string;
};

export type LocalCaseMaterialRoot = {
  displayName: string;
  rootFingerprint: string;
  linkedAt: string;
};

export type LocalCaseFolderInventory = {
  scanId: string;
  rootFingerprint: string;
  manifestHash: string;
  scannedAt: string;
  totalFiles: number;
  totalBytes: number;
  skippedSymlinks: number;
};

/**
 * A local-only case registry item.  This is case metadata, not extracted
 * evidence, a legal conclusion, or a claim that any material was read.
 */
export type LocalCaseSummary = {
  caseId: string;
  title: string;
  stage: "MATERIALS_PENDING" | "MATERIALS_INVENTORIED";
  matterVersion: number;
  materialRoot: LocalCaseMaterialRoot;
  inventory: LocalCaseFolderInventory | null;
  createdAt: string;
  updatedAt: string;
};

export type DesktopSessionGrant = {
  apiBase: string;
  accessToken: string;
  sessionId: string;
  expiresAt: string;
};

export type AuthorizedQwenOcrResult = {
  candidateId: string;
  matterVersion: number;
};

/**
 * The desktop bridge accepts only these product-level task choices.  It never
 * accepts a browser-supplied prompt, document text, URL, file path, model
 * name, or Tool invocation.
 */
export type CasePlanTaskKind =
  | "case_intake"
  | "evidence_review"
  | "legal_research"
  | "interest_review"
  | "document_review";

export type AuthorizedDeepSeekCasePlanResult = {
  runId: string;
  matterVersion: number;
  proposalCount: number;
};

export type DesktopEnrollmentVaultStatus = {
  phase:
    | "NOT_INITIALIZED"
    | "INSTALLATION_READY"
    | "CREDENTIAL_SAVED_VERIFIED"
    | "CREDENTIAL_PRESENT_UNVERIFIED"
    | "BROKEN_LOCAL_CREDENTIAL"
    | "REMOTE_REVOKED_CONFIRMED"
    | "REMOTE_OPERATION_PENDING"
    | "REMOTE_OPERATION_REJECTED"
    | "LOCAL_DISABLED_REMOTE_REVOCATION_UNCONFIRMED"
    | "UNAVAILABLE";
  message: string;
  installationInitialized: boolean;
  enrollmentEnvelopePresent: boolean;
};

export type DesktopModelProviderStatus = {
  providerId: "deepseek" | "qwen";
  displayName: string;
  modelId: string;
  /**
   * This is a non-secret local record.  It never means this status request
   * read or confirmed a Keychain credential.
   */
  configurationState:
    | "NOT_CHECKED"
    | "CONFIGURATION_RECORDED"
    | "VALIDATED_FOR_CURRENT_SESSION"
    | "NOT_CONFIGURED";
  configured: boolean;
  connectionReady: boolean;
  connectionLabel: string;
};

export async function readDesktopRuntimeStatus(): Promise<DesktopRuntimeStatus | null> {
  if (!await waitForDesktopNativeBridge()) return null;
  return invoke<DesktopRuntimeStatus>("desktop_runtime_status");
}

export async function readDesktopEnrollmentVaultStatus(): Promise<DesktopEnrollmentVaultStatus | null> {
  if (typeof window === "undefined" || window.__TAURI_INTERNALS__ === undefined) return null;
  return invoke<DesktopEnrollmentVaultStatus>("desktop_enrollment_vault_status");
}

export async function readDesktopModelProviderStatuses(): Promise<DesktopModelProviderStatus[] | null> {
  if (typeof window === "undefined" || window.__TAURI_INTERNALS__ === undefined) return null;
  return invoke<DesktopModelProviderStatus[]>("desktop_model_provider_statuses");
}

export async function configureDesktopModelProviderKey(
  providerId: DesktopModelProviderStatus["providerId"],
): Promise<DesktopModelProviderStatus> {
  return invoke<DesktopModelProviderStatus>("configure_desktop_model_provider_key", { providerId });
}

export async function configureDesktopQwenConnection(
  regionId: "cn-beijing" | "ap-southeast-1",
  workspaceId: string,
): Promise<DesktopModelProviderStatus> {
  return invoke<DesktopModelProviderStatus>("configure_desktop_qwen_connection", { regionId, workspaceId });
}

export async function removeDesktopModelProviderKey(
  providerId: DesktopModelProviderStatus["providerId"],
): Promise<DesktopModelProviderStatus> {
  return invoke<DesktopModelProviderStatus>("remove_desktop_model_provider_key", { providerId });
}

export async function readDesktopSessionGrant(): Promise<DesktopSessionGrant> {
  return invoke<DesktopSessionGrant>("desktop_session_grant");
}

export async function selectLocalCaseFolder(): Promise<LocalCaseFolderSelection | null> {
  const result = await invoke<LocalCaseFolderSelection | null>("select_local_case_folder");
  return result === null ? null : validateLocalCaseFolderSelection(result);
}

export async function createLocalCase(input: {
  title: string;
  selectionId: string;
}): Promise<LocalCaseSummary> {
  const title = input.title.trim();
  if (title.length < 2 || title.length > 160) {
    throw new Error("案件名称应为 2 至 160 个字符。");
  }
  if (!UUID_PATTERN.test(input.selectionId)) {
    throw new Error("本机资料文件夹选择已失效；请重新选择。");
  }
  return validateLocalCaseSummary(await invoke<LocalCaseSummary>("create_local_case", {
    input: {
      title,
      selectionId: input.selectionId,
    },
  }));
}

export async function listLocalCases(): Promise<LocalCaseSummary[]> {
  const result = await invoke<LocalCaseSummary[]>("list_local_cases");
  if (!Array.isArray(result)) throw new Error("本机案件目录回执无效，未显示任何案件。");
  return result.map(validateLocalCaseSummary);
}

export async function openLocalCase(caseId: string): Promise<LocalCaseSummary> {
  if (!UUID_PATTERN.test(caseId)) throw new Error("本机案件标识无效，未打开案件。");
  return validateLocalCaseSummary(await invoke<LocalCaseSummary>("open_local_case", {
    input: { caseId },
  }));
}

export async function reconnectLocalCaseFolder(input: {
  caseId: string;
  selectionId: string;
}): Promise<LocalCaseSummary> {
  if (!UUID_PATTERN.test(input.caseId) || !UUID_PATTERN.test(input.selectionId)) {
    throw new Error("本机案件或资料文件夹选择无效，未关联任何文件夹。");
  }
  return validateLocalCaseSummary(await invoke<LocalCaseSummary>("reconnect_local_case_folder", {
    input: {
      caseId: input.caseId,
      selectionId: input.selectionId,
    },
  }));
}

export async function inventoryLocalCaseFolder(input: {
  caseId: string;
  selectionId: string;
}): Promise<LocalCaseSummary> {
  if (!UUID_PATTERN.test(input.caseId) || !UUID_PATTERN.test(input.selectionId)) {
    throw new Error("本机案件或资料文件夹选择无效，未开始材料盘点。");
  }
  return validateLocalCaseSummary(await invoke<LocalCaseSummary>("inventory_local_case_folder", {
    input: {
      caseId: input.caseId,
      selectionId: input.selectionId,
    },
  }));
}

function validateLocalCaseFolderSelection(value: unknown): LocalCaseFolderSelection {
  if (!isRecord(value)
    || !UUID_PATTERN.test(stringField(value, "selectionId"))
    || !isDisplayLabel(stringField(value, "displayName"))
    || !isFingerprint(stringField(value, "rootFingerprint"))
    || !isIsoTimestamp(stringField(value, "selectedAt"))) {
    throw new Error("本机资料文件夹回执无效；未建立案件，也没有读取文件。");
  }
  return {
    selectionId: stringField(value, "selectionId"),
    displayName: stringField(value, "displayName"),
    rootFingerprint: stringField(value, "rootFingerprint"),
    selectedAt: stringField(value, "selectedAt"),
  };
}

function validateLocalCaseSummary(value: unknown): LocalCaseSummary {
  if (!isRecord(value) || !isRecord(value.materialRoot)) {
    throw new Error("本机案件回执无效；未切换到任何案件。");
  }
  const materialRoot = value.materialRoot;
  if (!UUID_PATTERN.test(stringField(value, "caseId"))
    || !isCaseTitle(stringField(value, "title"))
    || (value.stage !== "MATERIALS_PENDING" && value.stage !== "MATERIALS_INVENTORIED")
    || !Number.isInteger(value.matterVersion)
    || (value.matterVersion as number) < 1
    || !isDisplayLabel(stringField(materialRoot, "displayName"))
    || !isFingerprint(stringField(materialRoot, "rootFingerprint"))
    || !isIsoTimestamp(stringField(materialRoot, "linkedAt"))
    || !isIsoTimestamp(stringField(value, "createdAt"))
    || !isIsoTimestamp(stringField(value, "updatedAt"))) {
    throw new Error("本机案件回执字段无效；未切换到任何案件。");
  }
  const inventory = validateLocalCaseInventory(value.inventory);
  if (value.stage === "MATERIALS_INVENTORIED" && inventory === null) {
    throw new Error("本机材料盘点回执不完整；未将材料标记为已盘点。");
  }
  if (value.stage === "MATERIALS_PENDING" && inventory !== null) {
    throw new Error("本机案件状态与材料盘点回执不一致；未显示任何盘点结果。");
  }
  if (inventory !== null && inventory.rootFingerprint !== stringField(materialRoot, "rootFingerprint")) {
    throw new Error("本机材料盘点资料根与当前案件不一致；未切换到任何案件。");
  }
  return {
    caseId: stringField(value, "caseId"),
    title: stringField(value, "title"),
    stage: value.stage,
    matterVersion: value.matterVersion as number,
    materialRoot: {
      displayName: stringField(materialRoot, "displayName"),
      rootFingerprint: stringField(materialRoot, "rootFingerprint"),
      linkedAt: stringField(materialRoot, "linkedAt"),
    },
    inventory,
    createdAt: stringField(value, "createdAt"),
    updatedAt: stringField(value, "updatedAt"),
  };
}

function validateLocalCaseInventory(value: unknown): LocalCaseFolderInventory | null {
  if (value === undefined || value === null) return null;
  if (!isRecord(value)
    || !UUID_PATTERN.test(stringField(value, "scanId"))
    || !isFingerprint(stringField(value, "rootFingerprint"))
    || !isFingerprint(stringField(value, "manifestHash"))
    || !isIsoTimestamp(stringField(value, "scannedAt"))
    || !isNonNegativeInteger(value.totalFiles)
    || !isNonNegativeInteger(value.totalBytes)
    || !isNonNegativeInteger(value.skippedSymlinks)) {
    throw new Error("本机材料盘点回执无效；没有显示盘点结果。");
  }
  return {
    scanId: stringField(value, "scanId"),
    rootFingerprint: stringField(value, "rootFingerprint"),
    manifestHash: stringField(value, "manifestHash"),
    scannedAt: stringField(value, "scannedAt"),
    totalFiles: value.totalFiles as number,
    totalBytes: value.totalBytes as number,
    skippedSymlinks: value.skippedSymlinks as number,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringField(value: Record<string, unknown>, key: string): string {
  return typeof value[key] === "string" ? value[key] : "";
}

function isDisplayLabel(value: string): boolean {
  return value.trim().length >= 1 && value.trim().length <= 240 && !/[\u0000-\u001F]/.test(value);
}

function isCaseTitle(value: string): boolean {
  return value.trim().length >= 2 && value.trim().length <= 160 && !/[\u0000-\u001F]/.test(value);
}

function isFingerprint(value: string): boolean {
  return /^[a-f0-9]{32,128}$/i.test(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isIsoTimestamp(value: string): boolean {
  return value.length <= 64 && Number.isFinite(Date.parse(value));
}

export async function executeAuthorizedQwenOcr(input: {
  matterId: string;
  evidencePageId: string;
  folderGrantId: string;
  externalRequestId: string;
  expectedVersion: number;
}): Promise<AuthorizedQwenOcrResult> {
  for (const value of [input.matterId, input.evidencePageId, input.folderGrantId, input.externalRequestId]) {
    if (!UUID_PATTERN.test(value)) throw new Error("OCR 执行标识无效；未发送任何案卷内容。");
  }
  if (!Number.isInteger(input.expectedVersion) || input.expectedVersion < 1) {
    throw new Error("OCR 授权版本无效；未发送任何案卷内容。");
  }
  return invoke<AuthorizedQwenOcrResult>("execute_authorized_qwen_ocr", { input });
}

export async function executeAuthorizedDeepSeekCasePlan(input: {
  matterId: string;
  externalRequestId: string;
  expectedVersion: number;
  taskKind: CasePlanTaskKind;
}): Promise<AuthorizedDeepSeekCasePlanResult> {
  for (const value of [input.matterId, input.externalRequestId]) {
    if (!UUID_PATTERN.test(value)) throw new Error("案件计划授权标识无效；未发送任何案件内容。");
  }
  if (!Number.isInteger(input.expectedVersion) || input.expectedVersion < 1) {
    throw new Error("案件计划授权版本无效；未发送任何案件内容。");
  }
  if (!(["case_intake", "evidence_review", "legal_research", "interest_review", "document_review"] as const).includes(input.taskKind)) {
    throw new Error("案件计划任务无效；未发送任何案件内容。");
  }
  return invoke<AuthorizedDeepSeekCasePlanResult>("execute_authorized_deepseek_case_plan", { input });
}

export async function initializeDesktopInstallation(): Promise<DesktopEnrollmentVaultStatus> {
  return invoke<DesktopEnrollmentVaultStatus>("initialize_desktop_installation", {
    confirmation: "INIT_LOCAL_KEYCHAIN",
  });
}

export async function disableLocalEnrollment(): Promise<DesktopEnrollmentVaultStatus> {
  return invoke<DesktopEnrollmentVaultStatus>("disable_local_enrollment", {
    confirmation: "DISABLE_LOCAL_ENROLLMENT",
  });
}

export async function importSignedEnrollmentPackage(): Promise<DesktopEnrollmentVaultStatus> {
  return invoke<DesktopEnrollmentVaultStatus>("import_signed_enrollment_package");
}

export async function activateDesktopEnrollment(): Promise<DesktopEnrollmentVaultStatus> {
  return invoke<DesktopEnrollmentVaultStatus>("activate_desktop_enrollment");
}

export async function renewDesktopEnrollment(): Promise<DesktopEnrollmentVaultStatus> {
  return invoke<DesktopEnrollmentVaultStatus>("renew_desktop_enrollment");
}

export async function revokeDesktopEnrollment(): Promise<DesktopEnrollmentVaultStatus> {
  return invoke<DesktopEnrollmentVaultStatus>("revoke_desktop_enrollment");
}

export async function resolvePendingDesktopEnrollment(): Promise<DesktopEnrollmentVaultStatus> {
  return invoke<DesktopEnrollmentVaultStatus>("resolve_pending_desktop_enrollment");
}

export function installDesktopBridge(): void {
  if (typeof window === "undefined" || window.__TAURI_INTERNALS__ === undefined || window.lawCaseDesktop) {
    return;
  }

  window.lawCaseDesktop = {
    runtimeStatus: readDesktopRuntimeStatus,
    sessionGrant: readDesktopSessionGrant,
    enrollmentVaultStatus: readDesktopEnrollmentVaultStatus,
    modelProviderStatuses: readDesktopModelProviderStatuses,
    configureModelProviderKey: configureDesktopModelProviderKey,
    configureQwenConnection: configureDesktopQwenConnection,
    executeAuthorizedQwenOcr,
    executeAuthorizedDeepSeekCasePlan,
    removeModelProviderKey: removeDesktopModelProviderKey,
    initializeInstallation: initializeDesktopInstallation,
    importSignedEnrollmentPackage,
    activateEnrollment: activateDesktopEnrollment,
    renewEnrollment: renewDesktopEnrollment,
    revokeEnrollment: revokeDesktopEnrollment,
    resolvePendingEnrollment: resolvePendingDesktopEnrollment,
    disableLocalEnrollment,
    selectLocalCaseFolder,
    createLocalCase,
    listLocalCases,
    openLocalCase,
    reconnectLocalCaseFolder,
    inventoryLocalCaseFolder,
    async selectCaseFolder({ matterId }) {
      if (!UUID_PATTERN.test(matterId)) {
        throw new Error("案件标识无效，未打开本机文件夹选择器。");
      }

      return invoke<SelectedCaseFolder | null>("select_case_folder", { matterId });
    },
  };
}
