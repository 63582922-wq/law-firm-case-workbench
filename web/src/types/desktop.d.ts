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
        identityPhase: "NOT_ENROLLED" | "BLOCKED" | "ENROLLED" | "UNAVAILABLE" | "UNKNOWN";
        enrollmentTrustPhase: "NOT_CONFIGURED" | "BLOCKED" | "READY" | "UNAVAILABLE" | "UNKNOWN";
        sessionPhase: "NOT_AVAILABLE" | "STARTING" | "READY" | "EXPIRED" | "UNAVAILABLE" | "UNKNOWN";
        sessionExpiresAt: string | null;
        persistencePhase: "NOT_CONFIGURED" | "CONFIGURED" | "UNAVAILABLE" | "UNKNOWN";
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
        configured: boolean;
      }> | null>;
      configureModelProviderKey(providerId: "deepseek" | "qwen"): Promise<{
        providerId: "deepseek" | "qwen";
        displayName: string;
        modelId: string;
        configured: boolean;
      }>;
      removeModelProviderKey(providerId: "deepseek" | "qwen"): Promise<{
        providerId: "deepseek" | "qwen";
        displayName: string;
        modelId: string;
        configured: boolean;
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
      selectCaseFolder(input: { matterId: string }): Promise<{ selectedRoot: string } | null>;
    };
  }
}
