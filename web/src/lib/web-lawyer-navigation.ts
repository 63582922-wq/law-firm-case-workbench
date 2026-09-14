export type WebLawyerInitialView =
  | "overview"
  | "evidence"
  | "facts"
  | "legal"
  | "calculation"
  | "bundle"
  | "security";

export type WebLawyerViewCapabilities = Readonly<{
  canReviewEvidence: boolean;
  canReviewFacts: boolean;
  canReviewLegal: boolean;
  canRunCalculation: boolean;
  canReviewSubmission: boolean;
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
  if (view === "bundle") return capabilities.canReviewSubmission;
  return false;
}
