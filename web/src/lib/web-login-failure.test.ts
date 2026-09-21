import assert from "node:assert/strict";
import test from "node:test";

import {
  WEB_LOGIN_FAILURE_MESSAGE,
  resolveWebLoginFailureLocation,
} from "./web-login-failure.ts";

test("the first-time authenticator message is cautious and gives the recovery action", () => {
  assert.match(WEB_LOGIN_FAILURE_MESSAGE, /可能已绑定/);
  assert.match(WEB_LOGIN_FAILURE_MESSAGE, /本次登录尚未完成/);
  assert.match(WEB_LOGIN_FAILURE_MESSAGE, /再次登录并输入新的动态验证码/);
  assert.doesNotMatch(WEB_LOGIN_FAILURE_MESSAGE, /已经绑定|绑定成功/);
});

test("recognizes the OIDC login failure marker and clears it from the address", () => {
  assert.deepEqual(
    resolveWebLoginFailureLocation("https://workbench.example.test/?login=failed"),
    { replacementPath: "/" },
  );
});

test("clears only the failure marker while preserving other navigation state", () => {
  assert.deepEqual(
    resolveWebLoginFailureLocation("https://workbench.example.test/evidence?case=case-1&login=failed&focus=page-2#review"),
    { replacementPath: "/evidence?case=case-1&focus=page-2#review" },
  );
});

test("does not treat an unrelated login value or malformed URL as a failure", () => {
  assert.equal(resolveWebLoginFailureLocation("https://workbench.example.test/?login=complete"), null);
  assert.equal(resolveWebLoginFailureLocation("https://[invalid"), null);
});
