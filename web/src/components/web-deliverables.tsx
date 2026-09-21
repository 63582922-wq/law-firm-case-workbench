"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  WEB_DELIVERABLE_STATES,
  downloadWebDeliverableArchive,
  emptyWebDeliverableParties,
  isWebLoginRequired,
  readWebDeliverableTemplate,
  readWebDeliverables,
  saveWebDeliverables,
  webDeliverableDocumentUrl,
  type WebDeliverableItem,
  type WebDeliverableMaterial,
  type WebDeliverableParties,
  type WebDeliverableState,
} from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";

/** 与后端 matter_documents 的文件名一一对应（含子目录）。 */
const DOCUMENTS: ReadonlyArray<Readonly<{
  path: string; label: string; signature: boolean; note: string;
}>> = [
  { path: "01-民事答辩状.docx", label: "民事答辩状", signature: true,
    note: "答辩人签名或捺印后提交法院" },
  { path: "02-证据目录.docx", label: "证据目录", signature: false,
    note: "补全每份证据的证明内容" },
  { path: "03-质证意见.docx", label: "质证意见", signature: false,
    note: "对原告证据逐份发表三性意见" },
  { path: "04-代理词.docx", label: "代理词", signature: false, note: "庭审后提交" },
  { path: "05-授权委托书（当事人签字）.docx", label: "授权委托书", signature: true,
    note: "当事人本人签署" },
  { path: "06-送达地址确认书（当事人签字）.docx", label: "送达地址确认书", signature: true,
    note: "当事人本人签署" },
  { path: "07-当事人陈述（当事人签字）.docx", label: "当事人陈述", signature: true,
    note: "事实必须由当事人本人填写并签署" },
  { path: "08-证据来源说明（当事人签字）.docx", label: "证据来源说明", signature: true,
    note: "当事人本人签署" },
  { path: "09-调解意见确认（当事人签字）.docx", label: "调解意见确认", signature: true,
    note: "当事人本人签署" },
  { path: "申请书（按需选用）/10-申请书（追加当事人）.docx", label: "申请书·追加当事人",
    signature: false, note: "按需选用" },
  { path: "申请书（按需选用）/11-申请书（调查取证）.docx", label: "申请书·调查取证",
    signature: false, note: "按需选用" },
  { path: "申请书（按需选用）/12-申请书（鉴定）.docx", label: "申请书·鉴定",
    signature: false, note: "按需选用" },
  { path: "申请书（按需选用）/13-申请书（延期举证）.docx", label: "申请书·延期举证",
    signature: false, note: "按需选用" },
  { path: "内部文件（不提交）/交付清单与填写指引.docx", label: "内部：交付清单与填写指引",
    signature: false, note: "内部使用，不要提交法院" },
];

const PARTY_FIELDS: ReadonlyArray<Readonly<{
  key: keyof WebDeliverableParties; label: string; placeholder: string; required: boolean;
}>> = [
  { key: "respondent", label: "被告（答辩人）", placeholder: "当事人姓名或名称", required: true },
  { key: "claimant", label: "原告（被答辩人）", placeholder: "对方当事人", required: true },
  { key: "court", label: "受理法院", placeholder: "例如：某某区人民法院", required: true },
  { key: "caseNumber", label: "案号", placeholder: "（2026）粤XXXX民初XXXX号", required: true },
  { key: "cause", label: "案由", placeholder: "例如：买卖合同纠纷", required: true },
  { key: "respondentAddress", label: "答辩人住所", placeholder: "用于答辩状与送达地址", required: false },
  { key: "respondentId", label: "身份证号／统一社会信用代码", placeholder: "可留空", required: false },
  { key: "respondentPhone", label: "联系电话", placeholder: "可留空", required: false },
  { key: "lawyer", label: "承办律师", placeholder: "可留空，签字时手写", required: false },
  { key: "lawFirm", label: "律师事务所", placeholder: "可留空", required: false },
];

/** 交付状态沿用后端目录 id。 */
function docStateKey(path: string): string {
  if (path.includes("01-")) return "answer";
  if (path.includes("02-")) return "evidence_list";
  if (path.includes("03-")) return "cross_examination";
  if (path.includes("04-")) return "argument";
  if (path.includes("05-")) return "authorisation";
  if (path.includes("06-")) return "service_address";
  if (path.includes("07-")) return "statement";
  if (path.includes("08-")) return "evidence_source";
  if (path.includes("09-")) return "mediation";
  if (path.includes("申请书")) return "applications";
  return path;
}

export function WebDeliverableChecklist({
  caseId,
  onSessionExpired,
}: {
  caseId: string;
  onSessionExpired: () => void;
}) {
  const [state, setState] = useState<WebDeliverableState | null>(null);
  const [parties, setParties] = useState<WebDeliverableParties>(emptyWebDeliverableParties());
  const [states, setStates] = useState<Record<string, string>>({});
  const [roles, setRoles] = useState<Record<string, { plaintiff: boolean; ours: boolean }>>({});
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<string>("");

  const apply = useCallback((next: WebDeliverableState) => {
    setState(next);
    setParties(next.parties);
    setStates({ ...next.states });
    setRoles(Object.fromEntries(next.materials.map((item) => [
      item.materialId, { plaintiff: item.plaintiff, ours: item.ours },
    ])));
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await readWebDeliverables(caseId, signal);
      if (!signal?.aborted) apply(next);
    } catch (caught) {
      if (signal?.aborted) return;
      if (isWebLoginRequired(caught)) return onSessionExpired();
      setError(caught instanceof Error ? caught.message : "读取交付清单失败。");
    }
  }, [caseId, onSessionExpired, apply]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void refresh(controller.signal), 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [refresh]);

  const missingRequired = useMemo(
    () => PARTY_FIELDS.filter((field) => field.required && !String(parties[field.key] ?? "").trim()),
    [parties],
  );

  const save = async (): Promise<boolean> => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      apply(await saveWebDeliverables(caseId, parties, states, roles));
      setNotice("已保存。");
      return true;
    } catch (caught) {
      if (isWebLoginRequired(caught)) {
        onSessionExpired();
        return false;
      }
      setError(caught instanceof Error ? caught.message : "保存失败。");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const downloadPack = async () => {
    if (missingRequired.length > 0) {
      setError(`请先填写：${missingRequired.map((field) => field.label).join("、")}`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      if (!(await save())) return;
      const blob = await downloadWebDeliverableArchive(caseId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "应诉材料包.zip";
      anchor.click();
      URL.revokeObjectURL(url);
      setNotice("已导出材料包：提交件、签字件、按需申请书、内部文件分开放置。");
    } catch (caught) {
      if (isWebLoginRequired(caught)) return onSessionExpired();
      setError(caught instanceof Error ? caught.message : "导出材料包失败。");
    } finally {
      setBusy(false);
    }
  };

  const openTemplate = async (item: WebDeliverableItem) => {
    try {
      const payload = await readWebDeliverableTemplate(caseId, item.itemId);
      setPreview(payload);
    } catch (caught) {
      if (isWebLoginRequired(caught)) return onSessionExpired();
      setError(caught instanceof Error ? caught.message : "读取模板失败。");
    }
  };

  const materials = state?.materials ?? [];
  const signatureDocuments = DOCUMENTS.filter((item) => item.signature);
  const lawyerDocuments = DOCUMENTS.filter(
    (item) => !item.signature && !item.path.includes("内部文件"));

  const materialRow = (item: WebDeliverableMaterial) => {
    const role = roles[item.materialId] ?? { plaintiff: false, ours: false };
    return (
      <tr key={item.materialId}>
        <td>{item.displayName}</td>
        <td>{item.pageCount}</td>
        <td style={{ textAlign: "center" }}>
          <input type="checkbox" aria-label={`${item.displayName} 是原告证据`} checked={role.plaintiff}
                 onChange={(event) => setRoles({ ...roles,
                   [item.materialId]: { ...role, plaintiff: event.target.checked } })} />
        </td>
        <td style={{ textAlign: "center" }}>
          <input type="checkbox" aria-label={`${item.displayName} 是我方证据`} checked={role.ours}
                 onChange={(event) => setRoles({ ...roles,
                   [item.materialId]: { ...role, ours: event.target.checked } })} />
        </td>
      </tr>
    );
  };

  return (
    <section className={styles.factsArea} aria-labelledby="web-deliverables-title">
      <header className={styles.webLawyerIntakeHeading}>
        <p className={styles.eyebrow}>交付</p>
        <h2 id="web-deliverables-title">应诉材料包</h2>
        <p>
          填四项基本信息、勾选哪些材料算证据，一次导出：每份文书一个 Word 文件，排版统一
          （宋体/黑体、三号字、固定行距、无颜色），内部提示单独放在「内部文件」。
        </p>
      </header>

      <section aria-label="基本信息" style={{ maxWidth: 1000 }}>
        <h3>一、基本信息（必填四项）</h3>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13 }}>
          {PARTY_FIELDS.map((field) => (
            <label key={field.key} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {field.label}{field.required ? " *" : ""}
              <input value={parties[field.key] ?? ""} placeholder={field.placeholder}
                     onChange={(event) => setParties({ ...parties, [field.key]: event.target.value })} />
            </label>
          ))}
        </div>
      </section>

      <section aria-label="材料归类" style={{ maxWidth: 1000 }}>
        <h3>二、哪些材料算证据</h3>
        <p style={{ fontSize: 13, opacity: 0.85 }}>
          「原告证据」进质证意见；「我方证据」进证据目录与证据来源说明。不勾选就不进文书
          ——法院送达的传票、通知书本来不是证据。
        </p>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>材料</th>
              <th style={{ textAlign: "left" }}>页数</th>
              <th style={{ textAlign: "center" }}>原告证据</th>
              <th style={{ textAlign: "center" }}>我方证据</th>
            </tr>
          </thead>
          <tbody>{materials.map(materialRow)}</tbody>
        </table>
      </section>

      <section aria-label="签字状态" style={{ maxWidth: 1000 }}>
        <h3>三、签字与提交状态</h3>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>需要当事人签字的文书</th>
              <th style={{ textAlign: "left" }}>状态</th>
            </tr>
          </thead>
          <tbody>
            {signatureDocuments.map((doc) => (
              <tr key={doc.path}>
                <td>{doc.label}</td>
                <td>
                  <select aria-label={`${doc.label}状态`}
                          value={states[docStateKey(doc.path)] ?? "未开始"}
                          onChange={(event) => setStates({ ...states,
                            [docStateKey(doc.path)]: event.target.value })}>
                    {WEB_DELIVERABLE_STATES.map((option) => (
                      <option key={option} value={option}>{option}</option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <details style={{ marginTop: 8, fontSize: 13 }}>
          <summary>律师署名文书</summary>
          <ul>
            {lawyerDocuments.map((doc) => (
              <li key={doc.path}>{doc.label}　<small>{doc.note}</small></li>
            ))}
          </ul>
        </details>
      </section>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", marginTop: 12 }}>
        <button type="button" onClick={() => void downloadPack()} disabled={busy}>
          {busy ? "处理中…" : "导出应诉材料包（ZIP）"}
        </button>
        <button type="button" onClick={() => void save()} disabled={busy}>保存</button>
        <span style={{ fontSize: 12, opacity: 0.75 }}>
          {state?.updatedAt ? `上次保存：${state.updatedAt}` : "尚未保存"}
        </span>
      </div>
      {notice ? <p className={styles.webLawyerNotice} role="status">{notice}</p> : null}
      {error ? <p role="alert">{error}</p> : null}

      <section aria-label="材料包内容" style={{ maxWidth: 1000 }}>
        <h3>四、材料包内容（每份一个文件，可单独下载）</h3>
        <ul style={{ fontSize: 13 }}>
          {DOCUMENTS.map((doc) => (
            <li key={doc.path} style={{ marginBottom: 4 }}>
              <a href={webDeliverableDocumentUrl(caseId, doc.path)}>{doc.label}.docx</a>
              {doc.signature ? <strong style={{ marginLeft: 6 }}>· 需当事人签字</strong> : null}
              <span style={{ marginLeft: 6, opacity: 0.8 }}>{doc.note}</span>
            </li>
          ))}
        </ul>
      </section>

      {preview ? (
        <section aria-label="模板预览" style={{ maxWidth: 900 }}>
          <h3>模板预览</h3>
          <button type="button" onClick={() => setPreview("")}>收起</button>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>{preview}</pre>
        </section>
      ) : null}

      <section aria-label="模板入口" hidden>
        {state?.catalogue.filter((item) => item.source === "template").map((item) => (
          <button key={item.itemId} type="button" onClick={() => void openTemplate(item)}>
            {item.name}
          </button>
        ))}
      </section>
    </section>
  );
}
