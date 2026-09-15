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
  WEB_LOSS_BASES,
  WEB_PAYMENT_CLASSES,
  type WebCaseParameterPayment,
  type WebCaseParameters,
} from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";
import { WebMarkdown } from "@/components/web-markdown";

/* ------------------------------------------------ 案件计算参数（界面用百分比，契约用小数） */

type ParameterDebtDraft = {
  debtId: string;
  principal: string;
  disbursedOn: string;
  dueOn: string;
  ratePercent: string;
  evidencePending: boolean;
};

type ParameterPaymentDraft = {
  paymentId: string;
  paidOn: string;
  amount: string;
  classification: string;
  debtId: string;
  memo: string;
};

type SalesClaimDraft = {
  enabled: boolean;
  claimAmount: string;
  confirmedPrincipal: string;
  overdueFrom: string;
  cutoff: string;
  lossBasis: string;
  /** 百分数形式（3 表示年化 3%） */
  lprAnnualPercent: string;
  agreedAnnualPercent: string;
};

type ParameterDraft = {
  capPercent: string;
  interestCutoff: string;
  debts: ParameterDebtDraft[];
  payments: ParameterPaymentDraft[];
  salesClaim: SalesClaimDraft;
};

function emptyParameterDraft(): ParameterDraft {
  return {
    capPercent: "",
    interestCutoff: "",
    debts: [{ debtId: "L1", principal: "", disbursedOn: "", dueOn: "",
              ratePercent: "", evidencePending: false }],
    payments: [],
    salesClaim: {
      enabled: false, claimAmount: "", confirmedPrincipal: "", overdueFrom: "",
      cutoff: "", lossBasis: "LPR", lprAnnualPercent: "", agreedAnnualPercent: "",
    },
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
    payments: parameters.payments.map((payment) => ({
      paymentId: payment.paymentId,
      paidOn: payment.paidOn,
      amount: payment.amount,
      classification: payment.classification,
      debtId: payment.debtId,
      memo: payment.memo,
    })),
    salesClaim: {
      enabled: parameters.salesClaim.enabled,
      claimAmount: parameters.salesClaim.claimAmount,
      confirmedPrincipal: parameters.salesClaim.confirmedPrincipal,
      overdueFrom: parameters.salesClaim.overdueFrom,
      cutoff: parameters.salesClaim.cutoff,
      lossBasis: parameters.salesClaim.lossBasis,
      lprAnnualPercent: percentFromDecimal(parameters.salesClaim.lprAnnualPercent),
      agreedAnnualPercent: percentFromDecimal(parameters.salesClaim.agreedAnnualPercent),
    },
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
    payments: draft.payments.map((payment, index) => ({
      paymentId: payment.paymentId.trim() || `P${index + 1}`,
      paidOn: payment.paidOn.trim(),
      amount: payment.amount.trim(),
      classification: payment.classification,
      debtId: payment.debtId.trim(),
      memo: payment.memo.trim(),
    })),
    salesClaim: {
      enabled: draft.salesClaim.enabled,
      claimAmount: draft.salesClaim.claimAmount.trim(),
      confirmedPrincipal: draft.salesClaim.confirmedPrincipal.trim(),
      overdueFrom: draft.salesClaim.overdueFrom.trim(),
      cutoff: draft.salesClaim.cutoff.trim(),
      lossBasis: draft.salesClaim.lossBasis,
      lprAnnualPercent: decimalFromPercent(draft.salesClaim.lprAnnualPercent),
      agreedAnnualPercent: decimalFromPercent(draft.salesClaim.agreedAnnualPercent),
    },
  };
}

const _DATE_INPUT = /^\d{4}-\d{2}-\d{2}$/;
const _MONEY_INPUT = /^\d+(?:\.\d{1,2})?$/;
const _PERCENT_INPUT = /^\d+(?:\.\d+)?$/;

/** 前端只做格式校验；法律口径与金额一律由律师填写的参数和确定性引擎决定。 */
function validateParameterDraft(draft: ParameterDraft): string {
  if (!draft.salesClaim.enabled) {
    if (!_PERCENT_INPUT.test(draft.capPercent.trim()) || Number(draft.capPercent) <= 0) {
      return "请填写司法保护上限月利率（按百分比填，例如月利率 1% 填 1）。";
    }
    if (!_DATE_INPUT.test(draft.interestCutoff.trim())) {
      return "请填写利息暂计截止日（YYYY-MM-DD）。";
    }
    if (draft.debts.length === 0) return "至少填写一笔借款。";
  }
  const seen = new Set<string>();
  for (const debt of draft.salesClaim.enabled ? [] : draft.debts) {
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
  if (draft.salesClaim.enabled) {
    const sales = draft.salesClaim;
    if (!_MONEY_INPUT.test(sales.claimAmount.trim()) || Number(sales.claimAmount) <= 0) {
      return "买卖合同：请填写原告主张的货款金额（元）。";
    }
    if (sales.confirmedPrincipal.trim()
        && (!_MONEY_INPUT.test(sales.confirmedPrincipal.trim())
            || Number(sales.confirmedPrincipal) <= 0)) {
      return "买卖合同：律师确认应付货款请填数字（元），或留空表示与主张金额一致。";
    }
    if (!_DATE_INPUT.test(sales.overdueFrom.trim())) {
      return "买卖合同：请填写逾期起算日（YYYY-MM-DD）。";
    }
    if (!_DATE_INPUT.test(sales.cutoff.trim())) {
      return "买卖合同：请填写暂计截止日（YYYY-MM-DD）。";
    }
    if (!WEB_LOSS_BASES.some((item) => item.id === sales.lossBasis)) {
      return "买卖合同：请选择逾期损失口径。";
    }
    if (["LPR", "LPR_1_5"].includes(sales.lossBasis)
        && (!_PERCENT_INPUT.test(sales.lprAnnualPercent.trim())
            || Number(sales.lprAnnualPercent) <= 0)) {
      return "买卖合同：请填写逾期起算时的一年期 LPR（按百分比填，3 表示 3%）。";
    }
    if (sales.lossBasis === "AGREED"
        && (!_PERCENT_INPUT.test(sales.agreedAnnualPercent.trim())
            || Number(sales.agreedAnnualPercent) <= 0)) {
      return "买卖合同：请填写约定的年化违约金率（按百分比填，18 表示年化 18%）。";
    }
  }
  const paymentIds = new Set<string>();
  for (const [index, payment] of draft.payments.entries()) {
    const label = `第 ${index + 1} 笔付款`;
    const paymentId = payment.paymentId.trim();
    if (!paymentId) return `${label}：请填付款编号，例如 P1。`;
    if (paymentIds.has(paymentId)) return `付款编号重复：${paymentId}`;
    paymentIds.add(paymentId);
    if (!_DATE_INPUT.test(payment.paidOn.trim())) return `${label}：付款日请填 YYYY-MM-DD。`;
    if (!_MONEY_INPUT.test(payment.amount.trim()) || Number(payment.amount) <= 0) {
      return `${label}：金额请填数字（元）。`;
    }
    if (!WEB_PAYMENT_CLASSES.includes(payment.classification)) {
      return `${label}：性质必须为 ${WEB_PAYMENT_CLASSES.join(" / ")}。`;
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
      ))
      || draft.payments.length > 0
      || draft.salesClaim.enabled
      || draft.salesClaim.claimAmount.trim() || draft.salesClaim.overdueFrom.trim()
      || draft.salesClaim.cutoff.trim(),
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
          : `分析已开始，进度会自动刷新。（当前口径：${draft.salesClaim.enabled ? "买卖合同货款" : "民间借贷"}）`);
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
          <button type="button" disabled={busy} onClick={() => setDraft({
            ...draft,
            payments: [...draft.payments, {
              paymentId: `P${draft.payments.length + 1}`, paidOn: "", amount: "",
              classification: "付息", debtId: draft.debts[0]?.debtId ?? "", memo: "",
            }],
          })}>新增一笔已付款项</button>
          <button type="button" disabled={busy || !parametersLoaded}
                  onClick={() => void saveParameters()}>仅保存参数</button>
          <span style={{ fontSize: 12, opacity: 0.7 }}>
            {parametersLoaded ? "参数保存在本机案卷内，重新打开页面会自动回填。" : "正在读取已保存参数…"}
          </span>
        </div>

        <h4 style={{ marginTop: 16 }}>案由口径</h4>
        <p style={{ fontSize: 13, opacity: 0.85 }}>
          民间借贷按司法保护上限（LPR 四倍）计算；买卖合同货款按下面的口径计算。
          两套规则不混用——引擎只按你选的口径算。
        </p>
        <label style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}>
          <input type="checkbox" checked={draft.salesClaim.enabled}
                 aria-label="按买卖合同货款口径计算"
                 onChange={(event) => setDraft({
                   ...draft,
                   salesClaim: { ...draft.salesClaim, enabled: event.target.checked },
                 })} />
          <span>按买卖合同货款口径计算（勾选后不再使用借贷的 LPR 四倍口径）</span>
        </label>
        {draft.salesClaim.enabled ? (
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13, marginTop: 8 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              原告主张货款（元）
              <input type="text" inputMode="decimal" value={draft.salesClaim.claimAmount}
                     placeholder="10000"
                     aria-label="原告主张货款"
                     onChange={(event) => setDraft({
                       ...draft,
                       salesClaim: { ...draft.salesClaim, claimAmount: event.target.value },
                     })} />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              律师确认应付货款（元，留空=与主张一致）
              <input type="text" inputMode="decimal" value={draft.salesClaim.confirmedPrincipal}
                     placeholder="留空表示与主张金额一致"
                     aria-label="律师确认应付货款"
                     onChange={(event) => setDraft({
                       ...draft,
                       salesClaim: { ...draft.salesClaim,
                                     confirmedPrincipal: event.target.value },
                     })} />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              逾期起算日
              <input type="date" value={draft.salesClaim.overdueFrom}
                     aria-label="逾期起算日"
                     onChange={(event) => setDraft({
                       ...draft,
                       salesClaim: { ...draft.salesClaim, overdueFrom: event.target.value },
                     })} />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              暂计截止日
              <input type="date" value={draft.salesClaim.cutoff}
                     aria-label="暂计截止日"
                     onChange={(event) => setDraft({
                       ...draft,
                       salesClaim: { ...draft.salesClaim, cutoff: event.target.value },
                     })} />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              逾期损失口径
              <select value={draft.salesClaim.lossBasis} aria-label="逾期损失口径"
                      onChange={(event) => setDraft({
                        ...draft,
                        salesClaim: { ...draft.salesClaim, lossBasis: event.target.value },
                      })}>
                {WEB_LOSS_BASES.map((item) => (
                  <option key={item.id} value={item.id}>{item.label}</option>
                ))}
              </select>
            </label>
            {["LPR", "LPR_1_5"].includes(draft.salesClaim.lossBasis) ? (
              <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                逾期起算时一年期 LPR（%，3 表示 3%）
                <input type="text" inputMode="decimal" value={draft.salesClaim.lprAnnualPercent}
                       placeholder="3"
                       aria-label="一年期LPR"
                       onChange={(event) => setDraft({
                         ...draft,
                         salesClaim: { ...draft.salesClaim,
                                       lprAnnualPercent: event.target.value },
                       })} />
              </label>
            ) : null}
            {draft.salesClaim.lossBasis === "AGREED" ? (
              <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                约定年化违约金率（%，18 表示年化 18%）
                <input type="text" inputMode="decimal"
                       value={draft.salesClaim.agreedAnnualPercent} placeholder="18"
                       aria-label="约定年化违约金率"
                       onChange={(event) => setDraft({
                         ...draft,
                         salesClaim: { ...draft.salesClaim,
                                       agreedAnnualPercent: event.target.value },
                       })} />
              </label>
            ) : null}
          </div>
        ) : null}

        <h4 style={{ marginTop: 16 }}>已付款项性质确认（律师逐笔确认后才进入计算）</h4>
        <p style={{ fontSize: 13, opacity: 0.85 }}>
          只有「还本 / 付息 / 代付」进入确定性计算，按法定顺序先冲利息、后冲本金；
          「争议 / 排除」仅登记，不进入正式数字。未确认性质的付款一律不进计算。
        </p>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left" }}>编号</th>
              <th style={{ textAlign: "left" }}>付款日</th>
              <th style={{ textAlign: "left" }}>金额（元）</th>
              <th style={{ textAlign: "left" }}>性质</th>
              <th style={{ textAlign: "left" }}>归属借款</th>
              <th style={{ textAlign: "left" }}>备注</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {draft.payments.map((payment, index) => {
              const update = (patch: Partial<WebCaseParameterPayment>) => {
                const payments = draft.payments.map((item, itemIndex) =>
                  itemIndex === index
                    ? {
                        paymentId: patch.paymentId ?? item.paymentId,
                        paidOn: patch.paidOn ?? item.paidOn,
                        amount: patch.amount ?? item.amount,
                        classification: patch.classification ?? item.classification,
                        debtId: patch.debtId ?? item.debtId,
                        memo: patch.memo ?? item.memo,
                      }
                    : item);
                setDraft({ ...draft, payments });
              };
              return (
                <tr key={`payment-${index}`}>
                  <td><input type="text" style={{ width: 60 }} value={payment.paymentId}
                             aria-label={`第 ${index + 1} 笔付款编号`}
                             onChange={(event) => update({ paymentId: event.target.value })} /></td>
                  <td><input type="date" value={payment.paidOn}
                             aria-label={`第 ${index + 1} 笔付款日`}
                             onChange={(event) => update({ paidOn: event.target.value })} /></td>
                  <td><input type="text" inputMode="decimal" style={{ width: 100 }}
                             value={payment.amount} placeholder="2250.00"
                             aria-label={`第 ${index + 1} 笔付款金额`}
                             onChange={(event) => update({ amount: event.target.value })} /></td>
                  <td>
                    <select value={payment.classification}
                            aria-label={`第 ${index + 1} 笔付款性质`}
                            onChange={(event) => update({ classification: event.target.value })}>
                      {WEB_PAYMENT_CLASSES.map((option) => (
                        <option key={option} value={option}>{option}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select value={payment.debtId}
                            aria-label={`第 ${index + 1} 笔付款归属借款`}
                            onChange={(event) => update({ debtId: event.target.value })}>
                      <option value="">（不指定）</option>
                      {draft.debts.map((debt) => (
                        <option key={debt.debtId} value={debt.debtId}>{debt.debtId}</option>
                      ))}
                    </select>
                  </td>
                  <td><input type="text" style={{ width: 140 }} value={payment.memo}
                             aria-label={`第 ${index + 1} 笔付款备注`}
                             onChange={(event) => update({ memo: event.target.value })} /></td>
                  <td>
                    <button type="button" onClick={() => setDraft({
                      ...draft,
                      payments: draft.payments.filter((_, itemIndex) => itemIndex !== index),
                    })}>删除</button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
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
          <WebMarkdown markdown={report} />
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
