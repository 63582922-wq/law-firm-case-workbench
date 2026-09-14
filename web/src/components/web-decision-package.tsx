"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  exportWebAnalysis,
  readWebAnalysisReport,
  readWebCaseAnalysisState,
  readWebCaseParameters,
  runWebCaseAgentAnalysis,
  saveWebCaseParameters,
  toWebCaseConfigPayload,
  isWebLoginRequired,
  type WebAnalysisAgentState,
  type WebCaseAnalysisState,
  type WebCaseParameters,
} from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";

/* ------------------------------------------------ 案件计算参数（界面用百分比，契约用小数） */

type ParameterDebtDraft = {
  debtId: string;
  principal: string;
  disbursedOn: string;
  dueOn: string;
  ratePercent: string;
  evidencePending: boolean;
};

type ParameterDraft = {
  capPercent: string;
  interestCutoff: string;
  debts: ParameterDebtDraft[];
};

function emptyParameterDraft(): ParameterDraft {
  return {
    capPercent: "",
    interestCutoff: "",
    debts: [{ debtId: "L1", principal: "", disbursedOn: "", dueOn: "",
              ratePercent: "", evidencePending: false }],
  };
}

function percentFromDecimal(value: string): string {
  const numeric = Number(value);
  if (!value.trim() || !Number.isFinite(numeric)) return "";
  return String(numeric * 100);
}

function decimalFromPercent(value: string): string {
  const numeric = Number(value);
  if (!value.trim() || !Number.isFinite(numeric) || numeric <= 0) return "";
  return String(numeric / 100);
}

function draftFromParameters(parameters: WebCaseParameters): ParameterDraft {
  return {
    capPercent: percentFromDecimal(parameters.lpr4xMonthlyRate),
    interestCutoff: parameters.interestCutoff,
    debts: parameters.debts.map((debt) => ({
      debtId: debt.debtId,
      principal: debt.principal,
      disbursedOn: debt.disbursedOn,
      dueOn: debt.dueOn,
      ratePercent: percentFromDecimal(debt.agreedMonthlyRate),
      evidencePending: debt.evidencePending,
    })),
  };
}

function parametersFromDraft(draft: ParameterDraft): WebCaseParameters {
  return {
    lpr4xMonthlyRate: decimalFromPercent(draft.capPercent),
    interestCutoff: draft.interestCutoff.trim(),
    debts: draft.debts.map((debt) => ({
      debtId: debt.debtId.trim(),
      principal: debt.principal.trim(),
      disbursedOn: debt.disbursedOn.trim(),
      dueOn: debt.dueOn.trim(),
      agreedMonthlyRate: decimalFromPercent(debt.ratePercent),
      evidencePending: debt.evidencePending,
    })),
  };
}

const _DATE_INPUT = /^\d{4}-\d{2}-\d{2}$/;
const _MONEY_INPUT = /^\d+(?:\.\d{1,2})?$/;
const _PERCENT_INPUT = /^\d+(?:\.\d+)?$/;

/** 前端只做格式校验；法律口径与金额一律由律师填写的参数和确定性引擎决定。 */
function validateParameterDraft(draft: ParameterDraft): string {
  if (!_PERCENT_INPUT.test(draft.capPercent.trim()) || Number(draft.capPercent) <= 0) {
    return "请填写司法保护上限月利率（按百分比填，例如月利率 1% 填 1）。";
  }
  if (!_DATE_INPUT.test(draft.interestCutoff.trim())) {
    return "请填写利息暂计截止日（YYYY-MM-DD）。";
  }
  if (draft.debts.length === 0) return "至少填写一笔借款。";
  const seen = new Set<string>();
  for (const debt of draft.debts) {
    const id = debt.debtId.trim() || "（未编号）";
    if (!debt.debtId.trim()) return "每笔借款都要有编号，例如 L1、L2。";
    if (seen.has(id)) return `借款编号重复：${id}`;
    seen.add(id);
    if (!_MONEY_INPUT.test(debt.principal.trim()) || Number(debt.principal) <= 0) {
      return `${id}：本金请填数字（元），例如 100000 或 100000.00。`;
    }
    if (!_DATE_INPUT.test(debt.disbursedOn.trim())) return `${id}：放款日请填 YYYY-MM-DD。`;
    if (debt.dueOn.trim() && !_DATE_INPUT.test(debt.dueOn.trim())) {
      return `${id}：到期日请填 YYYY-MM-DD，或留空。`;
    }
    if (!_PERCENT_INPUT.test(debt.ratePercent.trim()) || Number(debt.ratePercent) <= 0) {
      return `${id}：约定月利率按百分比填，例如月利率 1.5% 填 1.5。`;
    }
  }
  return "";
}

const STATUS_LABEL: Record<WebAnalysisAgentState["status"], string> = {
  NOT_RUN: "尚未分析",
  RUNNING: "分析进行中",
  COMPLETED: "分析完成",
  FAILED: "分析失败",
  BLOCKED: "已拦截",
  MODEL_NOT_CONFIGURED: "未配置模型",
  STALE: "结果已失效",
  DISABLED: "后台分析已禁用",
};

const GATE_LABEL: Record<string, string> = {
  PASS: "门禁通过",
  AUTO_REPAIRED: "格式已自动修复",
  MARK_FOR_REVIEW: "含待律师确认项",
  HARD_BLOCKED: "命中安全红线",
};

function statusTone(status: WebAnalysisAgentState["status"]): string {
  if (status === "COMPLETED") return styles.webLawyerNotice ?? "";
  if (status === "RUNNING") return styles.eyebrow ?? "";
  if (status === "STALE" || status === "BLOCKED" || status === "FAILED") {
    return styles.webLawyerEmptyPanel ?? "";
  }
  return styles.eyebrow ?? "";
}

/** 极简 Markdown 渲染：标题、表格行、列表、引用、段落。 */
function renderMarkdownLines(markdown: string) {
  const lines = markdown.split("\n");
  return lines.map((line, index) => {
    const text = line.trimEnd();
    if (!text.trim()) return <div key={index} style={{ height: 8 }} />;
    const key = `md-${index}`;
    if (text.startsWith("# ")) return <h2 key={key}>{text.slice(2)}</h2>;
    if (text.startsWith("## ")) return <h3 key={key}>{text.slice(3)}</h3>;
    if (text.startsWith("### ")) return <h4 key={key}>{text.slice(4)}</h4>;
    if (text.startsWith("> ")) {
      return <blockquote key={key} style={{ margin: "4px 0", opacity: 0.85 }}>{text.slice(2)}</blockquote>;
    }
    if (text.startsWith("| ")) {
      if (/^\|[\s:|-]+\|$/.test(text)) return null;
      const cells = text.split("|").slice(1, -1).map((cell) => cell.trim());
      return (
        <div key={key} style={{ display: "grid", gridTemplateColumns: `repeat(${cells.length}, minmax(0, 1fr))`, gap: 8, fontSize: 13, padding: "2px 0" }}>
          {cells.map((cell, cellIndex) => (
            <span key={`${key}-${cellIndex}`}>{cell}</span>
          ))}
        </div>
      );
    }
    if (text.startsWith("- [ ] ")) return <p key={key}>☐ {text.slice(6)}</p>;
    if (text.startsWith("- ")) return <p key={key}>• {text.slice(2)}</p>;
    return <p key={key}>{text}</p>;
  });
}

export function WebDecisionPackage({
  caseId,
  caseNumber,
  onSessionExpired,
}: {
  caseId: string;
  caseNumber: string;
  onSessionExpired: () => void;
}) {
  const [state, setState] = useState<WebCaseAnalysisState | null>(null);
  const [report, setReport] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [allowImageIdentifiers, setAllowImageIdentifiers] = useState(false);
  const [draft, setDraft] = useState<ParameterDraft>(emptyParameterDraft());
  const [parametersLoaded, setParametersLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const next = await readWebCaseAnalysisState(caseId);
      setState(next);
      if (next.agent.reportAvailable) {
        try {
          setReport(await readWebAnalysisReport(caseId));
        } catch {
          setReport("");
        }
      }
    } catch (caught) {
      if (isWebLoginRequired(caught)) {
        onSessionExpired();
        return;
      }
      setError(caught instanceof Error ? caught.message : "读取分析状态失败。");
    }
  }, [caseId, onSessionExpired]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 回填律师已确认的计算参数（正式数字的唯一来源）
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const stored = await readWebCaseParameters(caseId);
        if (!cancelled && stored) setDraft(draftFromParameters(stored));
      } catch {
        // 读取失败时保持空白表单，由律师重新填写；不静默编造参数。
      } finally {
        if (!cancelled) setParametersLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, [caseId]);

  // 运行中自动轮询进度（不阻塞界面）。
  // refresh 的身份随父组件渲染变化，因此放进 ref：定时器不能被反复重建，
  // 否则在慢速/被节流的环境里会静默停止轮询，界面永远停在「分析进行中」。
  const refreshRef = useRef(refresh);
  useEffect(() => { refreshRef.current = refresh; }, [refresh]);
  const running = state?.agent.status === "RUNNING";
  useEffect(() => {
    if (!running) return;
    let timer = 0;
    let cancelled = false;
    const startedAt = Date.now();
    const tick = () => {
      if (cancelled) return;
      void refreshRef.current();
      const elapsed = Date.now() - startedAt;
      // 前 30 秒用较密节奏（降级路径可能几百毫秒就结束），之后放缓。
      timer = window.setTimeout(tick, elapsed < 30_000 ? 1_200 : 3_000);
    };
    timer = window.setTimeout(tick, 1_200);
    // 律师离开页面再回来时必须立刻对齐，而不是等下一次节流后的轮询。
    const wake = () => {
      if (document.visibilityState === "visible") void refreshRef.current();
    };
    window.addEventListener("focus", wake);
    document.addEventListener("visibilitychange", wake);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.removeEventListener("focus", wake);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [running, caseId]);

  const start = async () => {
    const invalid = validateParameterDraft(draft);
    const touched = Boolean(
      draft.capPercent.trim() || draft.interestCutoff.trim()
      || draft.debts.some((debt) => (
        debt.principal.trim() || debt.disbursedOn.trim() || debt.dueOn.trim() || debt.ratePercent.trim()
      )),
    );
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (invalid && touched) {
        // 已经填了一部分：绝不静默丢弃律师输入的数字。
        setError(`案件计算参数未填完整：${invalid}`);
        return;
      }
      let caseConfig: Record<string, unknown> | undefined;
      if (!invalid) {
        const saved = await saveWebCaseParameters(caseId, parametersFromDraft(draft));
        if (saved) setDraft(draftFromParameters(saved));
        caseConfig = toWebCaseConfigPayload(parametersFromDraft(draft));
      }
      const next = await runWebCaseAgentAnalysis(caseId, {
        caseNumber,
        allowImageIdentifiers,
        ...(caseConfig ? { caseConfig } : {}),
      });
      setState(next);
      setNotice(next.agent.status === "MODEL_NOT_CONFIGURED"
        ? "未配置模型：已产出确定性结果与正式数字；配置模型后可获得深度分析。"
        : invalid
          ? "未填写案件计算参数：本次只做材料核对与争点分析，不产出正式数字；补齐参数后重新运行即可。"
          : "分析已开始，进度会自动刷新。");
    } catch (caught) {
      if (isWebLoginRequired(caught)) {
        onSessionExpired();
        return;
      }
      setError(caught instanceof Error ? caught.message : "启动分析失败。");
    } finally {
      setBusy(false);
    }
  };

  const saveParameters = async () => {
    const invalid = validateParameterDraft(draft);
    setError("");
    setNotice("");
    if (invalid) {
      setError(`案件计算参数未填完整：${invalid}`);
      return;
    }
    setBusy(true);
    try {
      const saved = await saveWebCaseParameters(caseId, parametersFromDraft(draft));
      if (saved) setDraft(draftFromParameters(saved));
      setNotice("计算参数已保存。参数变化会使既有决策包失效，请重新运行分析以刷新正式数字。");
      await refresh();
    } catch (caught) {
      if (isWebLoginRequired(caught)) {
        onSessionExpired();
        return;
      }
      setError(caught instanceof Error ? caught.message : "保存计算参数失败。");
    } finally {
      setBusy(false);
    }
  };

  const download = async (format: "md" | "docx") => {
    try {
      const blob = await exportWebAnalysis(caseId, format);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `案件决策包.${format}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "导出失败。");
    }
  };

  const agent = state?.agent;
  const numbers = useMemo(
    () => Object.entries(agent?.engineNumbers ?? {}),
    [agent?.engineNumbers],
  );

  return (
    <section className={styles.factsArea} aria-labelledby="web-decision-package-title">
      <header className={styles.webLawyerIntakeHeading}>
        <p className={styles.eyebrow}>Agent 分析</p>
        <h2 id="web-decision-package-title">案件决策包</h2>
        <p>
          Agent 只提议：读材料、提事实、找争点、给方向；金额由确定性引擎按律师确认参数计算；
          付款性质与诉讼立场由律师决定。
        </p>
      </header>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <span className={statusTone(agent?.status ?? "NOT_RUN")} role="status">
          {STATUS_LABEL[agent?.status ?? "NOT_RUN"]}
        </span>
        {agent?.gateLevel ? (
          <span className={styles.eyebrow}>{GATE_LABEL[agent.gateLevel] ?? agent.gateLevel}</span>
        ) : null}
        {agent && agent.calls > 0 ? (
          <span className={styles.eyebrow}>模型调用 {agent.calls} 次 · 已用 ¥{agent.costCny}</span>
        ) : null}
        <button type="button" onClick={() => void start()} disabled={busy || agent?.status === "RUNNING"}>
          {agent?.status === "RUNNING" ? "分析中…" : "开始分析"}
        </button>
        {agent?.reportAvailable ? (
          <>
            <button type="button" onClick={() => void download("md")}>导出 Markdown</button>
            <button type="button" onClick={() => void download("docx")}>导出 Word</button>
          </>
        ) : null}
      </div>

      <label style={{ display: "flex", gap: 8, alignItems: "flex-start", fontSize: 13 }}>
        <input
          type="checkbox"
          checked={allowImageIdentifiers}
          disabled={agent?.status === "RUNNING"}
          onChange={(event) => setAllowImageIdentifiers(event.target.checked)}
        />
        <span>
          扫描件页面以图像原样发送（勾选后：图像内的身份证号/银行卡号无法在本机自动脱敏，检出后逐项记为待核，不阻断整份分析）。
          不勾选时，一旦在扫描件文字里检出完整证件号/账号，分析会 fail closed 并提示先人工脱敏。
        </span>
      </label>

      <section aria-label="案件计算参数" style={{ maxWidth: 900, marginTop: 12 }}>
        <h3>案件计算参数（律师确认，正式数字的唯一来源）</h3>
        <p style={{ fontSize: 13, opacity: 0.85 }}>
          正式数字由确定性引擎按这里的参数计算，模型不参与任何计算。参数变化会使既有决策包失效，
          需重新运行分析。缺少出借凭证的借款请勾选「缺凭证挂起」，该笔不计入合计。
        </p>
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            司法保护上限月利率（%，例：1 表示月利率 1%）
            <input
              type="text"
              inputMode="decimal"
              value={draft.capPercent}
              placeholder="例如 1"
              onChange={(event) => setDraft({ ...draft, capPercent: event.target.value })}
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            利息暂计截止日
            <input
              type="date"
              value={draft.interestCutoff}
              onChange={(event) => setDraft({ ...draft, interestCutoff: event.target.value })}
            />
          </label>
        </div>

        <table style={{ width: "100%", fontSize: 13, marginTop: 8, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>编号</th>
              <th style={{ textAlign: "left" }}>本金（元）</th>
              <th style={{ textAlign: "left" }}>放款日</th>
              <th style={{ textAlign: "left" }}>到期日（可空）</th>
              <th style={{ textAlign: "left" }}>约定月利率（%）</th>
              <th style={{ textAlign: "left" }}>缺凭证挂起</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {draft.debts.map((debt, index) => {
              const update = (patch: Partial<ParameterDebtDraft>) => {
                const debts = draft.debts.map((item, itemIndex) =>
                  itemIndex === index ? { ...item, ...patch } : item);
                setDraft({ ...draft, debts });
              };
              return (
                <tr key={`debt-${index}`}>
                  <td><input type="text" style={{ width: 70 }} value={debt.debtId}
                             onChange={(event) => update({ debtId: event.target.value })} /></td>
                  <td><input type="text" inputMode="decimal" style={{ width: 110 }} value={debt.principal}
                             placeholder="100000"
                             onChange={(event) => update({ principal: event.target.value })} /></td>
                  <td><input type="date" value={debt.disbursedOn}
                             onChange={(event) => update({ disbursedOn: event.target.value })} /></td>
                  <td><input type="date" value={debt.dueOn}
                             onChange={(event) => update({ dueOn: event.target.value })} /></td>
                  <td><input type="text" inputMode="decimal" style={{ width: 80 }} value={debt.ratePercent}
                             placeholder="1.5"
                             onChange={(event) => update({ ratePercent: event.target.value })} /></td>
                  <td style={{ textAlign: "center" }}>
                    <input type="checkbox" checked={debt.evidencePending}
                           aria-label={`${debt.debtId || "该笔"}缺凭证挂起`}
                           onChange={(event) => update({ evidencePending: event.target.checked })} />
                  </td>
                  <td>
                    <button type="button" disabled={draft.debts.length <= 1}
                            onClick={() => setDraft({ ...draft,
                              debts: draft.debts.filter((_, itemIndex) => itemIndex !== index) })}>
                      删除
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div style={{ display: "flex", gap: 12, marginTop: 8 }}>
          <button type="button" disabled={busy} onClick={() => setDraft({
            ...draft,
            debts: [...draft.debts, {
              debtId: `L${draft.debts.length + 1}`, principal: "", disbursedOn: "", dueOn: "",
              ratePercent: draft.debts[0]?.ratePercent ?? "", evidencePending: false,
            }],
          })}>新增一笔借款</button>
          <button type="button" disabled={busy || !parametersLoaded}
                  onClick={() => void saveParameters()}>仅保存参数</button>
          <span style={{ fontSize: 12, opacity: 0.7 }}>
            {parametersLoaded ? "参数保存在本机案卷内，重新打开页面会自动回填。" : "正在读取已保存参数…"}
          </span>
        </div>
      </section>

      {agent?.status === "RUNNING" ? (
        <p role="status">
          进度 {agent.progress}%{agent.stage ? ` · ${agent.stage}` : ""}（OCR 与模型分析需要数分钟，可离开页面稍后回来）
        </p>
      ) : null}
      {notice ? <p className={styles.webLawyerNotice} role="status">{notice}</p> : null}
      {agent?.error ? <p role="alert">{agent.error}</p> : null}
      {error ? <p role="alert">{error}</p> : null}

      {numbers.length > 0 ? (
        <section aria-label="正式数字">
          <h3>正式数字（引擎输出，模型未参与计算）</h3>
          <ul>
            {numbers.map(([key, value]) => (
              <li key={key}>{key}：{value}</li>
            ))}
          </ul>
        </section>
      ) : null}

      {report ? (
        <section aria-label="决策包正文" style={{ maxWidth: 900 }}>
          {renderMarkdownLines(report)}
        </section>
      ) : (
        <section className={styles.webLawyerEmptyPanel}>
          {agent?.status === "MODEL_NOT_CONFIGURED"
            ? "当前未配置模型：点击「开始分析」仍会完成材料导入与正式数字计算，并在报告中给出降级说明。"
            : "尚无决策包。点击「开始分析」，系统将先做确定性材料核对，再调用模型形成争点、对抗与决策清单。"}
        </section>
      )}
    </section>
  );
}
