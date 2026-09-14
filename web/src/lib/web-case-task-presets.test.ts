import assert from "node:assert/strict";
import test from "node:test";
import { CASE_TASK_PRESETS, DEFAULT_CASE_TASK } from "./web-case-task-presets.ts";

test("business presets provide editable briefs without granting execution authority", () => {
  assert.equal(new Set(CASE_TASK_PRESETS.map((item) => item.id)).size, 3);
  assert.equal(DEFAULT_CASE_TASK.id, "risk");
  for (const item of CASE_TASK_PRESETS) {
    assert.ok(item.objective.length >= 2 && item.objective.length <= 4000);
    assert.ok(item.criteria.split("\n").length >= 3);
    assert.ok(item.criteria.includes("来源"));
    assert.deepEqual(Object.keys(item).sort(), ["criteria", "id", "label", "objective"]);
  }
});
