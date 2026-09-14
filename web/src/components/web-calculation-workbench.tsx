"use client";

import { useEffect, useState } from "react";
import {
  createWebFormalCalculation,
  isWebLoginRequired,
  readWebCaseReview,
  readWebCurrentFormalCalculation,
  readWebCaseReadiness,
  type WebFormalCalculation,
  type WebCaseReadiness,
  type WebCaseReview,
} from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";

export function WebCalculationWorkbench({
  caseId,
  caseVersion,
  canRunCalculation,
  onSessionExpired,
  onVersionAdvanced,
}: {
  caseId: string;
  caseVersion: number;
  canRunCalculation: boolean;
  onSessionExpired: () => void;
  onVersionAdvanced: (caseId: string, version: number) => void;
}) {
  const [obligationId, setObligationId] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [allocationPolicy, setAllocationPolicy] = useState<"INTEREST_THEN_PRINCIPAL" | "PRINCIPAL_THEN_INTEREST">("INTEREST_THEN_PRINCIPAL");
  const [calculation, setCalculation] = useState<WebFormalCalculation | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [readiness, setReadiness] = useState<WebCaseReadiness | null>(null);
  const [review, setReview] = useState<WebCaseReview | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void readWebCaseReadiness(caseId, controller.signal)
      .then((next) => setReadiness(next))
      .catch(() => setReadiness(null));
    return () => controller.abort();
  }, [caseId]);

  useEffect(() => {
    const controller = new AbortController();
    void readWebCaseReview(caseId, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setReview(next);
        const first = calculationItemsFromReview(next)[0];
        setObligationId((current) => current || first?.obligationLabel || "");
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        if (isWebLoginRequired(reason)) onSessionExpired();
        setReview(null);
      });
    return () => controller.abort();
  }, [caseId, caseVersion, onSessionExpired]);

  const calculationReady = readiness?.matterId === caseId && readiness.matterVersion === caseVersion && readiness.checks.every((check) => check.status === "READY");
  const calculationItems = review ? calculationItemsFromReview(review) : [];

  async function loadCurrent() {
    if (!obligationId) {
      setMessage("请先在案件要点中确认一笔已核实收付款的性质和归属事项；系统不会猜测本金或债务关系。");
      return;
    }
    setLoading(true);
    setMessage(null);
    try {
      const next = await readWebCurrentFormalCalculation(caseId, obligationId.trim());
      setCalculation(next);
      onVersionAdvanced(caseId, next.matterVersion);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setMessage(reason instanceof Error ? reason.message : "无法读取当前利息测算。");
    } finally {
      setLoading(false);
    }
  }

  async function submitCalculation(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canRunCalculation) {
      setMessage("当前角色不能批准正式测算；请由案件负责人完成确认。");
      return;
    }
    if (!calculationReady) {
      setMessage(readiness?.nextAction ?? "案件前置条件尚未完成，系统不会开始正式测算。");
      return;
    }
    setSubmitting(true);
    setMessage(null);
    try {
      const receipt = await createWebFormalCalculation({ caseId, obligationId, expectedVersion: caseVersion, startDate, endDate, allocationPolicy });
      onVersionAdvanced(caseId, receipt.matterVersion);
      const next = await readWebCurrentFormalCalculation(caseId, obligationId);
      setCalculation(next);
      setMessage("测算已完成，可查看本次结果。");
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setMessage(reason instanceof Error ? reason.message : "测算未完成；系统没有显示未经核验的金额。");
    } finally {
      setSubmitting(false);
    }
  }

  const run = calculation?.run;
  return (
    <section className={styles.webLawyerCreatePanel} aria-labelledby="web-calculation-title">
      <div>
        <p className={styles.eyebrow}>金额核对</p>
        <h2 id="web-calculation-title">核对利息和还款结果</h2>
        <p>选择已确认的款项归属事项，再核对计算期间和抵扣顺序。利率、付款记录和规则均以本案已确认内容为准。</p>
      </div>
      <form onSubmit={submitCalculation}>
        <label>
          <span>选择需要核对的事项</span>
          <select disabled={calculationItems.length === 0} onChange={(event) => setObligationId(event.target.value)} value={obligationId}>
            {calculationItems.length === 0 ? <option value="">请先完成款项性质与归属确认</option> : calculationItems.map((item) => <option key={item.obligationLabel} value={item.obligationLabel}>{item.obligationLabel} · 已确认 {item.classificationCount} 笔</option>)}
          </select>
        </label>
        {calculationItems.length === 0 ? <p className={styles.webLawyerUploadEmpty}>尚无可用于金额核对的事项。请先在<a href={`/facts?case=${encodeURIComponent(caseId)}#payment-classification`}>“确认案情”</a>中确认每笔收付款的性质和归属。</p> : null}
        <div className={styles.webCalculationFormGrid}>
          <label><span>计算开始日期</span><input required type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label>
          <label><span>计算结束日期（不含当日）</span><input required type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></label>
        </div>
        <label><span>还款抵扣顺序</span><select value={allocationPolicy} onChange={(event) => setAllocationPolicy(event.target.value as typeof allocationPolicy)}><option value="INTEREST_THEN_PRINCIPAL">先抵利息，再抵本金</option><option value="PRINCIPAL_THEN_INTEREST">先抵本金，再抵利息</option></select></label>
        <div className={styles.webLawyerCreateActions}><button className={styles.webLawyerPrimaryAction} disabled={submitting || !canRunCalculation || !calculationReady || !obligationId} type="submit">{submitting ? "正在核对…" : "开始金额核对"}</button><button className={styles.webLawyerSecondaryAction} disabled={loading || !obligationId} onClick={() => void loadCurrent()} type="button">{loading ? "读取中…" : "查看已有结果"}</button><small>案件信息、款项归属或规则变化后，旧结果需要重新核对。</small></div>
      </form>
      {readiness && !calculationReady ? <section className={styles.webReadinessPanel} aria-label="测算前置状态"><header><strong>测算暂未开放</strong><span>需全部满足</span></header><div className={styles.webReadinessChecks}>{readiness.checks.map((check) => <div className={check.status === "READY" ? styles.webReadinessReady : styles.webReadinessBlocked} key={check.key}><span>{check.status === "READY" ? "已满足" : "待处理"}</span><strong>{check.label}</strong><small>{check.detail}</small></div>)}</div></section> : null}
      {message ? <p className={styles.webLawyerNotice} role="status">{message}</p> : null}
      {!calculation ? <p className={styles.webLawyerUploadEmpty}>暂未形成金额结果。完成本案事实、交易和规则核对后，可在这里查看。</p> : run ? <CalculationResult calculation={calculation} /> : <p className={styles.webLawyerUploadEmpty}>该事项尚无可用的金额结果。请先确认事实、收付款记录和适用规则。</p>}
    </section>
  );
}

function calculationItemsFromReview(review: WebCaseReview): readonly { obligationLabel: string; classificationCount: number }[] {
  const counts = new Map<string, number>();
  for (const classification of review.paymentClassifications) {
    if (classification.status !== "APPROVED") continue;
    for (const allocation of classification.allocations) {
      counts.set(allocation.obligationLabel, (counts.get(allocation.obligationLabel) ?? 0) + 1);
    }
  }
  return [...counts.entries()]
    .map(([obligationLabel, classificationCount]) => ({ obligationLabel, classificationCount }))
    .sort((left, right) => left.obligationLabel.localeCompare(right.obligationLabel, "zh-CN"));
}

function CalculationResult({ calculation }: { calculation: WebFormalCalculation }) {
  const run = calculation.run;
  if (!run) return null;
  return (
    <section className={styles.webCalculationResult} aria-label="已验证利息测算结果">
      <header><div><p className={styles.eyebrow}>已完成核对</p><h3>本次金额结果</h3></div><span>案件版本 {calculation.matterVersion}</span></header>
      <dl className={styles.webLawyerReceipt}>
        <div><dt>累计应计利息</dt><dd>{run.totalInterestAccrued ?? "—"} CNY</dd></div>
        <div><dt>已抵扣利息</dt><dd>{run.totalInterestPaid ?? "—"} CNY</dd></div>
        <div><dt>剩余本金</dt><dd>{run.remainingPrincipal ?? "—"} CNY</dd></div>
        <div><dt>未付利息</dt><dd>{run.remainingUnpaidInterest ?? "—"} CNY</dd></div>
        <div><dt>未分配款项</dt><dd>{run.unappliedPayments ?? "—"} CNY</dd></div>
        <div><dt>独立复核</dt><dd>通过 · {run.engineVersion}</dd></div>
      </dl>
      <p className={styles.webLawyerUploadEmpty}>结果绑定法律规则包、交易快照、输入哈希和独立复核哈希；如案件版本、事实或规则发生变化，系统会要求重新测算。</p>
      <div className={styles.webCalculationLines}><strong>期间明细（{run.lineItems.length} 段）</strong>{run.lineItems.slice(0, 100).map((line) => <div key={line.lineSequence}><span>{line.periodStart} 至 {line.periodEnd}</span><span>{line.dayCount} 天 · 年利率 {line.annualRate ?? "—"} · 应计 {line.accruedInterest ?? "—"} CNY</span></div>)}</div>
    </section>
  );
}
