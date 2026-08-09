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
};

export async function readDesktopRuntimeStatus(): Promise<DesktopRuntimeStatus | null> {
  if (typeof window === "undefined" || window.__TAURI_INTERNALS__ === undefined) return null;
  return invoke<DesktopRuntimeStatus>("desktop_runtime_status");
}

export function installDesktopBridge(): void {
  if (typeof window === "undefined" || window.__TAURI_INTERNALS__ === undefined || window.lawCaseDesktop) {
    return;
  }

  window.lawCaseDesktop = {
    runtimeStatus: readDesktopRuntimeStatus,
    async selectCaseFolder({ matterId }) {
      if (!UUID_PATTERN.test(matterId)) {
        throw new Error("案件标识无效，未打开本机文件夹选择器。");
      }

      return invoke<SelectedCaseFolder | null>("select_case_folder", { matterId });
    },
  };
}
