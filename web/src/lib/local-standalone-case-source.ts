import {
  openLocalCase,
  readDesktopSessionGrant,
  type DesktopRuntimeStatus,
  type LocalCaseFolderInventory,
  type LocalCaseSummary,
} from "@/lib/desktop-bridge";
import { validateDesktopGrant } from "@/lib/persistent-api-client";

const ACTIVE_LOCAL_CASE_STORAGE_KEY = "lawcase.local-standalone.active-case.v1";
const LOCAL_CASE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export type LocalStandaloneRestore = {
  caseSummary: LocalCaseSummary | null;
  message: string | null;
};

export type LocalStandaloneInventoryItem = {
  relativePath: string;
  byteSize: number;
  sha256: string;
  detectedKind: string;
};

export type LocalStandaloneInventoryPage = {
  scanId: string;
  items: LocalStandaloneInventoryItem[];
  offset: number;
  nextOffset: number | null;
};

/**
 * The local-first workspace is deliberately independent of firm enrollment,
 * PostgreSQL, and cloud setup.  It is only available when the native shell
 * explicitly reports that its local case registry is ready.
 */
export function isReadyLocalStandaloneWorkspace(status: DesktopRuntimeStatus | null): boolean {
  return typeof window !== "undefined"
    && window.__TAURI_INTERNALS__ !== undefined
    && status?.phase === "READY"
    && status.workspaceMode === "LOCAL_STANDALONE"
    && status.localWorkspacePhase === "READY";
}

export function activeLocalStandaloneCaseId(): string | null {
  if (typeof window === "undefined") return null;
  const caseId = window.sessionStorage.getItem(ACTIVE_LOCAL_CASE_STORAGE_KEY)?.trim() || "";
  return LOCAL_CASE_ID_PATTERN.test(caseId) ? caseId : null;
}

export function activateLocalStandaloneCase(caseSummary: LocalCaseSummary): LocalCaseSummary {
  if (typeof window === "undefined") {
    throw new Error("本机案件只能在桌面应用中打开。");
  }
  if (!LOCAL_CASE_ID_PATTERN.test(caseSummary.caseId)) {
    throw new Error("本机案件标识无效，未切换到任何案件。");
  }
  window.sessionStorage.setItem(ACTIVE_LOCAL_CASE_STORAGE_KEY, caseSummary.caseId);
  return caseSummary;
}

export function clearActiveLocalStandaloneCase(): void {
  if (typeof window !== "undefined") window.sessionStorage.removeItem(ACTIVE_LOCAL_CASE_STORAGE_KEY);
}

/**
 * Restoring only reads the local registry.  A failed read leaves the saved
 * identifier in place so a transient native-shell failure cannot make a
 * lawyer lose the currently selected case.
 */
export async function restoreActiveLocalStandaloneCase(): Promise<LocalStandaloneRestore> {
  const caseId = activeLocalStandaloneCaseId();
  if (!caseId) return { caseSummary: null, message: null };
  try {
    return { caseSummary: await openLocalCase(caseId), message: null };
  } catch (reason: unknown) {
    return {
      caseSummary: null,
      message: reason instanceof Error ? reason.message : "无法重新打开上次的本机案件。",
    };
  }
}

/**
 * Reads a paginated inventory from the same short-lived desktop session.  It
 * never sends a file path, folder selection, or document content back to the
 * browser: the sidecar resolves the case and scan identifiers itself.
 */
export async function loadLocalStandaloneInventoryPage(
  caseId: string,
  inventory: LocalCaseFolderInventory,
  offset = 0,
): Promise<LocalStandaloneInventoryPage> {
  if (!LOCAL_CASE_ID_PATTERN.test(caseId) || !LOCAL_CASE_ID_PATTERN.test(inventory.scanId)) {
    throw new Error("本机材料盘点标识无效，未读取文件清单。");
  }
  if (!Number.isSafeInteger(offset) || offset < 0) {
    throw new Error("本机材料清单页码无效，未读取文件清单。");
  }
  const grant = validateDesktopGrant(await readDesktopSessionGrant());
  const response = await fetch(
    `${grant.apiBase}/v1/local-standalone/cases/${caseId}/folder-inventories/${inventory.scanId}/items?limit=100&offset=${offset}`,
    {
      headers: { Accept: "application/json", Authorization: `Bearer ${grant.accessToken}` },
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
    },
  );
  if (response.status === 401) throw new Error("本机会话已失效；请重新打开本机案件后再读取材料清单。");
  if (!response.ok) throw new Error("本机材料清单暂时无法读取；已盘点结果仍保留。");
  if (response.headers.get("Content-Type")?.split(";", 1)[0] !== "application/json") {
    throw new Error("本机材料清单返回了非 JSON 内容，已停止读取。");
  }
  const contentLength = Number(response.headers.get("Content-Length") || "0");
  if (!Number.isFinite(contentLength) || contentLength < 0 || contentLength > 2 * 1024 * 1024) {
    throw new Error("本机材料清单返回大小异常，已停止读取。");
  }
  const text = await response.text();
  if (text.length > 2 * 1024 * 1024) throw new Error("本机材料清单返回过大，已停止读取。");
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error("本机材料清单回执无效，已停止读取。");
  }
  return validateInventoryPage(payload, inventory.scanId, offset);
}

function validateInventoryPage(value: unknown, scanId: string, offset: number): LocalStandaloneInventoryPage {
  if (!isRecord(value)
    || value.scan_id !== scanId
    || !Array.isArray(value.items)
    || value.offset !== offset
    || (value.next_offset !== null && !isNonNegativeInteger(value.next_offset))) {
    throw new Error("本机材料清单回执字段无效，已停止读取。");
  }
  const items = value.items.map((item) => {
    if (!isRecord(item)
      || !isRelativePath(stringField(item, "relative_path"))
      || !isNonNegativeInteger(item.byte_size)
      || !isHash(stringField(item, "sha256"))
      || !isDetectedKind(stringField(item, "detected_kind"))) {
      throw new Error("本机材料清单项目无效，已停止读取。");
    }
    return {
      relativePath: stringField(item, "relative_path"),
      byteSize: item.byte_size as number,
      sha256: stringField(item, "sha256"),
      detectedKind: stringField(item, "detected_kind"),
    };
  });
  if (items.length > 100) throw new Error("本机材料清单超过分页上限，已停止读取。");
  const nextOffset = value.next_offset === null ? null : value.next_offset as number;
  if (nextOffset !== null && nextOffset <= offset) {
    throw new Error("本机材料清单分页无效，已停止读取。");
  }
  return { scanId, items, offset, nextOffset };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringField(value: Record<string, unknown>, key: string): string {
  return typeof value[key] === "string" ? value[key] : "";
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isHash(value: string): boolean {
  return /^[a-f0-9]{64}$/i.test(value);
}

function isRelativePath(value: string): boolean {
  return value.length >= 1
    && value.length <= 1_024
    && !value.startsWith("/")
    && !value.split("/").some((segment) => segment === "" || segment === "." || segment === "..")
    && !/[\u0000-\u001F]/.test(value);
}

function isDetectedKind(value: string): boolean {
  return /^[A-Z][A-Z0-9_]{0,63}$/.test(value);
}
