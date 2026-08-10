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
        identityPhase: "NOT_ENROLLED" | "UNAVAILABLE" | "UNKNOWN";
        persistencePhase: "NOT_CONFIGURED" | "UNAVAILABLE" | "UNKNOWN";
      } | null>;
      enrollmentVaultStatus(): Promise<{
        phase:
          | "NOT_INITIALIZED"
          | "INSTALLATION_READY"
          | "CREDENTIAL_PRESENT_UNVERIFIED"
          | "BROKEN_LOCAL_CREDENTIAL"
          | "LOCAL_DISABLED_REMOTE_REVOCATION_UNCONFIRMED"
          | "UNAVAILABLE";
        message: string;
        installationInitialized: boolean;
        enrollmentEnvelopePresent: boolean;
      } | null>;
      initializeInstallation(): Promise<{
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
