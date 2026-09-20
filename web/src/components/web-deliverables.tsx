"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  WEB_DELIVERABLE_STATES,
  emptyWebDeliverableParties,
  exportWebDeliverables,
  isWebLoginRequired,
  readWebDeliverableTemplate,
  readWebDeliverables,
  saveWebDeliverables,
  type WebDeliverableItem,
  type WebDeliverableParties,
  type WebDeliverableState,
} from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";
import { WebMarkdown } from "@/components/web-markdown";

const PARTY_FIELDS: ReadonlyArray<Readonly<{
  key: keyof WebDeliverableParties; label: string; placeholder: string;
}>> = [
  { key: "respondent", label: "被告（答辩人）", placeholder: "当事人姓名或名称" },
  { key: "respondentId", label: "身份证号／统一社会信用代码", placeholder: "可留空" },
  { key: "respondentAddress", label: "住所／送达地址", placeholder: "用于送达地址确认书" },
  { key: "respondentPhone", label: "联系电话", placeholder: "用于送达地址确认书" },
  { key: "claimant", label: "原告（被答辩人）", placeholder: "对方当事人" },
  { key: "court", label: "受理法院", placeholder: "例如：某某区人民法院" },
  { key: "caseNumber", label: "案号", placeholder: "（2026）粤XXXX民初XXXX号" },
  { key: "cause", label: "案由", placeholder: "例如：买卖合同纠纷" },
  { key: "lawyer", label: "承办律师", placeholder: "用于授权委托书" },
  { key: "lawFirm", label: "律师事务所", placeholder: "用于授权委托书" },
];

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
  const [preview, setPreview] = useState<{ itemId: string; markdown: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const apply = useCallback((next: WebDeliverableState) => {
    setState(next);
    setParties(next.parties);
    setStates({ ...next.states });
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

  const save = async (): Promise<boolean> => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const next = await saveWebDeliverables(caseId, parties, states);
      apply(next);
      setNotice("交付清单已保存。");
      return true;
    } catch (caught) {
      if (isWebLoginRequired(caught)) {
        onSessionExpired();
        return false;
      }
      setError(caught instanceof Error ? caught.message : "保存交付清单失败。");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const openTemplate = async (item: WebDeliverableItem) => {
    setError("");
    try {
      const markdown = await readWebDeliverableTemplate(caseId, item.itemId);
      setPreview({ itemId: item.itemId, markdown });
      setNotice("");
    } catch (caught) {
      if (isWebLoginRequired(caught)) return onSessionExpired();
      setError(caught instanceof Error ? caught.message : "读取交付物模板失败。");
    }
  };

  const download = async (format: "md" | "docx") => {
    setError("");
    try {
      if (!(await save())) return;
      const blob = await exportWebDeliverables(caseId, format);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `应诉材料包.${format}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "导出材料包失败。");
    }
  };

  const catalogue = state?.catalogue ?? [];
  const signatureItems = useMemo(
    () => catalogue.filter((item) => item.needsClientSignature), [catalogue]);
  const lawyerItems = useMemo(
    () => catalogue.filter((item) => !item.needsClientSignature && item.destination !== "内部"),
    [catalogue]);
  const internalItems = useMemo(
    () => catalogue.filter((item) => item.destination === "内部"), [catalogue]);

  const statusSelect = (item: WebDeliverableItem) => (
    <select
      aria-label={`${item.name}状态`}
      value={states[item.itemId] ?? "未开始"}
      onChange={(event) => setStates({ ...states, [item.itemId]: event.target.value })}
    >
      {WEB_DELIVERABLE_STATES.map((option) => (
        <option key={option} value={option}>{option}</option>
      ))}
    </select>
  );

  const row = (item: WebDeliverableItem, withSignatureColumn: boolean) => (
    <tr key={item.itemId}>
      <td>{item.name}</td>
      <td>{item.destination}</td>
      {withSignatureColumn ? <td>{item.signer}</td> : null}
      <td>{statusSelect(item)}</td>
      <td>{item.note}</td>
      <td>
        {item.source === "template" ? (
          <button type="button" onClick={() => void openTemplate(item)}>查看模板</button>
        ) : item.source === "brief" ? (
          <span style={{ opacity: 0.7 }}>在「答辩状」页生成</span>
        ) : item.source === "analysis" ? (
          <span style={{ opacity: 0.7 }}>在「决策包」页生成</span>
        ) : (
          <span style={{ opacity: 0.6 }}>—</span>
        )}
      </td>
    </tr>
  );

  return (
    <section className={styles.factsArea} aria-labelledby="web-deliverables-title">
      <header className={styles.webLawyerIntakeHeading}>
        <p className={styles.eyebrow}>交付管理</p>
        <h2 id="web-deliverables-title">交付清单与应诉材料包</h2>
        <p>
          哪些递法院、哪些要当事人签字、哪些只是内部工作件，一次列清；
          签字文件按案件信息生成模板，事实部分一律留空由当事人确认。
        </p>
      </header>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <button type="button" onClick={() => void save()} disabled={busy}>保存清单</button>
        <button type="button" onClick={() => void download("docx")} disabled={busy}>
          导出应诉材料包（Word）
        </button>
        <button type="button" onClick={() => void download("md")} disabled={busy}>
          导出 Markdown
        </button>
        <span style={{ fontSize: 12, opacity: 0.75 }}>
          {state?.updatedAt ? `上次保存：${state.updatedAt}` : "尚未保存过清单"}
        </span>
      </div>

      {notice ? <p className={styles.webLawyerNotice} role="status">{notice}</p> : null}
      {error ? <p role="alert">{error}</p> : null}

      <section aria-label="案件主体信息" style={{ maxWidth: 900 }}>
        <h3>案件主体信息（签字文件与答辩状共用）</h3>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13 }}>
          {PARTY_FIELDS.map((field) => (
            <label key={field.key} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {field.label}
              <input
                value={parties[field.key] ?? ""}
                placeholder={field.placeholder}
                onChange={(event) => setParties({ ...parties, [field.key]: event.target.value })}
              />
            </label>
          ))}
        </div>
      </section>

      <section aria-label="需要当事人签字的文件" style={{ maxWidth: 1100 }}>
        <h3>一、需要当事人签字的文件（未签字不得提交）</h3>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>交付物</th>
              <th style={{ textAlign: "left" }}>去向</th>
              <th style={{ textAlign: "left" }}>署名人</th>
              <th style={{ textAlign: "left" }}>状态</th>
              <th style={{ textAlign: "left" }}>说明</th>
              <th />
            </tr>
          </thead>
          <tbody>{signatureItems.map((item) => row(item, true))}</tbody>
        </table>
      </section>

      <section aria-label="律师署名文件" style={{ maxWidth: 1100 }}>
        <h3>二、由律师／律所署名的文件</h3>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>交付物</th>
              <th style={{ textAlign: "left" }}>去向</th>
              <th style={{ textAlign: "left" }}>状态</th>
              <th style={{ textAlign: "left" }}>说明</th>
              <th />
            </tr>
          </thead>
          <tbody>{lawyerItems.map((item) => row(item, false))}</tbody>
        </table>
      </section>

      <section aria-label="内部工作件" style={{ maxWidth: 900 }}>
        <h3>三、内部工作件（不对外提交）</h3>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>交付物</th>
              <th style={{ textAlign: "left" }}>状态</th>
              <th style={{ textAlign: "left" }}>说明</th>
            </tr>
          </thead>
          <tbody>
            {internalItems.map((item) => (
              <tr key={item.itemId}>
                <td>{item.name}</td>
                <td>{statusSelect(item)}</td>
                <td>{item.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {preview ? (
        <section aria-label="交付物模板预览" style={{ maxWidth: 900 }}>
          <h3>模板预览（导出材料包时会一并生成）</h3>
          <button type="button" onClick={() => setPreview(null)}>收起</button>
          <WebMarkdown markdown={preview.markdown} />
        </section>
      ) : null}
    </section>
  );
}
