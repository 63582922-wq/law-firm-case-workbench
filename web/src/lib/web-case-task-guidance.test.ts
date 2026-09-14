import assert from "node:assert/strict";
import test from "node:test";
import { emptyCaseTaskDecisionMessage } from "./web-case-task-guidance.ts";

test("an empty inbox never hides a stopped or failed task", () => {
  assert.match(emptyCaseTaskDecisionMessage("WAITING_INPUT"), /任务已停下/);
  assert.match(emptyCaseTaskDecisionMessage("FAILED"), /技术失败不能通过律师批准解决/);
  assert.match(emptyCaseTaskDecisionMessage("RECONCILIATION_REQUIRED"), /请勿重复交办/);
});
test("review and completion do not imply legal approval or court submission", () => {
  assert.match(emptyCaseTaskDecisionMessage("READY_FOR_REVIEW"), /不代表内容已经获批/);
  assert.match(emptyCaseTaskDecisionMessage("COMPLETED"), /不代表材料已经提交法院/);
});
test("paused, cancelled and stale tasks have distinct instructions", () => {
  assert.match(emptyCaseTaskDecisionMessage("PAUSED"), /任务已暂停/);
  assert.match(emptyCaseTaskDecisionMessage("CANCELLED"), /不会继续执行/);
  assert.match(emptyCaseTaskDecisionMessage("STALE"), /不能代替当前案情/);
});
