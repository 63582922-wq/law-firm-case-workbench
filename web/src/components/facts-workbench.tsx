"use client";

import { useEffect, useState } from "react";
import {
  caseDataSourceConfig,
  approvePaymentClassification,
  confirmSyntheticFact,
  confirmPersistentTransaction,
  createPaymentClassificationCandidate,
  decidePersistentFact,
  loadCaseReview,
  loadMoreCaseFacts,
  loadMoreCaseTransactions,
  type CaseReviewView,
} from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

type PaymentClassificationDraft = {
  transactionId: string;
  nature: "DISBURSEMENT" | "REPAYMENT_UNSPECIFIED" | "INTEREST_PAYMENT" | "PRINCIPAL_REPAYMENT" | "REFUND" | "FEE" | "UNRELATED";
  obligationId: string;
  allocationAmount: string;
  sameDaySequence: string;
  confirmed: boolean;
};

export function FactsWorkbench() {
  const [review, setReview] = useState<CaseReviewView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState<"facts" | "transactions" | null>(null);
  const [classificationDraft, setClassificationDraft] = useState<PaymentClassificationDraft | null>(null);
  const [classificationBusy, setClassificationBusy] = useState<string | null>(null);
  const [classificationNotice, setClassificationNotice] = useState<string | null>(null);
  const [transactionConfirming, setTransactionConfirming] = useState<string | null>(null);
  const [transactionConfirmation, setTransactionConfirmation] = useState<string | null>(null);
  const [transactionConfirmed, setTransactionConfirmed] = useState(false);
  const [transactionNotice, setTransactionNotice] = useState<string | null>(null);

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
      if (review?.sourceKind === "persistent-preview" && review.matterVersion !== null) {
        await decidePersistentFact({ factId, expectedVersion: review.matterVersion, status: "CONFIRMED" });
        await reloadReview();
      } else {
        setReview(await confirmSyntheticFact(factId));
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法确认候选事实"); }
    finally { setConfirming(null); }
  }

  function startClassification(transaction: CaseReviewView["transactions"][number]) {
    setClassificationNotice(null);
    setClassificationDraft({
      transactionId: transaction.transactionId,
      nature: transaction.classification?.status === "CANDIDATE" ? transaction.nature as "DISBURSEMENT" | "REPAYMENT_UNSPECIFIED" | "INTEREST_PAYMENT" | "PRINCIPAL_REPAYMENT" | "REFUND" | "FEE" | "UNRELATED" : "REPAYMENT_UNSPECIFIED",
      obligationId: transaction.classification?.allocations[0]?.obligationId ?? "",
      allocationAmount: transaction.classification?.allocations[0]?.amount ?? transaction.amount,
      sameDaySequence: transaction.classification?.sameDaySequence?.toString() ?? "",
      confirmed: false,
    });
  }

  async function saveClassification(transaction: CaseReviewView["transactions"][number]) {
    if (!review || review.sourceKind !== "persistent-preview" || review.matterVersion === null || !classificationDraft) return;
    if (!classificationDraft.confirmed) {
      setClassificationNotice("请先确认付款性质、债务单元、同日顺序和原始交易证据继承关系。");
      return;
    }
    const sameDaySequence = classificationDraft.sameDaySequence.trim() ? Number(classificationDraft.sameDaySequence) : null;
    setClassificationBusy(transaction.transactionId);
    setClassificationNotice(null);
    try {
      const receipt = await createPaymentClassificationCandidate({
        expectedVersion: review.matterVersion,
        transactionId: transaction.transactionId,
        origin: "DEFENDANT_STATEMENT",
        nature: classificationDraft.nature,
        obligationId: classificationDraft.obligationId,
        allocationAmount: classificationDraft.allocationAmount,
        currency: transaction.currency,
        sameDaySequence,
      });
      await reloadReview();
      setClassificationDraft(null);
      setClassificationNotice(`付款分类候选已建立（案件版本 ${receipt.matterVersion}），请核对后单独批准。`);
    } catch (cause) {
      setClassificationNotice(cause instanceof Error ? cause.message : "付款分类候选未建立");
    } finally {
      setClassificationBusy(null);
    }
  }

  async function approveClassification(transaction: CaseReviewView["transactions"][number]) {
    if (!review || review.sourceKind !== "persistent-preview" || review.matterVersion === null || !transaction.classification || transaction.classification.status !== "CANDIDATE") return;
    setClassificationBusy(transaction.transactionId);
    setClassificationNotice(null);
    try {
      const receipt = await approvePaymentClassification({ expectedVersion: review.matterVersion, classificationId: transaction.classification.classificationId });
      await reloadReview();
      setClassificationNotice(`付款分类已批准（案件版本 ${receipt.matterVersion}）。后续正式计算会读取这项已批准分配。`);
    } catch (cause) {
      setClassificationNotice(cause instanceof Error ? cause.message : "付款分类未获批准");
    } finally {
      setClassificationBusy(null);
    }
  }

  function startTransactionConfirmation(transactionId: string) {
    setTransactionNotice(null);
    setTransactionConfirmation(transactionId);
    setTransactionConfirmed(false);
  }

  async function confirmTransaction(transaction: CaseReviewView["transactions"][number]) {
    if (!review || review.sourceKind !== "persistent-preview" || review.matterVersion === null || !transactionConfirmed) {
      setTransactionNotice("请先确认已按原始页核对交易日期、金额、币种、方向和主体。 ");
      return;
    }
    setTransactionConfirming(transaction.transactionId);
    setTransactionNotice(null);
    try {
      const receipt = await confirmPersistentTransaction({ expectedVersion: review.matterVersion, transactionId: transaction.transactionId });
      await reloadReview();
      setTransactionConfirmation(null);
      setTransactionConfirmed(false);
      setTransactionNotice(`交易已确认（案件版本 ${receipt.matterVersion}）。现在可以另行建立付款分类，分类获批前不会进入利息计算。`);
    } catch (cause) {
      setTransactionNotice(cause instanceof Error ? cause.message : "交易候选未确认。 ");
    } finally {
      setTransactionConfirming(null);
    }
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
        {review.transactions.map((transaction) => <div className={styles.ledgerRow} key={transaction.transactionId}>
          <strong>{transaction.date ?? "日期待确认"} · {currencySymbol(transaction.currency)} {transaction.amount}</strong>
          <small>{statusLabel(transaction.status)} · {statusLabel(transaction.nature)} · {transaction.application} · {transaction.currency}</small>
          {transaction.classification && <small>分类来源：{statusLabel(transaction.classification.origin)}；{transaction.classification.sameDaySequence ? `同日第 ${transaction.classification.sameDaySequence} 笔；` : "未设同日顺序；"}{transaction.classification.allocations.length ? `债务单元 ${transaction.classification.allocations.map((item) => `${item.obligationId} / ${item.amount} ${item.currency}`).join("；")}` : "不进入本金、利息计算。"}</small>}
          {review.sourceKind === "persistent-preview" && transaction.sourceStatus === "CANDIDATE" && transactionConfirmation !== transaction.transactionId && <button className={styles.candidateAction} disabled={transactionConfirming === transaction.transactionId} onClick={() => startTransactionConfirmation(transaction.transactionId)} type="button">核验并确认交易候选</button>}
          {review.sourceKind === "persistent-preview" && transaction.sourceStatus === "CANDIDATE" && transactionConfirmation === transaction.transactionId && <div className={styles.paymentClassificationForm}><p>确认会固定此笔候选交易的版本与审计哈希；付款性质、本金冲抵及利息属性仍需在下一步另行判断。</p><label className={styles.formalCalculationCheck}><input checked={transactionConfirmed} disabled={transactionConfirming === transaction.transactionId} onChange={(event) => setTransactionConfirmed(event.target.checked)} type="checkbox" /><span>我已按原始证据逐项核验日期、金额、币种、方向、收付款主体及重复情况。</span></label><div className={styles.formalCalculationActions}><button disabled={transactionConfirming === transaction.transactionId || !transactionConfirmed} onClick={() => void confirmTransaction(transaction)} type="button">{transactionConfirming === transaction.transactionId ? "正在确认…" : "确认本笔交易"}</button><button className={styles.secondaryAction} disabled={transactionConfirming === transaction.transactionId} onClick={() => { setTransactionConfirmation(null); setTransactionConfirmed(false); }} type="button">取消</button></div></div>}
          {review.sourceKind === "persistent-preview" && transaction.sourceStatus === "CONFIRMED" && transaction.classification?.status !== "CANDIDATE" && <button className={styles.candidateAction} disabled={classificationBusy === transaction.transactionId} onClick={() => startClassification(transaction)} type="button">{transaction.classification ? "更正付款分类" : "建立付款分类"}</button>}
          {review.sourceKind === "persistent-preview" && transaction.classification?.status === "CANDIDATE" && <button className={styles.candidateAction} disabled={classificationBusy === transaction.transactionId} onClick={() => void approveClassification(transaction)} type="button">{classificationBusy === transaction.transactionId ? "正在批准…" : "批准该付款分类"}</button>}
          {classificationDraft?.transactionId === transaction.transactionId && <PaymentClassificationForm draft={classificationDraft} transaction={transaction} busy={classificationBusy === transaction.transactionId} onChange={setClassificationDraft} onCancel={() => setClassificationDraft(null)} onSubmit={() => void saveClassification(transaction)} />}
        </div>)}
        <LedgerPagination loaded={review.transactionPage.loadedCount} total={review.transactionPage.totalCount} hasMore={review.transactionPage.hasMore} busy={loadingMore === "transactions"} onMore={() => void loadMore("transactions")} />
      </LedgerSection>
      {review.pendingFacts.length > 0 && <LedgerSection title="待律师确认的事实候选" note={review.sourceKind === "synthetic-alpha" ? "此操作仅改变本机合成台账。" : "确认操作固定案件版本、事实标识、决定状态与审计哈希；上游依赖会随决定变化重新核验。"}>
        {review.pendingFacts.map((fact) => <div className={styles.ledgerRow} key={fact.factId}><strong>{fact.text}</strong><small>{fact.origin} · {fact.evidenceCount} 个原始证据定位</small><button className={styles.candidateAction} disabled={confirming === fact.factId} onClick={() => void confirmCandidate(fact.factId)} type="button">{confirming === fact.factId ? "正在确认…" : review.sourceKind === "synthetic-alpha" ? "确认合成候选" : "确认本案事实"}</button>{review.sourceKind === "persistent-preview" && <small>确认后才能作为法律规则所需事实锚点；不代替对方主张、付款性质或最终诉讼立场。</small>}</div>)}
      </LedgerSection>}
    </div>
    {pageError && <div className={styles.inlineError} role="alert"><strong>后续记录未载入</strong><span>{pageError}</span><button className={styles.candidateAction} onClick={() => void reloadReview()} type="button">重新载入当前案件</button></div>}
    {classificationNotice && <div className={styles.inlineError} role="status"><strong>付款分类</strong><span>{classificationNotice}</span></div>}
    {transactionNotice && <div className={styles.inlineError} role="status"><strong>交易确认</strong><span>{transactionNotice}</span></div>}
    <p className={styles.resultHash}>案件摘要投影 {review.snapshotHash.slice(0, 16)}…{review.transactionSnapshotHash ? `；交易快照 ${review.transactionSnapshotHash.slice(0, 16)}…` : ""}。后续页严格绑定案件版本；修改上游材料或律师决定后，必须重新载入。{review.requestId ? ` 请求号 ${review.requestId}` : ""}</p>
  </section>;
}

function PaymentClassificationForm({ draft, transaction, busy, onChange, onCancel, onSubmit }: { draft: PaymentClassificationDraft; transaction: CaseReviewView["transactions"][number]; busy: boolean; onChange: (draft: PaymentClassificationDraft) => void; onCancel: () => void; onSubmit: () => void }) {
  const financial = ["DISBURSEMENT", "REPAYMENT_UNSPECIFIED", "INTEREST_PAYMENT", "PRINCIPAL_REPAYMENT"].includes(draft.nature);
  return <form className={styles.paymentClassificationForm} onSubmit={(event) => { event.preventDefault(); onSubmit(); }}><p>原始证据将逐项继承自这笔已确认交易；系统不会用生成摘要、红框图片或推断结果替代原始定位。</p><div className={styles.paymentClassificationFields}><label><span>付款性质</span><select value={draft.nature} onChange={(event) => onChange({ ...draft, nature: event.target.value as typeof draft.nature })}><option value="DISBURSEMENT">出借款</option><option value="REPAYMENT_UNSPECIFIED">还款（待冲抵）</option><option value="INTEREST_PAYMENT">支付利息</option><option value="PRINCIPAL_REPAYMENT">归还本金</option><option value="REFUND">退款</option><option value="FEE">费用</option><option value="UNRELATED">与本案无关</option></select></label>{financial && <><label><span>债务单元</span><input required value={draft.obligationId} onChange={(event) => onChange({ ...draft, obligationId: event.target.value })} placeholder="例如：借款合同-01" /></label><label><span>分配金额（{transaction.currency}）</span><input required inputMode="decimal" value={draft.allocationAmount} onChange={(event) => onChange({ ...draft, allocationAmount: event.target.value })} /><small>必须等于本笔交易全额 {transaction.amount} {transaction.currency}</small></label></>}<label><span>同日顺序（可选）</span><input inputMode="numeric" value={draft.sameDaySequence} onChange={(event) => onChange({ ...draft, sameDaySequence: event.target.value })} placeholder="同日多笔时填写 1、2…" /></label></div><label className={styles.formalCalculationCheck}><input checked={draft.confirmed} onChange={(event) => onChange({ ...draft, confirmed: event.target.checked })} type="checkbox" /><span>我已核对该笔交易、付款性质、金额归属及同日先后；分类候选仅在我随后单独批准后才进入正式计算。</span></label><div className={styles.formalCalculationActions}><button disabled={busy} type="submit">{busy ? "正在建立…" : "建立分类候选"}</button><button className={styles.secondaryAction} disabled={busy} onClick={onCancel} type="button">取消</button></div></form>;
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
