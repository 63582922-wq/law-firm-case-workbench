export {};

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
    lawCaseDesktop?: {
      runtimeStatus(): Promise<{
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
        workspaceMode?: "LOCAL_STANDALONE" | "FIRM_MANAGED" | "SYNTHETIC_ALPHA" | "UNAVAILABLE";
        localWorkspacePhase?: "NOT_CONFIGURED" | "READY" | "UNAVAILABLE" | "UNKNOWN";
      } | null>;
      sessionGrant(): Promise<{
        apiBase: string;
        accessToken: string;
        sessionId: string;
        expiresAt: string;
      }>;
      enrollmentVaultStatus(): Promise<{
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
      } | null>;
      modelProviderStatuses(): Promise<Array<{
        providerId: "deepseek" | "qwen";
        displayName: string;
        modelId: string;
        configurationState: "NOT_CHECKED" | "CONFIGURATION_RECORDED" | "VALIDATED_FOR_CURRENT_SESSION" | "NOT_CONFIGURED";
        configured: boolean;
        connectionReady: boolean;
        connectionLabel: string;
      }> | null>;
      configureModelProviderKey(providerId: "deepseek" | "qwen"): Promise<{
        providerId: "deepseek" | "qwen";
        displayName: string;
        modelId: string;
        configurationState: "NOT_CHECKED" | "CONFIGURATION_RECORDED" | "VALIDATED_FOR_CURRENT_SESSION" | "NOT_CONFIGURED";
        configured: boolean;
        connectionReady: boolean;
        connectionLabel: string;
      }>;
      configureQwenConnection(regionId: "cn-beijing" | "ap-southeast-1", workspaceId: string): Promise<{
        providerId: "deepseek" | "qwen";
        displayName: string;
        modelId: string;
        configurationState: "NOT_CHECKED" | "CONFIGURATION_RECORDED" | "VALIDATED_FOR_CURRENT_SESSION" | "NOT_CONFIGURED";
        configured: boolean;
        connectionReady: boolean;
        connectionLabel: string;
      }>;
      executeAuthorizedQwenOcr(input: {
        matterId: string;
        evidencePageId: string;
        folderGrantId: string;
        externalRequestId: string;
        expectedVersion: number;
      }): Promise<{
        candidateId: string;
        matterVersion: number;
      }>;
      executeAuthorizedDeepSeekCasePlan(input: {
        matterId: string;
        externalRequestId: string;
        expectedVersion: number;
        taskKind: "case_intake" | "evidence_review" | "legal_research" | "interest_review" | "document_review";
      }): Promise<{
        runId: string;
        matterVersion: number;
        proposalCount: number;
      }>;
      removeModelProviderKey(providerId: "deepseek" | "qwen"): Promise<{
        providerId: "deepseek" | "qwen";
        displayName: string;
        modelId: string;
        configurationState: "NOT_CHECKED" | "CONFIGURATION_RECORDED" | "VALIDATED_FOR_CURRENT_SESSION" | "NOT_CONFIGURED";
        configured: boolean;
        connectionReady: boolean;
        connectionLabel: string;
      }>;
      initializeInstallation(): Promise<{
        phase: string;
        message: string;
        installationInitialized: boolean;
        enrollmentEnvelopePresent: boolean;
      }>;
      importSignedEnrollmentPackage(): Promise<{
        phase: string;
        message: string;
        installationInitialized: boolean;
        enrollmentEnvelopePresent: boolean;
      }>;
      activateEnrollment(): Promise<{
        phase: string;
        message: string;
        installationInitialized: boolean;
        enrollmentEnvelopePresent: boolean;
      }>;
      renewEnrollment(): Promise<{
        phase: string;
        message: string;
        installationInitialized: boolean;
        enrollmentEnvelopePresent: boolean;
      }>;
      revokeEnrollment(): Promise<{
        phase: string;
        message: string;
        installationInitialized: boolean;
        enrollmentEnvelopePresent: boolean;
      }>;
      resolvePendingEnrollment(): Promise<{
        phase: string;
        message: string;
        installationInitialized: boolean;
        enrollmentEnvelopePresent: boolean;
      }>;
      disableLocalEnrollment(): Promise<{
        phase: string;
        message: string;
        installationInitialized: boolean;
        enrollmentEnvelopePresent: boolean;
      }>;
      selectLocalCaseFolder(): Promise<{
        selectionId: string;
        displayName: string;
        rootFingerprint: string;
        selectedAt: string;
      } | null>;
      createLocalCase(input: { title: string; selectionId: string }): Promise<{
        caseId: string;
        title: string;
        stage: "MATERIALS_PENDING" | "MATERIALS_INVENTORIED";
        matterVersion: number;
        materialRoot: { displayName: string; rootFingerprint: string; linkedAt: string };
        inventory: { scanId: string; rootFingerprint: string; manifestHash: string; scannedAt: string; totalFiles: number; totalBytes: number; skippedSymlinks: number } | null;
        createdAt: string;
        updatedAt: string;
      }>;
      listLocalCases(): Promise<Array<{
        caseId: string;
        title: string;
        stage: "MATERIALS_PENDING" | "MATERIALS_INVENTORIED";
        matterVersion: number;
        materialRoot: { displayName: string; rootFingerprint: string; linkedAt: string };
        inventory: { scanId: string; rootFingerprint: string; manifestHash: string; scannedAt: string; totalFiles: number; totalBytes: number; skippedSymlinks: number } | null;
        createdAt: string;
        updatedAt: string;
      }>>;
      openLocalCase(caseId: string): Promise<{
        caseId: string;
        title: string;
        stage: "MATERIALS_PENDING" | "MATERIALS_INVENTORIED";
        matterVersion: number;
        materialRoot: { displayName: string; rootFingerprint: string; linkedAt: string };
        inventory: { scanId: string; rootFingerprint: string; manifestHash: string; scannedAt: string; totalFiles: number; totalBytes: number; skippedSymlinks: number } | null;
        createdAt: string;
        updatedAt: string;
      }>;
      reconnectLocalCaseFolder(input: { caseId: string; selectionId: string }): Promise<{
        caseId: string;
        title: string;
        stage: "MATERIALS_PENDING" | "MATERIALS_INVENTORIED";
        matterVersion: number;
        materialRoot: { displayName: string; rootFingerprint: string; linkedAt: string };
        inventory: { scanId: string; rootFingerprint: string; manifestHash: string; scannedAt: string; totalFiles: number; totalBytes: number; skippedSymlinks: number } | null;
        createdAt: string;
        updatedAt: string;
      }>;
      inventoryLocalCaseFolder(input: { caseId: string; selectionId: string }): Promise<{
        caseId: string;
        title: string;
        stage: "MATERIALS_PENDING" | "MATERIALS_INVENTORIED";
        matterVersion: number;
        materialRoot: { displayName: string; rootFingerprint: string; linkedAt: string };
        inventory: { scanId: string; rootFingerprint: string; manifestHash: string; scannedAt: string; totalFiles: number; totalBytes: number; skippedSymlinks: number } | null;
        createdAt: string;
        updatedAt: string;
      }>;
      selectCaseFolder(input: { matterId: string }): Promise<{ selectedRoot: string } | null>;
    };
  }
}
