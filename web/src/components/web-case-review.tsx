"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import {
  confirmWebCaseDisputeIssue,
  confirmWebCaseClaimScope,
  setWebCaseClaimResponse,
  confirmWebCaseTransaction,
  confirmWebPaymentClassification,
  createWebCaseClaimCandidate,
  createWebCaseDisputeIssueCandidate,
  createWebPaymentClassificationCandidate,
  decideWebCaseFact,
  recoverWebCaseFactDecision,
  createWebCaseIdempotencyKey,
  WebLawyerApiError,
  isWebLoginRequired,
  readCurrentWebCaseAgentRun,
  readWebCaseAgentArtifactReview,
  readWebCaseAgentInbox,
  readWebCaseReview,
  type WebCaseAgentArtifactReview,
  type WebCaseClaim,
  type WebCaseFact,
  type WebCaseIssue,
  type WebCaseReview as WebCaseReviewData,
  type WebCaseTransaction,
  type WebPaymentClassification,
} from "@/lib/web-lawyer-api";
import { WebAgentLedgerExtractionReview } from "@/components/web-agent-ledger-extraction-review";
import { WebFactCorrection } from "@/components/web-fact-correction";
import styles from "./case-workbench.module.css";

export function WebCaseReview({
  caseId,
  canDecide,
  canReviewAgentLedgerExtractions,
  canReviewAgentLedgerExceptionFollowups,
  onSessionExpired,
  onVersionAdvanced,
}: {
  caseId: string;
  canDecide: boolean;
  canReviewAgentLedgerExtractions: boolean;
  canReviewAgentLedgerExceptionFollowups: boolean;
  onSessionExpired: () => void;
  onVersionAdvanced: (caseId: string, version: number) => void;
}) {
  const router = useRouter();
  const [review, setReview] = useState<WebCaseReviewData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyFactId, setBusyFactId] = useState<string | null>(null);
  const [busyLedgerId, setBusyLedgerId] = useState<string | null>(null);
  const [busyCandidate, setBusyCandidate] = useState<"CLAIM" | "ISSUE" | null>(null);
  const [claimText, setClaimText] = useState("");
  const [claimAmount, setClaimAmount] = useState("");
  const [claimCurrency, setClaimCurrency] = useState("CNY");
  const [claimFactIds, setClaimFactIds] = useState<string[]>([]);
  const [issueQuestion, setIssueQuestion] = useState("");
  const [issueClaimIds, setIssueClaimIds] = useState<string[]>([]);
  const [issueFactIds, setIssueFactIds] = useState<string[]>([]);
  const [caseFramingGuidance, setCaseFramingGuidance] = useState<WebCaseAgentArtifactReview | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void readWebCaseReview(caseId, controller.signal)
        .then((nextReview) => {
          if (controller.signal.aborted) return;
          setReview(nextReview);
          onVersionAdvanced(caseId, nextReview.version);
          setError(null);
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          if (isWebLoginRequired(reason)) {
            onSessionExpired();
            return;
          }
          setError(reason instanceof Error ? reason.message : "暂时无法读取本案要点，请稍后重试。");
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [caseId, onSessionExpired, onVersionAdvanced, refreshKey]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const run = await readCurrentWebCaseAgentRun(caseId, controller.signal);
          if (!run || run.status !== "READY_FOR_REVIEW") {
            if (!controller.signal.aborted) setCaseFramingGuidance(null);
            return;
          }
          const inbox = await readWebCaseAgentInbox(caseId, run.runId, controller.signal);
          const candidate = inbox.artifacts.find(
            (item) => item.artifactType === "LAWYER_DECISION_PACKAGE_CANDIDATE" && item.status === "READY_FOR_REVIEW",
          );
          if (!candidate) {
            if (!controller.signal.aborted) setCaseFramingGuidance(null);
            return;
          }
          const nextGuidance = await readWebCaseAgentArtifactReview(
            caseId,
            run.runId,
            candidate.artifactId,
            controller.signal,
          );
          const hasDiscoveredIssue = nextGuidance.sections.some((section) => section.sectionId === "discovered-issues");
          if (!controller.signal.aborted) setCaseFramingGuidance(hasDiscoveredIssue ? nextGuidance : null);
        } catch (reason: unknown) {
          if (controller.signal.aborted) return;
          if (isWebLoginRequired(reason)) {
            onSessionExpired();
            return;
          }
          // Guidance is optional. The underlying case review remains usable when it is unavailable.
          setCaseFramingGuidance(null);
        }
      })();
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [caseId, onSessionExpired, refreshKey]);

  if (loading) return <section className={styles.webLawyerEmptyPanel}>正在读取本案已登记的事实、诉请与收付款记录…</section>;
  if (error) {
    return (
      <section className={styles.webLawyerEmptyPanel} role="alert">
        <p className={styles.eyebrow}>案件要点</p>
        <h2>暂不能读取本案要点</h2>
        <p>{error}</p>
        <small>系统不会用示例内容替代真实案卷。</small>
      </section>
    );
  }
  if (!review) return null;

  async function handleFactDecision(factId:string,status?:"CONFIRMED"|"DISPUTED"|"DENIED"|"INVALIDATED") {
    if(!review||busyFactId!==null)return;
    setBusyFactId(factId);setNotice(null);
    const storageKey=`lawcase:fact-decision:${caseId}:${factId}`;
    let sentNewRequest=false;
    try {
      const retained=sessionStorage.getItem(storageKey);
      let receipt;
      if(retained) {
        receipt=await recoverWebCaseFactDecision(caseId,factId,retained);
        if(!receipt){setNotice("原事实决定尚未查到回执，请继续查询原请求，不重新批准。");return;}
      } else {
        if(!status){setNotice("本浏览器没有待恢复的事实决定，请刷新案件查看当前状态。");return;}
        const key=createWebCaseIdempotencyKey();
        // No legal text or chosen legal position is stored in the browser.
        sessionStorage.setItem(storageKey,key);
        sentNewRequest=true;
        receipt=await decideWebCaseFact(caseId,factId,review.version,status,key);
      }
      sessionStorage.removeItem(storageKey);
      onVersionAdvanced(caseId,Math.max(review.version,receipt.matterVersion));
      setNotice("事实决定已保存，正在刷新案件；受影响的金额和成果文件需要重新核对。");
      setRefreshKey(current=>current+1);
    } catch(reason) {
      if(sentNewRequest&&reason instanceof WebLawyerApiError&&reason.status!==null&&[400,401,403,404,409,422].includes(reason.status))sessionStorage.removeItem(storageKey);
      if(isWebLoginRequired(reason))onSessionExpired();
      // Keep the fact rows and recovery controls available after a network error.
      setNotice(reason instanceof Error?reason.message:"事实决定结果不明，请查询原请求。");
    } finally {setBusyFactId(null);}
  }

  async function commitLedgerDecision(
    objectId: string,
    action: () => Promise<{ matterVersion: number }>,
    successMessage: string,
  ) {
    if (!review) return;
    setBusyLedgerId(objectId);
    setNotice(null);
    setError(null);
    try {
      const receipt = await action();
      onVersionAdvanced(caseId, receipt.matterVersion);
      setNotice(successMessage);
      setRefreshKey((current) => current + 1);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "台账决定未完成，请刷新后再试。");
    } finally {
      setBusyLedgerId(null);
    }
  }

  async function commitCandidate(
    kind: "CLAIM" | "ISSUE",
    action: () => Promise<{ matterVersion: number }>,
    successMessage: string,
    reset: () => void,
  ) {
    setBusyCandidate(kind);
    setNotice(null);
    setError(null);
    try {
      const receipt = await action();
      onVersionAdvanced(caseId, receipt.matterVersion);
      reset();
      setNotice(successMessage);
      setRefreshKey((current) => current + 1);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "候选事项未保存，请刷新后再试。");
    } finally {
      setBusyCandidate(null);
    }
  }

  function submitClaimCandidate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!review) return;
    void commitCandidate(
      "CLAIM",
      () => createWebCaseClaimCandidate(caseId, review.version, {
        text: claimText,
        claimedAmount: claimAmount || null,
        currency: claimAmount ? claimCurrency : null,
        confirmedFactIds: validClaimFactIds,
      }),
      "诉请候选已保存并绑定原始证据；仍须主办律师另行确认诉请范围。",
      () => {
        setClaimText("");
        setClaimAmount("");
        setClaimFactIds([]);
      },
    );
  }

  function submitIssueCandidate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!review) return;
    void commitCandidate(
      "ISSUE",
      () => createWebCaseDisputeIssueCandidate(caseId, review.version, {
        question: issueQuestion,
        claimIds: validIssueClaimIds,
        confirmedFactIds: validIssueFactIds,
      }),
      "争点候选已保存；正式法律检索仍须在主办律师确认该争点后开始。",
      () => {
        setIssueQuestion("");
        setIssueClaimIds([]);
        setIssueFactIds([]);
      },
    );
  }

  const confirmedFacts = review.facts.filter((item) => item.status === "CONFIRMED");
  const confirmedClaims = review.claims.filter((item) => item.status === "CONFIRMED_SCOPE");
  const confirmedIssues = review.issues.filter((item) => item.status === "CONFIRMED");
  const confirmedFactIdSet = new Set(confirmedFacts.map((item) => item.factId));
  const confirmedClaimIdSet = new Set(confirmedClaims.map((item) => item.claimId));
  const validClaimFactIds = claimFactIds.filter((item) => confirmedFactIdSet.has(item));
  const validIssueFactIds = issueFactIds.filter((item) => confirmedFactIdSet.has(item));
  const validIssueClaimIds = issueClaimIds.filter((item) => confirmedClaimIdSet.has(item));
  const canFrameCase = confirmedFacts.length > 0 || confirmedClaims.length > 0 || confirmedIssues.length > 0;

  return (
    <section className={styles.factsArea} aria-labelledby="web-case-review-title">
      <header className={styles.webLawyerIntakeHeading}>
        <div>
          <p className={styles.eyebrow}>核对案件要点</p>
          <h2 id="web-case-review-title">确认本案要回应什么</h2>
          <p>登记对方诉请、关键问题和收付款；每一项都可回到原件核对。</p>
        </div>
        <dl><div><dt>已确认事实</dt><dd>{confirmedFacts.length}</dd></div><div><dt>待回应请求</dt><dd>{review.claims.length}</dd></div><div><dt>收付款</dt><dd>{review.transactions.length}</dd></div></dl>
      </header>

      {notice ? <p className={styles.webLawyerNotice} role="status">{notice}</p> : null}

      <section aria-label="材料整理结果" id="ledger-extraction-review">
      <WebAgentLedgerExtractionReview
        canReview={canReviewAgentLedgerExtractions}
        canReviewFollowups={canReviewAgentLedgerExceptionFollowups}
        caseId={caseId}
        onConfirmed={(receipt) => {
          onVersionAdvanced(caseId, receipt.matterVersion);
          setNotice(
            `已确认 ${receipt.confirmedFactCount} 项事实、${receipt.confirmedTransactionCount} 项收付款。案件将按当前记录继续整理。`,
          );
          setRefreshKey((current) => current + 1);
        }}
        onExceptionDecided={(receipt) => {
          if (receipt.matterVersion > review.version) {
            onVersionAdvanced(caseId, receipt.matterVersion);
          }
          setNotice(
            receipt.exceptionReviewStatus === "RESOLVED"
              ? `已处理全部 ${receipt.exceptionGroupCount} 组需要单独核对的内容。案件将按处理结果继续整理。`
              : `已处理 ${receipt.decidedExceptionGroupCount}/${receipt.exceptionGroupCount} 组需要单独核对的内容；其余内容仍待处理。`,
          );
          setRefreshKey((current) => current + 1);
        }}
        onFollowupVersionAdvanced={(version) => {
          onVersionAdvanced(caseId, version);
          setRefreshKey((current) => current + 1);
        }}
        onSessionExpired={onSessionExpired}
      />
      </section>

      {canFrameCase ? <CaseFramingGate
        canDecide={canDecide}
        caseId={caseId}
        claimAmount={claimAmount}
        claimCurrency={claimCurrency}
        claimFactIds={validClaimFactIds}
        claimText={claimText}
        confirmedClaims={confirmedClaims}
        confirmedFacts={confirmedFacts}
        confirmedIssueCount={confirmedIssues.length}
        issueClaimIds={validIssueClaimIds}
        issueFactIds={validIssueFactIds}
        issueQuestion={issueQuestion}
        busyCandidate={busyCandidate}
        onClaimAmountChange={setClaimAmount}
        onClaimCurrencyChange={setClaimCurrency}
        onClaimFactToggle={(factId) => setClaimFactIds((current) => toggleId(current, factId))}
        onClaimTextChange={setClaimText}
        onIssueClaimToggle={(claimId) => setIssueClaimIds((current) => toggleId(current, claimId))}
        onIssueFactToggle={(factId) => setIssueFactIds((current) => toggleId(current, factId))}
        onIssueQuestionChange={setIssueQuestion}
        agentGuidance={caseFramingGuidance}
        onUseAgentIssueSuggestion={(question) => {
          setIssueQuestion(question);
          // The Agent only supplies a question draft.  Selecting the current
          // lawyer-confirmed scope makes the next user action legible without
          // allowing the suggestion itself to create a formal dispute issue.
          setIssueClaimIds(confirmedClaims.map((claim) => claim.claimId));
          setIssueFactIds(confirmedFacts.map((fact) => fact.factId));
          setNotice(
            confirmedClaims.length > 0
              ? "已带入关键问题，并预选本案已确认的诉请与事实；请核对后保存争点候选。"
              : "已将材料整理出的关键问题带入草稿；请先确认对方请求，再选择关联事实后保存。",
          );
        }}
        onOpenAgentGuidanceSources={(pageIds) => {
          const focus = pageIds.slice(0, 100).join(",");
          router.push(`/evidence?case=${encodeURIComponent(caseId)}${focus ? `&focus=${encodeURIComponent(focus)}` : ""}`);
        }}
        onSubmitClaim={submitClaimCandidate}
        onSubmitIssue={submitIssueCandidate}
      /> : <section className={styles.webCaseInputCard} aria-label="待确认材料后的下一步"><div><h2>先核对材料</h2><p>确认材料后，再登记诉请和关键问题。</p></div></section>}

      <div className={styles.factsGrid}>
        <ReviewCard title="案件事实" note="已确认内容均可回到原件。">
          {review.facts.length === 0 ? <EmptyLine text="尚无已登记事实；请先完成材料审阅或由律师建立事实候选。" /> : review.facts.map((fact) => <FactRow caseId={caseId} version={review.version} onSessionExpired={onSessionExpired} canDecide={canDecide} fact={fact} key={fact.factId} busy={busyFactId!==null} onDecide={status=>handleFactDecision(fact.factId,status)} onRecover={()=>handleFactDecision(fact.factId)} />)}
        </ReviewCard>
        <ReviewCard title="原告诉请与回应" note="记录已登记的诉请和回应范围。">
          {review.claims.length === 0 ? <EmptyLine text="尚无已登记诉请；不会从案件名称或文件名推测诉请。" /> : review.claims.map((claim) => <ClaimRow canDecide={canDecide} claim={claim} confirmedFacts={confirmedFacts} key={claim.claimId} busy={busyLedgerId === claim.claimId} onConfirm={() => void commitLedgerDecision(claim.claimId, () => confirmWebCaseClaimScope(caseId, claim.claimId, review.version), "诉请范围已确认，并写入本案审计记录。")} onSaveResponse={(input) => void commitLedgerDecision(claim.claimId, () => setWebCaseClaimResponse(caseId, claim.claimId, review.version, input), "本方回应已保存，并绑定已确认事实；受影响的争点需要重新核对。")} />)}
        </ReviewCard>
        <ReviewCard title="本案争点" note="关联已确认事实和诉请。">
          {review.issues.length === 0 ? <EmptyLine text="尚无已登记争点；依据与金额核对暂不能继续。" /> : review.issues.map((issue) => <IssueRow canDecide={canDecide} issue={issue} key={issue.issueId} busy={busyLedgerId === issue.issueId} onConfirm={() => void commitLedgerDecision(issue.issueId, () => confirmWebCaseDisputeIssue(caseId, issue.issueId, review.version), "争点已确认。接下来可以据此补齐适用依据并开展研判。")} onRebuild={() => void commitLedgerDecision(issue.issueId, () => createWebCaseDisputeIssueCandidate(caseId, review.version, { question: issue.question, claimIds: issue.claimIds, confirmedFactIds: issue.confirmedFactIds }), "已按当前回应重新建立问题草稿；请复核后确认。")} />)}
        </ReviewCard>
        <ReviewCard title="收付款记录" note="保留金额、币种和原件链接。">
          {review.transactions.length === 0 ? <EmptyLine text="尚无已登记收付款记录；不会从 PDF 文件名自动生成交易。" /> : review.transactions.map((transaction) => <TransactionRow canDecide={canDecide} key={transaction.transactionId} transaction={transaction} busy={busyLedgerId === transaction.transactionId} onConfirm={() => void commitLedgerDecision(transaction.transactionId, () => confirmWebCaseTransaction(caseId, transaction.transactionId, review.version), "收付款记录已确认，并写入本案审计记录。")} />)}
        </ReviewCard>
      </div>

      <PaymentClassificationGate
        canDecide={canDecide}
        caseId={caseId}
        classifications={review.paymentClassifications}
        onRefresh={() => setRefreshKey((current) => current + 1)}
        onSessionExpired={onSessionExpired}
        onVersionAdvanced={onVersionAdvanced}
        transactions={review.transactions.filter((transaction) => transaction.status === "CONFIRMED")}
        version={review.version}
      />

      <footer className={styles.webLawyerNotice}>未确认的内容不会进入后续成果。</footer>
    </section>
  );
}

function PaymentClassificationGate({
  canDecide,
  caseId,
  classifications,
  onRefresh,
  onSessionExpired,
  onVersionAdvanced,
  transactions,
  version,
}: {
  canDecide: boolean;
  caseId: string;
  classifications: readonly WebPaymentClassification[];
  onRefresh: () => void;
  onSessionExpired: () => void;
  onVersionAdvanced: (caseId: string, version: number) => void;
  transactions: readonly WebCaseTransaction[];
  version: number;
}) {
  const [transactionId, setTransactionId] = useState("");
  const [obligationLabel, setObligationLabel] = useState("");
  const [nature, setNature] = useState<"DISBURSEMENT" | "REPAYMENT_UNSPECIFIED" | "INTEREST_PAYMENT" | "PRINCIPAL_REPAYMENT">("REPAYMENT_UNSPECIFIED");
  const [sameDaySequence, setSameDaySequence] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const selectedTransaction = transactions.find((transaction) => transaction.transactionId === transactionId) ?? transactions[0] ?? null;
  const activeClassifications = classifications.filter((classification) => classification.status !== "INVALIDATED");

  async function createCandidate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedTransaction || !canDecide || busyId) return;
    setBusyId("new");
    setNotice(null);
    try {
      const receipt = await createWebPaymentClassificationCandidate({
        caseId,
        transactionId: selectedTransaction.transactionId,
        expectedVersion: version,
        obligationLabel,
        nature,
        sameDaySequence: sameDaySequence ? Number(sameDaySequence) : null,
      });
      onVersionAdvanced(caseId, receipt.matterVersion);
      setObligationLabel("");
      setSameDaySequence("");
      setNotice("款项性质与归属已保存为待确认项；确认前不会进入金额核对。");
      onRefresh();
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setNotice(reason instanceof Error ? reason.message : "款项归属未保存，请刷新后再试。");
    } finally {
      setBusyId(null);
    }
  }

  async function confirmClassification(classificationId: string) {
    if (!canDecide || busyId) return;
    setBusyId(classificationId);
    setNotice(null);
    try {
      const receipt = await confirmWebPaymentClassification(caseId, classificationId, version);
      onVersionAdvanced(caseId, receipt.matterVersion);
      setNotice("款项归属已确认；它现在可以在金额核对中被选择。");
      onRefresh();
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setNotice(reason instanceof Error ? reason.message : "款项归属未确认，请刷新后再试。");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className={styles.caseFramingGate} id="payment-classification" aria-labelledby="payment-classification-title">
      <header className={styles.caseFramingGateHeader}>
        <div>
          <p className={styles.eyebrow}>金额核对准备</p>
          <h3 id="payment-classification-title">确认每笔收付款的性质和归属</h3>
          <p>选择已确认的收付款，标记用途并关联到对应事项。</p>
        </div>
        <strong data-state={activeClassifications.some((item) => item.status === "APPROVED") ? "ready" : "blocked"}>
          {activeClassifications.filter((item) => item.status === "APPROVED").length > 0 ? "已有可核对事项" : "待确认款项归属"}
        </strong>
      </header>

      {!canDecide ? <p className={styles.caseFramingRoleNotice}>当前角色可查看归属记录；请由主办律师确认款项性质和归属。</p> : null}
      {transactions.length === 0 ? <p className={styles.webLawyerUploadEmpty}>请先在上方确认至少一笔收付款记录；系统不会把候选记录直接当作计算依据。</p> : (
        <form className={styles.caseFramingForm} onSubmit={(event) => void createCandidate(event)}>
          <header><span>01</span><div><strong>建立款项归属</strong><small>保存后由主办律师确认。</small></div></header>
          <label><span>已确认的收付款</span><select disabled={!canDecide || busyId !== null} onChange={(event) => setTransactionId(event.target.value)} value={selectedTransaction?.transactionId ?? ""}>{transactions.map((transaction) => <option key={transaction.transactionId} value={transaction.transactionId}>{transactionLabel(transaction)}</option>)}</select></label>
          <div className={styles.webCalculationFormGrid}>
            <label><span>款项性质</span><select disabled={!canDecide || busyId !== null} onChange={(event) => setNature(event.target.value as typeof nature)} value={nature}><option value="DISBURSEMENT">出借 / 放款</option><option value="REPAYMENT_UNSPECIFIED">还款（暂未区分本息）</option><option value="INTEREST_PAYMENT">支付利息</option><option value="PRINCIPAL_REPAYMENT">偿还本金</option></select></label>
            <label><span>同日顺序（如同日有多笔）</span><input disabled={!canDecide || busyId !== null} inputMode="numeric" min="1" max="999" onChange={(event) => setSameDaySequence(event.target.value)} placeholder="例如 1" type="number" value={sameDaySequence} /></label>
          </div>
          <label className={styles.caseFramingTextField}><span>归属事项名称</span><input disabled={!canDecide || busyId !== null} maxLength={160} onChange={(event) => setObligationLabel(event.target.value)} placeholder="例如：2024年3月10日借款" required value={obligationLabel} /></label>
          <div className={styles.caseFramingSubmitLine}><small>{selectedTransaction ? `将沿用该笔 ${selectedTransaction.amount ?? "金额待核对"} ${selectedTransaction.currency ?? ""}`.trim() + " 的原始登记与证据定位。" : ""}</small><button className={styles.webLawyerPrimaryAction} disabled={!canDecide || !selectedTransaction || !obligationLabel.trim() || busyId !== null} type="submit">{busyId === "new" ? "正在保存…" : "保存待确认项"}</button></div>
        </form>
      )}

      <div className={styles.factsGrid}>
        <ReviewCard title="已登记的款项归属" note="每项均关联原始收付款。">
          {activeClassifications.length === 0 ? <EmptyLine text="尚未登记款项归属。" /> : activeClassifications.map((classification) => <div className={styles.ledgerRow} key={classification.classificationId}><div><strong>{classification.allocations.map((allocation) => allocation.obligationLabel).join("、") || "待补充归属事项"}</strong><small>{paymentNatureLabel(classification.nature)} · {classification.evidenceCount} 个原始证据定位 · {classification.status === "APPROVED" ? "已确认，可用于金额核对" : "待主办律师确认"}</small></div>{classification.status === "CANDIDATE" ? <button className={styles.webLawyerPrimaryAction} disabled={!canDecide || busyId !== null} onClick={() => void confirmClassification(classification.classificationId)} type="button">{busyId === classification.classificationId ? "正在确认…" : "确认归属"}</button> : null}</div>)}
        </ReviewCard>
      </div>
      {notice ? <p className={styles.webLawyerNotice} role="status">{notice}</p> : null}
    </section>
  );
}

function transactionLabel(transaction: WebCaseTransaction): string {
  return [transaction.localDate ?? "日期待核对", transaction.amount ? `${transaction.amount} ${transaction.currency ?? ""}`.trim() : "金额待核对", transaction.payerLabel && transaction.payeeLabel ? `${transaction.payerLabel} → ${transaction.payeeLabel}` : null].filter(Boolean).join(" · ");
}

function paymentNatureLabel(nature: string): string {
  return ({ DISBURSEMENT: "出借 / 放款", REPAYMENT_UNSPECIFIED: "还款（暂未区分本息）", INTEREST_PAYMENT: "支付利息", PRINCIPAL_REPAYMENT: "偿还本金" } as Record<string, string>)[nature] ?? "待核对款项性质";
}

function CaseFramingGate({
  canDecide,
  caseId,
  claimAmount,
  claimCurrency,
  claimFactIds,
  claimText,
  confirmedClaims,
  confirmedFacts,
  confirmedIssueCount,
  issueClaimIds,
  issueFactIds,
  issueQuestion,
  agentGuidance,
  busyCandidate,
  onClaimAmountChange,
  onClaimCurrencyChange,
  onClaimFactToggle,
  onClaimTextChange,
  onIssueClaimToggle,
  onIssueFactToggle,
  onIssueQuestionChange,
  onUseAgentIssueSuggestion,
  onOpenAgentGuidanceSources,
  onSubmitClaim,
  onSubmitIssue,
}: {
  canDecide: boolean;
  caseId: string;
  claimAmount: string;
  claimCurrency: string;
  claimFactIds: readonly string[];
  claimText: string;
  confirmedClaims: readonly WebCaseClaim[];
  confirmedFacts: readonly WebCaseFact[];
  confirmedIssueCount: number;
  issueClaimIds: readonly string[];
  issueFactIds: readonly string[];
  issueQuestion: string;
  agentGuidance: WebCaseAgentArtifactReview | null;
  busyCandidate: "CLAIM" | "ISSUE" | null;
  onClaimAmountChange: (value: string) => void;
  onClaimCurrencyChange: (value: string) => void;
  onClaimFactToggle: (factId: string) => void;
  onClaimTextChange: (value: string) => void;
  onIssueClaimToggle: (claimId: string) => void;
  onIssueFactToggle: (factId: string) => void;
  onIssueQuestionChange: (value: string) => void;
  onUseAgentIssueSuggestion: (question: string) => void;
  onOpenAgentGuidanceSources: (pageIds: readonly string[]) => void;
  onSubmitClaim: (event: FormEvent<HTMLFormElement>) => void;
  onSubmitIssue: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const blocked = confirmedIssueCount === 0;
  const claimReady = canDecide && claimText.trim().length > 0 && claimFactIds.length > 0 && busyCandidate === null;
  const issueReady = canDecide
    && issueQuestion.trim().length > 0
    && issueClaimIds.length > 0
    && issueFactIds.length > 0
    && busyCandidate === null;
  const suggestedIssue = agentGuidance?.sections.find((section) => section.sectionId === "discovered-issues")?.items[0] ?? null;
  const suggestedAction = agentGuidance?.sections.find((section) => section.sectionId === "discovered-actions")?.items[0] ?? null;
  const suggestedGap = agentGuidance?.sections.find((section) => section.sectionId === "discovered-gaps")?.items[0] ?? null;
  const suggestedIssuePageIds = [...new Set(suggestedIssue?.sources.flatMap((source) => source.evidencePageId ? [source.evidencePageId] : []) ?? [])];

  return (
    <section className={styles.caseFramingGate} id="case-framing" aria-labelledby="case-framing-title">
      <header className={styles.caseFramingGateHeader}>
        <div>
          <p className={styles.eyebrow}>确认案件范围</p>
          <h3 id="case-framing-title">确认要回应的请求与关键问题</h3>
          <p>先登记对方诉请，再确定本案要处理的关键问题。</p>
        </div>
        <strong data-state={blocked ? "blocked" : "ready"}>
          {blocked ? "待确认关键问题" : `已确认 ${confirmedIssueCount} 项关键问题`}
        </strong>
      </header>

      {!canDecide ? <p className={styles.caseFramingRoleNotice}>当前角色可查看候选，但只有主办律师能建立并确认诉请范围和争点。</p> : null}

      {suggestedIssue ? <details className={styles.caseFramingGuidance} aria-label="材料整理出的案件问题">
        <summary><strong>材料提示</strong><span>{suggestedIssue.title}</span></summary>
        <div className={styles.caseFramingGuidanceContent}>
          <article>
            <div>
              <strong>{suggestedIssue.title}</strong>
              <p>{caseFramingSummary(suggestedIssue.detail)}</p>
              {suggestedIssue.sources.length ? <small>依据：{suggestedIssue.sources.map((source) => source.label).join("；")}</small> : null}
            </div>
            <div className={styles.caseFramingGuidanceActions}>
              <button
                className={styles.webLawyerSecondaryAction}
                disabled={!canDecide || busyCandidate !== null}
                onClick={() => onUseAgentIssueSuggestion(suggestedIssue.title)}
                type="button"
              >
                {issueQuestion.trim() === suggestedIssue.title ? "已带入下方草稿" : "带入关键问题草稿"}
              </button>
              {suggestedIssuePageIds.length ? <button
                className={styles.webLawyerSecondaryAction}
                onClick={() => onOpenAgentGuidanceSources(suggestedIssuePageIds)}
                type="button"
              >查看材料来源</button> : null}
            </div>
          </article>
          {(suggestedAction || suggestedGap) ? <footer>
            {suggestedAction ? <p><strong>建议先做：</strong>{caseFramingSummary(suggestedAction.detail)}</p> : null}
            {suggestedGap ? <p><strong>还缺：</strong>{suggestedGap.title}</p> : null}
          </footer> : null}
        </div>
      </details> : null}

      <div className={styles.caseFramingSteps}>
        <form className={styles.caseFramingForm} id="case-framing-claims" onSubmit={onSubmitClaim}>
          <header>
            <span>01</span>
            <div><strong>对方要求你回应什么？</strong><small>根据已确认材料登记；保存后由主办律师确认范围。</small></div>
          </header>
          <label className={styles.caseFramingTextField}>
            <span>需要回应的请求</span>
            <textarea
              disabled={!canDecide || busyCandidate !== null || confirmedFacts.length === 0}
              maxLength={8_000}
              onChange={(event) => onClaimTextChange(event.target.value)}
              placeholder="例如：请求支付已交付货物对应的剩余价款及依法核验后的逾期损失。"
              rows={4}
              value={claimText}
            />
          </label>
          <div className={styles.caseFramingMoneyFields}>
            <label><span>主张金额（可暂不填）</span><input disabled={!canDecide || busyCandidate !== null} inputMode="decimal" maxLength={19} onChange={(event) => onClaimAmountChange(event.target.value)} placeholder="金额必须来自已核验材料" value={claimAmount} /></label>
            <label><span>币种</span><select disabled={!canDecide || busyCandidate !== null || !claimAmount} onChange={(event) => onClaimCurrencyChange(event.target.value)} value={claimCurrency}><option value="CNY">人民币 CNY</option><option value="HKD">港币 HKD</option><option value="USD">美元 USD</option></select></label>
          </div>
          <fieldset className={styles.caseFramingSources} disabled={!canDecide || busyCandidate !== null || confirmedFacts.length === 0}>
            <legend>绑定已确认事实</legend>
            {confirmedFacts.length === 0 ? <small>尚无已确认事实，不能建立来源不明的诉请。</small> : confirmedFacts.map((fact) => (
              <label key={fact.factId}>
                <input checked={claimFactIds.includes(fact.factId)} onChange={() => onClaimFactToggle(fact.factId)} type="checkbox" />
                <span><strong>{fact.text}</strong><small>{fact.evidenceCount} 个原始证据定位，由服务器重新绑定</small></span>
              </label>
            ))}
          </fieldset>
          <div className={styles.caseFramingSubmitLine}>
            <small>下一步：确认这项请求的范围。</small>
            <button className={styles.webLawyerPrimaryAction} disabled={!claimReady} type="submit">{busyCandidate === "CLAIM" ? "正在保存…" : "保存为待确认请求"}</button>
          </div>
        </form>

        {confirmedClaims.length > 0 ? <form className={styles.caseFramingForm} onSubmit={onSubmitIssue}>
          <header>
            <span>02</span>
            <div><strong>本案要处理的关键问题</strong><small>关联已确认请求和事实后，再由主办律师确认。</small></div>
          </header>
          <label className={styles.caseFramingTextField}>
            <span>需要裁判解决的问题</span>
            <textarea
              disabled={!canDecide || busyCandidate !== null || confirmedClaims.length === 0 || confirmedFacts.length === 0}
              maxLength={2_000}
              onChange={(event) => onIssueQuestionChange(event.target.value)}
              placeholder="例如：案涉货物是否已经完成交付，以及剩余价款是否已经到期？"
              rows={4}
              value={issueQuestion}
            />
          </label>
          <fieldset className={styles.caseFramingSources} disabled={!canDecide || busyCandidate !== null || confirmedClaims.length === 0}>
            <legend>关联已确认诉请范围</legend>
            {confirmedClaims.length === 0 ? <small>请先确认至少一项诉请范围。</small> : confirmedClaims.map((claim) => (
              <label key={claim.claimId}>
                <input checked={issueClaimIds.includes(claim.claimId)} onChange={() => onIssueClaimToggle(claim.claimId)} type="checkbox" />
                <span><strong>{claim.text}</strong><small>{claim.claimedAmount ? `${claim.claimedAmount} ${claim.currency ?? ""}`.trim() : "金额尚未登记"}</small></span>
              </label>
            ))}
          </fieldset>
          <fieldset className={styles.caseFramingSources} disabled={!canDecide || busyCandidate !== null || confirmedFacts.length === 0}>
            <legend>关联已确认事实</legend>
            {confirmedFacts.length === 0 ? <small>尚无可关联的已确认事实。</small> : confirmedFacts.map((fact) => (
              <label key={fact.factId}>
                <input checked={issueFactIds.includes(fact.factId)} onChange={() => onIssueFactToggle(fact.factId)} type="checkbox" />
                <span><strong>{fact.text}</strong><small>{fact.evidenceCount} 个原始证据定位</small></span>
              </label>
            ))}
          </fieldset>
          <div className={styles.caseFramingSubmitLine}>
              <small>核对预选依据后保存；保存的仍只是待确认争点。</small>
            <button className={styles.webLawyerPrimaryAction} disabled={!issueReady} type="submit">{busyCandidate === "ISSUE" ? "正在保存…" : "保存争点候选"}</button>
          </div>
        </form> : <section className={styles.caseFramingPendingStep} aria-label="待确认案件问题">
          <header>
            <span>02</span>
            <div><strong>确认关键问题</strong><small>确认对方请求后，系统会在这里引导你关联已确认事实并保存问题草稿。</small></div>
          </header>
          <p>先完成左侧“对方要求你回应什么”的确认。系统不会把材料整理提示直接变成本案争点。</p>
          <a href="#case-framing-claims">去登记对方请求</a>
        </section>}
      </div>
      {confirmedIssueCount > 0 ? <section className={styles.caseFramingPendingStep} aria-label="确认争点后的下一步">
        <header>
          <span>03</span>
          <div><strong>核对依据与测算</strong><small>关键问题已确认。系统会以当前案件版本重新核对法源、规则和必要测算；完成后再生成文书候选。</small></div>
        </header>
        <p>争点一旦确认，旧的依据包会失效，避免用旧规则撰写答辩。请先完成这一项，再回到办案主页继续。</p>
        <a href={`/legal?case=${encodeURIComponent(caseId)}`}>去核对依据与测算</a>
      </section> : null}
    </section>
  );
}

function ReviewCard({ title, note, children }: { title: string; note: string; children: React.ReactNode }) {
  return <section className={styles.ledgerCard}><div><strong>{title}</strong><small>{note}</small></div>{children}</section>;
}

function FactRow({ caseId,version,onSessionExpired,canDecide, fact, busy, onDecide,onRecover }: { caseId:string;version:number;onSessionExpired:()=>void;canDecide: boolean; fact: WebCaseFact; busy: boolean;onRecover:()=>Promise<void>; onDecide: (status: "CONFIRMED" | "DISPUTED" | "DENIED" | "INVALIDATED") => Promise<void> }) {
  const [sourceVersion,setSourceVersion]=useState<number|null>(null);
  const candidate = fact.status === "CANDIDATE";
  const blocked=busy||!!fact.correctionCandidateId&&sourceVersion!==version;
  const origins:Record<string,string>={AGENT_CANDIDATE:"AI提取候选",ASSISTANT_ENTRY:"人工录入",PLAINTIFF_PLEADING:"原告诉状",DEFENDANT_STATEMENT:"被告陈述"};
  return <div className={styles.ledgerRow}>
    <strong>{fact.text}</strong>
    <small>{statusLabel(fact.status)} · {fact.correctionCandidateId?"律师纠正稿":origins[fact.origin]??fact.origin} · {fact.evidenceCount} 个证据定位</small>
    {fact.evidenceSources?.map(source=><small key={source.pageId}>原件：{source.label}{source.pageNumber!==null?` · 第 ${source.pageNumber} 页`:" · 文件级定位"}</small>)}
    {fact.correctionCandidateId?<WebFactCorrection key={`${fact.factId}:${version}`} caseId={caseId} candidateId={fact.correctionCandidateId} version={version} originalText={fact.text} expectedFactId={fact.factId} onSessionExpired={onSessionExpired} onSourceContextLoaded={setSourceVersion}/>:null}
    {candidate&&fact.correctionCandidateId&&sourceVersion!==version?<small>请先展开并核对本版本的原候选、修改理由和原始摘录，再作事实决定。</small>:null}
    {candidate ? canDecide ? <div className={styles.webCaseFactActions} aria-label="事实决定"><button disabled={blocked} onClick={() => void onDecide("CONFIRMED")} type="button">确认</button><button disabled={blocked} onClick={() => void onDecide("DISPUTED")} type="button">有争议</button><button disabled={blocked} onClick={() => void onDecide("DENIED")} type="button">否认</button></div> : <small>当前角色不能作事实决定，请由主办律师确认。</small> : null}
    {canDecide?<button type="button" disabled={busy} onClick={()=>void onRecover()}>查询上次事实决定</button>:null}
  </div>;
}

function ClaimRow({ canDecide, claim, confirmedFacts, busy, onConfirm, onSaveResponse }: { canDecide: boolean; claim: WebCaseClaim; confirmedFacts: readonly WebCaseFact[]; busy: boolean; onConfirm: () => void; onSaveResponse: (input: { position: "ADMIT" | "PARTIALLY_ADMIT" | "DISPUTE" | "OUTSIDE_SCOPE"; confirmedFactIds: readonly string[]; partialAmount: string | null; currency: string | null }) => void }) {
  const [position, setPosition] = useState<"ADMIT" | "PARTIALLY_ADMIT" | "DISPUTE" | "OUTSIDE_SCOPE">("DISPUTE");
  const [factIds, setFactIds] = useState<string[]>([]);
  const [partialAmount, setPartialAmount] = useState("");
  const [currency, setCurrency] = useState(claim.currency ?? "CNY");
  const amount = claim.claimedAmount ? `${claim.claimedAmount} ${claim.currency ?? ""}`.trim() : "金额未登记";
  const response = claim.response ? `回应：${statusLabel(claim.response.position)}${claim.response.partialAmount ? ` · ${claim.response.partialAmount} ${claim.response.currency ?? ""}` : ""}` : "尚无回应";
  const canRespond = canDecide && claim.status === "CONFIRMED_SCOPE" && confirmedFacts.length > 0;
  return <div className={styles.ledgerRow}><strong>{claim.text}</strong><small>{statusLabel(claim.status)} · 诉请 {amount} · {response}</small>{claim.status === "CANDIDATE" ? canDecide ? <div className={styles.webCaseFactActions}><button disabled={busy} onClick={onConfirm} type="button">确认诉请范围</button></div> : <small>当前角色不能确认诉请范围，请由主办律师处理。</small> : null}{canRespond ? <details className={styles.claimResponseForm} open={!claim.response}><summary>{claim.response ? "调整本方回应" : "登记本方回应"}</summary><form onSubmit={(event) => { event.preventDefault(); onSaveResponse({ position, confirmedFactIds: factIds, partialAmount: position === "PARTIALLY_ADMIT" ? partialAmount : null, currency: position === "PARTIALLY_ADMIT" ? currency : null }); }}><label>回应方式<select disabled={busy} value={position} onChange={(event) => setPosition(event.target.value as typeof position)}><option value="DISPUTE">提出异议</option><option value="ADMIT">承认</option><option value="PARTIALLY_ADMIT">部分承认</option><option value="OUTSIDE_SCOPE">不在本案回应范围</option></select></label>{position === "PARTIALLY_ADMIT" ? <div className={styles.claimResponseAmount}><label>承认金额<input disabled={busy} inputMode="decimal" required value={partialAmount} onChange={(event) => setPartialAmount(event.target.value)} /></label><label>币种<input disabled={busy} maxLength={3} required value={currency} onChange={(event) => setCurrency(event.target.value.toUpperCase())} /></label></div> : null}<fieldset disabled={busy}><legend>依据哪些已确认事实作出回应</legend>{confirmedFacts.map((fact) => <label key={fact.factId}><input checked={factIds.includes(fact.factId)} onChange={() => setFactIds((current) => toggleId(current, fact.factId))} type="checkbox" value={fact.factId} />{fact.text}</label>)}</fieldset><button className={styles.webLawyerSecondaryAction} disabled={busy || factIds.length === 0} type="submit">{busy ? "正在保存…" : "保存本方回应"}</button></form></details> : claim.status === "CONFIRMED_SCOPE" && canDecide ? <small>请先确认至少一项案件事实，再登记本方回应。</small> : null}</div>;
}

function IssueRow({ canDecide, issue, busy, onConfirm, onRebuild }: { canDecide: boolean; issue: WebCaseIssue; busy: boolean; onConfirm: () => void; onRebuild: () => void }) {
  return <div className={styles.ledgerRow}><strong>{issue.question}</strong><small>{statusLabel(issue.status)} · {issue.claimIds.length} 项诉请 · {issue.confirmedFactIds.length} 项已确认事实</small>{issue.status === "CANDIDATE" ? canDecide ? <div className={styles.webCaseFactActions}><button disabled={busy} onClick={onConfirm} type="button">确认本案争点</button></div> : <small>当前角色不能确认争点，请由主办律师处理。</small> : null}{issue.status === "INVALIDATED" ? canDecide ? <div className={styles.webCaseFactActions}><small>诉请回应已变更，需按当前回应重新核对。</small><button disabled={busy} onClick={onRebuild} type="button">重新建立问题草稿</button></div> : <small>诉请回应已变更，请由主办律师重新建立问题草稿。</small> : null}</div>;
}

function TransactionRow({ canDecide, transaction, busy, onConfirm }: { canDecide: boolean; transaction: WebCaseTransaction; busy: boolean; onConfirm: () => void }) {
  const date = transaction.localDate ?? "日期未登记";
  const amount = transaction.amount ? `${transaction.amount} ${transaction.currency ?? ""}`.trim() : "金额未登记";
  const parties = [transaction.payerLabel, transaction.payeeLabel].filter(Boolean).join(" → ");
  return <div className={styles.ledgerRow}><strong>{date} · {amount}</strong><small>{statusLabel(transaction.status)} · {transaction.direction} · {parties || "收付款人未登记"} · {transaction.evidenceCount} 个证据定位</small>{transaction.status === "CANDIDATE" ? canDecide ? <div className={styles.webCaseFactActions}><button disabled={busy} onClick={onConfirm} type="button">确认收付款记录</button></div> : <small>当前角色不能确认收付款记录，请由主办律师处理。</small> : null}</div>;
}

function EmptyLine({ text }: { text: string }) {
  return <p className={styles.webLawyerUploadEmpty}>{text}</p>;
}

function caseFramingSummary(detail: string): string {
  const firstLine = detail.split("\n", 1)[0]?.trim() ?? "";
  if (firstLine.length <= 120) return firstLine;
  const firstSentence = firstLine.match(/^.{1,120}?[。；]/)?.[0]?.trim();
  return firstSentence || `${firstLine.slice(0, 118).trimEnd()}…`;
}

function toggleId(current: readonly string[], value: string): string[] {
  return current.includes(value) ? current.filter((item) => item !== value) : [...current, value];
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    CANDIDATE: "待律师确认",
    CONFIRMED: "已确认",
    CONFIRMED_SCOPE: "诉请范围已确认",
    APPROVED: "已批准",
    PENDING: "待处理",
    ACTIVE: "有效",
    DISPUTED: "有争议",
    DENIED: "已否认",
    ADMIT: "承认",
    PARTIALLY_ADMIT: "部分承认",
    DISPUTE: "提出异议",
    OUTSIDE_SCOPE: "不在本案回应范围",
    INCLUDE: "保留",
    EXCLUDE: "排除",
  };
  return labels[status] ?? status;
}
