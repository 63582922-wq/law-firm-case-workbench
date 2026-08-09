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
      } | null>;
      selectCaseFolder(input: { matterId: string }): Promise<{ selectedRoot: string } | null>;
    };
  }
}
