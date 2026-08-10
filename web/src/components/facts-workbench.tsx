"use client";

import { useEffect, useState } from "react";
import {
  caseDataSourceConfig,
  confirmSyntheticFact,
  loadCaseReview,
  loadMoreCaseFacts,
  loadMoreCaseTransactions,
  type CaseReviewView,
} from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

export function FactsWorkbench() {
  const [review, setReview] = useState<CaseReviewView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState<"facts" | "transactions" | null>(null);

  useEffect(() => {
    void reloadReview();
  }, []);

  async function reloadReview() {
    setError(null);
    setPageError(null);
    try {
      setReview(await loadCaseReview());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取案件台账");
    }
  }

  async function loadMore(kind: "facts" | "transactions") {
    if (!review || loadingMore) return;
    setLoadingMore(kind);
    setPageError(null);
    try {
      setReview(kind === "facts" ? await loadMoreCaseFacts(review) : await loadMoreCaseTransactions(review));
    } catch (cause) {
      setPageError(cause instanceof Error ? cause.message : "后续记录未能载入，请重新载入案件。");
    } finally {
      setLoadingMore(null);
    }
  }

  async function confirmCandidate(factId: string) {
    setConfirming(factId);
    try {
      setReview(await confirmSyntheticFact(factId));
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法确认候选事实"); }
    finally { setConfirming(null); }
  }

  if (error) return <section className={styles.calculationBlocked}><p className={styles.eyebrow}>事实与争点</p><h3>{caseDataSourceConfig.kind === "persistent-disabled" ? "持久化模式未启用" : "案件台账未连接"}</h3><p>{error}</p><small>系统没有回退到另一套数据，也没有把未连接状态显示为成功。</small><button className={styles.candidateAction} onClick={() => void reloadReview()} type="button">重新载入案件</button></section>;
  if (!review) return <section className={styles.calculationLoading}>正在读取案件审批链生成的版本化快照…</section>;

  return <section className={styles.factsArea} aria-label="事实与争点台账">
    <header className={styles.evidenceHeading}><div><p className={styles.eyebrow}>结构化审查</p><h2>{review.matterTitle ?? "事实、诉请与交易台账"}</h2></div><div className={styles.dataSourceState}><strong>{review.sourceLabel}</strong><small>{review.matterVersion ? `案件版本 ${review.matterVersion}` : "仅合成数据"}</small></div></header>
    <div className={styles.factsGrid}>
      <LedgerSection title="律师已处理事实" note="候选内容与律师决定分开显示。">
        {review.facts.map((fact) => <div className={styles.ledgerRow} key={fact.factId}><strong>{fact.text}</strong><small>{statusLabel(fact.status)} · {fact.origin} · {fact.evidenceCount} 个原始证据定位</small></div>)}
        <LedgerPagination loaded={review.factPage.loadedCount} total={review.factPage.totalCount} hasMore={review.factPage.hasMore} busy={loadingMore === "facts"} onMore={() => void loadMore("facts")} />
      </LedgerSection>
      <LedgerSection title="诉请回应" note="立场与金额分开保存。">
        {review.claims.map((claim) => <div className={styles.ledgerRow} key={claim.claimId}><strong>{claim.text}</strong><small>{statusLabel(claim.position)} · {claim.responseAmount ?? "—"} {claim.currency ?? ""}</small></div>)}
      </LedgerSection>
      <LedgerSection title="争点" note="每个确认争点后续须有证据矩阵项。">
        {review.issues.map((issue) => <div className={styles.ledgerRow} key={issue.issueId}><strong>{issue.question}</strong><small>{statusLabel(issue.status)} · {issue.factCount} 项确认事实 · {issue.claimCount} 项诉请范围</small></div>)}
      </LedgerSection>
      <LedgerSection title="计算前交易快照" note="付款性质决定是否可进入测算。">
        {review.transactions.map((transaction) => <div className={styles.ledgerRow} key={transaction.transactionId}><strong>{transaction.date ?? "日期待确认"} · {currencySymbol(transaction.currency)} {transaction.amount}</strong><small>{statusLabel(transaction.status)} · {statusLabel(transaction.nature)} · {transaction.application} · {transaction.currency}</small></div>)}
        <LedgerPagination loaded={review.transactionPage.loadedCount} total={review.transactionPage.totalCount} hasMore={review.transactionPage.hasMore} busy={loadingMore === "transactions"} onMore={() => void loadMore("transactions")} />
      </LedgerSection>
      {review.pendingFacts.length > 0 && <LedgerSection title="待律师确认的事实候选" note={review.sourceKind === "synthetic-alpha" ? "此操作仅改变本机合成台账。" : "持久化确认必须绑定案件版本与审计请求。"}>
        {review.pendingFacts.map((fact) => <div className={styles.ledgerRow} key={fact.factId}><strong>{fact.text}</strong><small>{fact.origin} · {fact.evidenceCount} 个原始证据定位</small>{review.sourceKind === "synthetic-alpha" ? <button className={styles.candidateAction} disabled={confirming === fact.factId} onClick={() => void confirmCandidate(fact.factId)} type="button">{confirming === fact.factId ? "正在确认…" : "确认合成候选"}</button> : <button className={styles.disabledAction} disabled type="button">请在版本化审批流程确认</button>}</div>)}
      </LedgerSection>}
    </div>
    {pageError && <div className={styles.inlineError} role="alert"><strong>后续记录未载入</strong><span>{pageError}</span><button className={styles.candidateAction} onClick={() => void reloadReview()} type="button">重新载入当前案件</button></div>}
    <p className={styles.resultHash}>案件摘要投影 {review.snapshotHash.slice(0, 16)}…{review.transactionSnapshotHash ? `；交易快照 ${review.transactionSnapshotHash.slice(0, 16)}…` : ""}。后续页严格绑定案件版本；修改上游材料或律师决定后，必须重新载入。{review.requestId ? ` 请求号 ${review.requestId}` : ""}</p>
  </section>;
}

function LedgerSection({ title, note, children }: { title: string; note: string; children: React.ReactNode }) {
  return <article className={styles.ledgerCard}><div><p className={styles.cardKicker}>{title}</p><small>{note}</small></div>{children}</article>;
}

function LedgerPagination({ loaded, total, hasMore, busy, onMore }: { loaded: number; total: number; hasMore: boolean; busy: boolean; onMore: () => void }) {
  return <div className={styles.ledgerPagination} aria-live="polite"><small>已载入 {loaded} / {total} 条</small>{hasMore && <button className={styles.candidateAction} disabled={busy} onClick={onMore} type="button">{busy ? "正在载入…" : "继续载入 50 条"}</button>}</div>;
}

function currencySymbol(currency: string) {
  return currency === "CNY" ? "人民币 ¥" : currency;
}

function statusLabel(value: string) {
  const labels: Record<string, string> = {
    CONFIRMED: "已确认", DISPUTED: "有争议", DENIED: "不认可", INVALIDATED: "已失效", CANDIDATE: "待确认",
    CONFIRMED_SCOPE: "范围已确认", APPROVED: "已批准", ADMIT: "认可", PARTIALLY_ADMIT: "部分认可", DISPUTE: "不予认可",
    OUTSIDE_SCOPE: "超出诉请范围", DISBURSEMENT: "出借款", REPAYMENT_UNSPECIFIED: "还款性质待分配", INTEREST_PAYMENT: "支付利息",
    PRINCIPAL_REPAYMENT: "归还本金", REFUND: "退款", FEE: "费用", UNRELATED: "与本案无关", PAYMENT: "付款事件",
  };
  return labels[value] ?? value;
}
