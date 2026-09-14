export type WebLawyerInitialView =
  | "overview"
  | "evidence"
  | "facts"
  | "legal"
  | "calculation"
  | "analysis"
  | "bundle"
  | "security";

export type WebLawyerViewCapabilities = Readonly<{
  canReviewEvidence: boolean;
  canReviewFacts: boolean;
  canReviewLegal: boolean;
  canRunCalculation: boolean;
  canReviewSubmission: boolean;
  /**
   * Agent 深度分析（决策包）是否已装配。本机模式已实现该链路，故本机会话为真；
   * 受管服务在装配 Agent 运行时后为真。决策包只产出提议与正式数字，因此它不依赖
   * 尚未实现的事实确认/法律审阅步骤，而受管服务仍由这些步骤把守正式成果文件。
   */
  canRunAgent: boolean;
}>;

/**
 * The server-projected count survives route changes and page reloads.  The
 * in-memory receipt is only an immediate same-page bridge while the list is
 * being refreshed after a successful upload.
 */
export function hasRegisteredCaseMaterials(
  materialCount: number,
  hasCurrentPageReceipt = false,
): boolean {
  return hasCurrentPageReceipt || materialCount > 0;
}

export function canOpenWebLawyerView(
  capabilities: WebLawyerViewCapabilities,
  view: WebLawyerInitialView,
  hasCase: boolean,
  hasMaterials = true,
): boolean {
  if (view === "overview") return true;
  if (!hasCase) return false;
  if (view === "evidence") return capabilities.canReviewEvidence;
  if (!hasMaterials) return false;
  if (view === "facts") return capabilities.canReviewFacts;
  if (view === "legal") return capabilities.canReviewLegal;
  if (view === "calculation") return capabilities.canReviewLegal && capabilities.canRunCalculation;
  if (view === "analysis") return capabilities.canRunAgent;
  if (view === "bundle") return capabilities.canReviewSubmission;
  return false;
}
