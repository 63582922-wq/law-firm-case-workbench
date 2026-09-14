export type WebLoginFailureLocation = {
  replacementPath: string;
};

export const WEB_LOGIN_FAILURE_MESSAGE =
  "验证器可能已绑定，本次登录尚未完成；请再次登录并输入新的动态验证码。";

/**
 * Detect the server-owned OIDC failure marker and return a same-origin URL
 * with only that marker removed. Other navigation state remains intact.
 */
export function resolveWebLoginFailureLocation(currentUrl: string): WebLoginFailureLocation | null {
  let url: URL;
  try {
    url = new URL(currentUrl, "https://workbench.invalid");
  } catch {
    return null;
  }

  if (!url.searchParams.getAll("login").includes("failed")) return null;

  url.searchParams.delete("login");
  const query = url.searchParams.toString();
  return {
    replacementPath: `${url.pathname}${query ? `?${query}` : ""}${url.hash}`,
  };
}
