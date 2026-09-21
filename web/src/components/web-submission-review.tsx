"use client";

import { useEffect, useState } from "react";
import {
  approveWebSubmissionWorkProduct,
  approveWebDocumentDraft,
  createWebDocumentDraft,
  createWebCaseAgentIdempotencyKey,
  downloadWebCaseAgentDocumentFile,
  isWebLoginRequired,
  readCurrentWebCaseAgentRun,
  readWebCaseAgentDocumentReview,
  readWebCaseAgentInbox,
  readWebDocumentDraftReview,
  requestWebCaseAgentDocumentRevision,
  lockWebSubmissionBundle,
  readWebSubmissionReview,
  webDocumentDraftDeliveryUrl,
  type WebSubmissionReview as WebSubmissionReviewData,
  type WebCaseAgentDocumentReview,
  type WebDocumentDraftReview,
} from "@/lib/web-lawyer-api";
import { CaseAgentDocumentReviewPanel } from "@/components/web-agent-panel";
import styles from "./case-workbench.module.css";

export function WebSubmissionReview({
  canApprove,
  canLock,
  caseId,
  caseVersion,
  onSessionExpired,
  onVersionAdvanced,
}: {
  canApprove: boolean;
  canLock: boolean;
  caseId: string;
  caseVersion: number;
  onSessionExpired: () => void;
  onVersionAdvanced: (caseId: string, version: number) => void;
}) {
  const [review, setReview] = useState<WebSubmissionReviewData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [draftReview, setDraftReview] = useState<WebDocumentDraftReview | null>(null);
  const [openedDraftPairIds, setOpenedDraftPairIds] = useState<ReadonlySet<string>>(() => new Set());

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void readWebSubmissionReview(caseId, controller.signal)
        .then(async (next) => {
          if (!controller.signal.aborted) {
            setReview(next);
            setError(null);
          }
          if (controller.signal.aborted || !next.documentDraftsAvailable) {
            if (!controller.signal.aborted) setDraftReview(null);
            return;
          }
          try {
            const drafts = await readWebDocumentDraftReview(caseId, controller.signal);
            if (!controller.signal.aborted) setDraftReview(drafts);
          } catch (reason: unknown) {
            if (controller.signal.aborted) return;
            if (isWebLoginRequired(reason)) {
              onSessionExpired();
              return;
            }
            setError(reason instanceof Error ? reason.message : "无法读取文书候选台账。");
          }
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          if (isWebLoginRequired(reason)) {
            onSessionExpired();
            return;
          }
          setError(reason instanceof Error ? reason.message : "无法读取应诉材料台账。");
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [caseId, onSessionExpired]);

  if (loading) return <section className={styles.webLawyerEmptyPanel}>正在读取应诉文书、证据清单和质量核对状态…</section>;
  if (error) return <section className={styles.webLawyerEmptyPanel} role="alert"><p className={styles.eyebrow}>第五步：应诉材料</p><h2>暂不能读取应诉材料</h2><p>{error}</p><small>系统不会用示例答辩状或未核验文件替代本案提交包。</small></section>;
  if (!review) return null;

  const bundle = review.currentBundle ?? review.bundles.find((item) => item.lifecycle === "QA_READY") ?? null;
  const exported = review.currentExport;
  async function approve(workProductId: string) {
    if (busy || !canApprove) return;
    setBusy(`approve:${workProductId}`);
    setError(null);
    try {
      const receipt = await approveWebSubmissionWorkProduct(caseId, workProductId, caseVersion);
      onVersionAdvanced(caseId, receipt.matterVersion);
      const next = await readWebSubmissionReview(caseId);
      setReview(next);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "应诉文书审批结果未确认。请刷新案件后再核对。");
    } finally {
      setBusy(null);
    }
  }
  async function lockBundle() {
    if (busy || !canLock || !bundle || bundle.lifecycle !== "QA_READY") return;
    setBusy("lock-bundle");
    setError(null);
    try {
      const receipt = await lockWebSubmissionBundle(caseId, bundle.bundleId, caseVersion);
      onVersionAdvanced(caseId, receipt.matterVersion);
      const next = await readWebSubmissionReview(caseId);
      setReview(next);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "应诉材料包锁定结果未确认。请刷新案件后再核对。");
    } finally {
      setBusy(null);
    }
  }
  async function generateDraft(documentKind: "CASE_REVIEW_MEMO" | "PAYMENT_LEDGER") {
    if (busy || !canApprove || !review?.documentDraftsAvailable) return;
    setBusy(`generate:${documentKind}`); setError(null);
    try {
      const receipt = await createWebDocumentDraft({ caseId, expectedVersion: caseVersion, documentKind });
      onVersionAdvanced(caseId, receipt.matterVersion);
      setDraftReview(await readWebDocumentDraftReview(caseId));
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) { onSessionExpired(); return; }
      setError(reason instanceof Error ? reason.message : "文书候选生成结果未确认。");
    } finally { setBusy(null); }
  }
  async function approveDraft(pairId: string) {
    if (busy || !canApprove || !openedDraftPairIds.has(pairId)) return;
    setBusy(`draft-approve:${pairId}`); setError(null);
    try {
      const receipt = await approveWebDocumentDraft(caseId, pairId, caseVersion);
      onVersionAdvanced(caseId, receipt.matterVersion);
      setDraftReview(await readWebDocumentDraftReview(caseId));
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) { onSessionExpired(); return; }
      setError(reason instanceof Error ? reason.message : "文书候选审批结果未确认。");
    } finally { setBusy(null); }
  }
  function viewDraftPdf(pairId: string) {
    try {
      const location = webDocumentDraftDeliveryUrl({ caseId, pairId, purpose: "REVIEW_PDF" });
      window.open(location, "_blank", "noopener,noreferrer");
      setOpenedDraftPairIds((current) => new Set(current).add(pairId));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "无法打开文书审阅 PDF。");
    }
  }
  function downloadEditableDraft(pairId: string) {
    try {
      const location = webDocumentDraftDeliveryUrl({ caseId, pairId, purpose: "DOWNLOAD_EDITABLE" });
      const link = document.createElement("a");
      link.href = location;
      link.download = "";
      link.rel = "noopener noreferrer";
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "无法下载可编辑文书。");
    }
  }
  return (
    <section className={styles.webLawyerIntake} aria-labelledby="web-submission-review-title">
      <header className={styles.webLawyerIntakeHeading}>
        <div><p className={styles.eyebrow}>第五步：成果文件</p><h2 id="web-submission-review-title">核对成果，准备提交</h2><p>先审阅文书、证据清单、法律依据和金额结果；确认无误后，再由律师锁定提交文件。</p></div>
        <dl><div><dt>案件进度</dt><dd>{submissionStageLabel(review.stage)}</dd></div><div><dt>待核对文件</dt><dd>{review.workProducts.length} 份</dd></div><div><dt>提交材料</dt><dd>{bundle ? bundleLifecycleLabel(bundle.lifecycle) : "尚未形成"}</dd></div></dl>
      </header>
      <section className={styles.webReadinessPanel} aria-label="提交前状态">
        <header><strong>{exported ? "已存在经核验导出记录" : bundle?.lifecycle === "LOCKED" ? "材料包已锁定，等待导出回执" : "提交包尚未锁定"}</strong><span>案件版本 {review.matterVersion}</span></header>
        <div className={styles.webReadinessChecks}>
          <SubmissionCheck label="法院提交候选" ready={review.workProducts.some((item) => item.status === "APPROVED")} detail="至少需要律师批准的法院文书。" />
          <SubmissionCheck label="证据清单" ready={Boolean(bundle)} detail="必须绑定当前锁定的证据 Manifest。" />
          <SubmissionCheck label="法律与测算" ready={Boolean(bundle?.legalBundleId && bundle.calculationRunId)} detail="必须绑定批准规则包和已验证测算运行。" />
          <SubmissionCheck label="最终导出" ready={Boolean(exported)} detail="材料核对完成后，才会开放提交文件下载。" />
        </div>
      </section>
      {error ? <p className={styles.webEvidenceError} role="alert">{error}</p> : null}
      <AgentDocumentCandidateReview caseId={caseId} onSessionExpired={onSessionExpired} />
      <section className={styles.ledgerCard} aria-label="文书候选生成">
        <div><strong>生成可审阅文书候选</strong><small>仅使用本案已确认事实、诉请和收付款；不会自动生成答辩策略或法律结论。</small></div>
        {review.documentDraftsAvailable ? <div className={styles.ledgerRow}>
          <button className={styles.webLawyerSecondaryAction} disabled={!canApprove || busy !== null} onClick={() => void generateDraft("CASE_REVIEW_MEMO")} type="button">{busy === "generate:CASE_REVIEW_MEMO" ? "正在生成…" : "生成案件核对摘要（Word/PDF）"}</button>
          <button className={styles.webLawyerSecondaryAction} disabled={!canApprove || busy !== null} onClick={() => void generateDraft("PAYMENT_LEDGER")} type="button">{busy === "generate:PAYMENT_LEDGER" ? "正在生成…" : "生成收付款核对表（Excel/PDF）"}</button>
        </div> : <p className={styles.webLawyerNotice}>当前部署未接入隔离文书渲染服务，系统不会把文书候选标记为可生成。</p>}
        {draftReview?.pairs.map((pair) => <div className={styles.ledgerRow} key={pair.pairId}>
          <strong>{pair.documentKind} · {pair.status}</strong>
          <small>{pair.reviewPdfPageCount} 页审阅 PDF · 可编辑文件 {pair.editableBytes} 字节</small>
          <div className={styles.webDocumentDraftActions}>
            <button className={styles.webLawyerSecondaryAction} disabled={busy !== null} onClick={() => viewDraftPdf(pair.pairId)} type="button">查看审阅 PDF</button>
            <button className={styles.webLawyerSecondaryAction} disabled={busy !== null} onClick={() => downloadEditableDraft(pair.pairId)} type="button">下载可编辑文档</button>
            {pair.status === "CANDIDATE" ? <button className={styles.webLawyerSecondaryAction} disabled={!canApprove || busy !== null || !openedDraftPairIds.has(pair.pairId)} onClick={() => void approveDraft(pair.pairId)} type="button">{busy === `draft-approve:${pair.pairId}` ? "正在审批…" : openedDraftPairIds.has(pair.pairId) ? "批准此文书候选" : "请先查看审阅 PDF"}</button> : <small>已批准，仍需后续绑定法院 PDF 文书和提交包。</small>}
          </div>
        </div>)}
      </section>
      {review.workProducts.length === 0 ? <p className={styles.webLawyerUploadEmpty}>当前还没有可纳入提交材料的文件。完成材料、案情、法律依据和金额核对后，才能进入提交准备。</p> : <section className={styles.ledgerCard}><div><strong>待审阅的文件</strong><small>每份文件均可追溯到当前材料和版本。</small></div>{review.workProducts.map((item) => <div className={styles.ledgerRow} key={item.workProductId}><strong>{documentKindLabel(item.documentKind)} · {workProductStatusLabel(item.status)}</strong><small>{item.pageCount} 页{item.staleReason ? ` · ${item.staleReason}` : ""}</small>{item.status === "CANDIDATE" ? <button className={styles.webLawyerSecondaryAction} disabled={!canApprove || busy !== null} onClick={() => void approve(item.workProductId)} type="button">{busy === `approve:${item.workProductId}` ? "正在核对…" : canApprove ? "确认此份文件" : "等待复核律师"}</button> : null}</div>)}</section>}
      {bundle ? <section className={styles.ledgerCard}><div><strong>当前提交材料</strong><small>已关联证据、法律依据和金额核对结果。</small></div><div className={styles.ledgerRow}><strong>{bundleLifecycleLabel(bundle.lifecycle)}</strong><small>所需文件：{bundle.requiredDocumentKinds.map(documentKindLabel).join("、")}</small>{bundle.lifecycle === "QA_READY" ? <button className={styles.webLawyerPrimaryAction} disabled={!canLock || busy !== null} onClick={() => void lockBundle()} type="button">{busy === "lock-bundle" ? "正在锁定…" : canLock ? "锁定应诉材料包" : "等待主办律师锁定"}</button> : null}</div></section> : null}
      {exported ? <p className={styles.webLawyerNotice}>提交文件已准备完成（共 {exported.componentCount} 项）。请按律所权限下载并完成最后核对。</p> : <p className={styles.webLawyerNotice}>完成文件确认、证据清单、金额核对和最后确认前，不开放法院提交文件下载。</p>}
    </section>
  );
}

/**
 * An Agent document is an internal review draft, not a court-submission work
 * product.  Keep that distinction, but surface it on the user-facing
 * “成果文件” step as well: a lawyer should not have to know which internal
 * subsystem created a document in order to read or download it.
 */
function AgentDocumentCandidateReview({ caseId, onSessionExpired }: {
  caseId: string;
  onSessionExpired: () => void;
}) {
  const [runId, setRunId] = useState<string | null>(null);
  const [documentReviews, setDocumentReviews] = useState<readonly WebCaseAgentDocumentReview[]>([]);
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadedFiles, setDownloadedFiles] = useState<ReadonlySet<string>>(() => new Set());

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const run = await readCurrentWebCaseAgentRun(caseId, controller.signal);
        if (!run || run.inputSnapshotStatus !== "CURRENT") {
          if (!controller.signal.aborted) {
            setRunId(null);
            setDocumentReviews([]);
            setSelectedArtifactId(null);
          }
          return;
        }
        const inbox = await readWebCaseAgentInbox(caseId, run.runId, controller.signal);
        const artifacts = inbox.artifacts.filter((item) => (
          item.artifactType === "REVIEWABLE_DOCUMENT_CANDIDATE_JSON"
          && item.status === "READY_FOR_REVIEW"
          && !item.recoveryReviewOnly
        ));
        if (artifacts.length === 0) {
          if (!controller.signal.aborted) {
            setRunId(null);
            setDocumentReviews([]);
            setSelectedArtifactId(null);
          }
          return;
        }
        const next = await Promise.all(artifacts.map((artifact) => (
          readWebCaseAgentDocumentReview(caseId, run.runId, artifact.artifactId, controller.signal)
        )));
        if (!controller.signal.aborted) {
          setRunId(run.runId);
          setDocumentReviews(next);
          setSelectedArtifactId((current) => (
            current && next.some((item) => item.artifactId === current)
              ? current
              : next[0]?.artifactId ?? null
          ));
          setError(null);
        }
      } catch (reason: unknown) {
        if (controller.signal.aborted) return;
        if (isWebLoginRequired(reason)) {
          onSessionExpired();
          return;
        }
        setError(reason instanceof Error ? reason.message : "暂不能读取本案待审阅成果。");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [caseId, onSessionExpired]);

  async function download(review: WebCaseAgentDocumentReview, fileRole: "editable" | "pdf-preview") {
    if (!runId || !review.downloadReady || !review.reviewVersion) {
      throw new Error("当前文书版本尚不能安全下载，请刷新后重试。");
    }
    const receipt = await downloadWebCaseAgentDocumentFile(
      caseId,
      runId,
      review.artifactId,
      fileRole,
      review.outputFormat,
      review.reviewVersion,
    );
    setDownloadedFiles((current) => new Set([
      ...current,
      `${review.deliverableKind}:${fileRole}`,
    ]));
    setNotice(`已核验并发起下载：${receipt.fileName}。`);
  }

  async function requestRevision(review: WebCaseAgentDocumentReview) {
    if (!runId || busy || !review.canRequestRevision) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await requestWebCaseAgentDocumentRevision(
        caseId,
        runId,
        review.artifactId,
        review.revisionNumber,
        createWebCaseAgentIdempotencyKey(),
      );
      setDocumentReviews((current) => current.map((item) => (
        item.artifactId === updated.artifactId ? updated : item
      )));
      setDownloadedFiles(new Set());
      setNotice("已开始生成新版本；现有版本和历史记录都会保留，完成后请重新审阅。 ");
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "文书更新请求未确认。");
    } finally {
      setBusy(false);
    }
  }

  if (loading) return null;
  if (documentReviews.length === 0 || !runId) {
    return error ? <p className={styles.webEvidenceError} role="alert">{error}</p> : null;
  }
  const selectedReview = documentReviews.find((item) => item.artifactId === selectedArtifactId) ?? documentReviews[0];
  return <section className={styles.ledgerCard} aria-labelledby="agent-document-candidate-title">
    <div>
      <strong id="agent-document-candidate-title">待律师审阅的候选成果</strong>
      <small>先选择一份成果查看来源、下载或提出修改。候选成果不会自动成为律师意见或法院提交文件。</small>
    </div>
    {notice ? <p className={styles.webAgentResultNotice} role="status">{notice}</p> : null}
    {error ? <p className={styles.webEvidenceError} role="alert">{error}</p> : null}
    <div className={styles.caseAgentCandidateList} role="list" aria-label="待审阅成果列表">
      {documentReviews.map((review) => {
        const selected = review.artifactId === selectedReview.artifactId;
        return <button
          aria-current={selected ? "true" : undefined}
          className={selected ? styles.caseAgentCandidateSelected : undefined}
          key={review.artifactId}
          onClick={() => setSelectedArtifactId(review.artifactId)}
          role="listitem"
          type="button"
        >
          <span><strong>{review.deliverableLabel}</strong><small>{review.outputFormat === "DOCX" ? "Word / PDF" : "Excel / PDF"}</small></span>
          <span>{review.reviewPdfPageCount} 页 · 待律师审阅</span>
        </button>;
      })}
    </div>
    <CaseAgentDocumentReviewPanel
      busy={busy}
      caseId={caseId}
      downloadedFiles={downloadedFiles}
      onDownload={download}
      onRequestRevision={requestRevision}
      review={selectedReview}
      runId={runId}
    />
  </section>;
}

function SubmissionCheck({ label, ready, detail }: { label: string; ready: boolean; detail: string }) {
  return <div className={ready ? styles.webReadinessReady : styles.webReadinessBlocked}><span>{ready ? "已满足" : "待处理"}</span><strong>{label}</strong><small>{detail}</small></div>;
}

function submissionStageLabel(stage: string): string {
  return ({
    INTAKE: "材料整理",
    REVIEW: "律师审阅",
    SUBMISSION: "提交准备",
    CLOSED: "已归档",
  } as Record<string, string>)[stage] ?? "办理中";
}

function bundleLifecycleLabel(lifecycle: string): string {
  return ({
    QA_READY: "待主办律师锁定",
    LOCKED: "已锁定，等待导出",
    EXPORTED: "已导出",
    STALE: "已失效，需要更新",
  } as Record<string, string>)[lifecycle] ?? "正在准备";
}

function documentKindLabel(kind: string): string {
  return ({
    DEFENCE_STATEMENT: "民事答辩状",
    EVIDENCE_CATALOGUE: "证据目录",
    CASE_REVIEW_MEMO: "案件审阅意见",
    PAYMENT_LEDGER: "收付款核对表",
  } as Record<string, string>)[kind] ?? "应诉文件";
}

function workProductStatusLabel(status: string): string {
  return ({
    CANDIDATE: "待律师确认",
    APPROVED: "已确认",
    STALE: "已失效",
  } as Record<string, string>)[status] ?? "待处理";
}
