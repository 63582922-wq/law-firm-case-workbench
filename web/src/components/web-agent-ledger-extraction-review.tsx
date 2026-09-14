"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  confirmWebAgentLedgerExtractionLowRisk,
  createWebAgentLedgerExtractionIdempotencyKey,
  createWebAgentLedgerFollowupIdempotencyKey,
  decideWebAgentLedgerExceptionGroup,
  isWebLoginRequired,
  readWebAgentLedgerExceptionGroupMembers,
  readWebAgentLedgerExceptionFollowupPage,
  readWebAgentLedgerEligibleEvidenceSources,
  readWebAgentLedgerFollowupEvidencePage,
  readWebAgentLedgerExtractionBatches,
  recoverWebAgentLedgerExceptionFollowups,
  resolveWebAgentLedgerExceptionFollowup,
  WebLawyerApiError,
  type WebAgentLedgerExceptionAction,
  type WebAgentLedgerExceptionDecision,
  type WebAgentLedgerExceptionDecisionReceipt,
  type WebAgentLedgerExceptionGroup,
  type WebAgentLedgerExceptionMember,
  type WebAgentLedgerExceptionReason,
  type WebAgentLedgerExtractionBatch,
  type WebAgentLedgerExtractionCandidate,
  type WebAgentLedgerExtractionConfirmationReceipt,
  type WebAgentLedgerExceptionFollowup,
  type WebAgentLedgerExceptionFollowupPage,
  type WebAgentLedgerFollowupActionCode,
  type WebManagedEvidenceSource,
  type WebManagedEvidenceSourceSelection,
} from "@/lib/web-lawyer-api";
import { webAgentLedgerAutomationLabel } from "@/lib/web-agent-ledger-followup-contract";
import styles from "./case-workbench.module.css";
import { WebFactCorrection } from "./web-fact-correction";

type UnknownWrite =
  | Readonly<{
      kind: "LOW_RISK";
      batchId: string;
      expectedVersion: number;
      idempotencyKey: string;
    }>
  | Readonly<{
      kind: "EXCEPTION_GROUP";
      batchId: string;
      groupId: string;
      expectedVersion: number;
      idempotencyKey: string;
      decision: WebAgentLedgerExceptionDecision;
      reason: WebAgentLedgerExceptionReason;
      note: string | null;
    }>;

type ExceptionDecisionInput = Readonly<{
  action: WebAgentLedgerExceptionAction;
  reason: WebAgentLedgerExceptionReason;
  note: string | null;
}>;

const LOW_RISK_PAGE_SIZE = 25;
const EXCEPTION_MEMBER_PAGE_SIZE = 50;
const EXCEPTION_DISPLAY_PAGE_SIZE = 25;
const FOLLOWUP_API_PAGE_SIZE = 50;
const FOLLOWUP_DISPLAY_PAGE_SIZE = 10;
const FOLLOWUP_EVIDENCE_PAGE_SIZE = 25;
const FOLLOWUP_SOURCE_SELECTION_LIMIT = 100;

export function WebAgentLedgerExtractionReview({
  canReview,
  canReviewFollowups,
  caseId,
  onConfirmed,
  onExceptionDecided,
  onFollowupVersionAdvanced,
  onSessionExpired,
}: {
  canReview: boolean;
  canReviewFollowups: boolean;
  caseId: string;
  onConfirmed: (receipt: WebAgentLedgerExtractionConfirmationReceipt) => void;
  onExceptionDecided?: (receipt: WebAgentLedgerExceptionDecisionReceipt) => void;
  onFollowupVersionAdvanced?: (version: number) => void;
  onSessionExpired: () => void;
}) {
  const [batches, setBatches] = useState<readonly WebAgentLedgerExtractionBatch[]>([]);
  const [loading, setLoading] = useState(canReview);
  const [busyScope, setBusyScope] = useState<string | null>(null);
  const [unknown, setUnknown] = useState<UnknownWrite | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const operationKeys = useRef(new Map<string, string>());

  const read = useCallback(async (signal?: AbortSignal) => {
    if (!canReview) {
      setBatches([]);
      setLoading(false);
      setError(null);
      return [];
    }
    setLoading(true);
    try {
      const current = await readWebAgentLedgerExtractionBatches(caseId, signal);
      if (!signal?.aborted) {
        setBatches(current);
        setError(null);
      }
      return current;
    } catch (reason: unknown) {
      if (signal?.aborted) return [];
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return [];
      }
      setError(reason instanceof Error ? reason.message : "暂不能读取 Agent 已完成的事实与交易提取批次。");
      return [];
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [canReview, caseId, onSessionExpired]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void read(controller.signal), 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [read]);

  const visibleBatches = useMemo(
    () => [...batches].sort((left, right) => {
      const statusRank = (value: WebAgentLedgerExtractionBatch["status"]) => ({
        REVIEW_READY: 0,
        EXCEPTIONS_ONLY: 1,
        EXCEPTIONS_PARTIALLY_RESOLVED: 1,
        LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN: 2,
        LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL: 2,
        RESOLVED: 3,
        CONFIRMED: 3,
        STALE: 4,
      })[value];
      return statusRank(left.status) - statusRank(right.status)
        || Date.parse(right.stagedAt) - Date.parse(left.stagedAt);
    }),
    [batches],
  );
  const hasPendingReview = visibleBatches.some((batch) => [
    "REVIEW_READY",
    "EXCEPTIONS_ONLY",
    "EXCEPTIONS_PARTIALLY_RESOLVED",
    "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN",
    "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL",
  ].includes(batch.status));

  if (!canReview && !canReviewFollowups) return null;

  function stableOperationKey(scope: string): string {
    const existing = operationKeys.current.get(scope);
    if (existing) return existing;
    const created = createWebAgentLedgerExtractionIdempotencyKey();
    operationKeys.current.set(scope, created);
    return created;
  }

  async function confirmLowRisk(batch: WebAgentLedgerExtractionBatch) {
    if (batch.status !== "REVIEW_READY" || !batch.canConfirmLowRisk || busyScope || unknown) return;
    const scope = `confirm:${caseId}:${batch.batchId}:${batch.currentMatterVersion}`;
    const idempotencyKey = stableOperationKey(scope);
    setBusyScope(`batch:${batch.batchId}`);
    setError(null);
    setNotice(null);
    try {
      const receipt = await confirmWebAgentLedgerExtractionLowRisk(
        caseId,
        batch.batchId,
        batch.currentMatterVersion,
        idempotencyKey,
      );
      onConfirmed(receipt);
      setNotice(
        `已确认 ${receipt.confirmedFactCount} 项事实、${receipt.confirmedTransactionCount} 项收付款。${batch.exceptionGroupCount > 0 ? `另有 ${batch.exceptionGroupCount} 组内容需要单独处理。` : "本批次已完成。"}`,
      );
      await read();
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (isUnknownWriteOutcome(reason)) {
        setUnknown({
          kind: "LOW_RISK",
          batchId: batch.batchId,
          expectedVersion: batch.currentMatterVersion,
          idempotencyKey,
        });
        setError("本次低风险整组确认结果待核验。系统不会自动重试，请先读取当前批次状态。");
      } else {
        setError(reason instanceof Error ? reason.message : "低风险候选组没有完成确认，请刷新后核对状态。");
      }
    } finally {
      setBusyScope(null);
    }
  }

  async function decideExceptionGroup(
    batch: WebAgentLedgerExtractionBatch,
    group: WebAgentLedgerExceptionGroup,
    input: ExceptionDecisionInput,
  ) {
    if (!group.canDecide || group.status !== "OPEN" || busyScope || unknown) return;
    const scope = [
      "exception",
      caseId,
      batch.batchId,
      group.groupId,
      batch.currentMatterVersion,
      input.action.code,
      input.reason,
      input.note ?? "",
    ].join(":");
    const idempotencyKey = stableOperationKey(scope);
    setBusyScope(`group:${group.groupId}`);
    setError(null);
    setNotice(null);
    try {
      const receipt = await decideWebAgentLedgerExceptionGroup(
        caseId,
        batch.batchId,
        group.groupId,
        batch.currentMatterVersion,
        input.action.code,
        input.reason,
        input.note,
        idempotencyKey,
      );
      onExceptionDecided?.(receipt);
      setNotice(
        receipt.exceptionReviewStatus === "RESOLVED"
          ? `本批次 ${receipt.exceptionGroupCount} 组需要单独处理的内容均已确定下一步。`
          : `已处理 1 组内容；当前 ${receipt.decidedExceptionGroupCount}/${receipt.exceptionGroupCount} 组已处理。其余内容仍待处理。`,
      );
      await read();
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (isDefinitiveWriteConflict(reason)) {
        try {
          const current = await readWebAgentLedgerExtractionBatches(caseId);
          setBatches(current);
          const winningGroup = current
            .find((item) => item.batchId === batch.batchId)
            ?.exceptionGroups.find((item) => item.groupId === group.groupId);
          setNotice(
            winningGroup?.status === "DECIDED"
              ? `另一项并发操作已为本组确定“${winningGroup.decisionLabel}”。本次操作没有覆盖该结果。`
              : "案件版本已变化，本次异常组意图没有提交。请按当前页面重新决定。",
          );
        } catch (readReason: unknown) {
          if (isWebLoginRequired(readReason)) return onSessionExpired();
          setError("处置冲突已确定，但仍无法读取服务器当前结果。请刷新后核对。");
        }
      } else if (isUnknownWriteOutcome(reason)) {
        setUnknown({
          kind: "EXCEPTION_GROUP",
          batchId: batch.batchId,
          groupId: group.groupId,
          expectedVersion: batch.currentMatterVersion,
          idempotencyKey,
          decision: input.action.code,
          reason: input.reason,
          note: input.note,
        });
        setError("本次异常组处置结果待核验。系统不会自动重传决定，请先读取服务器状态。");
      } else {
        setError(reason instanceof Error ? reason.message : "异常组处置没有完成，请刷新后核对状态。");
      }
    } finally {
      setBusyScope(null);
    }
  }

  async function verifyUnknown() {
    if (!unknown) return;
    setLoading(true);
    setError(null);
    try {
      if (unknown.kind === "EXCEPTION_GROUP") {
        // A GET projection deliberately omits the private note and cannot
        // prove whether another tab submitted the exact same intent. The
        // lawyer's explicit verification therefore replays the complete
        // normalized payload with the original idempotency key. This is never
        // automatic: the database returns the saved receipt or applies this
        // one exact command.
        const receipt = await decideWebAgentLedgerExceptionGroup(
          caseId,
          unknown.batchId,
          unknown.groupId,
          unknown.expectedVersion,
          unknown.decision,
          unknown.reason,
          unknown.note,
          unknown.idempotencyKey,
        );
        onExceptionDecided?.(receipt);
        setUnknown(null);
        setNotice("已用原请求编号核验：服务器已保存这一完整异常组分流意图，无需再次提交。");
        await read();
        return;
      }
      const current = await readWebAgentLedgerExtractionBatches(caseId);
      setBatches(current);
      const batch = current.find((item) => item.batchId === unknown.batchId);
      if (!batch) {
        setNotice("批次状态已经变化或不再属于当前案件；原请求不会自动重试。请以当前页面为准。");
        setUnknown(null);
        return;
      }
      const lowRiskConfirmed = batch.status === "CONFIRMED"
        || batch.status === "RESOLVED"
        || batch.status === "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN"
        || batch.status === "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL";
      if (lowRiskConfirmed) {
        onConfirmed({
          batchId: batch.batchId,
          matterVersion: batch.currentMatterVersion,
          confirmedFactCount: batch.lowRiskCandidates.filter((item) => item.candidateKind === "FACT").length,
          confirmedTransactionCount: batch.lowRiskCandidates.filter((item) => item.candidateKind === "TRANSACTION").length,
          confirmedTotalCount: batch.lowRiskCount,
        });
        setNotice("已核验：低风险组已经确认，无需再次提交；尚未分流的异常组仍保持未入账。");
        setUnknown(null);
        return;
      }
      if (batch.status !== "REVIEW_READY" || batch.currentMatterVersion !== unknown.expectedVersion || !batch.canConfirmLowRisk) {
        setNotice("批次或案件版本已经变化，原请求不会重试。请以当前批次状态为准。");
        setUnknown(null);
        return;
      }
      setNotice("已核验：服务器仍显示原低风险组未确认。再次点击时会沿用原请求编号。");
      setUnknown(null);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (unknown.kind === "EXCEPTION_GROUP" && isDefinitiveWriteConflict(reason)) {
        try {
          const current = await readWebAgentLedgerExtractionBatches(caseId);
          setBatches(current);
          const group = current
            .find((item) => item.batchId === unknown.batchId)
            ?.exceptionGroups.find((item) => item.groupId === unknown.groupId);
          setNotice(
            group?.status === "DECIDED"
              ? `已核验：另一项并发操作已为本组确定“${group.decisionLabel}”。原请求没有覆盖该结果。`
              : "已核验：案件版本已经变化，原异常组意图没有提交。请按当前页面重新决定。",
          );
          setUnknown(null);
          return;
        } catch (readReason: unknown) {
          if (isWebLoginRequired(readReason)) return onSessionExpired();
          setError("处置冲突已确定，但仍无法读取服务器胜出结果。请保持当前页面，不要再次提交。");
          return;
        }
      }
      setError("仍无法核验服务器状态。请保持当前页面，不要再次提交该操作。");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className={styles.ledgerExtractionPanel} aria-labelledby="agent-ledger-extraction-title">
      <header>
        <div>
          <p className={styles.eyebrow}>材料整理结果</p>
          <h3 id="agent-ledger-extraction-title">{hasPendingReview ? "核对材料记录" : "已整理的材料记录"}</h3>
          <p>{hasPendingReview ? "先处理需要核对的记录。" : "已确认记录仍可回到原件查看。"}</p>
        </div>
        <span className={styles.webAgentMode}>{hasPendingReview ? "待核对" : "已整理"}</span>
      </header>

      {loading ? <p className={styles.webAgentEmpty}>正在读取本案材料整理结果…</p> : null}
      {error ? <p className={styles.webAgentError} role="alert">{error}</p> : null}
      {unknown ? (
        <div className={styles.ledgerExtractionUnknown}>
          <span>结果待核验；系统不会自动或盲目重试。</span>
          <button onClick={() => void verifyUnknown()} type="button">先核验服务器状态</button>
        </div>
      ) : null}
      {notice ? <p className={styles.ledgerExtractionReceipt} role="status">{notice}</p> : null}
      <LedgerExceptionFollowupPanel
        canReview={canReviewFollowups}
        caseId={caseId}
        onSessionExpired={onSessionExpired}
        onVersionAdvanced={onFollowupVersionAdvanced}
      />

      {canReview && !loading && !error && visibleBatches.length === 0 ? (
        <div className={styles.ledgerExtractionEmpty}>
          <strong>暂未形成材料记录</strong>
          <span>材料整理完成后会显示在这里。</span>
        </div>
      ) : null}

      {canReview ? <div className={styles.ledgerExtractionBatches}>
        {visibleBatches.map((batch) => (
          <ExtractionBatch
            batch={batch}
            busyScope={busyScope}
            decisionBlocked={Boolean(unknown) || Boolean(busyScope)}
            key={batch.batchId}
            onConfirm={() => void confirmLowRisk(batch)}
            onDecide={(group, input) => void decideExceptionGroup(batch, group, input)}
            onSessionExpired={onSessionExpired}
          />
        ))}
      </div> : null}
    </section>
  );
}

type FollowupCommandIntent = Readonly<{
  followupId: string;
  expectedVersion: number;
  action: WebAgentLedgerFollowupActionCode;
  reasonNote: string;
  sources: readonly WebManagedEvidenceSourceSelection[];
  idempotencyKey: string;
}>;

type FollowupUnknownWrite =
  | Readonly<{ kind: "ACTION"; intent: FollowupCommandIntent }>
  | Readonly<{
      kind: "RECOVERY";
      expectedVersion: number;
      idempotencyKey: string;
    }>;

type FollowupProjection = Readonly<{
  totalCount: number;
  controlHealth: WebAgentLedgerExceptionFollowupPage["controlHealth"];
  canRecover: boolean;
  followups: readonly WebAgentLedgerExceptionFollowup[];
}>;

function LedgerExceptionFollowupPanel({
  canReview,
  caseId,
  onSessionExpired,
  onVersionAdvanced,
}: {
  canReview: boolean;
  caseId: string;
  onSessionExpired: () => void;
  onVersionAdvanced?: (version: number) => void;
}) {
  const [projection, setProjection] = useState<FollowupProjection>({
    totalCount: 0,
    controlHealth: null,
    canRecover: false,
    followups: [],
  });
  const [displayPage, setDisplayPage] = useState(0);
  const [loading, setLoading] = useState(canReview);
  const [busyScope, setBusyScope] = useState<string | null>(null);
  const [unknown, setUnknown] = useState<FollowupUnknownWrite | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const operationKeys = useRef(new Map<string, string>());

  const readAll = useCallback(async (signal?: AbortSignal): Promise<FollowupProjection> => {
    if (!canReview) {
      const empty: FollowupProjection = { totalCount: 0, controlHealth: null, canRecover: false, followups: [] };
      setProjection(empty);
      setLoading(false);
      return empty;
    }
    setLoading(true);
    let offset = 0;
    let expectedTotal: number | null = null;
    let expectedControl: FollowupProjection["controlHealth"] | undefined;
    let expectedCanRecover: boolean | undefined;
    const followups: WebAgentLedgerExceptionFollowup[] = [];
    try {
      do {
        const page = await readWebAgentLedgerExceptionFollowupPage(
          caseId,
          offset,
          FOLLOWUP_API_PAGE_SIZE,
          signal,
        );
        if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
        if (expectedTotal === null) {
          expectedTotal = page.totalCount;
          expectedControl = page.controlHealth;
          expectedCanRecover = page.canRecover;
        } else if (
          page.totalCount !== expectedTotal
          || page.controlHealth !== expectedControl
          || page.canRecover !== expectedCanRecover
        ) {
          throw new Error("异常后续工作在分页读取期间已变化，请重新刷新。");
        }
        if (page.offset !== offset) throw new Error("异常后续工分页不连续。");
        followups.push(...page.followups);
        if (page.nextOffset === null) break;
        if (page.nextOffset <= offset) throw new Error("异常后续工分页没有前进。");
        offset = page.nextOffset;
      } while (offset > 0);

      const totalCount = expectedTotal ?? 0;
      if (
        followups.length !== totalCount
        || new Set(followups.map((item) => item.followupId)).size !== followups.length
        || new Set(followups.map((item) => item.currentMatterVersion)).size > 1
      ) {
        throw new Error("异常后续工不完整，本页已停止操作。");
      }
      const next: FollowupProjection = {
        totalCount,
        controlHealth: expectedControl ?? null,
        canRecover: expectedCanRecover ?? false,
        followups,
      };
      setProjection(next);
      setDisplayPage((current) => Math.min(current, Math.max(0, Math.ceil(totalCount / FOLLOWUP_DISPLAY_PAGE_SIZE) - 1)));
      setError(null);
      return next;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [canReview, caseId]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void readAll(controller.signal).catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        if (isWebLoginRequired(reason)) return onSessionExpired();
        setError(reason instanceof Error ? reason.message : "暂不能读取已分流的异常后续工作。");
      });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [onSessionExpired, readAll]);

  if (!canReview) return null;

  function stableOperationKey(scope: string): string {
    const current = operationKeys.current.get(scope);
    if (current) return current;
    const created = createWebAgentLedgerFollowupIdempotencyKey();
    operationKeys.current.set(scope, created);
    return created;
  }

  async function refreshAfterConflict(followupId?: string): Promise<void> {
    try {
      const current = await readAll();
      if (followupId) {
        const winner = current.followups.find((item) => item.followupId === followupId);
        setNotice(
          winner
            ? `服务器当前结果：该项仍待处理，但案件已到版本 ${winner.currentMatterVersion}。本次操作没有覆盖已生效的并发变更。`
            : "服务器当前结果：该项后续工作已结束或被其他操作替代；本次操作没有覆盖胜出结果。",
        );
      } else {
        setNotice(
          current.controlHealth === "HEALTHY"
            ? "服务器当前结果：异常工作的分析控制已恢复，本次操作没有覆盖胜出结果。"
            : "案件状态已变化，恢复请求未提交。请按当前页面重新决定。",
        );
      }
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError("并发冲突已确定，但仍无法读取服务器胜出结果。请保持当前页面，不要重复提交。");
    }
  }

  async function submitAction(intent: FollowupCommandIntent) {
    if (busyScope || (unknown && unknown.kind !== "ACTION")) return;
    setBusyScope(`followup:${intent.followupId}`);
    setError(null);
    setNotice(null);
    try {
      const receipt = await resolveWebAgentLedgerExceptionFollowup(
        caseId,
        intent.followupId,
        intent.expectedVersion,
        intent.action,
        intent.reasonNote,
        intent.sources,
        intent.idempotencyKey,
      );
      setUnknown(null);
      onVersionAdvanced?.(receipt.matterVersion);
      setNotice(`${followupTerminalLabel(receipt.terminalState)}。Agent 将以案件版本 ${receipt.matterVersion} 重新判断后续工作。`);
      try {
        await readAll();
      } catch (readReason: unknown) {
        if (isWebLoginRequired(readReason)) return onSessionExpired();
        setError("操作回执已确认，但暂时无法刷新待办列表。请以上方成功回执为准，稍后重新读取。");
      }
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (isDefinitiveWriteConflict(reason)) {
        setUnknown(null);
        await refreshAfterConflict(intent.followupId);
      } else if (isUnknownWriteOutcome(reason)) {
        setUnknown({ kind: "ACTION", intent });
        setError("本次后续工作操作结果待核验。系统不会自动重传；请用原请求编号核验完整意图。");
      } else {
        setError(reason instanceof Error ? reason.message : "异常后续工作没有完成，请刷新后核对当前状态。");
      }
    } finally {
      setBusyScope(null);
    }
  }

  function prepareAction(
    followup: WebAgentLedgerExceptionFollowup,
    action: WebAgentLedgerFollowupActionCode,
    reasonNote: string,
    sources: readonly WebManagedEvidenceSourceSelection[],
  ) {
    const normalizedSources = [...sources]
      .map((source) => ({ objectType: source.objectType, objectId: source.objectId }))
      .sort((left, right) => `${left.objectType}:${left.objectId}`.localeCompare(`${right.objectType}:${right.objectId}`));
    const scope = JSON.stringify({
      kind: "followup",
      caseId,
      followupId: followup.followupId,
      headSequence: followup.headSequence,
      expectedVersion: followup.currentMatterVersion,
      action,
      reasonNote,
      sources: normalizedSources,
    });
    void submitAction({
      followupId: followup.followupId,
      expectedVersion: followup.currentMatterVersion,
      action,
      reasonNote,
      sources: normalizedSources,
      idempotencyKey: stableOperationKey(scope),
    });
  }

  async function startRecovery(
    expectedVersion: number,
    idempotencyKey = stableOperationKey(`recovery:${caseId}:${expectedVersion}`),
  ) {
    if (busyScope || (unknown && unknown.kind !== "RECOVERY")) return;
    setBusyScope("recovery");
    setError(null);
    setNotice(null);
    try {
      const receipt = await recoverWebAgentLedgerExceptionFollowups(caseId, expectedVersion, idempotencyKey);
      setUnknown(null);
      setNotice("新的受控恢复任务已建立并接管全部待办。网页未选择或传入任何运行编号。");
      onVersionAdvanced?.(receipt.matterVersion);
      try {
        await readAll();
      } catch (readReason: unknown) {
        if (isWebLoginRequired(readReason)) return onSessionExpired();
        setError("恢复接管回执已确认，但暂时无法刷新待办列表。请以上方成功回执为准，稍后重新读取。");
      }
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (isDefinitiveWriteConflict(reason)) {
        setUnknown(null);
        await refreshAfterConflict();
      } else if (isUnknownWriteOutcome(reason)) {
        setUnknown({ kind: "RECOVERY", expectedVersion, idempotencyKey });
        setError("恢复任务结果待核验。系统不会自动新建第二个任务；请用原请求编号核验。");
      } else {
        setError(reason instanceof Error ? reason.message : "异常后续工作恢复没有完成。");
      }
    } finally {
      setBusyScope(null);
    }
  }

  function verifyUnknown() {
    if (!unknown) return;
    if (unknown.kind === "ACTION") {
      void submitAction(unknown.intent);
      return;
    }
    void startRecovery(unknown.expectedVersion, unknown.idempotencyKey);
  }

  const pageCount = Math.max(1, Math.ceil(projection.followups.length / FOLLOWUP_DISPLAY_PAGE_SIZE));
  const safePage = Math.min(displayPage, pageCount - 1);
  const pageStart = safePage * FOLLOWUP_DISPLAY_PAGE_SIZE;
  const visible = projection.followups.slice(pageStart, pageStart + FOLLOWUP_DISPLAY_PAGE_SIZE);
  const matterVersion = projection.followups[0]?.currentMatterVersion ?? null;

  return (
    <section className={styles.ledgerFollowupPanel} aria-labelledby="ledger-followup-title">
      <header>
        <div>
          <p className={styles.eyebrow}>待补材料与后续事项</p>
          <h4 id="ledger-followup-title">需要补充或重新核对的内容</h4>
          <p>这里保留尚未办结的补证、重新提取和暂缓事项。</p>
        </div>
        <strong>{loading ? "正在核对…" : `${projection.totalCount} 项待完成`}</strong>
      </header>

      {projection.controlHealth === "RECOVERY_REQUIRED" ? (
        <div className={styles.ledgerFollowupRecovery} role="alert">
          <div>
            <strong>自动分析控制已中断</strong>
            <span>原任务不再被当作可继续执行。需由服务器建立新的受控任务，并一次接管全部待办。</span>
          </div>
          {projection.canRecover && matterVersion !== null ? (
            <button disabled={Boolean(busyScope) || Boolean(unknown)} onClick={() => void startRecovery(matterVersion)} type="button">
              {busyScope === "recovery" ? "正在建立恢复任务…" : "启动恢复任务并接管"}
            </button>
          ) : <small>请由主办律师启动恢复。</small>}
        </div>
      ) : null}

      {error ? <p className={styles.webAgentError} role="alert">{error}</p> : null}
      {unknown ? (
        <div className={styles.ledgerExtractionUnknown}>
          <span>结果待核验；只能重放原完整请求，不会产生新意图。</span>
          <button disabled={Boolean(busyScope)} onClick={verifyUnknown} type="button">用原请求编号核验</button>
        </div>
      ) : null}
      {notice ? <p className={styles.ledgerExtractionReceipt} role="status">{notice}</p> : null}
      {!loading && !error && projection.followups.length === 0 ? (
        <div className={styles.ledgerFollowupEmpty}>
          <strong>当前没有未完成的异常后续工作</strong>
          <span>这只表示当前 ACTIVE 待办为空；已有决定和完成回执仍保留在审计记录中。</span>
        </div>
      ) : null}

      {visible.length > 0 ? (
        <>
          <div className={styles.ledgerFollowupPager}>
            <span>显示 {pageStart + 1}–{Math.min(pageStart + FOLLOWUP_DISPLAY_PAGE_SIZE, projection.totalCount)} / {projection.totalCount}</span>
            <div>
              <button disabled={safePage === 0} onClick={() => setDisplayPage((value) => Math.max(0, value - 1))} type="button">上一页</button>
              <button disabled={safePage >= pageCount - 1} onClick={() => setDisplayPage((value) => Math.min(pageCount - 1, value + 1))} type="button">下一页</button>
            </div>
          </div>
          <div className={styles.ledgerFollowupList}>
            {visible.map((followup) => (
              <LedgerExceptionFollowupCard
                blocked={Boolean(busyScope) || Boolean(unknown)}
                busy={busyScope === `followup:${followup.followupId}`}
                caseId={caseId}
                followup={followup}
                key={`${followup.followupId}:${followup.headSequence}`}
                onSessionExpired={onSessionExpired}
                onSubmit={(action, reasonNote, sources) => prepareAction(followup, action, reasonNote, sources)}
              />
            ))}
          </div>
        </>
      ) : null}
    </section>
  );
}

function LedgerExceptionFollowupCard({
  blocked,
  busy,
  caseId,
  followup,
  onSessionExpired,
  onSubmit,
}: {
  blocked: boolean;
  busy: boolean;
  caseId: string;
  followup: WebAgentLedgerExceptionFollowup;
  onSessionExpired: () => void;
  onSubmit: (
    action: WebAgentLedgerFollowupActionCode,
    reasonNote: string,
    sources: readonly WebManagedEvidenceSourceSelection[],
  ) => void;
}) {
  const [selectedAction, setSelectedAction] = useState<WebAgentLedgerFollowupActionCode | "">("");
  const [reasonNote, setReasonNote] = useState("");
  const [sources, setSources] = useState<readonly WebManagedEvidenceSource[]>([]);
  const [sourceTotalCount, setSourceTotalCount] = useState(0);
  const [sourceOffset, setSourceOffset] = useState(0);
  const [selectedSources, setSelectedSources] = useState<readonly WebManagedEvidenceSource[]>([]);
  const [sourcesLoaded, setSourcesLoaded] = useState(false);
  const [sourcesLoading, setSourcesLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const action = followup.allowedActions.find((item) => item.code === selectedAction) ?? null;

  async function loadSources(offset = 0) {
    if (sourcesLoading) return;
    setSourcesLoading(true);
    setSourceError(null);
    try {
      const current = await readWebAgentLedgerEligibleEvidenceSources(
        caseId,
        followup.followupId,
        offset,
        FOLLOWUP_API_PAGE_SIZE,
      );
      setSources(current.sources);
      setSourceTotalCount(current.totalCount);
      setSourceOffset(current.offset);
      setSourcesLoaded(true);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setSourceError(reason instanceof Error ? reason.message : "暂不能读取符合本次补证要求的新材料。");
    } finally {
      setSourcesLoading(false);
    }
  }

  function selectAction(value: string) {
    const selected = followup.allowedActions.find((item) => item.code === value) ?? null;
    setSelectedAction(selected?.code ?? "");
    setReasonNote("");
    setSelectedSources([]);
    if (selected?.code === "CONFIRM_MORE_EVIDENCE") void loadSources();
  }

  function toggleSource(source: WebManagedEvidenceSource) {
    const key = managedSourceKey(source);
    setSelectedSources((current) => {
      if (current.some((item) => managedSourceKey(item) === key)) {
        return current.filter((item) => managedSourceKey(item) !== key);
      }
      if (current.length >= FOLLOWUP_SOURCE_SELECTION_LIMIT) return current;
      return [...current, source];
    });
  }

  const selectedSourceKeys = new Set(selectedSources.map(managedSourceKey));
  const selectedSourceRefs = selectedSources
    .map((source) => ({ objectType: source.objectType, objectId: source.objectId }));
  const normalizedReason = reasonNote.trim();
  const canSubmit = followup.canAct
    && action !== null
    && normalizedReason.length >= 1
    && normalizedReason.length <= 500
    && (action.code !== "CONFIRM_MORE_EVIDENCE" || selectedSourceRefs.length > 0)
    && selectedSourceRefs.length <= FOLLOWUP_SOURCE_SELECTION_LIMIT
    && !blocked;

  return (
    <article className={styles.ledgerFollowupCard}>
      <header>
        <div>
          <span>{followupKindLabel(followup.kind)} · {formatDateTime(followup.createdAt)}</span>
          <strong>{followup.reason}</strong>
        </div>
        <small>{followup.candidateCount} 项候选</small>
      </header>
      {followup.reasonNote ? <p className={styles.ledgerFollowupRouteNote}>{followup.reasonNote}</p> : null}
      <ul className={styles.ledgerFollowupReasons}>
        {followup.reviewReasons.map((reason) => <li key={reason}>{reason}</li>)}
      </ul>
      {followup.kind === "REEXTRACTION" ? (
        <div className={styles.ledgerFollowupAutomation} role="status">
          <strong>{webAgentLedgerAutomationLabel(followup.automationStatus)}</strong>
          <span>重新提取会继续使用原异常组的完整受管来源，通过独立核验前不会结束本项工作。</span>
        </div>
      ) : null}
      {followup.acceptanceRequirements.length > 0 ? (
        <div className={styles.ledgerFollowupRequirements}>
          <strong>补证验收条件</strong>
          <ul>{followup.acceptanceRequirements.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
      ) : null}
      <FollowupEvidenceBrowser
        caseId={caseId}
        evidencePageCount={followup.evidencePageCount}
        followupId={followup.followupId}
        onSessionExpired={onSessionExpired}
      />

      <div className={styles.ledgerFollowupActionForm}>
        {followup.canAct ? (
          <>
            <label>
              <span>本项后续操作</span>
              <select disabled={blocked} onChange={(event) => selectAction(event.target.value)} value={selectedAction}>
                <option value="">请选择</option>
                {followup.allowedActions.map((item) => <option key={item.code} value={item.code}>{item.label}</option>)}
              </select>
            </label>
            {action ? (
              <>
                <p>{action.consequence}</p>
                {action.code === "CONFIRM_MORE_EVIDENCE" ? (
                  <fieldset className={styles.ledgerFollowupSources}>
                    <legend>选择本次新增的受管材料</legend>
                    {sourcesLoading ? <span>正在由服务器核对可用材料…</span> : null}
                    {sourceError ? (
                      <div role="alert"><span>{sourceError}</span><button onClick={() => void loadSources()} type="button">重新读取</button></div>
                    ) : null}
                    {sourcesLoaded && sourceTotalCount === 0 ? <span>尚无符合本次要求的新入卷材料。请先到“材料与证据”上传。</span> : null}
                    {sources.length > 0 ? (
                      <>
                        <div className={styles.ledgerFollowupSourceTools}>
                          <span>已选 {selectedSources.length} / 全部 {sourceTotalCount}（单次最多 {FOLLOWUP_SOURCE_SELECTION_LIMIT} 份）</span>
                          <button
                            onClick={() => setSelectedSources((current) => {
                              const byKey = new Map(current.map((item) => [managedSourceKey(item), item]));
                              for (const source of sources) {
                                if (byKey.size >= FOLLOWUP_SOURCE_SELECTION_LIMIT) break;
                                byKey.set(managedSourceKey(source), source);
                              }
                              return [...byKey.values()];
                            })}
                            type="button"
                          >选择本页</button>
                          <button onClick={() => setSelectedSources([])} type="button">清空</button>
                        </div>
                        <div className={styles.ledgerFollowupPager}>
                          <span>显示 {sourceOffset + 1}–{Math.min(sourceOffset + sources.length, sourceTotalCount)} / {sourceTotalCount}</span>
                          <div>
                            <button disabled={sourceOffset === 0 || sourcesLoading} onClick={() => void loadSources(Math.max(0, sourceOffset - FOLLOWUP_API_PAGE_SIZE))} type="button">上一页</button>
                            <button disabled={sourceOffset + sources.length >= sourceTotalCount || sourcesLoading} onClick={() => void loadSources(sourceOffset + sources.length)} type="button">下一页</button>
                          </div>
                        </div>
                        <div className={styles.ledgerFollowupSourceList}>
                          {sources.map((source) => (
                            <label key={managedSourceKey(source)}>
                              <input
                                checked={selectedSourceKeys.has(managedSourceKey(source))}
                                disabled={!selectedSourceKeys.has(managedSourceKey(source)) && selectedSources.length >= FOLLOWUP_SOURCE_SELECTION_LIMIT}
                                onChange={() => toggleSource(source)}
                                type="checkbox"
                              />
                              <span><strong>{source.displayLabel}</strong><small>{managedSourceTypeLabel(source.objectType)} · {formatDateTime(source.createdAt)}</small></span>
                            </label>
                          ))}
                        </div>
                      </>
                    ) : null}
                  </fieldset>
                ) : null}
                <label>
                  <span>操作说明（必填，将进入审计记录）</span>
                  <textarea disabled={blocked} maxLength={500} onChange={(event) => setReasonNote(event.target.value)} rows={3} value={reasonNote} />
                  <small>{reasonNote.length}/500</small>
                </label>
                <button
                  disabled={!canSubmit}
                  onClick={() => {
                    if (!action) return;
                    onSubmit(action.code, normalizedReason, selectedSourceRefs);
                  }}
                  type="button"
                >
                  {busy ? "正在由服务器核对当前状态…" : `确认：${action.label}`}
                </button>
              </>
            ) : null}
          </>
        ) : <small>当前身份可以查看完整后续工作；请由主办律师确认补证、恢复、撤回或替代。</small>}
      </div>
    </article>
  );
}

function FollowupEvidenceBrowser({
  caseId,
  evidencePageCount,
  followupId,
  onSessionExpired,
}: {
  caseId: string;
  evidencePageCount: number;
  followupId: string;
  onSessionExpired: () => void;
}) {
  const [offset, setOffset] = useState(0);
  const [visible, setVisible] = useState<readonly string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadPage = useCallback(async (nextOffset: number, signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const page = await readWebAgentLedgerFollowupEvidencePage(
        caseId,
        followupId,
        nextOffset,
        FOLLOWUP_EVIDENCE_PAGE_SIZE,
        signal,
      );
      if (page.totalCount !== evidencePageCount) {
        throw new Error("异常后续工作的来源页集合已变化，请刷新待办。");
      }
      setOffset(page.offset);
      setVisible(page.evidencePageIds);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (!signal?.aborted) {
        setError(reason instanceof Error ? reason.message : "暂不能读取来源页定位。");
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [caseId, evidencePageCount, followupId, onSessionExpired]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void loadPage(0, controller.signal);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loadPage]);

  return (
    <details className={styles.ledgerFollowupEvidence}>
      <summary>查看全部 {evidencePageCount} 个来源页定位</summary>
      {error ? <div role="alert"><span>{error}</span><button onClick={() => void loadPage(offset)} type="button">重新读取</button></div> : null}
      <div className={styles.ledgerFollowupPager}>
        <span>{loading ? "正在读取…" : `显示 ${offset + 1}–${Math.min(offset + visible.length, evidencePageCount)} / ${evidencePageCount}`}</span>
        <div>
          <button disabled={offset === 0 || loading} onClick={() => void loadPage(Math.max(0, offset - FOLLOWUP_EVIDENCE_PAGE_SIZE))} type="button">上一页</button>
          <button disabled={offset + visible.length >= evidencePageCount || loading} onClick={() => void loadPage(offset + visible.length)} type="button">下一页</button>
        </div>
      </div>
      <div className={styles.ledgerFollowupEvidenceLinks}>
        {visible.map((pageId, index) => (
          <Link href={`/evidence?case=${encodeURIComponent(caseId)}&focus=${encodeURIComponent(pageId)}`} key={pageId}>
            查看来源页 {offset + index + 1}
          </Link>
        ))}
      </div>
    </details>
  );
}

function managedSourceKey(source: WebManagedEvidenceSourceSelection): string {
  return `${source.objectType}:${source.objectId}`;
}

function managedSourceTypeLabel(value: WebManagedEvidenceSourceSelection["objectType"]): string {
  return value === "EVIDENCE_FILE" ? "证据文件" : "受管材料";
}

function followupKindLabel(value: WebAgentLedgerExceptionFollowup["kind"]): string {
  return ({
    REEXTRACTION: "重新提取",
    MORE_EVIDENCE: "等待补证",
    DEFERRED_REVIEW: "暂缓复核",
  })[value];
}

function followupTerminalLabel(value: "SATISFIED" | "RESUMED" | "WITHDRAWN" | "SUPERSEDED"): string {
  return ({
    SATISFIED: "已核对并绑定本次新增受管材料",
    RESUMED: "已结束暂缓并恢复本组复核",
    WITHDRAWN: "已撤回本项后续工作",
    SUPERSEDED: "已记录本项后续工作由新情况替代",
  })[value];
}

function ExtractionBatch({
  batch,
  busyScope,
  decisionBlocked,
  onConfirm,
  onDecide,
  onSessionExpired,
}: {
  batch: WebAgentLedgerExtractionBatch;
  busyScope: string | null;
  decisionBlocked: boolean;
  onConfirm: () => void;
  onDecide: (group: WebAgentLedgerExceptionGroup, input: ExceptionDecisionInput) => void;
  onSessionExpired: () => void;
}) {
  const lowRiskKinds = kindCounts(batch.lowRiskCandidates);
  const lowRiskConfirmed = isLowRiskConfirmed(batch);
  const lowRiskContent = batch.lowRiskCount > 0 ? <>
    <div>
      <strong>{lowRiskConfirmed ? "已确认的材料记录" : `可确认 ${batch.lowRiskCount} 项材料记录`}</strong>
      <span>{lowRiskConfirmed ? "原件和确认记录可随时查看。" : "请先抽查来源，再确认可直接采用的记录。"}</span>
    </div>
    <LowRiskCandidateBrowser batch={batch} />
    {batch.status === "REVIEW_READY" ? (
      <div className={styles.ledgerExtractionConfirm}>
        <p>确认后，这些记录会进入案件台账。</p>
        {batch.canConfirmLowRisk ? (
          <button disabled={decisionBlocked} onClick={onConfirm} type="button">
            {busyScope === `batch:${batch.batchId}` ? "正在确认…" : `确认 ${batch.lowRiskCount} 项记录`}
          </button>
        ) : <small>请由主办律师确认。</small>}
      </div>
    ) : null}
  </> : null;
  return (
    <article className={styles.ledgerExtractionBatch}>
      <header>
        <div>
          <strong>{batchStatusLabel(batch.status)}</strong>
          <span>{formatDateTime(batch.stagedAt)}</span>
        </div>
        <small>{batch.candidateCount} 项候选</small>
      </header>
      <dl className={styles.ledgerExtractionSummary}>
        <div><dt>低风险整组</dt><dd>{batch.lowRiskCount}</dd></div>
        <div><dt>事实 / 收付款</dt><dd>{lowRiskKinds.fact} / {lowRiskKinds.transaction}</dd></div>
        <div><dt>异常原因组</dt><dd>{batch.exceptionGroupCount}</dd></div>
        <div><dt>已分流 / 全部</dt><dd>{batch.decidedExceptionGroupCount} / {batch.exceptionGroupCount}</dd></div>
      </dl>

      {batch.lowRiskCount > 0 ? lowRiskConfirmed ? <details className={styles.ledgerExtractionLowRisk}><summary>已确认 {batch.lowRiskCount} 项记录 · 查看明细</summary>{lowRiskContent}</details> : <section className={styles.ledgerExtractionLowRisk}>{lowRiskContent}</section> : null}

      {batch.exceptionGroupCount > 0 ? (
        <section className={styles.ledgerExceptionGroups} aria-label="材料提取异常原因组">
          <header>
            <div>
              <strong>发现 {batch.exceptionCount} 项需要单独处理的内容</strong>
              <span>{exceptionProgressLabel(batch)}</span>
            </div>
            <small>按原因分组处理，避免遗漏</small>
          </header>
          <div>
            {batch.exceptionGroups.map((group) => (
              <ExceptionGroupReview
                batch={batch}
                busy={busyScope === `group:${group.groupId}`}
                decisionBlocked={decisionBlocked}
                group={group}
                key={group.groupId}
                onDecide={(input) => onDecide(group, input)}
                onSessionExpired={onSessionExpired}
              />
            ))}
          </div>
        </section>
      ) : null}
    </article>
  );
}

function ExceptionGroupReview({
  batch,
  group,
  busy,
  decisionBlocked,
  onDecide,
  onSessionExpired,
}: {
  batch: WebAgentLedgerExtractionBatch;
  group: WebAgentLedgerExceptionGroup;
  busy: boolean;
  decisionBlocked: boolean;
  onDecide: (input: ExceptionDecisionInput) => void;
  onSessionExpired: () => void;
}) {
  const [members, setMembers] = useState<readonly WebAgentLedgerExceptionMember[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
  const [memberPage, setMemberPage] = useState(0);
  const [loadingMembers, setLoadingMembers] = useState(false);
  const [memberError, setMemberError] = useState<string | null>(null);
  const [selectedAction, setSelectedAction] = useState<WebAgentLedgerExceptionDecision | "">("");
  const [selectedReason, setSelectedReason] = useState<WebAgentLedgerExceptionReason | "">("");
  const [note, setNote] = useState("");

  const action = group.allowedActions.find((item) => item.code === selectedAction) ?? null;
  const complete = members.length === group.candidateCount && nextOffset === null;
  const pageCount = Math.max(1, Math.ceil(members.length / EXCEPTION_DISPLAY_PAGE_SIZE));
  const safePage = Math.min(memberPage, pageCount - 1);
  const visibleMembers = members.slice(
    safePage * EXCEPTION_DISPLAY_PAGE_SIZE,
    (safePage + 1) * EXCEPTION_DISPLAY_PAGE_SIZE,
  );

  async function loadNextPage() {
    if (nextOffset === null || loadingMembers) return;
    setLoadingMembers(true);
    setMemberError(null);
    try {
      const page = await readWebAgentLedgerExceptionGroupMembers(
        batch.matterId,
        batch.batchId,
        group.groupId,
        nextOffset,
        EXCEPTION_MEMBER_PAGE_SIZE,
      );
      if (page.totalCount !== group.candidateCount) {
        throw new Error("异常组成员数量已变化，请刷新批次。");
      }
      const combined = [...members, ...page.members];
      if (new Set(combined.map((item) => item.sequence)).size !== combined.length) {
        throw new Error("异常组成员分页重复，请刷新批次。");
      }
      setMembers(combined);
      setNextOffset(page.nextOffset);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setMemberError(reason instanceof Error ? reason.message : "暂不能读取完整异常组。");
    } finally {
      setLoadingMembers(false);
    }
  }

  function chooseAction(value: string) {
    const next = group.allowedActions.find((item) => item.code === value) ?? null;
    setSelectedAction(next?.code ?? "");
    setSelectedReason("");
    setNote("");
  }

  const noteValue = note.trim();
  const canSubmit = complete
    && group.canDecide
    && action !== null
    && selectedReason !== ""
    && (!action.requiresNote || noteValue.length > 0)
    && noteValue.length <= 500
    && !decisionBlocked;

  return (
    <article className={styles.ledgerExceptionGroup}>
      <header>
        <div>
          <span>{kindLabel(group.candidateKind)} · {group.riskLabel}</span>
          <strong>{group.summary}</strong>
        </div>
        <small className={group.status === "DECIDED" ? styles.ledgerGroupDone : styles.ledgerGroupOpen}>
          {group.status === "DECIDED" ? "已处置" : `${group.candidateCount} 项待整组处理`}
        </small>
      </header>
      <p>{group.sourceGuidance}</p>
      <ul>{group.reviewReasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>

      {group.status === "DECIDED" ? (
        <div className={styles.ledgerExceptionDecisionReceipt}>
          <strong>{group.decisionLabel}</strong>
          <span>{group.decisionReasonLabel}</span>
          <small>本组已记录下一步处置，不代表组内事实均已确认；仍可展开原文和修改稿。</small>
        </div>
      ) : null}
        <>
          <div className={styles.ledgerExceptionInspect}>
            <div>
              <strong>{complete ? `完整组已读取 · ${members.length} 项` : `先读取完整组 · 已读取 ${members.length}/${group.candidateCount} 项`}</strong>
              <span>处置按钮仅在全部成员和来源均已读取后开放；提交时仍由服务器重新绑定完整成员。</span>
            </div>
            {!complete ? (
              <button disabled={loadingMembers} onClick={() => void loadNextPage()} type="button">
                {loadingMembers ? "正在读取下一页…" : members.length === 0 ? "读取本组成员" : "继续读取剩余成员"}
              </button>
            ) : null}
          </div>
          {memberError ? <p className={styles.webAgentError} role="alert">{memberError}</p> : null}
          {members.length > 0 ? (
            <div className={styles.ledgerExceptionMemberBrowser}>
              <div className={styles.ledgerExtractionPager}>
                <span>显示 {safePage * EXCEPTION_DISPLAY_PAGE_SIZE + 1}–{Math.min((safePage + 1) * EXCEPTION_DISPLAY_PAGE_SIZE, members.length)} / {group.candidateCount}</span>
                <div>
                  <button disabled={safePage === 0} onClick={() => setMemberPage((value) => Math.max(0, value - 1))} type="button">上一页</button>
                  <button disabled={safePage >= pageCount - 1} onClick={() => setMemberPage((value) => Math.min(pageCount - 1, value + 1))} type="button">下一页</button>
                </div>
              </div>
              <div className={styles.ledgerExceptionMembers}>
                {visibleMembers.map((member) => (
                  <ExceptionMember caseId={batch.matterId} version={batch.currentMatterVersion} onSessionExpired={onSessionExpired} key={member.sequence} member={member} />
                ))}
              </div>
            </div>
          ) : null}
          {complete && group.status !== "DECIDED" ? (
            <div className={styles.ledgerExceptionDecisionForm}>
              {group.canDecide ? (
                <>
                  <label>
                    <span>整组下一步</span>
                    <select onChange={(event) => chooseAction(event.target.value)} value={selectedAction}>
                      <option value="">请选择</option>
                      {group.allowedActions.map((item) => <option key={item.code} value={item.code}>{item.label}</option>)}
                    </select>
                  </label>
                  {action ? (
                    <>
                      <p>{action.consequence}</p>
                      <label>
                        <span>结构化原因</span>
                        <select onChange={(event) => setSelectedReason(event.target.value as WebAgentLedgerExceptionReason)} value={selectedReason}>
                          <option value="">请选择</option>
                          {action.reasons.map((reason) => <option key={reason.code} value={reason.code}>{reason.label}</option>)}
                        </select>
                      </label>
                      <label>
                        <span>{action.requiresNote ? "补充说明（必填）" : "补充说明（可选）"}</span>
                        <textarea maxLength={500} onChange={(event) => setNote(event.target.value)} rows={3} value={note} />
                        <small>{note.length}/500</small>
                      </label>
                      <button
                        disabled={!canSubmit}
                        onClick={() => {
                          if (!action || selectedReason === "") return;
                          onDecide({ action, reason: selectedReason, note: noteValue || null });
                        }}
                        type="button"
                      >
                        {busy ? "正在由服务器核对完整组…" : `确认：${action.label}`}
                      </button>
                    </>
                  ) : null}
                </>
              ) : <small>当前角色可查看完整组，请由主办律师决定整组下一步。</small>}
            </div>
          ) : null}
        </>
    </article>
  );
}

function ExceptionMember({
  caseId,
  member,
  version,
  onSessionExpired,
}: {
  caseId: string;
  member: WebAgentLedgerExceptionMember;
  version: number;
  onSessionExpired: () => void;
}) {
  return (
    <article className={styles.ledgerExtractionException}>
      <header>
        <div><strong>{kindLabel(member.candidateKind)}</strong><span>{member.summary}</span></div>
        <small>来源匹配 {Math.round(member.confidence * 100)}%</small>
      </header>
      <ul>{member.reviewReasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
      <div className={styles.ledgerExtractionExcerpts}>
        {member.excerpts.map((excerpt) => (
          <blockquote key={excerpt.evidencePageId}>
            <p>“{excerpt.text}”</p>
            <Link href={`/evidence?case=${encodeURIComponent(caseId)}&focus=${encodeURIComponent(excerpt.evidencePageId)}`}>
              查看证据第 {excerpt.pageNumber} 页
            </Link>
          </blockquote>
        ))}
      </div>
      {member.candidateKind === "FACT" && member.extractionCandidateId ? <WebFactCorrection
        caseId={caseId} candidateId={member.extractionCandidateId} version={version}
        originalText={member.summary} onSessionExpired={onSessionExpired} /> : null}
    </article>
  );
}

function LowRiskCandidateBrowser({ batch }: { batch: WebAgentLedgerExtractionBatch }) {
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(batch.lowRiskCandidates.length / LOW_RISK_PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const start = safePage * LOW_RISK_PAGE_SIZE;
  const end = Math.min(start + LOW_RISK_PAGE_SIZE, batch.lowRiskCandidates.length);
  const candidates = batch.lowRiskCandidates.slice(start, end);
  return (
    <details className={styles.ledgerExtractionFullGroup}>
      <summary>展开查看完整不可编辑组（共 {batch.lowRiskCount} 项）</summary>
      <div className={styles.ledgerExtractionPager}>
        <span>当前显示 {start + 1}–{end} / {batch.lowRiskCount}</span>
        <div>
          <button disabled={safePage === 0} onClick={() => setPage((value) => Math.max(0, value - 1))} type="button">上一页</button>
          <button disabled={safePage >= pageCount - 1} onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))} type="button">下一页</button>
        </div>
      </div>
      <div className={styles.ledgerExtractionLowRiskList}>
        {candidates.map((candidate) => (
          <LowRiskCandidate candidate={candidate} caseId={batch.matterId} key={candidate.sequence} />
        ))}
      </div>
    </details>
  );
}

function LowRiskCandidate({ candidate, caseId }: { candidate: WebAgentLedgerExtractionCandidate; caseId: string }) {
  return (
    <article>
      <header>
        <div><strong>{kindLabel(candidate.candidateKind)}</strong><span>{candidate.summary}</span></div>
        <small>来源匹配 {Math.round(candidate.confidence * 100)}%</small>
      </header>
      <details>
        <summary>查看 {candidate.excerpts.length} 个精确来源定位</summary>
        <div className={styles.ledgerExtractionExcerpts}>
          {candidate.excerpts.map((excerpt) => (
            <blockquote key={excerpt.evidencePageId}>
              <p>“{excerpt.text}”</p>
              <Link href={`/evidence?case=${encodeURIComponent(caseId)}&focus=${encodeURIComponent(excerpt.evidencePageId)}`}>
                查看证据第 {excerpt.pageNumber} 页
              </Link>
            </blockquote>
          ))}
        </div>
      </details>
    </article>
  );
}

function kindCounts(candidates: readonly WebAgentLedgerExtractionCandidate[]) {
  return {
    fact: candidates.filter((item) => item.candidateKind === "FACT").length,
    transaction: candidates.filter((item) => item.candidateKind === "TRANSACTION").length,
  };
}

function kindLabel(value: "FACT" | "TRANSACTION"): string {
  return value === "FACT" ? "事实" : "收付款";
}

function batchStatusLabel(value: WebAgentLedgerExtractionBatch["status"]): string {
  return ({
    REVIEW_READY: "当前批次 · 低风险组待确认",
    EXCEPTIONS_ONLY: "当前批次 · 异常组待处理",
    EXCEPTIONS_PARTIALLY_RESOLVED: "异常组已部分分流",
    LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN: "低风险已确认 · 异常组待处理",
    LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL: "低风险已确认 · 异常组部分处理",
    CONFIRMED: "本批次已完成",
    RESOLVED: "本批次异常组已全部分流",
    STALE: "历史批次 · 案件输入已变化",
  })[value];
}

function exceptionProgressLabel(batch: WebAgentLedgerExtractionBatch): string {
  if (batch.exceptionReviewStatus === "RESOLVED") return "全部内容的下一步已确定，案件将按当前结果继续整理";
  if (batch.exceptionReviewStatus === "PARTIALLY_RESOLVED") return `${batch.decidedExceptionGroupCount} 组已分流，${batch.exceptionGroupCount - batch.decidedExceptionGroupCount} 组仍待处理`;
  return "异常候选尚未入账；请逐组查看完整来源后选择下一步";
}

function isLowRiskConfirmed(batch: WebAgentLedgerExtractionBatch): boolean {
  return batch.confirmedAt !== null;
}

function formatDateTime(value: string): string {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed)
    ? new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(parsed)
    : "时间待核验";
}

function isUnknownWriteOutcome(reason: unknown): boolean {
  if (!(reason instanceof WebLawyerApiError)) return true;
  return reason.status === null
    || reason.status === 408
    || reason.status >= 500
    || (reason.status >= 200 && reason.status < 300);
}

function isDefinitiveWriteConflict(reason: unknown): boolean {
  return reason instanceof WebLawyerApiError
    && reason.status === 409;
}
