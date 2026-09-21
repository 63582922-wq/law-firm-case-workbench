/**
 * Browser-only client boundary for the self-hosted Web workbench.
 *
 * It deliberately accepts a relative, same-origin API path only.  Browser
 * session cookies stay in the browser's cookie jar; this module neither
 * reads authentication credentials nor stores them in JavaScript storage.
 */

const API_PATH_PATTERN = /^\/api\/(?:v1|local\/v1)(?:\/[A-Za-z0-9][A-Za-z0-9._-]{0,127})+\/?(?:\?[A-Za-z0-9._~!$'()*+,;=:@%&/-]{1,1024})?$/;
const COOKIE_NAME_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,64}$/;
const COOKIE_VALUE_PATTERN = /^[A-Za-z0-9_-]{16,256}$/;
const HEADER_NAME_PATTERN = /^[!#$%&'*+\-.^_`|~0-9A-Za-z-]{1,128}$/;
const ALLOWED_METHODS = new Set(["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]);
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const CREDENTIAL_REQUEST_HEADERS = new Set([
  "authorization",
  "cookie",
  "cookie2",
  "set-cookie",
]);
const CSRF_REQUEST_HEADERS = new Set([
  "x-csrf-token",
  "x-xsrf-token",
  "x-lawcase-csrf",
]);

export type WebApiClientOptions = Readonly<{
  /** Name of the script-visible double-submit CSRF cookie. */
  csrfCookieName?: string;
  /** Request header that carries the double-submit CSRF value. */
  csrfHeaderName?: string;
}>;

export type WebApiRequestInit = Omit<
  RequestInit,
  "headers" | "credentials" | "cache" | "referrerPolicy" | "redirect" | "mode"
> & {
  headers?: HeadersInit;
};

/**
 * Makes one same-origin request to the deployed Web API.
 *
 * Query strings are allowed only as the endpoint helper's already-validated
 * URLSearchParams output.  The path remains same-origin and fragment-free;
 * endpoint helpers must never concatenate raw user input into it.
 */
export async function webApiFetch(
  path: string,
  init: WebApiRequestInit = {},
  options: WebApiClientOptions = {},
): Promise<Response> {
  const origin = currentWebOrigin();
  const normalizedPath = normalizeApiPath(path, origin);
  const method = normalizeMethod(init.method);
  if (SAFE_METHODS.has(method) && init.body !== undefined && init.body !== null) {
    throw new Error("只读 Web API 请求不能携带请求正文。");
  }

  // Must stay aligned with WebSessionPolicy: the readable CSRF cookie is
  // paired with the HttpOnly __Host-lawcase_session cookie on the same HTTPS
  // origin. It contains no login credential.
  const csrfCookieName = normalizeCookieName(options.csrfCookieName ?? (isLocalApiPath(normalizedPath) ? "lawcase_local_csrf" : "__Host-lawcase_csrf"));
  const csrfHeaderName = normalizeHeaderName(options.csrfHeaderName ?? "X-Lawcase-CSRF");
  const headers = normalizeCallerHeaders(init.headers, csrfHeaderName);
  if (!SAFE_METHODS.has(method)) {
    headers.set(csrfHeaderName, readVisibleCsrfCookie(csrfCookieName));
  }

  const response = await fetch(normalizedPath, {
    ...init,
    method,
    headers,
    cache: "no-store",
    credentials: "include",
    mode: "same-origin",
    referrerPolicy: "no-referrer",
    // Do not follow a redirect where the browser could otherwise carry a
    // session cookie or CSRF header to a different origin.
    redirect: "error",
  });
  rejectRedirectedOrCrossOriginResponse(response, origin);
  return response;
}

function currentWebOrigin(): string {
  if (typeof window === "undefined" || typeof window.location?.href !== "string") {
    throw new Error("Web API 只能由已部署的浏览器工作台发起。");
  }
  let location: URL;
  try {
    location = new URL(window.location.href);
  } catch {
    throw new Error("当前浏览器来源无效，Web API 请求已停止。");
  }
  if ((location.protocol !== "https:" && location.protocol !== "http:") || location.origin === "null") {
    throw new Error("当前浏览器来源不支持 Web API 请求。");
  }
  return location.origin;
}

function normalizeApiPath(path: string, origin: string): string {
  if (
    typeof path !== "string" ||
    path.length > 2_048 ||
    !API_PATH_PATTERN.test(path) ||
    path.includes("#") ||
    path.includes("\\")
  ) {
    throw new Error("Web API 路径必须是同源受控路径。");
  }
  const configuredPrefix = process.env.NEXT_PUBLIC_WEB_API_PREFIX;
  const effectivePath = configuredPrefix === "/api/local/v1" && path.startsWith("/api/v1")
    ? `/api/local/v1${path.slice("/api/v1".length)}`
    : path;
  let target: URL;
  try {
    target = new URL(effectivePath, origin);
  } catch {
    throw new Error("Web API 路径无效，案件请求已停止。");
  }
  if (
    target.origin !== origin ||
    `${target.pathname}${target.search}` !== effectivePath ||
    target.hash !== ""
  ) {
    throw new Error("Web API 路径越过同源边界，案件请求已停止。");
  }
  return `${target.pathname}${target.search}`;
}

function isLocalApiPath(path: string): boolean {
  return path.startsWith("/api/local/v1/");
}

function normalizeMethod(value: string | undefined): string {
  const method = (value ?? "GET").toUpperCase();
  if (!ALLOWED_METHODS.has(method)) {
    throw new Error("Web API 请求方法不在受控范围内。");
  }
  return method;
}

function normalizeCookieName(value: string): string {
  if (!COOKIE_NAME_PATTERN.test(value)) {
    throw new Error("CSRF Cookie 名称无效，案件请求已停止。");
  }
  return value;
}

function normalizeHeaderName(value: string): string {
  if (!HEADER_NAME_PATTERN.test(value)) {
    throw new Error("CSRF 请求头名称无效，案件请求已停止。");
  }
  const normalized = value.toLowerCase();
  if (!normalized.startsWith("x-") || CREDENTIAL_REQUEST_HEADERS.has(normalized)) {
    throw new Error("CSRF 请求头名称不能覆盖受保护的身份请求头。");
  }
  return value;
}

function normalizeCallerHeaders(value: HeadersInit | undefined, csrfHeaderName: string): Headers {
  const headers = new Headers(value);
  const configuredCsrfHeader = csrfHeaderName.toLowerCase();
  for (const [name] of headers) {
    const normalized = name.toLowerCase();
    if (
      CREDENTIAL_REQUEST_HEADERS.has(normalized) ||
      CSRF_REQUEST_HEADERS.has(normalized) ||
      normalized === configuredCsrfHeader
    ) {
      throw new Error("Web API 不接受调用方提供的身份、Cookie 或 CSRF 请求头。");
    }
  }
  return headers;
}

function readVisibleCsrfCookie(name: string): string {
  if (typeof document === "undefined") {
    throw new Error("当前环境无法读取浏览器 CSRF Cookie，写入请求已停止。");
  }
  const cookieValue = document.cookie;
  let csrfValue: string | null = null;
  for (const segment of cookieValue.split(";")) {
    const trimmed = segment.trim();
    if (trimmed.length === 0) continue;
    const separator = trimmed.indexOf("=");
    if (separator < 1) {
      if (trimmed === name) {
        throw new Error("浏览器 CSRF Cookie 无效或存在歧义，写入请求已停止。");
      }
      continue;
    }
    const candidateName = trimmed.slice(0, separator);
    if (candidateName !== name) continue;
    const candidateValue = trimmed.slice(separator + 1);
    if (
      separator !== trimmed.lastIndexOf("=") ||
      !COOKIE_VALUE_PATTERN.test(candidateValue) ||
      csrfValue !== null
    ) {
      throw new Error("浏览器 CSRF Cookie 无效或存在歧义，写入请求已停止。");
    }
    csrfValue = candidateValue;
  }
  if (csrfValue === null) {
    throw new Error("浏览器 CSRF Cookie 缺失，写入请求已停止。");
  }
  return csrfValue;
}

function rejectRedirectedOrCrossOriginResponse(response: Response, origin: string): void {
  if (response.type === "opaqueredirect" || response.redirected) {
    throw new Error("Web API 重定向已被拒绝，案件请求已停止。");
  }
  if (response.url.length === 0) return;
  let responseUrl: URL;
  try {
    responseUrl = new URL(response.url);
  } catch {
    throw new Error("Web API 响应来源无效，案件请求已停止。");
  }
  if (responseUrl.origin !== origin) {
    throw new Error("Web API 响应越过同源边界，案件请求已停止。");
  }
}
