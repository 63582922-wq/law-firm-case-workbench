import { readDesktopSessionGrant, type DesktopSessionGrant } from "@/lib/desktop-bridge";

const DESKTOP_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,160}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export type PersistentApiTarget = {
  apiBase: string | null;
};

export type PersistentApiAuthorization = "desktop-session" | "provided-bearer";

export async function persistentApiFetch(
  target: PersistentApiTarget,
  path: string,
  init: RequestInit = {},
  authorization: PersistentApiAuthorization = "desktop-session",
): Promise<Response> {
  const normalizedPath = normalizeApiPath(path);
  const headers = new Headers(init.headers);
  const desktop = isTauriDesktop();
  let apiBase: string;

  if (desktop) {
    const grant = validateDesktopGrant(await readDesktopSessionGrant());
    apiBase = grant.apiBase;
    if (target.apiBase !== null && normalizeApiBase(target.apiBase) !== apiBase) {
      throw new Error("桌面服务地址与已核验会话不一致，案件请求已停止。");
    }
    if (authorization === "desktop-session") {
      if (headers.has("Authorization")) {
        throw new Error("正式案件请求不能覆盖桌面身份凭证。");
      }
      headers.set("Authorization", `Bearer ${grant.accessToken}`);
    } else if (!isValidBearer(headers.get("Authorization"))) {
      throw new Error("本机派生件读取缺少有效的一次性许可。");
    }
  } else {
    if (target.apiBase === null) {
      throw new Error("当前不是已登记桌面运行环境，无法取得本机案件会话。");
    }
    apiBase = normalizeApiBase(target.apiBase);
    if (authorization === "provided-bearer" && !isValidBearer(headers.get("Authorization"))) {
      throw new Error("派生件读取缺少有效的一次性许可。");
    }
  }

  const response = await fetch(`${apiBase}${normalizedPath}`, {
    ...init,
    headers,
    cache: "no-store",
    credentials: desktop ? "omit" : "include",
    referrerPolicy: "no-referrer",
  });
  if (desktop && authorization === "desktop-session" && response.status === 401) {
    throw new Error("本机会话已失效；请重新启动工作台并再次核验登记。");
  }
  return response;
}

export function validateDesktopGrant(grant: DesktopSessionGrant): DesktopSessionGrant {
  const apiBase = normalizeDesktopApiBase(grant.apiBase);
  if (!DESKTOP_TOKEN_PATTERN.test(grant.accessToken) || !UUID_PATTERN.test(grant.sessionId)) {
    throw new Error("桌面会话回执格式无效，案件请求已停止。");
  }
  const expiresAt = Date.parse(grant.expiresAt);
  const now = Date.now();
  if (!Number.isFinite(expiresAt) || expiresAt <= now || expiresAt > now + 31 * 60 * 1000) {
    throw new Error("桌面会话已到期或有效期异常，案件请求已停止。");
  }
  return { ...grant, apiBase };
}

function isTauriDesktop(): boolean {
  return typeof window !== "undefined" && window.__TAURI_INTERNALS__ !== undefined;
}

function normalizeApiPath(path: string): string {
  if (!path.startsWith("/v1/") || path.includes("#") || path.includes("?")) {
    throw new Error("持久 API 路径不符合受控边界。");
  }
  return path;
}

function normalizeDesktopApiBase(value: string): string {
  const url = parseUrl(value);
  const port = Number(url.port);
  if (
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65535 ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  ) {
    throw new Error("桌面会话只能连接数字 loopback 随机端口。");
  }
  return `http://127.0.0.1:${port}`;
}

function normalizeApiBase(value: string): string {
  const url = parseUrl(value.trim());
  if (url.username || url.password || url.search || url.hash || (url.pathname !== "/" && url.pathname !== "")) {
    throw new Error("持久 API 地址不能包含凭证、路径、查询或片段。");
  }
  const localHost = url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "[::1]";
  if (url.protocol !== "https:" && !(url.protocol === "http:" && localHost)) {
    throw new Error("持久 API 必须使用 HTTPS；仅数字或名称 loopback 可使用 HTTP。");
  }
  return url.origin;
}

function parseUrl(value: string): URL {
  try {
    return new URL(value);
  } catch {
    throw new Error("持久 API 地址无效，案件请求已停止。");
  }
}

function isValidBearer(value: string | null): boolean {
  if (value === null) return false;
  const [scheme, token, extra] = value.split(" ");
  return extra === undefined && scheme === "Bearer" && DESKTOP_TOKEN_PATTERN.test(token ?? "");
}
