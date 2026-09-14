/** Non-authorizing, tab-scoped recovery metadata. Never accepts document text. */
export type DocumentRecovery = Readonly<{ key: string; revision: number; createdAt: number }>;
type RecoveryStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function documentRecoverySlot(namespace: string, scope: readonly string[]): string {
  if (!/^[a-f0-9]{64}$/.test(namespace) || scope.length !== 5 || scope.some((part) => !/^[A-Za-z0-9_-]{1,128}$/.test(part))) {
    throw new Error("修改恢复范围无效，未发送修改。");
  }
  return `lawcase.document-recovery.v1:${namespace}:${scope.join(":")}`;
}

function validate(value: unknown): DocumentRecovery {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("修改恢复记录损坏，请先核对服务器记录。");
  const item = value as Record<string, unknown>;
  if (Object.keys(item).sort().join(",") !== "createdAt,key,revision" ||
    typeof item.key !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{15,159}$/.test(item.key) ||
    typeof item.revision !== "number" || !Number.isInteger(item.revision) || item.revision < 1 || item.revision > 999 ||
    typeof item.createdAt !== "number" || !Number.isSafeInteger(item.createdAt) || item.createdAt < 0) {
    throw new Error("修改恢复记录损坏，请先核对服务器记录。");
  }
  return { key: item.key, revision: item.revision, createdAt: item.createdAt };
}

export function readDocumentRecovery(storage: RecoveryStorage, slot: string): DocumentRecovery | null {
  const raw = storage.getItem(slot);
  if (raw === null) return null;
  if (raw.length > 512) throw new Error("修改恢复记录损坏，请先核对服务器记录。");
  return validate(JSON.parse(raw));
}

export function rememberDocumentRecovery(storage: RecoveryStorage, slot: string, value: DocumentRecovery): void {
  const safe = validate(value);
  const prior = readDocumentRecovery(storage, slot);
  if (prior && prior.key !== safe.key) throw new Error("本段已有待确认修改，请先核对原请求。");
  storage.setItem(slot, JSON.stringify(safe));
  if (readDocumentRecovery(storage, slot)?.key !== safe.key) throw new Error("浏览器未保存恢复编号，修改未发送。");
}

export function forgetDocumentRecovery(storage: RecoveryStorage, slot: string, key: string): void {
  if (readDocumentRecovery(storage, slot)?.key === key) storage.removeItem(slot);
}
