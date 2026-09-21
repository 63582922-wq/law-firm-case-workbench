"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  approveWebCaseAgentItem,
  commandWebCaseAgentRun,
  completeWebCaseAgentRun,
  createWebCaseAgentIdempotencyKey,
  createWebCaseAgentRun,
  decideWebCaseAgentItem,
  downloadWebCaseAgentDocumentFile,
  isWebLoginRequired,
  queueCurrentEvidenceWebAgentRun,
  reconcileWebCaseAgentCompletion,
  readCurrentWebAgentMaterialRun,
  readCurrentWebCaseAgentRun,
  readWebCaseAgentRun,
  readWebCaseAgentArtifactReview,
  readWebCaseAgentDocumentReview,
  requestWebCaseAgentDocumentRevision,
  readWebAgentCandidateBatch,
  readWebCaseAgentInbox,
  readWebMaterialAnalysis,
  readWebRepresentationProfile,
  runWebMaterialAnalysis,
  type WebAgentCandidateBatch,
  type WebAgentMaterialCandidate,
  type WebAgentMaterialRun,
  type WebCaseAgentApproval,
  type WebCaseAgentArtifact,
  type WebCaseAgentArtifactReview,
  type WebCaseAgentDocumentReview,
  type WebCaseAgentDecision,
  type WebCaseAgentRequestedDeliverable,
  type WebCaseAgentRun,
  type WebLocalMaterialAnalysis,
  type WebRepresentationProfile,
  WebLawyerApiError,
} from "@/lib/web-lawyer-api";
import {
  activePlanReviewEpoch,
  activePlanReviewNotice,
  canCompleteActivePlanRun,
  requiresNewCaseAgentRound,
} from "@/lib/web-active-plan-execution";
import styles from "./case-workbench.module.css";
import { WebDocumentParagraphEditor, WebDocumentModificationHistory } from "@/components/web-document-paragraph-editor";
import { stageWebAgentEvidenceDecisionCandidates } from "@/lib/web-evidence-api";
import { CASE_TASK_PRESETS, DEFAULT_CASE_TASK } from "@/lib/web-case-task-presets";
import { emptyCaseTaskDecisionMessage } from "@/lib/web-case-task-guidance";

const isLocalWebMode = process.env.NEXT_PUBLIC_WEB_API_PREFIX === "/api/local/v1";
const reviewableDocumentArtifactKinds = new Set([
  "REVIEWABLE_DOCUMENT_CANDIDATE_JSON",
  "REVIEWABLE_DOCUMENT_EDITABLE",
  "REVIEWABLE_DOCUMENT_PDF_PREVIEW",
]);

export function WebAgentPanel({
  canRunAgent,
  caseId,
  caseVersion,
  hasMaterials,
  onSessionExpired,
  onVersionAdvanced,
  onOpenEvidence,
}: {
  canRunAgent: boolean;
  caseId: string;
  caseVersion: number;
  hasMaterials: boolean;
  onSessionExpired: () => void;
  onVersionAdvanced: (caseId: string, version: number) => void;
  onOpenEvidence: (pageIds: readonly string[]) => void;
}) {
  const [localAnalysis, setLocalAnalysis] = useState<WebLocalMaterialAnalysis | null>(null);
  const [agentRun, setAgentRun] = useState<WebAgentMaterialRun | null>(null);
  const [candidateBatch, setCandidateBatch] = useState<WebAgentCandidateBatch | null>(null);
  const [representationProfile, setRepresentationProfile] = useState<WebRepresentationProfile | null>(null);
  const [consented, setConsented] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adoptionNotice, setAdoptionNotice] = useState<string | null>(null);

  const read = useCallback(async (signal?: AbortSignal) => {
    if (!isLocalWebMode && !canRunAgent) {
      setLocalAnalysis(null);
      setAgentRun(null);
      setCandidateBatch(null);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      if (isLocalWebMode) {
        setLocalAnalysis(await readWebMaterialAnalysis(caseId, signal));
      } else {
        const [profile, run] = await Promise.all([
          readWebRepresentationProfile(caseId, signal),
          readCurrentWebAgentMaterialRun(caseId, signal),
        ]);
        setRepresentationProfile(profile);
        setAgentRun(run);
        setCandidateBatch(run?.status === "NEEDS_REVIEW" ? await readWebAgentCandidateBatch(caseId, run.runId, signal) : null);
      }
      if (!signal?.aborted) setError(null);
    } catch (reason: unknown) {
      if (signal?.aborted) return;
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "暂不能读取本案处理状态。");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [canRunAgent, caseId, onSessionExpired]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void read(controller.signal), 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [read]);

  useEffect(() => {
    if (!agentRun || !["QUEUED", "RUNNING"].includes(agentRun.status)) return;
    const timer = window.setInterval(() => void read(), 3000);
    return () => window.clearInterval(timer);
  }, [agentRun, read]);

  async function start() {
    if (!hasMaterials || busy || (!isLocalWebMode && (!canRunAgent || !consented || representationProfile?.status !== "CONFIRMED"))) return;
    setBusy(true);
    setError(null);
    try {
      if (isLocalWebMode) {
        setLocalAnalysis(await runWebMaterialAnalysis(caseId));
      } else {
        const run = await queueCurrentEvidenceWebAgentRun(caseId, caseVersion);
        setAgentRun(run);
        setCandidateBatch(null);
      }
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "本案处理尚未开始。请先查看现有进展，避免重复开始同一项工作。");
    } finally {
      setBusy(false);
    }
  }

  async function adoptLowRiskSuggestions() {
    if (isLocalWebMode || !agentRun || agentRun.status !== "NEEDS_REVIEW" || busy) return;
    setBusy(true);
    setError(null);
    setAdoptionNotice(null);
    try {
      const result = await stageWebAgentEvidenceDecisionCandidates(caseId, agentRun.runId, caseVersion);
      onVersionAdvanced(caseId, result.receipt.matterVersion);
      const exceptionCount = result.candidateBatch.excluded.reduce((sum, item) => sum + item.count, 0);
      setAdoptionNotice(`已把 ${result.candidateBatch.decisionIds.length} 项明确低风险建议转为待确认候选（建议纳入 ${result.candidateBatch.includeCount} 页、建议排除 ${result.candidateBatch.excludeCount} 页）。${exceptionCount > 0 ? `另有 ${exceptionCount} 页因 OCR、重复、冲突、低置信或既有决定保留为人工例外。` : "没有候选因风险规则被排除。"} 这些仍不是正式证据决定。`);
      onOpenEvidence(result.candidateBatch.pageIds);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "材料整理结果暂未保存。请刷新后查看案件进展。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
    <section className={styles.webAgentPanel} aria-labelledby="web-agent-panel-title">
      <header>
        <div>
          <p className={styles.eyebrow}>材料处理</p>
          <h3 id="web-agent-panel-title">先把材料整理清楚，再审阅关键问题</h3>
          <p>系统会按本案已入卷的材料进行整理，并把需要你判断的例外单独列出。未经核对的内容不会成为正式事实或应诉结论。</p>
        </div>
        <span className={styles.webAgentMode}>{isLocalWebMode ? "本机审阅" : "材料处理"}</span>
      </header>

      {!hasMaterials ? <p className={styles.webAgentEmpty}>先添加至少一份案件材料，再开始整理。</p> : null}
      {error ? <p className={styles.webAgentError} role="alert">{error}</p> : null}
      {adoptionNotice ? <p className={styles.webAgentResultNotice} role="status">{adoptionNotice}</p> : null}
      {loading && hasMaterials ? <p className={styles.webAgentEmpty}>正在读取本案进展…</p> : null}

      {!loading && hasMaterials && !isLocalWebMode && !canRunAgent ? (
        <div className={styles.webAgentStart}><div><strong>暂不能继续处理材料</strong><span>已入卷材料会保留在案件中。恢复后会从当前进展接续，不需要重复上传。</span></div><span className={styles.webAgentMode}>暂待处理</span></div>
      ) : null}

      {!loading && hasMaterials && isLocalWebMode && !localAnalysis ? (
        <div className={styles.webAgentStart}><div><strong>材料尚未整理</strong><span>系统会整理已接收材料，并标出需要人工查看的内容。</span></div><button className={styles.webLawyerPrimaryAction} disabled={busy} onClick={() => void start()} type="button">{busy ? "正在整理…" : "整理全部材料"}</button></div>
      ) : null}

      {!loading && hasMaterials && !isLocalWebMode && canRunAgent && representationProfile?.status !== "CONFIRMED" ? (
        <div className={styles.webAgentStart}><div><strong>先补充本案基本情况</strong><span>请先确认代理对象和程序阶段。系统不会自行猜测当事人立场，也不会直接套用文书。</span></div><span className={styles.webAgentMode}>待补充</span></div>
      ) : null}

      {!loading && hasMaterials && !isLocalWebMode && canRunAgent && representationProfile?.status === "CONFIRMED" && !agentRun ? (
        <div className={styles.webAgentConsent}>
          <label><input checked={consented} onChange={(event) => setConsented(event.target.checked)} type="checkbox" /><span><strong>确认本次处理范围</strong>系统将根据本案的代理角色、程序阶段和已入卷材料进行整理。材料仅按律所既定的数据处理规则使用；系统不会自行向法院、对方或其他人员发送内容。</span></label>
          <button className={styles.webLawyerPrimaryAction} disabled={busy || !consented} onClick={() => void start()} type="button">{busy ? "正在开始…" : "开始整理与分析"}</button>
        </div>
      ) : null}

      {localAnalysis ? <LocalAnalysisResult analysis={localAnalysis} busy={busy} onOpenEvidence={onOpenEvidence} onRerun={() => void start()} /> : null}
      {agentRun ? <AgentRunResult batch={candidateBatch} busy={busy} onAdoptLowRisk={() => void adoptLowRiskSuggestions()} onOpenEvidence={onOpenEvidence} onRefresh={() => void read()} run={agentRun} /> : null}
    </section>
    </>
  );
}

export function UnifiedCaseAgentPanel({ blockedReason, canCompleteCaseAgentRun, canReview, canReviewCaseAgentDocuments, canRun, caseId, caseVersion, onOpenEvidence, onSessionExpired }: { blockedReason?: string; canCompleteCaseAgentRun: boolean; canReview: boolean; canReviewCaseAgentDocuments: boolean; canRun: boolean; caseId: string; caseVersion: number; onOpenEvidence: (pageIds: readonly string[]) => void; onSessionExpired: () => void }) {
  const [run, setRun] = useState<WebCaseAgentRun | null>(null);
  const [decisions, setDecisions] = useState<readonly WebCaseAgentDecision[]>([]);
  const [approvals, setApprovals] = useState<readonly WebCaseAgentApproval[]>([]);
  const [artifacts, setArtifacts] = useState<readonly WebCaseAgentArtifact[]>([]);
  const [artifactReview, setArtifactReview] = useState<WebCaseAgentArtifactReview | null>(null);
  const [documentReview, setDocumentReview] = useState<WebCaseAgentDocumentReview | null>(null);
  const [artifactReviewLoading, setArtifactReviewLoading] = useState<string | null>(null);
  const [objective, setObjective] = useState<string>(DEFAULT_CASE_TASK.objective);
  const [criteria, setCriteria] = useState<string>(DEFAULT_CASE_TASK.criteria);
  const [constraints, setConstraints] = useState("");
  const [includeDefenceStatement, setIncludeDefenceStatement] = useState(false);
  const [includeEvidenceCatalogue, setIncludeEvidenceCatalogue] = useState(false);
  const [includeSupplementaryEvidenceChecklist, setIncludeSupplementaryEvidenceChecklist] = useState(true);
  const [preparingNewRound, setPreparingNewRound] = useState(false);
  const [decisionNotes, setDecisionNotes] = useState<Record<string, string>>({});
  const [viewedDocumentKinds, setViewedDocumentKinds] = useState<ReadonlySet<string>>(() => new Set());
  const [downloadedDocumentFiles, setDownloadedDocumentFiles] = useState<ReadonlySet<string>>(() => new Set());
  const documentReviewVersions = useRef(new Map<string, string | null>());
  const documentReviewArtifacts = useRef(new Map<string, string>());
  const acceptDocumentReview = useCallback((review: WebCaseAgentDocumentReview) => {
    const version = review.downloadReady && review.reviewArtifactId ? review.reviewVersion ?? null : null;
    if (documentReviewVersions.current.get(review.deliverableKind) !== version || version === null) {
      setViewedDocumentKinds((current) => new Set([...current].filter((kind) => kind !== review.deliverableKind)));
      setDownloadedDocumentFiles((current) => new Set([...current].filter((file) => !file.startsWith(`${review.deliverableKind}:`))));
    }
    documentReviewVersions.current.set(review.deliverableKind, version);
    if (review.reviewArtifactId) documentReviewArtifacts.current.set(review.deliverableKind, review.reviewArtifactId);
    else documentReviewArtifacts.current.delete(review.deliverableKind);
    setDocumentReview(review);
    if (version !== null) setViewedDocumentKinds((current) => new Set([...current, review.deliverableKind]));
  }, []);
  const [reviewedRunEpoch, setReviewedRunEpoch] = useState<string | null>(null);
  const [completionNotice, setCompletionNotice] = useState<string | null>(null);
  const [unknownCompletion, setUnknownCompletion] = useState<Readonly<{
    runId: string;
    expectedRunVersion: number;
    idempotencyKey: string;
    documentReviewVersions: Readonly<Record<string, string>>;
  }> | null>(null);
  const runIdRef = useRef<string | null>(null);
  const autoOpenedDecisionPackageRef = useRef<string | null>(null);
  const currentReviewEpoch = activePlanReviewEpoch(run);
  const currentReviewEpochRef = useRef<string | null>(currentReviewEpoch);
  const appliedReviewEpochRef = useRef<string | null | undefined>(undefined);
  const [loading, setLoading] = useState(canReview);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const read = useCallback(async (signal?: AbortSignal) => {
    if (!canReview) { setRun(null); setLoading(false); return; }
    try {
      const current = await readCurrentWebCaseAgentRun(caseId, signal);
      setRun(current);
      if (current) {
        const inbox = await readWebCaseAgentInbox(caseId, current.runId, signal);
        setDecisions(inbox.decisions); setApprovals(inbox.approvals); setArtifacts(inbox.artifacts);
        const decisionPackage = inbox.artifacts.find(
          (item) => item.artifactType === "LAWYER_DECISION_PACKAGE_CANDIDATE"
            && item.status === "READY_FOR_REVIEW",
        );
        if (
          current.status === "READY_FOR_REVIEW"
          && decisionPackage
          && autoOpenedDecisionPackageRef.current !== `${current.runId}:${decisionPackage.artifactId}`
        ) {
          autoOpenedDecisionPackageRef.current = `${current.runId}:${decisionPackage.artifactId}`;
          setArtifactReviewLoading(decisionPackage.artifactId);
          setDocumentReview(null);
          setArtifactReview(
            await readWebCaseAgentArtifactReview(
              caseId,
              current.runId,
              decisionPackage.artifactId,
              signal,
            ),
          );
          setArtifactReviewLoading(null);
        }
      } else {
        setDecisions([]); setApprovals([]); setArtifacts([]);
        setArtifactReview(null);
        setDocumentReview(null);
      }
      if (!signal?.aborted) setError(null);
    } catch (reason: unknown) {
      if (signal?.aborted) return;
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "暂不能读取本案处理进展。");
    } finally {
      if (!signal?.aborted) {
        setLoading(false);
        setArtifactReviewLoading(null);
      }
    }
  }, [canReview, caseId, onSessionExpired]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void read(controller.signal), 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [read]);

  useEffect(() => {
    const nextRunId = run?.runId ?? null;
    if (runIdRef.current === nextRunId) return;
    runIdRef.current = nextRunId;
    setCompletionNotice(null);
    setUnknownCompletion(null);
  }, [run?.runId]);

  useEffect(() => {
    currentReviewEpochRef.current = currentReviewEpoch;
    if (appliedReviewEpochRef.current === currentReviewEpoch) return;
    appliedReviewEpochRef.current = currentReviewEpoch;
    setReviewedRunEpoch(currentReviewEpoch);
    setViewedDocumentKinds(new Set());
    setDownloadedDocumentFiles(new Set());
    documentReviewVersions.current.clear();
    documentReviewArtifacts.current.clear();
    setArtifactReview(null);
    setDocumentReview(null);
  }, [currentReviewEpoch]);

  useEffect(() => {
    if (!canRun || !run || !["CREATED", "PLANNING", "EXECUTING", "VERIFYING"].includes(run.status)) return;
    const timer = window.setInterval(() => void read(), 3000);
    return () => window.clearInterval(timer);
  }, [canRun, read, run]);

  useEffect(() => {
    if (!run || documentReview?.versionStatus !== "GENERATING") return;
    const controller = new AbortController();
    let polling = false;
    const timer = window.setInterval(() => {
      if (polling) return;
      polling = true;
      void readWebCaseAgentDocumentReview(
        caseId,
        run.runId,
        documentReview.artifactId,
        controller.signal,
      ).then((next) => {
        if (controller.signal.aborted) return;
        acceptDocumentReview(next);
        if (next.downloadReady) {
          setCompletionNotice("文书已更新完成，请重新查看并下载当前版本文件。");
        }
      }).catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        if (isWebLoginRequired(reason)) return onSessionExpired();
        setError(reason instanceof Error ? reason.message : "文书更新状态暂时无法读取。 ");
      }).finally(() => { polling = false; });
    }, 2500);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [caseId, documentReview?.artifactId, documentReview?.versionStatus, onSessionExpired, run, acceptDocumentReview]);

  async function start() {
    const successCriteria = criteria.split("\n").map((item) => item.trim()).filter(Boolean);
    const constraintItems = constraints.split("\n").map((item) => item.trim()).filter(Boolean);
    if (!canRun || busy || objective.trim().length < 2 || successCriteria.length === 0) return;
    const requestedDeliverables: readonly WebCaseAgentRequestedDeliverable[] = [
      "CASE_REVIEW_MEMO",
      ...(includeSupplementaryEvidenceChecklist ? ["SUPPLEMENTARY_EVIDENCE_CHECKLIST"] as const : []),
      ...(includeDefenceStatement ? ["DEFENCE_STATEMENT"] as const : []),
      ...(includeEvidenceCatalogue ? ["EVIDENCE_CATALOGUE"] as const : []),
      "PAYMENT_LEDGER",
    ];
    setBusy(true); setError(null);
    try {
      setRun(await createWebCaseAgentRun(
        caseId,
        caseVersion,
        objective,
        successCriteria,
        constraintItems,
        requestedDeliverables,
      ));
      setDecisions([]); setApprovals([]); setArtifacts([]);
      setPreparingNewRound(false);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "办案目标未建立；请先核验现有任务状态，不要重复提交。");
    } finally { setBusy(false); }
  }

  async function command(action: "pause" | "resume" | "cancel") {
    if (!run || busy) return;
    setBusy(true); setError(null);
    try { setRun(await commandWebCaseAgentRun(caseId, run.runId, action, run.version)); }
    catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "任务状态未更新，请刷新后核对。");
    } finally { setBusy(false); }
  }

  function prepareReplacementRun() {
    if (!run || busy || !["CANCELLED", "FAILED"].includes(run.status)) return;
    // A terminal run remains durable audit evidence.  This only returns the
    // lawyer to the goal composer so a distinct, idempotent replacement run
    // can be created deliberately; it never resumes or resubmits the old
    // external request.
    setObjective(run.objective);
    setCriteria(DEFAULT_CASE_TASK.criteria);
    setConstraints("");
    setIncludeDefenceStatement(false);
    setIncludeSupplementaryEvidenceChecklist(true);
    setRun(null);
    setDecisions([]);
    setApprovals([]);
    setArtifacts([]);
    setArtifactReview(null);
    setDocumentReview(null);
    setError(null);
    setCompletionNotice(null);
    setUnknownCompletion(null);
  }

  function prepareNewCaseRound() {
    if (!run || busy || caseVersion <= run.snapshotMatterVersion) return;
    setObjective("基于当前已确认诉请和争点重新研判本案，形成律师可直接审阅、决策和行动的风险分析、证据缺口、法律研究计划、对方抗辩、策略选项与下一步工作清单。");
    setCriteria([
      "以当前已确认诉请和争点为锚点，区分已确认事实、待证事实和证据缺口，并逐项绑定来源",
      "形成可供律师批准的官方法源检索问题与安全公开检索词，不把未核验法源写成结论",
      "列明核心风险、对方可能抗辩、策略选项、优先行动及每项行动的目的和不处理风险",
      "金额只在本案资料、规则和计算条件齐全后呈现；缺少信息时明确列为待补事项",
    ].join("\n"));
    setConstraints([
      "所有成果仅供律师审阅，不得自动批准、锁定、发送或提交",
      "不得自行确认法律适用、诉请范围或最终律师意见",
      "公开法律依据需要另行核对；本次处理不会自动向法院、对方或其他人员发送任何内容。",
    ].join("\n"));
    setIncludeDefenceStatement(false);
    setPreparingNewRound(true);
    setError(null);
    setCompletionNotice(null);
  }

  async function decide(item: WebCaseAgentDecision, optionId: string) {
    if (!run || busy) return;
    const note = decisionNotes[item.decisionId]?.trim() || null;
    setBusy(true); setError(null);
    try {
      setRun(await decideWebCaseAgentItem(caseId, run.runId, item.decisionId, run.version, optionId, note));
      setDecisionNotes((current) => { const next = { ...current }; delete next[item.decisionId]; return next; });
      await read();
    }
    catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "律师决定未记录，请刷新任务后核对。");
    } finally { setBusy(false); }
  }

  async function approve(item: WebCaseAgentApproval, approved: boolean) {
    if (!run || busy) return;
    setBusy(true); setError(null);
    try { setRun(await approveWebCaseAgentItem(caseId, run.runId, item.approvalId, run.version, approved, null)); await read(); }
    catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "律师审批未记录，请刷新任务后核对。");
    } finally { setBusy(false); }
  }

  async function openArtifact(item: WebCaseAgentArtifact) {
    if (!run || artifactReviewLoading) return;
    const requestReviewEpoch = currentReviewEpoch;
    const documentArtifact = reviewableDocumentArtifactKinds.has(item.artifactType);
    if (documentArtifact && requiresNewCaseAgentRound(run)) {
      setError("案件输入已变化，旧文书不再作为当前可下载成果。请基于新案情重新研判并生成新版本。");
      return;
    }
    if (artifactReview?.artifactId === item.artifactId || documentReview?.artifactId === item.artifactId) {
      setArtifactReview(null);
      setDocumentReview(null);
      return;
    }
    setArtifactReviewLoading(item.artifactId); setError(null);
    try {
      if (documentArtifact) {
        setArtifactReview(null);
        const review = await readWebCaseAgentDocumentReview(caseId, run.runId, item.artifactId);
        if (requestReviewEpoch === null || currentReviewEpochRef.current !== requestReviewEpoch) {
          throw new Error("当前成果版本已变化，请重新打开新版本文书。");
        }
        acceptDocumentReview(review);
      } else {
        setDocumentReview(null);
        const review = await readWebCaseAgentArtifactReview(caseId, run.runId, item.artifactId);
        if (requestReviewEpoch === null || currentReviewEpochRef.current !== requestReviewEpoch) {
          throw new Error("读取期间案件或成果版本已变化，请重新打开当前分析。");
        }
        setArtifactReview(review);
      }
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "分析成果暂时无法读取；系统不会用未复核内容代替。 ");
    } finally { setArtifactReviewLoading(null); }
  }

  async function completeFinalReview(replayUnknown = false) {
    if (!run || busy) return;
    const pending = unknownCompletion;
    const canStart = canCompleteActivePlanRun({
      run,
      canCompleteCaseAgentRun,
      canReviewCaseAgentDocuments,
      reviewedRunEpoch,
      viewedDeliverableKinds: viewedDocumentKinds,
      downloadedDocumentFiles,
    });
    if (!replayUnknown && !canStart) return;
    if (
      replayUnknown
      && (
        !pending
        || pending.runId !== run.runId
        || pending.expectedRunVersion !== run.version
        || run.status !== "READY_FOR_REVIEW"
      )
    ) return;
    const command = pending ?? {
      runId: run.runId,
      expectedRunVersion: run.version,
      documentReviewVersions: Object.fromEntries([...documentReviewVersions.current]
        .filter(([, version]) => version !== null)
        .map(([kind, version]) => [documentReviewArtifacts.current.get(kind) ?? "", version as string])),
      idempotencyKey: createWebCaseAgentIdempotencyKey(),
    };
    setUnknownCompletion(command);
    setBusy(true);
    setError(null);
    setCompletionNotice(null);
    try {
      const result = await completeWebCaseAgentRun(
        caseId,
        command.runId,
        command.expectedRunVersion,
        command.idempotencyKey,
        command.documentReviewVersions,
      );
      setRun(result.run);
      setUnknownCompletion(null);
      setCompletionNotice("本次成果复核已完成。相关 Word、Excel 和 PDF 文件仍可查看与下载。");
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (isUnknownCaseAgentWrite(reason)) {
        setError("终审保存结果暂未确认。系统已保留本次审阅版本，不会重复保存；请先刷新核对当前结果。");
      } else {
        setUnknownCompletion(null);
        setError(reason instanceof Error ? reason.message : "本次成果复核未完成；请刷新后核对任务版本。");
      }
    } finally {
      setBusy(false);
    }
  }

  async function verifyUnknownCompletion() {
    if (!unknownCompletion || busy) return;
    setBusy(true);
    setError(null);
    try {
      const exactReceipt = await reconcileWebCaseAgentCompletion(
        caseId,
        unknownCompletion.runId,
        unknownCompletion.expectedRunVersion,
        unknownCompletion.idempotencyKey,
      );
      if (exactReceipt) {
        const authoritativeRun = await readWebCaseAgentRun(
          caseId,
          exactReceipt.runId,
        );
        setRun((current) => current?.runId === authoritativeRun.runId
          ? authoritativeRun
          : current);
        setUnknownCompletion(null);
        setCompletionNotice("已确认：本次律师终审已完成，无需重复提交。");
        return;
      }
      const current = await readCurrentWebCaseAgentRun(caseId);
      setRun(current);
      if (
        current
        && current.runId === unknownCompletion.runId
        && current.status === "READY_FOR_REVIEW"
        && current.version === unknownCompletion.expectedRunVersion
      ) {
        setCompletionNotice("已核验：当前仍是原终审版本。如需继续，只能由律师明确点击下方按钮，用原请求编号和完全相同的负载重放。");
      } else if (
        current
        && current.runId === unknownCompletion.runId
        && current.status === "COMPLETED"
      ) {
        setError("当前任务已被完成，但服务端没有返回与本次原请求编号匹配的回执；不能把别的律师或别的请求误认为本次完成，请由管理员核验。");
      } else {
        setError("当前任务或版本已变化，不能重放原终审请求。请由管理员核验。");
      }
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError("仍无法核验终审状态。请勿换用新请求或重复点击，需由管理员处理。");
    } finally {
      setBusy(false);
    }
  }

  async function downloadDocumentFile(
    review: WebCaseAgentDocumentReview,
    fileRole: "editable" | "pdf-preview",
  ) {
    if (!run || !review.downloadReady) throw new Error("当前文书尚未生成可下载版本。");
    if (requiresNewCaseAgentRound(run)) throw new Error("案件输入已变化，旧文书不能作为当前成果下载。");
    if (!review.reviewVersion) throw new Error("文书审阅版本暂不可确认，请重新打开当前文书。");
    const requestReviewEpoch = currentReviewEpoch;
    setError(null);
    try {
      const receipt = await downloadWebCaseAgentDocumentFile(
        caseId,
        run.runId,
        review.artifactId,
        fileRole,
        review.outputFormat,
        review.reviewVersion,
      );
      if (requestReviewEpoch === null || currentReviewEpochRef.current !== requestReviewEpoch) {
        throw new Error("下载期间成果版本已变化；本次下载不计入新版本终审，请重新打开并下载。");
      }
      if (!review.reviewVersion || documentReviewVersions.current.get(review.deliverableKind) !== review.reviewVersion) {
        throw new Error("文书审阅版本已变化或无法确认，本次下载不计入终审。");
      }
      setDownloadedDocumentFiles((current) => new Set([
        ...current,
        `${review.deliverableKind}:${fileRole}`,
      ]));
      setCompletionNotice(`已由服务器完成二次核验并发起下载：${receipt.fileName}（${formatDownloadedBytes(receipt.byteSize)}）。`);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      const message = reason instanceof Error ? reason.message : "文书下载未完成，不会计入终审前置条件。";
      setError(message);
      throw reason;
    }
  }

  async function requestDocumentRevision(review: WebCaseAgentDocumentReview) {
    if (!run || busy || !review.canRequestRevision) return;
    setBusy(true);
    setError(null);
    setCompletionNotice(null);
    try {
      const next = await requestWebCaseAgentDocumentRevision(
        caseId,
        run.runId,
        review.artifactId,
        review.revisionNumber,
        createWebCaseAgentIdempotencyKey(),
      );
      acceptDocumentReview(next);
      setViewedDocumentKinds((current) => new Set(
        [...current].filter((kind) => kind !== review.deliverableKind),
      ));
      setDownloadedDocumentFiles((current) => new Set(
        [...current].filter((item) => !item.startsWith(`${review.deliverableKind}:`)),
      ));
      setCompletionNotice("已开始更新文书。系统会保留原版本，新的可下载版本完成后会在这里显示。");
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "文书更新任务未建立，请刷新版本后核对。 ");
    } finally {
      setBusy(false);
    }
  }

  const openDecisions = decisions.filter((item) => item.status === "OPEN");
  const openApprovals = approvals.filter((item) => item.status === "OPEN");
  const approvalIds = new Set(openApprovals.map((item) => item.approvalId));
  const decisionOnlyItems = openDecisions.filter((item) => !approvalIds.has(item.decisionId));
  const visibleArtifacts = artifacts.filter((item) => ![
    "REVIEWABLE_DOCUMENT_EDITABLE",
    "REVIEWABLE_DOCUMENT_PDF_PREVIEW",
  ].includes(item.artifactType));
  const sealedRecoveryArtifacts = visibleArtifacts.filter((item) => item.recoveryReviewOnly);
  const ordinaryArtifacts = visibleArtifacts.filter((item) => !item.recoveryReviewOnly);
  const interventionCount = new Set([
    ...openDecisions.map((item) => item.decisionId),
    ...openApprovals.map((item) => item.approvalId),
  ]).size;
  const canCompleteFinalReview = canCompleteActivePlanRun({
    run,
    canCompleteCaseAgentRun,
    canReviewCaseAgentDocuments,
    reviewedRunEpoch,
    viewedDeliverableKinds: viewedDocumentKinds,
    downloadedDocumentFiles,
  });

  const caseInputsChanged = requiresNewCaseAgentRound(run);
  const readyForLawyerReview = run?.status === "READY_FOR_REVIEW" && !caseInputsChanged;
  const canContinueFromMaterialReview = Boolean(
    run
    && readyForLawyerReview
    && artifacts.some((item) => item.artifactType === "CASE_LEDGER_EXTRACTION_CANDIDATE")
    && !artifacts.some((item) => item.artifactType === "LAWYER_DECISION_PACKAGE_CANDIDATE"),
  );
  const canPrepareNewRound = Boolean(
    canRun
    && run
    && caseInputsChanged
    && ["READY_FOR_REVIEW", "COMPLETED", "STALE", "FAILED"].includes(run.status),
  );
  const showGoalComposer = canRun && !loading && (!run || preparingNewRound);

  return <section className={`${styles.webAgentPanel} ${styles.unifiedCaseAgent}`} aria-labelledby="unified-case-agent-title">
    <header><div><p className={styles.eyebrow}>办案助手</p><h3 id="unified-case-agent-title">{canContinueFromMaterialReview ? "先核对材料整理结果" : readyForLawyerReview ? "本轮办案成果待审阅" : "为本案准备办案成果"}</h3><p>{canContinueFromMaterialReview ? "先处理需要确认的材料，再进入风险、补证和应诉文件工作。" : readyForLawyerReview ? "先看依据、风险和待办，再决定是否采用相关结论或文件。" : "选择本次需要的办案成果；系统会把需要律师决定的事项集中呈现。"}</p></div><span className={styles.webAgentMode}>{canContinueFromMaterialReview ? "待核对" : readyForLawyerReview ? "待审阅" : canRun ? "可以开始" : blockedReason ? "待补充" : canReview ? "查看成果" : "暂待处理"}</span></header>
    {error ? <p className={styles.webAgentError} role="alert">{error}</p> : null}
    {completionNotice ? <p className={styles.webAgentResultNotice} role="status">{completionNotice}</p> : null}
    {unknownCompletion ? <div className={styles.dynamicPlanUnknown}><span>暂未确认本次终审是否保存成功。请先查询结果；系统保留了你刚才审阅的版本，不会另建一笔终审。</span><div><button disabled={busy} onClick={() => void verifyUnknownCompletion()} type="button">查询终审结果</button><button disabled={busy || run?.status !== "READY_FOR_REVIEW" || run.version !== unknownCompletion.expectedRunVersion} onClick={() => void completeFinalReview(true)} type="button">恢复保存本次终审</button></div></div> : null}
    {!canRun ? <div className={styles.webAgentStart}><div><strong>{blockedReason ?? "暂不能继续处理本案"}</strong><span>{blockedReason ? "完成上述事项后即可继续。" : canReview ? "已有成果仍可查看；新的处理会在条件具备后继续。" : "案件与材料会保留在当前案卷中。"}</span></div><span className={styles.webAgentMode}>{blockedReason ? "待确认" : "暂待处理"}</span></div> : null}
    {canReview && loading ? <p className={styles.webAgentEmpty}>正在读取本案进展…</p> : null}
    {preparingNewRound && run ? <p className={styles.webAgentResultNotice} role="status">案件信息已经更新。旧成果会继续保留；新一轮只会读取当前确认后的诉请、争点、事实和证据。</p> : null}
    {showGoalComposer ? <div className={styles.caseAgentGoalComposer}>
      <div className={styles.caseAgentTaskChoice} role="group" aria-label="选择本次要办的事">
        <p>选择本次重点。没有特别要求时，系统会按本案当前材料形成可审阅结果。</p>
        <div className={styles.webAgentActions}>{CASE_TASK_PRESETS.map((preset) => <button className={objective === preset.objective ? styles.caseAgentTaskSelected : undefined} type="button" key={preset.id} disabled={busy} onClick={() => { setObjective(preset.objective); setCriteria(preset.criteria); }}>{preset.label}</button>)}</div>
      </div>
      <details className={styles.caseAgentMoreContext}>
        <summary>补充本案关注点（可选）</summary>
        <label><span>例如：优先核对某笔款项、某份证据或某项抗辩</span><textarea maxLength={4000} onChange={(event) => setConstraints(event.target.value)} placeholder="输入需要特别关注的事项。未填写时，系统将按所选目标处理现有材料。" rows={3} value={constraints} /></label>
      </details>
      <label className={styles.caseAgentDeliverableOption}>
        <input checked={includeSupplementaryEvidenceChecklist} onChange={(event) => setIncludeSupplementaryEvidenceChecklist(event.target.checked)} type="checkbox" />
        <span><strong>同时生成独立补证清单</strong>将本轮发现的缺失材料、当事人待答问题和取得动作整理为可编辑清单；它仅供内部核对，不会自动发函、取证或提交。</span>
      </label>
      <label className={styles.caseAgentDeliverableOption}>
        <input checked={includeDefenceStatement} onChange={(event) => setIncludeDefenceStatement(event.target.checked)} type="checkbox" />
        <span><strong>同时准备民事答辩状草稿</strong>须先确认诉请、争点和法律依据；缺少其中任一项时，系统会先列出缺口，不会勉强生成文书。草稿必须经律师审阅，不会自动对外发送或作为正式答辩。</span>
      </label>
      <label className={styles.caseAgentDeliverableOption}>
        <input checked={includeEvidenceCatalogue} onChange={(event) => setIncludeEvidenceCatalogue(event.target.checked)} type="checkbox" />
        <span><strong>同时整理独立证据目录</strong>仅列出已确认纳入材料范围的页面；拟证明事项和证据三性仍由律师逐项核对。</span>
      </label>
      <div><p>结果供律师审阅，不会自动发送、作为正式意见或法院提交文件。</p><div className={styles.webAgentActions}>{preparingNewRound ? <button className={styles.webLawyerSecondaryAction} disabled={busy} onClick={() => setPreparingNewRound(false)} type="button">暂不开始</button> : null}<button className={styles.webLawyerPrimaryAction} disabled={busy || objective.trim().length < 2 || criteria.trim().length < 1} onClick={() => void start()} type="button">{busy ? "正在开始…" : preparingNewRound ? "按当前材料重新处理" : "开始处理"}</button></div></div>
    </div> : null}
    {run ? <>
      {!canContinueFromMaterialReview ? <><div className={`${styles.webAgentMetrics} ${styles.caseAgentProgress}`}><div><span>当前状态</span><strong>{caseProgressLabel(run.status)}</strong></div><div><span>待你处理</span><strong>{run.openDecisionCount + run.openApprovalCount} 项</strong></div><div><span>可审阅成果</span><strong>{run.artifactCount} 项</strong></div></div>
      <div className={styles.webAgentResultHeading}><div><strong>{caseTaskLabel(run.objective)}</strong><span>{caseProgressMessage(run.status)}</span>{caseInputsChanged ? <span>案件信息已经更新，历史成果会保留；重新处理时将以当前材料为准。</span> : null}</div><div className={styles.webAgentActions}><button className={styles.webLawyerSecondaryAction} disabled={busy} onClick={() => void read()} type="button">刷新</button>{canPrepareNewRound && !preparingNewRound ? <button className={styles.webLawyerPrimaryAction} disabled={busy} onClick={prepareNewCaseRound} type="button">按当前材料重新处理</button> : null}{run.actions.canPause ? <button className={styles.webLawyerSecondaryAction} disabled={busy} onClick={() => void command("pause")} type="button">暂停</button> : null}{run.actions.canResume ? <button className={styles.webLawyerSecondaryAction} disabled={busy} onClick={() => void command("resume")} type="button">继续</button> : null}{run.actions.canCancel ? <button className={styles.webLawyerSecondaryAction} disabled={busy} onClick={() => void command("cancel")} type="button">取消</button> : null}{["CANCELLED", "FAILED"].includes(run.status) && !caseInputsChanged ? <button className={styles.webLawyerPrimaryAction} disabled={busy} onClick={prepareReplacementRun} type="button">重新开始本次工作</button> : null}</div></div></> : null}
      {run.failureMessage ? <p className={styles.webAgentError} role="alert">本次处理暂未完成。现有材料和已形成的成果已保留，请先刷新查看进展，再决定是否重新开始。</p> : null}
      {sealedRecoveryArtifacts.length ? <p className={styles.webAgentResultNotice} role="status">系统保留了 {sealedRecoveryArtifacts.length} 份中断前的历史候选，供律师查看来源和边界。本轮处理尚未完成；这些候选不会自动进入终审、文书或提交材料。</p> : null}
      {run.activePlanExecution && run.status === "FAILED" ? <p className={styles.webAgentError} role="alert">本次处理未完成。系统不会自动重复处理；已入卷材料和历史成果仍可查看。</p> : null}
      {run.activePlanExecution && run.status === "RECONCILIATION_REQUIRED" ? <p className={styles.webAgentResultNotice} role="status">系统正在核对本次处理是否已经完成。无需重复上传或重新开始，确认后会在这里更新。</p> : null}
      {canContinueFromMaterialReview ? <section className={styles.caseAgentNextStep} aria-label="核对材料整理结果"><div><p className={styles.eyebrow}>下一步</p><strong>材料已读完，先核对整理结果</strong><p>确认可用的事实和收付款记录，或处理需要补充的内容。完成后才能基于当前案情形成风险与补证清单。</p></div><a className={styles.webLawyerPrimaryAction} href={`/facts?case=${encodeURIComponent(caseId)}#ledger-extraction-review`}>核对整理结果</a></section> : null}
      {artifactReview ? <CaseAgentArtifactReviewPanel caseId={caseId} onOpenEvidence={onOpenEvidence} review={artifactReview} /> : artifactReviewLoading ? <p className={styles.webAgentEmpty}>正在整理律师审阅首页…</p> : null}
      {documentReview && run ? <CaseAgentDocumentReviewPanel key={`${caseId}:${run.runId}:${documentReview.artifactId}:${documentReview.revisionNumber}`} caseId={caseId} runId={run.runId} busy={busy} downloadedFiles={downloadedDocumentFiles} onDownload={downloadDocumentFile} onRequestRevision={requestDocumentRevision} review={documentReview} /> : null}
      {!canContinueFromMaterialReview ? <div className={styles.caseAgentColumns}>
        <section><header><strong>需要你决定的事项</strong><span>{interventionCount} 项</span></header>{openApprovals.map((approval) => {
          const decision = openDecisions.find((item) => item.decisionId === approval.approvalId) ?? null;
          return <article key={approval.approvalId}><strong>{approval.actionLabel}</strong><p>{approval.reason}；影响：{approval.impact}</p><div><button disabled={busy} onClick={() => void approve(approval, true)} type="button">批准执行</button></div>{decision ? <CaseAgentCorrectionChoices busy={busy} item={decision} note={decisionNotes[decision.decisionId] ?? ""} onDecide={decide} onNote={(value) => setDecisionNotes((current) => ({ ...current, [decision.decisionId]: value }))} /> : <p>当前任务没有可记录的调整入口，因此不能用“拒绝”假装完成。</p>}</article>;
        })}{decisionOnlyItems.map((item) => <article key={item.decisionId}><strong>{item.title}</strong><p>{item.question}</p><CaseAgentCorrectionChoices busy={busy} item={item} note={decisionNotes[item.decisionId] ?? ""} onDecide={decide} onNote={(value) => setDecisionNotes((current) => ({ ...current, [item.decisionId]: value }))} /></article>)}{interventionCount === 0 ? <p>{emptyCaseTaskDecisionMessage(run.status)}</p> : null}</section>
        <section><header><strong>成果</strong><span>{ordinaryArtifacts.length} 个可审阅成果</span></header>{sealedRecoveryArtifacts.map((item) => {
          const expanded = artifactReview?.artifactId === item.artifactId;
          return <article key={item.artifactId}><strong>{item.title}</strong><p>中断前保留的历史候选 · 仅供核对 · 不作为本轮结论或交付材料</p><button className={styles.webLawyerSecondaryAction} disabled={artifactReviewLoading !== null || item.status !== "READY_FOR_REVIEW"} onClick={() => void openArtifact(item)} type="button">{artifactReviewLoading === item.artifactId ? "正在读取…" : expanded ? "收起历史候选" : "查看历史候选与来源"}</button></article>;
        })}{ordinaryArtifacts.map((item) => {
          const documentArtifact = reviewableDocumentArtifactKinds.has(item.artifactType);
          const expanded = artifactReview?.artifactId === item.artifactId || documentReview?.artifactId === item.artifactId;
          const outdatedDocument = documentArtifact && caseInputsChanged;
          return <article key={item.artifactId}>
            <strong>{item.title}</strong>
            <p>{artifactLabel(item.artifactType)} · {outdatedDocument ? "案情已变化，历史文书已失效" : item.reviewRequired ? "等待律师复核" : "已通过当前复核门槛"}</p>
            {outdatedDocument ? <p>原文件和历史记录保留。请使用上方“基于新案情重新研判”，按当前资料生成新版本。</p> : null}
            <button className={styles.webLawyerSecondaryAction} disabled={outdatedDocument || artifactReviewLoading !== null || item.status !== "READY_FOR_REVIEW"} onClick={() => void openArtifact(item)} type="button">
              {outdatedDocument ? "旧版不可作为当前成果下载" : artifactReviewLoading === item.artifactId ? "正在读取…" : expanded ? (documentArtifact ? "收起文书" : "收起分析") : (documentArtifact ? "查看文书与下载" : "查看分析与来源")}
            </button>
          </article>;
        })}{visibleArtifacts.length === 0 ? <p>本次处理尚未形成可审阅成果。</p> : null}</section>
      </div> : null}
      {run.activePlanExecution && readyForLawyerReview ? <div className={styles.dynamicPlanActivation}><div><strong>律师终审</strong><span>{activePlanReviewNotice(run)}</span></div>{canCompleteFinalReview && !unknownCompletion ? <button disabled={busy} onClick={() => void completeFinalReview()} type="button">{busy ? "正在记录终审…" : "确认已完成本次成果复核"}</button> : null}</div> : null}
    </> : null}
  </section>;
}

function caseProgressLabel(status: WebCaseAgentRun["status"]): string {
  switch (status) {
    case "READY_FOR_REVIEW": return "等待审阅";
    case "COMPLETED": return "已完成";
    case "RECONCILIATION_REQUIRED": return "正在核对结果";
    case "FAILED": return "需要处理";
    case "CANCELLED": return "已暂停";
    case "PAUSED": return "已暂停";
    default: return "正在处理";
  }
}

function caseTaskLabel(objective: string): string {
  return CASE_TASK_PRESETS.find((preset) => preset.objective === objective)?.label ?? "本轮案件处理";
}

function caseProgressMessage(status: WebCaseAgentRun["status"]): string {
  switch (status) {
    case "READY_FOR_REVIEW": return "本轮结果已经准备好，请先审阅来源、风险和待决定事项。";
    case "COMPLETED": return "本轮工作已完成，相关成果仍可在下方查看。";
    case "RECONCILIATION_REQUIRED": return "系统正在核对本次处理是否已经完成；现有材料和成果都会保留。";
    case "FAILED": return "本次处理尚未完成。先查看保留的材料和成果，再决定下一步。";
    case "CANCELLED": return "本次工作已停止，已有材料和成果仍然保留。";
    case "PAUSED": return "本次工作已暂停，需要时可以继续。";
    default: return "系统正在整理本案材料和已确认信息，完成后会列出需要你审阅的事项。";
  }
}

function CaseAgentArtifactReviewPanel({ caseId, onOpenEvidence, review }: { caseId: string; onOpenEvidence: (pageIds: readonly string[]) => void; review: WebCaseAgentArtifactReview }) {
  const readiness = review.sections.find((section) => section.sectionId === "lawyer-readiness");
  const executive = review.sections.find((section) => section.sectionId === "lawyer-executive");
  const actions = review.sections.find((section) => section.sectionId === "lawyer-actions");
  const decisions = review.sections.find((section) => section.sectionId === "lawyer-decisions");
  const discoveredIssues = review.sections.find((section) => section.sectionId === "discovered-issues");
  const discoveredActions = review.sections.find((section) => section.sectionId === "discovered-actions");
  const discoveredGaps = review.sections.find((section) => section.sectionId === "discovered-gaps");
  const isDiscoveredAnalysis = Boolean(discoveredIssues);
  const prioritySectionTitles = new Set([
    "当前可用范围",
    "律师先看",
    "争点与证据风险",
    "律师行动清单",
    "必须由律师决定",
    "需要处理的问题",
    "下一步工作",
    "待补证据",
  ]);
  const prioritySections = review.sections.filter((section) => prioritySectionTitles.has(section.title));
  const supportingSections = review.sections.filter((section) => !prioritySections.includes(section) && section.sectionId !== "lawyer-model-receipt");
  const sourceCount = new Set(
    review.sections.flatMap((section) => section.items.flatMap((item) => item.sources.map((source) => `${source.sourceKind}:${source.sourceId}`))),
  ).size;
  const primaryDirection = executive?.items.find((item) => item.itemId === "lawyer-executive-direction")?.detail
    ?? discoveredIssues?.items[0]?.title
    ?? "先核对当前成果的来源和边界，再形成律师决定。";
  const needsCaseFraming = readiness?.items.some((item) => item.itemId === "lawyer-readiness-stage" && item.title.includes("初步风险研判")) ?? false;
  const needsCaseFramingAction = needsCaseFraming || isDiscoveredAnalysis;
  return <section className={styles.caseAgentArtifactReview} aria-live="polite"><header><div><p className={styles.eyebrow}>本案重点</p><h4>{isDiscoveredAnalysis ? "先处理这些问题" : review.title}</h4><p>{isDiscoveredAnalysis ? "先确认案件范围，再处理风险、补证和后续工作。" : "请先核对重点和来源，再决定下一步。"}</p></div><span className={styles.webAgentMode}>{isDiscoveredAnalysis ? "待你判断" : "待审阅"}</span></header>
    <section className={styles.caseAgentDecisionBrief} aria-labelledby="case-agent-decision-brief-title">
      <span className={styles.caseAgentDecisionTab}>{isDiscoveredAnalysis ? "重点" : "律师先看"}</span>
      <div className={styles.caseAgentDecisionMain}>
        <p className={styles.eyebrow}>{isDiscoveredAnalysis ? "优先处理" : "当前工作方向"}</p>
        <h5 id="case-agent-decision-brief-title">{primaryDirection}</h5>
        <p>{isDiscoveredAnalysis ? "先回看材料，再确认案件范围。" : "先处理前置缺口，再由律师决定下一步。"}</p>
        <nav aria-label="决策包章节">
          {readiness ? <a href="#lawyer-readiness">结论边界</a> : null}
          {needsCaseFramingAction ? <a href={`/facts?case=${encodeURIComponent(caseId)}#case-framing`}>确认案件范围</a> : null}
          {actions ? <a href="#lawyer-actions">行动清单</a> : null}
          {discoveredActions ? <a href="#discovered-actions">下一步工作</a> : null}
          {decisions ? <a href="#lawyer-decisions">律师决定</a> : null}
        </nav>
      </div>
      <dl className={styles.caseAgentDecisionImpact}>
        <div><dt>{isDiscoveredAnalysis ? "当前问题" : "前置缺口"}</dt><dd>{isDiscoveredAnalysis ? discoveredIssues?.items.length ?? 0 : readiness?.items.length ?? 0} 项</dd></div>
        <div><dt>建议行动</dt><dd>{isDiscoveredAnalysis ? discoveredActions?.items.length ?? 0 : actions?.items.length ?? 0} 项</dd></div>
        <div><dt>{isDiscoveredAnalysis ? "待补证据" : "律师判断"}</dt><dd>{isDiscoveredAnalysis ? discoveredGaps?.items.length ?? 0 : decisions?.items.length ?? 0} 项</dd></div>
        <div><dt>可回链来源</dt><dd>{sourceCount} 项</dd></div>
      </dl>
    </section>
    <div className={styles.caseAgentReviewFocus} aria-label="优先审阅内容">{prioritySections.map((section) => <details className={styles.caseAgentReviewCompact} key={section.sectionId}><summary><strong>{section.title}</strong><span>{section.items.length} 项</span></summary><CaseAgentReviewSection onOpenEvidence={onOpenEvidence} section={section} priority /></details>)}</div>
    {supportingSections.length ? <details className={styles.caseAgentSupportingReview}><summary>查看其余核对内容（{supportingSections.reduce((sum, section) => sum + section.items.length, 0)} 项）</summary><div className={styles.caseAgentArtifactSections}>{supportingSections.map((section) => <CaseAgentReviewSection key={section.sectionId} onOpenEvidence={onOpenEvidence} section={section} />)}</div></details> : null}
  </section>;
}

function CaseAgentReviewSection({ onOpenEvidence, priority = false, section }: { onOpenEvidence: (pageIds: readonly string[]) => void; priority?: boolean; section: WebCaseAgentArtifactReview["sections"][number] }) {
  const visibleItems = priority ? section.items.slice(0, 3) : section.items;
  const remainingItems = priority ? section.items.slice(3) : [];
  return <section className={styles.caseAgentReviewSection} id={section.sectionId}><header><div><strong>{section.title}</strong><span>{section.severity === "HIGH" ? "优先核对" : section.severity === "MEDIUM" ? "建议核对" : "供查阅"}</span></div><small>{section.items.length} 项</small></header><div className={styles.caseAgentReviewItems}>{visibleItems.map((item) => <CaseAgentReviewItem item={item} key={item.itemId} onOpenEvidence={onOpenEvidence} />)}</div>{remainingItems.length ? <details className={styles.caseAgentReviewRemainder}><summary>查看其余 {remainingItems.length} 项</summary>{remainingItems.map((item) => <CaseAgentReviewItem item={item} key={item.itemId} onOpenEvidence={onOpenEvidence} />)}</details> : null}</section>;
}

function CaseAgentReviewItem({ item, onOpenEvidence }: { item: WebCaseAgentArtifactReview["sections"][number]["items"][number]; onOpenEvidence: (pageIds: readonly string[]) => void }) {
  const pageIds = [...new Set(item.sources.flatMap((source) => source.evidencePageId ? [source.evidencePageId] : []))];
  const summary = reviewItemSummary(item.detail);
  return <article><div><strong>{item.title}</strong>{item.badge ? <span>{item.badge}</span> : null}{item.confidence !== null ? <small>{Math.round(item.confidence * 100)}% 置信</small> : null}</div><p>{summary}</p>{summary !== item.detail ? <details className={styles.caseAgentReviewDetail}><summary>查看判断依据</summary><p>{item.detail}</p></details> : null}<footer>{pageIds.length ? <button onClick={() => onOpenEvidence(pageIds)} type="button">查看来源</button> : null}{item.sources.length ? <details><summary>来源 {item.sources.length} 项</summary><div>{item.sources.map((source) => <span key={`${source.sourceKind}:${source.sourceId}`}>{source.label}</span>)}</div></details> : null}{item.externalUrl ? <a href={item.externalUrl} rel="noreferrer" target="_blank">打开公开来源</a> : null}</footer></article>;
}

function reviewItemSummary(detail: string): string {
  const firstLine = detail.split("\n", 1)[0]?.trim() ?? "";
  if (firstLine.length <= 96) return firstLine;
  const firstSentence = firstLine.match(/^.{1,96}?[。；]/)?.[0]?.trim();
  return firstSentence || `${firstLine.slice(0, 94).trimEnd()}…`;
}

export function CaseAgentDocumentReviewPanel({ caseId, runId, busy, downloadedFiles, onDownload, onRequestRevision, review }: { caseId: string; runId: string; busy: boolean; downloadedFiles: ReadonlySet<string>; onDownload: (review: WebCaseAgentDocumentReview, fileRole: "editable" | "pdf-preview") => Promise<void>; onRequestRevision: (review: WebCaseAgentDocumentReview) => Promise<void>; review: WebCaseAgentDocumentReview }) {
  const [downloading, setDownloading] = useState<"editable" | "pdf-preview" | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  // Historic candidates retain their complete immutable audit payload, but
  // implementation/audit notes are not legal work-product content.  The
  // current template no longer emits this heading; filtering also keeps an
  // already-issued candidate from making the lawyer read internal operations.
  const visibleSections = review.sections
    .map((section, originalIndex) => ({ section, originalIndex }))
    .filter(({ section }) => section.heading !== "模型运行与验证说明");
  const [expandedSections, setExpandedSections] = useState<ReadonlySet<string>>(
    () => visibleSections.length ? new Set([visibleSections[0].section.sectionId]) : new Set(),
  );
  async function download(fileRole: "editable" | "pdf-preview") {
    if (downloading) return;
    setDownloading(fileRole);
    setDownloadError(null);
    try {
      await onDownload(review, fileRole);
    } catch (reason: unknown) {
      setDownloadError(reason instanceof Error ? reason.message : "文书下载未完成。");
    } finally {
      setDownloading(null);
    }
  }
  const pdfDownloaded = downloadedFiles.has(`${review.deliverableKind}:pdf-preview`);
  const editableDownloaded = downloadedFiles.has(`${review.deliverableKind}:editable`);
  const versionStatusLabel = {
    CURRENT: "当前可交付版本",
    UPDATE_REQUIRED: "需要更新模板",
    GENERATING: "正在生成并复核",
    FAILED: "更新未通过",
    UNKNOWN: "更新结果待核验",
  }[review.versionStatus];
  const allSectionsExpanded = visibleSections.length > 0 && expandedSections.size === visibleSections.length;
  const setAllSectionsExpanded = (expanded: boolean) => setExpandedSections(
    expanded ? new Set(visibleSections.map(({ section }) => section.sectionId)) : new Set(),
  );
  return <section className={styles.caseAgentDocumentReview} aria-live="polite">
    <header>
      <div><p className={styles.eyebrow}>候选成果 · 第 {review.revisionNumber} 版</p><h4>{review.title}</h4><p>请先核对正文与来源，再决定是否修改或纳入后续材料。未经律师确认，它不会成为法院提交文件。</p><div className={styles.caseAgentDocumentVersionLine}><span>{versionStatusLabel}</span></div></div>
      <div className={styles.caseAgentDocumentActions}><span className={styles.webAgentMode}>{review.deliverableLabel}</span>{review.downloadReady ? <><button disabled={downloading !== null} onClick={() => void download("pdf-preview")} type="button">{downloading === "pdf-preview" ? "正在准备下载…" : pdfDownloaded ? `已下载 PDF（${review.reviewPdfPageCount} 页）` : `下载 PDF 审阅稿（${review.reviewPdfPageCount} 页）`}</button><button disabled={downloading !== null} onClick={() => void download("editable")} type="button">{downloading === "editable" ? "正在准备下载…" : editableDownloaded ? `已下载可编辑 ${review.outputFormat === "DOCX" ? "Word" : "Excel"}` : `下载可编辑 ${review.outputFormat === "DOCX" ? "Word" : "Excel"}`}</button></> : review.canRequestRevision ? <button disabled={busy} onClick={() => void onRequestRevision(review)} type="button">{busy ? "正在生成…" : "生成可下载版本"}</button> : <span className={styles.caseAgentDocumentProgress}>{review.versionStatus === "GENERATING" ? "正在生成 Word / Excel / PDF，页面会自动刷新。" : "当前版本不可下载，请稍后刷新查看。"}</span>}</div>
    </header>
    {downloadError ? <p className={styles.webAgentError} role="alert">{downloadError}</p> : null}
    {review.previewTruncated ? <p className={styles.webAgentLimitation}>当前页面先显示 {review.displayedItemCount}/{review.totalItemCount} 项；完整内容可在下载的 Word、Excel 或 PDF 中查看。</p> : null}
    {!review.downloadReady ? <div className={styles.caseAgentDocumentVersionGate}><strong>{versionStatusLabel}</strong><p>{review.versionStatus === "GENERATING" ? "正在生成新版文件。旧版本会保留。" : "当前版本暂不能下载；旧版本会保留。"}</p></div> : review.outputFormat === "DOCX" ? <div className={styles.caseAgentDocumentSections}><div className={styles.caseAgentDocumentReadingBar}><span>文书正文 · {visibleSections.length} 节</span><button onClick={() => setAllSectionsExpanded(!allSectionsExpanded)} type="button">{allSectionsExpanded ? "收起正文" : "展开全部"}</button></div>{visibleSections.map(({ section, originalIndex }) => <section key={section.sectionId}><details onToggle={(event) => setExpandedSections((current) => {
      const next = new Set(current);
      if (event.currentTarget.open) next.add(section.sectionId);
      else next.delete(section.sectionId);
      return next;
    })} open={expandedSections.has(section.sectionId)}><summary><h5>{section.heading}</h5><span>{section.paragraphs.length} 段</span></summary><div className={styles.caseAgentDocumentSectionBody}>{section.paragraphs.map((paragraph, paragraphIndex) => <article key={paragraph.paragraphId}><p>{paragraph.text}</p>{paragraph.sources.length ? <details className={styles.caseAgentDocumentParagraphSources}><summary>查看来源（{paragraph.sources.length}）</summary><footer>{paragraph.sources.map((source) => <span key={source.sourceRef}>{source.label}</span>)}</footer></details> : null}{process.env.NEXT_PUBLIC_DOCUMENT_CONTENT_PROPOSALS === "true" && !isLocalWebMode ? <WebDocumentParagraphEditor caseId={caseId} runId={runId} artifactId={review.artifactId} revisionNumber={review.revisionNumber} sectionIndex={originalIndex} paragraphIndex={paragraphIndex} text={paragraph.text} sourceRefs={paragraph.sources.map((source) => source.sourceRef)} disabled={busy || review.versionStatus !== "CURRENT"} /> : null}</article>)}</div></details></section>)}</div> : <div className={styles.caseAgentDocumentTableFrame}><table><thead><tr>{review.columns.map((column) => <th key={column.key} scope="col">{column.label}</th>)}<th scope="col">来源</th></tr></thead><tbody>{review.rows.map((row) => <tr key={row.rowId}>{row.cells.map((cell, index) => <td key={`${row.rowId}-${review.columns[index]?.key ?? index}`}>{formatDocumentCell(cell)}</td>)}<td><div className={styles.caseAgentDocumentSources}>{row.sources.map((source) => <span key={source.sourceRef}>{source.label}</span>)}</div></td></tr>)}</tbody></table></div>}
    {process.env.NEXT_PUBLIC_DOCUMENT_CONTENT_PROPOSALS === "true" && !isLocalWebMode ? <WebDocumentModificationHistory caseId={caseId} runId={runId} artifactId={review.artifactId} /> : null}
    <footer className={styles.caseAgentDocumentBoundary}>候选成果不等于律师意见或法院提交文件；须经律师确认并完成后续材料核对。</footer>
  </section>;
}

function formatDocumentCell(value: string | number | boolean | null): string {
  if (value === null) return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  return String(value);
}

function formatDownloadedBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

function CaseAgentCorrectionChoices({ busy, item, note, onDecide, onNote }: { busy: boolean; item: WebCaseAgentDecision; note: string; onDecide: (item: WebCaseAgentDecision, optionId: string) => Promise<void>; onNote: (value: string) => void }) {
  return <div className={styles.caseAgentCorrectionChoices}><details><summary>认为本次判断有误，或不需要继续此项工作</summary><p>{item.question} 你的纠正会保存在案件记录中；后续处理只会以更新后的案件信息为准。</p>{item.allowNote ? <label className={styles.caseAgentDecisionNote}><span>补充说明（涉及事实、法律方向或其他原因时必填）</span><textarea maxLength={600} onChange={(event) => onNote(event.target.value)} placeholder="说明哪里不对、缺什么，或希望按什么事实边界重新分析。" rows={3} value={note} /></label> : null}<div>{item.options.map((option) => <button disabled={busy || (option.requiresNote && !note.trim())} key={option.optionId} onClick={() => void onDecide(item, option.optionId)} title={option.requiresNote && !note.trim() ? "请先填写补充说明" : option.consequence} type="button">{option.label}{option.requiresNote ? "（需说明）" : ""}</button>)}</div></details></div>;
}

function artifactLabel(value: string): string {
  const labels: Record<string, string> = {
    CASE_REVIEW_MEMO: "案件审阅意见",
    SUPPLEMENTARY_EVIDENCE_CHECKLIST: "补证清单",
    LEGAL_RESEARCH_MEMO: "法律检索意见",
    EVIDENCE_MATRIX: "证据矩阵",
    EVIDENCE_CATALOGUE: "证据目录",
    DOCUMENT_DRAFT: "文书候选",
    CALCULATION: "测算结果",
    REVIEWABLE_DOCUMENT_CANDIDATE_JSON: "文书候选与来源清单",
    REVIEWABLE_DOCUMENT_EDITABLE: "可编辑 Word / Excel",
    REVIEWABLE_DOCUMENT_PDF_PREVIEW: "PDF 预览",
    PUBLIC_RESEARCH_LEADS_CANDIDATE: "网络研究线索",
    VISUAL_PAGE_REVIEW_CANDIDATE: "图片与扫描页识别候选",
    CASE_CONTEXT_REVIEW_CANDIDATE: "案件台账核对（非整案分析）",
  };
  return labels[value] ?? "办案成果";
}

function AgentRunResult({ run, batch, busy, onAdoptLowRisk, onRefresh, onOpenEvidence }: { run: WebAgentMaterialRun; batch: WebAgentCandidateBatch | null; busy: boolean; onAdoptLowRisk: () => void; onRefresh: () => void; onOpenEvidence: (pageIds: readonly string[]) => void }) {
  const groups: readonly [string, readonly WebAgentMaterialCandidate[], string][] = [
    ["优先核对：冲突与低置信", batch?.items.filter((item) => item.kind === "UNCERTAIN" || item.reviewPriority === "HIGH") ?? [], "没有高风险候选。"],
    ["需要 OCR / 视觉读取", batch?.items.filter((item) => item.kind === "OCR_REQUIRED") ?? [], "没有 OCR 例外。"],
    ["疑似重复", batch?.items.filter((item) => item.kind === "DUPLICATE_CANDIDATE") ?? [], "没有疑似重复页。"],
    ["可能相关", batch?.items.filter((item) => item.kind === "RELEVANT_PAGE") ?? [], "没有相关页候选。"],
    ["可能无关", batch?.items.filter((item) => item.kind === "UNRELATED_PAGE") ?? [], "没有无关页候选。"],
  ];
  const active = run.status === "QUEUED" || run.status === "RUNNING";
  return <>
    <div className={styles.webAgentMetrics}><div><span>整案页数</span><strong>{run.progress.totalPages} 页</strong></div><div><span>已处理</span><strong>{run.progress.processedPages} 页</strong></div><div><span>自动批次</span><strong>{run.progress.completedBatchCount}/{run.progress.batchCount}</strong></div><div><span>待律师核对</span><strong>{run.candidateCount} 项</strong></div></div>
    <div className={styles.webAgentResultHeading}><div><strong>{agentStatusLabel(run.status)}</strong><span>{run.progress.remainingPages > 0 ? `尚有 ${run.progress.remainingPages} 页未处理；系统不会静默漏页。` : "当前任务已覆盖全部已登记页。"} {run.externalServiceNotice}</span></div><div className={styles.webAgentActions}>{run.status === "NEEDS_REVIEW" && batch ? <button className={styles.webLawyerPrimaryAction} disabled={busy} onClick={onAdoptLowRisk} type="button">{busy ? "正在采用建议…" : "采用低风险建议为待确认候选"}</button> : null}<button className={styles.webLawyerSecondaryAction} disabled={busy} onClick={onRefresh} type="button">刷新状态</button></div></div>
    {run.status === "NEEDS_REVIEW" && batch ? <p className={styles.webAgentLimitation}>此操作只采用服务端重新核验通过的低风险相关页和极高置信无关页；OCR、重复、冲突、高风险和低置信页仍单列。采用后只是待确认候选，主办律师可在证据页一次批量确认，也可逐页改正。</p> : null}
    {run.failureState ? <p className={styles.webAgentError} role="alert">{run.failureState === "PROVIDER_RESULT_UNKNOWN" ? "外部服务接收结果不明。系统已禁止自动重试，请由管理员核验现有请求。" : `任务未完成（${run.failureState}）。${run.retryAllowed ? "可在管理员核验后重新建立任务。" : "当前不能重试。"}`}</p> : null}
    {active ? <p className={styles.webAgentEmpty}>系统正在整理材料并单列需要人工判断的内容；页面会自动刷新。</p> : null}
    {run.status === "NEEDS_REVIEW" && batch ? <div className={styles.webAgentGroups}>{groups.map(([title, items, empty]) => <AgentCandidateGroup empty={empty} items={items} key={title} onOpenEvidence={onOpenEvidence} title={title} />)}</div> : null}
    {batch?.hasMore ? <p className={styles.webAgentLimitation}>候选超过当前批次显示上限，仍有候选未展示；当前版本不会把未展示项视为已核对。</p> : null}
  </>;
}

function AgentCandidateGroup({ title, items, empty, onOpenEvidence }: { title: string; items: readonly WebAgentMaterialCandidate[]; empty: string; onOpenEvidence: (pageIds: readonly string[]) => void }) {
  const pageIds = [...new Set(items.map((item) => item.evidencePageId))];
  return <section className={styles.webAgentGroup}><header><strong>{title}</strong><div><span>{items.length} 项</span>{pageIds.length ? <button onClick={() => onOpenEvidence(pageIds)} type="button">核对这组</button> : null}</div></header>{items.length === 0 ? <p>{empty}</p> : <div>{items.map((item) => <button className={styles.webAgentCandidate} key={item.candidateId} onClick={() => onOpenEvidence([item.evidencePageId])} type="button"><span><strong>{item.sourceLabel} · 第 {item.pageNumber} 页</strong><small>{Math.round(item.confidence * 100)}% · {item.reviewPriority}</small></span><p>{item.supportingExcerpt}</p></button>)}</div>}</section>;
}

function LocalAnalysisResult({ analysis, busy, onOpenEvidence, onRerun }: { analysis: WebLocalMaterialAnalysis; busy: boolean; onOpenEvidence: (pageIds: readonly string[]) => void; onRerun: () => void }) {
  const payment = analysis.candidates.filter((item) => item.kind === "PAYMENT_OR_CASE_SIGNAL");
  const review = analysis.candidates.filter((item) => item.kind !== "PAYMENT_OR_CASE_SIGNAL");
  const allPageIds = analysis.candidates.flatMap((item) => item.evidencePageId ? [item.evidencePageId] : []);
  return <><div className={styles.webAgentMetrics}><div><span>已读材料</span><strong>{analysis.summary.fileCount} 份</strong></div><div><span>已读页数</span><strong>{analysis.summary.pageCount} 页</strong></div><div><span>优先候选</span><strong>{payment.length} 页</strong></div><div><span>需特殊处理</span><strong>{analysis.summary.scannedPages} 页</strong></div></div><div className={styles.webAgentResultHeading}><div><strong>材料预处理已完成</strong><span>这只是本机文本层候选，不是模型分析或正式决定。</span></div><div className={styles.webAgentActions}><button className={styles.webLawyerSecondaryAction} disabled={busy} onClick={onRerun} type="button">重新预处理</button><button className={styles.webLawyerPrimaryAction} disabled={!allPageIds.length} onClick={() => onOpenEvidence(allPageIds)} type="button">核对全部候选</button></div></div><div className={styles.webAgentGroups}><LocalCandidateGroup items={payment} onOpenEvidence={onOpenEvidence} title="还款、转账、金额或借贷信号" /><LocalCandidateGroup items={review} onOpenEvidence={onOpenEvidence} title="其他需快速浏览的候选" /></div><p className={styles.webAgentLimitation}>{analysis.limitations.join(" ")}</p></>;
}

function LocalCandidateGroup({ title, items, onOpenEvidence }: { title: string; items: WebLocalMaterialAnalysis["candidates"]; onOpenEvidence: (pageIds: readonly string[]) => void }) {
  const pageIds = items.flatMap((item) => item.evidencePageId ? [item.evidencePageId] : []);
  return <section className={styles.webAgentGroup}><header><strong>{title}</strong><div><span>{items.length} 项</span>{pageIds.length ? <button onClick={() => onOpenEvidence(pageIds)} type="button">核对这组</button> : null}</div></header>{items.length === 0 ? <p>没有候选。</p> : <div>{items.slice(0, 12).map((item) => <article key={item.candidateId}><div><strong>{item.sourceFile} · 第 {item.pageNumber} 页</strong><small>{[...item.signals, ...item.dates, ...item.amounts].join(" · ") || "需回到页面核对"}</small></div><p>{item.snippet}</p></article>)}</div>}</section>;
}

function agentStatusLabel(status: WebAgentMaterialRun["status"]): string {
  if (status === "QUEUED") return "整案材料整理已排队";
  if (status === "RUNNING") return "正在整理整案材料";
  if (status === "NEEDS_REVIEW") return "材料已整理，等待律师核对";
  return "整案材料整理未完成";
}

function isUnknownCaseAgentWrite(reason: unknown): boolean {
  if (!(reason instanceof WebLawyerApiError)) return true;
  return reason.status === null
    || reason.status === 408
    || reason.status >= 500
    || (reason.status >= 200 && reason.status < 300);
}
