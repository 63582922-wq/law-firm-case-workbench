"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  WEB_BRIEF_STANCES,
  webBriefClaims,
  webBriefGrounds,
  emptyWebBriefSelections,
  exportWebBrief,
  generateWebBrief,
  isWebLoginRequired,
  readWebBrief,
  saveWebBriefSelections,
  type WebBriefPayload,
  type WebBriefSelections,
  type WebBriefStatus,
} from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";
import { WebMarkdown } from "@/components/web-markdown";

const STATUS_LABEL: Record<WebBriefStatus, string> = {
  NOT_RUN: "尚未起草",
  RUNNING: "正在起草",
  COMPLETED: "草稿就绪",
  FAILED: "起草失败",
  BLOCKED: "已拦截",
  MODEL_NOT_CONFIGURED: "未配置模型（仅骨架）",
  STALE: "草稿已失效",
  DISABLED: "后台起草已禁用",
};

const GATE_LABEL: Record<string, string> = {
  PASS: "门禁通过",
  AUTO_REPAIRED: "格式已自动修复",
  MARK_FOR_REVIEW: "含待律师确认项",
  HARD_BLOCKED: "命中安全红线",
  MODEL_NOT_CONFIGURED: "确定性骨架",
};

export function WebDefenceBrief({
  caseId,
  caseNumber,
  onSessionExpired,
}: {
  caseId: string;
  caseNumber: string;
  onSessionExpired: () => void;
}) {
  const [payload, setPayload] = useState<WebBriefPayload | null>(null);
  const [draft, setDraft] = useState<WebBriefSelections>(emptyWebBriefSelections());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [authoritiesText, setAuthoritiesText] = useState("");
  const [loaded, setLoaded] = useState(false);

  const apply = useCallback((next: WebBriefPayload) => {
    setPayload(next);
    setDraft(next.selections);
    setAuthoritiesText(next.selections.authorities.join("\n"));
  }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await readWebBrief(caseId, signal);
      if (signal?.aborted) return;
      apply(next);
    } catch (caught) {
      if (signal?.aborted) return;
      if (isWebLoginRequired(caught)) {
        onSessionExpired();
        return;
      }
      setError(caught instanceof Error ? caught.message : "读取答辩状状态失败。");
    } finally {
      if (!signal?.aborted) setLoaded(true);
    }
  }, [caseId, onSessionExpired, apply]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void refresh(controller.signal), 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [refresh]);

  // 起草中轮询；回调放 ref，避免父组件渲染重建定时器导致静默停止。
  const refreshRef = useRef(refresh);
  useEffect(() => { refreshRef.current = refresh; }, [refresh]);
  const running = payload?.state.status === "RUNNING";
  useEffect(() => {
    if (!running) return;
    let timer = 0;
    let cancelled = false;
    const startedAt = Date.now();
    const tick = () => {
      if (cancelled) return;
      void refreshRef.current();
      timer = window.setTimeout(tick, Date.now() - startedAt < 30_000 ? 1_200 : 3_000);
    };
    timer = window.setTimeout(tick, 1_200);
    const wake = () => { if (document.visibilityState === "visible") void refreshRef.current(); };
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [running, caseId]);

  const currentSelections = (): WebBriefSelections => ({
    ...draft,
    authorities: authoritiesText.split("\n").map((line) => line.trim()).filter(Boolean),
  });

  const save = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const next = await saveWebBriefSelections(caseId, currentSelections());
      apply(next);
      setNotice(next.state.stale
        ? "选择已保存。选择变化会使已生成的草稿失效，请重新生成。"
        : "选择已保存。");
    } catch (caught) {
      if (isWebLoginRequired(caught)) return onSessionExpired();
      setError(caught instanceof Error ? caught.message : "保存选择失败。");
    } finally {
      setBusy(false);
    }
  };

  const generate = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await saveWebBriefSelections(caseId, currentSelections());
      const next = await generateWebBrief(caseId, { caseNumber });
      apply(next);
      setNotice(next.state.status === "MODEL_NOT_CONFIGURED" || next.state.status === "DISABLED"
        ? "已生成文书骨架：未配置模型或后台起草被禁用，正文论证需律师补写。"
        : "答辩状草稿生成已开始。");
    } catch (caught) {
      if (isWebLoginRequired(caught)) return onSessionExpired();
      setError(caught instanceof Error ? caught.message : "生成答辩状失败。");
    } finally {
      setBusy(false);
    }
  };

  const download = async (format: "md" | "docx") => {
    setError("");
    try {
      const blob = await exportWebBrief(caseId, format);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `答辩状草稿.${format}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "导出失败。");
    }
  };

  const state = payload?.state;
  const numbers = Object.entries(state?.engineNumbers ?? {});

  return (
    <section className={styles.factsArea} aria-labelledby="web-defence-brief-title">
      <header className={styles.webLawyerIntakeHeading}>
        <p className={styles.eyebrow}>文书起草</p>
        <h2 id="web-defence-brief-title">答辩状草稿</h2>
        <p>
          立场与主张由律师勾选，金额只来自计算表，法条只来自律师登记的法源；
          模型只写论证文字。产出的 Word 稿需律师逐句复核后才能使用。
        </p>
      </header>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <span role="status" className={styles.eyebrow}>
          {STATUS_LABEL[state?.status ?? "NOT_RUN"]}
        </span>
        {state?.gateLevel ? (
          <span className={styles.eyebrow}>{GATE_LABEL[state.gateLevel] ?? state.gateLevel}</span>
        ) : null}
        {state && state.calls > 0 ? (
          <span className={styles.eyebrow}>模型调用 {state.calls} 次 · 已用 ¥{state.costCny}</span>
        ) : null}
        <button type="button" onClick={() => void generate()} disabled={busy || running}>
          {running ? "起草中…" : "保存并生成草稿"}
        </button>
        <button type="button" onClick={() => void save()} disabled={busy || running}>仅保存选择</button>
        {state?.markdownAvailable ? (
          <>
            <button type="button" onClick={() => void download("md")}>导出 Markdown</button>
            <button type="button" onClick={() => void download("docx")}>导出 Word</button>
          </>
        ) : null}
      </div>

      {running ? (
        <p role="status">
          进度 {state?.progress}%{state?.stage ? ` · ${state.stage}` : ""}
          （模型起草需要数十秒，可离开页面稍后回来）
        </p>
      ) : null}
      {notice ? <p className={styles.webLawyerNotice} role="status">{notice}</p> : null}
      {state?.error ? <p role="alert">{state.error}</p> : null}
      {error ? <p role="alert">{error}</p> : null}

      <section aria-label="律师选择" style={{ maxWidth: 900, marginTop: 12 }}>
        <h3>律师选择（文书的唯一立场来源）</h3>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            答辩人
            <input value={draft.respondent} placeholder="被告姓名"
                   onChange={(event) => setDraft({ ...draft, respondent: event.target.value })} />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            被答辩人（原告）
            <input value={draft.claimant} placeholder="原告姓名"
                   onChange={(event) => setDraft({ ...draft, claimant: event.target.value })} />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            受理法院
            <input value={draft.court} placeholder="XX 人民法院"
                   onChange={(event) => setDraft({ ...draft, court: event.target.value })} />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            案号
            <input value={draft.caseNumber} placeholder="（2026）粤XXXX民初XXXX号"
                   onChange={(event) => setDraft({ ...draft, caseNumber: event.target.value })} />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            案由
            <input value={draft.cause} placeholder="例如：买卖合同纠纷"
                   onChange={(event) => setDraft({ ...draft, cause: event.target.value })} />
          </label>
        </div>
        <p style={{ fontSize: 12, opacity: 0.8, marginTop: 4 }}>
          案由决定文书的术语：买卖合同用「货款／供货／逾期付款损失」，
          民间借贷用「借款本金／出借／利息」。填错会把另一类案由的术语写进文书。
        </p>

        <h4 style={{ marginTop: 12 }}>主张哪些抗辩（不勾选则不写进文书）</h4>
        <ul style={{ listStyle: "none", paddingLeft: 0, fontSize: 13 }}>
          {webBriefGrounds(draft.cause).map((ground) => (
            <li key={ground.id} style={{ marginBottom: 6 }}>
              <label style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                <input type="checkbox" checked={Boolean(draft.grounds[ground.id])}
                       onChange={(event) => setDraft({
                         ...draft,
                         grounds: { ...draft.grounds, [ground.id]: event.target.checked },
                       })} />
                <span>
                  <strong>{ground.title}</strong>
                  <br />
                  <span style={{ opacity: 0.8 }}>{ground.description}</span>
                </span>
              </label>
            </li>
          ))}
        </ul>

        <h4>对各项诉请的态度</h4>
        <table style={{ fontSize: 13, borderCollapse: "collapse" }}>
          <tbody>
            {webBriefClaims(draft.cause).map((claim) => (
              <tr key={claim.id}>
                <td style={{ paddingRight: 12 }}>{claim.label}</td>
                <td>
                  <select value={draft.stances[claim.id] ?? "不发表意见"}
                          aria-label={`${claim.label}的态度`}
                          onChange={(event) => setDraft({
                            ...draft,
                            stances: { ...draft.stances, [claim.id]: event.target.value },
                          })}>
                    {WEB_BRIEF_STANCES.map((stance) => (
                      <option key={stance} value={stance}>{stance}</option>
                    ))}
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <label style={{ display: "block", marginTop: 12, fontSize: 13 }}>
          律师登记的法源（每行一条；未登记的引用会被替换为占位）
          <textarea value={authoritiesText} rows={4} style={{ width: "100%" }}
                    placeholder={"《最高人民法院关于审理民间借贷案件适用法律若干问题的规定》第二十五条"}
                    onChange={(event) => setAuthoritiesText(event.target.value)} />
        </label>
        <label style={{ display: "block", marginTop: 8, fontSize: 13 }}>
          律师补充说明（可写进文书的固定段落，例如调解意愿）
          <textarea value={draft.notes} rows={3} style={{ width: "100%" }}
                    onChange={(event) => setDraft({ ...draft, notes: event.target.value })} />
        </label>
      </section>

      {numbers.length > 0 ? (
        <section aria-label="正式数字">
          <h3>正式数字（引擎输出，模型未参与计算）</h3>
          <ul>
            {numbers.map(([key, value]) => (<li key={key}>{key}：{value}</li>))}
          </ul>
        </section>
      ) : null}

      {payload?.markdown ? (
        <section aria-label="答辩状正文" style={{ maxWidth: 900 }}>
          <WebMarkdown markdown={payload.markdown} />
        </section>
      ) : (
        <section className={styles.webLawyerEmptyPanel}>
          <h3>尚无答辩状草稿</h3>
          <p>
            {loaded
              ? "勾选主张、填写当事人与已登记法源后点击「保存并生成草稿」。未配置模型时只产出骨架，正文论证需律师补写。"
              : "正在读取本案的答辩状状态…"}
          </p>
        </section>
      )}
    </section>
  );
}
