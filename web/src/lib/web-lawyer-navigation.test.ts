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
  canRunAgent: true,
  canDraftDefenceBrief: true,
  canManageDeliverables: true,
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

test("决策包视图只要有案件与材料、且 Agent 已装配即可进入，不被尚未实现的事实确认步骤挡死", () => {
  assert.equal(canOpenWebLawyerView(capabilities, "analysis", false), false);
  assert.equal(canOpenWebLawyerView(capabilities, "analysis", true, false), false);
  assert.equal(canOpenWebLawyerView(capabilities, "analysis", true, true), true);
  // 本机模式没有事实确认/法律审阅写入接口，决策包仍须可用。
  const localMode: WebLawyerViewCapabilities = {
    ...capabilities,
    canReviewFacts: false,
    canReviewLegal: false,
    canReviewSubmission: false,
  };
  assert.equal(canOpenWebLawyerView(localMode, "analysis", true, true), true);
  assert.equal(canOpenWebLawyerView(localMode, "facts", true, true), false);
  // 未装配 Agent 运行时的受管服务仍必须挡住建包。
  const withoutAgent: WebLawyerViewCapabilities = { ...capabilities, canRunAgent: false };
  assert.equal(canOpenWebLawyerView(withoutAgent, "analysis", true, true), false);
});

test("答辩状视图要求有案件与材料，且文书起草已装配", () => {
  assert.equal(canOpenWebLawyerView(capabilities, "brief", false), false);
  assert.equal(canOpenWebLawyerView(capabilities, "brief", true, false), false);
  assert.equal(canOpenWebLawyerView(capabilities, "brief", true, true), true);
  // 本机模式没有事实确认/法律审阅接口，但装配了文书起草：答辩状必须可进入。
  const localMode: WebLawyerViewCapabilities = {
    ...capabilities, canReviewFacts: false, canReviewLegal: false, canReviewSubmission: false,
  };
  assert.equal(canOpenWebLawyerView(localMode, "brief", true, true), true);
  const withoutBrief: WebLawyerViewCapabilities = { ...capabilities, canDraftDefenceBrief: false };
  assert.equal(canOpenWebLawyerView(withoutBrief, "brief", true, true), false);
});

test("交付清单视图要求有案件与材料，且交付管理已装配", () => {
  assert.equal(canOpenWebLawyerView(capabilities, "deliverables", false), false);
  assert.equal(canOpenWebLawyerView(capabilities, "deliverables", true, false), false);
  assert.equal(canOpenWebLawyerView(capabilities, "deliverables", true, true), true);
  const withoutDeliverables: WebLawyerViewCapabilities = {
    ...capabilities, canManageDeliverables: false,
  };
  assert.equal(canOpenWebLawyerView(withoutDeliverables, "deliverables", true, true), false);
});
