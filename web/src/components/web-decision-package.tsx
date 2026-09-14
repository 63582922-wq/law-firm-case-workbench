"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  exportWebAnalysis,
  readWebAnalysisReport,
  readWebCaseAnalysisState,
  runWebCaseAgentAnalysis,
  isWebLoginRequired,
  type WebAnalysisAgentState,
  type WebCaseAnalysisState,
} from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";

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

  // 运行中自动轮询进度（不阻塞界面）
  useEffect(() => {
    if (state?.agent.status !== "RUNNING") return;
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [state?.agent.status, refresh]);

  const start = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const next = await runWebCaseAgentAnalysis(caseId, { caseNumber });
      setState(next);
      setNotice(next.agent.status === "MODEL_NOT_CONFIGURED"
        ? "未配置模型：已产出确定性结果与正式数字；配置模型后可获得深度分析。"
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
