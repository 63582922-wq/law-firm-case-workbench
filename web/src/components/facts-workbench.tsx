"use client";

import { useEffect, useState } from "react";
import { alphaCalculationApiBase } from "@/lib/synthetic-calculation";
import styles from "./case-workbench.module.css";

type AlphaReview = {
  mode: "synthetic-alpha-only";
  fact_snapshot_hash: string;
  transaction_snapshot_hash: string;
  facts: { fact_id: string; original_text: string; origin: string; evidence_count: number }[];
  claims: { claim_id: string; original_claim_text: string; claimed_amount: string | null; currency: string | null; response_position: string; response_amount: string | null }[];
  issues: { issue_id: string; question: string; claim_count: number; fact_count: number }[];
  transactions: { event_id: string; effective_date: string; kind: string; amount: string; currency: string; payment_application: string; evidence_ids: string[] }[];
  pending_facts: { fact_id: string; original_text: string; origin: string; evidence_count: number }[];
};

export function FactsWorkbench() {
  const [review, setReview] = useState<AlphaReview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const response = await fetch(`${alphaCalculationApiBase}/v1/alpha-review`, { headers: { "X-Alpha-Actor": "alpha_lead_lawyer" } });
        const payload = (await response.json()) as AlphaReview | { detail?: string };
        if (!response.ok || !("facts" in payload)) throw new Error("detail" in payload ? payload.detail : "合成台账快照不可用");
        setReview(payload);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : "无法读取本机合成台账");
      }
    })();
  }, []);

  async function confirmCandidate(factId: string) {
    setConfirming(factId);
    try {
      const response = await fetch(`${alphaCalculationApiBase}/v1/alpha-review/facts/${factId}/confirm`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-Alpha-Actor": "alpha_lead_lawyer" }, body: JSON.stringify({ approval_hash: "alpha-ui-fact-confirmation" }),
      });
      const payload = (await response.json()) as AlphaReview | { detail?: string };
      if (!response.ok || !("facts" in payload)) throw new Error("detail" in payload ? payload.detail : "确认未完成");
      setReview(payload);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法确认合成候选事实"); }
    finally { setConfirming(null); }
  }

  if (error) return <section className={styles.calculationBlocked}><p className={styles.eyebrow}>事实与争点</p><h3>本机合成快照未连接</h3><p>{error}</p></section>;
  if (!review) return <section className={styles.calculationLoading}>正在读取经审批链生成的本机合成快照…</section>;

  return <section className={styles.factsArea} aria-label="事实与争点台账">
    <header className={styles.evidenceHeading}><div><p className={styles.eyebrow}>结构化审查</p><h2>事实、诉请与交易台账</h2></div><p>只读合成快照 · 事实与交易均来自后端审批链</p></header>
    <div className={styles.factsGrid}>
      <LedgerSection title="已确认事实" note="候选内容不会出现在这里。">
        {review.facts.map((fact) => <div className={styles.ledgerRow} key={fact.fact_id}><strong>{fact.original_text}</strong><small>{fact.origin} · {fact.evidence_count} 个原始证据定位</small></div>)}
      </LedgerSection>
      <LedgerSection title="诉请回应" note="立场与金额分开保存。">
        {review.claims.map((claim) => <div className={styles.ledgerRow} key={claim.claim_id}><strong>{claim.original_claim_text}</strong><small>{claim.response_position} · {claim.response_amount ?? "—"} {claim.currency ?? ""}</small></div>)}
      </LedgerSection>
      <LedgerSection title="已确认争点" note="每个争点后续须有证据矩阵项。">
        {review.issues.map((issue) => <div className={styles.ledgerRow} key={issue.issue_id}><strong>{issue.question}</strong><small>{issue.fact_count} 项确认事实 · {issue.claim_count} 项诉请范围</small></div>)}
      </LedgerSection>
      <LedgerSection title="计算前交易快照" note="付款性质决定是否可进入测算。">
        {review.transactions.map((transaction) => <div className={styles.ledgerRow} key={transaction.event_id}><strong>{transaction.effective_date} · ¥ {transaction.amount}</strong><small>{transaction.kind} · {transaction.payment_application} · {transaction.currency}</small></div>)}
      </LedgerSection>
      {review.pending_facts.length > 0 && <LedgerSection title="待律师确认的事实候选" note="此操作仅改变本机合成台账。">
        {review.pending_facts.map((fact) => <div className={styles.ledgerRow} key={fact.fact_id}><strong>{fact.original_text}</strong><small>{fact.origin} · {fact.evidence_count} 个原始证据定位</small><button className={styles.candidateAction} disabled={confirming === fact.fact_id} onClick={() => void confirmCandidate(fact.fact_id)} type="button">{confirming === fact.fact_id ? "正在确认…" : "确认合成候选"}</button></div>)}
      </LedgerSection>}
    </div>
    <p className={styles.resultHash}>事实快照 {review.fact_snapshot_hash.slice(0, 16)}…；交易快照 {review.transaction_snapshot_hash.slice(0, 16)}… 。修改上游材料或律师决定后，旧快照将失效。</p>
  </section>;
}

function LedgerSection({ title, note, children }: { title: string; note: string; children: React.ReactNode }) {
  return <article className={styles.ledgerCard}><div><p className={styles.cardKicker}>{title}</p><small>{note}</small></div>{children}</article>;
}
