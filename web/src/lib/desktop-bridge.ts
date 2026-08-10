import { invoke } from "@tauri-apps/api/core";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export type SelectedCaseFolder = {
  selectedRoot: string;
};

export type DesktopRuntimeStatus = {
  phase: "STARTING" | "READY" | "BLOCKED" | "STOPPED";
  message: string;
  apiBase: string | null;
  processId: number | null;
  identityPhase: "NOT_ENROLLED" | "BLOCKED" | "ENROLLED" | "UNAVAILABLE" | "UNKNOWN";
  enrollmentTrustPhase: "NOT_CONFIGURED" | "BLOCKED" | "READY" | "UNAVAILABLE" | "UNKNOWN";
  sessionPhase: "NOT_AVAILABLE" | "STARTING" | "READY" | "EXPIRED" | "UNAVAILABLE" | "UNKNOWN";
  sessionExpiresAt: string | null;
  persistencePhase: "NOT_CONFIGURED" | "CONFIGURED" | "UNAVAILABLE" | "UNKNOWN";
};

export type DesktopSessionGrant = {
  apiBase: string;
  accessToken: string;
  sessionId: string;
  expiresAt: string;
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

export async function readDesktopRuntimeStatus(): Promise<DesktopRuntimeStatus | null> {
  if (typeof window === "undefined" || window.__TAURI_INTERNALS__ === undefined) return null;
  return invoke<DesktopRuntimeStatus>("desktop_runtime_status");
}

export async function readDesktopEnrollmentVaultStatus(): Promise<DesktopEnrollmentVaultStatus | null> {
  if (typeof window === "undefined" || window.__TAURI_INTERNALS__ === undefined) return null;
  return invoke<DesktopEnrollmentVaultStatus>("desktop_enrollment_vault_status");
}

export async function readDesktopSessionGrant(): Promise<DesktopSessionGrant> {
  return invoke<DesktopSessionGrant>("desktop_session_grant");
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
    initializeInstallation: initializeDesktopInstallation,
    importSignedEnrollmentPackage,
    activateEnrollment: activateDesktopEnrollment,
    renewEnrollment: renewDesktopEnrollment,
    revokeEnrollment: revokeDesktopEnrollment,
    resolvePendingEnrollment: resolvePendingDesktopEnrollment,
    disableLocalEnrollment,
    async selectCaseFolder({ matterId }) {
      if (!UUID_PATTERN.test(matterId)) {
        throw new Error("案件标识无效，未打开本机文件夹选择器。");
      }

      return invoke<SelectedCaseFolder | null>("select_case_folder", { matterId });
    },
  };
}
