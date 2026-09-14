import assert from "node:assert/strict";
import test from "node:test";

import {
  canOpenWebLawyerView,
  hasRegisteredCaseMaterials,
  type WebLawyerViewCapabilities,
} from "./web-lawyer-navigation.ts";

const capabilities: WebLawyerViewCapabilities = {
  canReviewEvidence: true,
  canReviewFacts: true,
  canReviewLegal: true,
  canRunCalculation: true,
  canReviewSubmission: true,
};

test("server-projected material count keeps downstream views reachable after reload", () => {
  const afterReload = hasRegisteredCaseMaterials(5, false);

  assert.equal(afterReload, true);
  assert.equal(canOpenWebLawyerView(capabilities, "facts", true, afterReload), true);
  assert.equal(canOpenWebLawyerView(capabilities, "legal", true, afterReload), true);
  assert.equal(canOpenWebLawyerView(capabilities, "calculation", true, afterReload), true);
  assert.equal(canOpenWebLawyerView(capabilities, "bundle", true, afterReload), true);
});

test("an empty case exposes material intake but keeps result workspaces locked", () => {
  const empty = hasRegisteredCaseMaterials(0, false);

  assert.equal(empty, false);
  assert.equal(canOpenWebLawyerView(capabilities, "evidence", true, empty), true);
  assert.equal(canOpenWebLawyerView(capabilities, "facts", true, empty), false);
});

test("决策包视图要求先有案件与材料，且需要核对案情权限", () => {
  assert.equal(canOpenWebLawyerView(capabilities, "analysis", false), false);
  assert.equal(canOpenWebLawyerView(capabilities, "analysis", true, false), false);
  assert.equal(canOpenWebLawyerView(capabilities, "analysis", true, true), true);
  const limited: WebLawyerViewCapabilities = { ...capabilities, canReviewFacts: false };
  assert.equal(canOpenWebLawyerView(limited, "analysis", true, true), false);
});
