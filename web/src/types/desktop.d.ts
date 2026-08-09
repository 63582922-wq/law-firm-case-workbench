export {};

declare global {
  interface Window {
    lawCaseDesktop?: {
      selectCaseFolder(input: { matterId: string }): Promise<{ selectedRoot: string } | null>;
    };
  }
}
