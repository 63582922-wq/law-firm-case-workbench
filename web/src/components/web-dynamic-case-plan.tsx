"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  activateWebDynamicCasePlan,
  createWebCaseAgentIdempotencyKey,
  createWebDynamicCasePlanIdempotencyKey,
  decideWebDynamicCasePlanItem,
  executeWebActivePlan,
  isWebLoginRequired,
  readCurrentWebCaseAgentRun,
  reconcileWebActivePlanExecution,
  readWebAgentLedgerExtractionBatches,
  readWebDynamicCasePlan,
  WebLawyerApiError,
  type WebCaseAgentRun,
  type WebDynamicCasePlan,
  type WebDynamicCasePlanDecision,
  type WebDynamicCasePlanItem,
} from "@/lib/web-lawyer-api";
import { activePlanExecutionUiState } from "@/lib/web-active-plan-execution";
import styles from "./case-workbench.module.css";

const GROUPS: readonly Readonly<{
  label: string;
  kinds: readonly WebDynamicCasePlanItem["category"][];
  empty: string;
}>[] = [
  { label: "补充材料", kinds: ["MATERIAL_REQUEST"], empty: "当前没有需要补充的材料事项。" },
  { label: "法律研究", kinds: ["RESEARCH_TASK"], empty: "当前没有待处理的法律研究事项。" },
  { label: "程序与期限", kinds: ["PROCEDURAL_TASK", "DEADLINE_RISK"], empty: "当前没有待处理的程序或期限事项。" },
  { label: "计算", kinds: ["CALCULATION"], empty: "当前没有待处理的计算事项。" },
  { label: "文书候选", kinds: ["DOCUMENT_CANDIDATE"], empty: "当前没有可生成的文书候选；系统不会自行假定代理立场。" },
  { label: "复核", kinds: ["REVIEW"], empty: "当前没有需要专项复核的事项。" },
] as const;

type UnknownPlanWrite = Readonly<{
  operation: "ACTIVATE" | "ITEM_REVIEW";
  planId: string;
  itemId: string | null;
  expectedVersion: number;
  idempotencyKey: string;
}>;

type LedgerReviewGateState = Readonly<{
  scope: string;
  status: "LOADING" | "CLEAR" | "PENDING" | "UNAVAILABLE";
  pendingBatchCount: number;
}>;

type UnknownExecution = Readonly<{
  planId: string;
  expectedVersion: number;
  idempotencyKey: string;
}>;

export function WebDynamicCasePlanPanel({
  canReview,
  canReviewAgentLedgerExtractions,
  canReviewCaseAgent,
  canExecuteActivePlan,
  caseId,
  caseVersion,
  hasMaterials,
  onSessionExpired,
  onVersionAdvanced,
}: {
  canReview: boolean;
  canReviewAgentLedgerExtractions: boolean;
  canReviewCaseAgent: boolean;
  canExecuteActivePlan: boolean;
  caseId: string;
  caseVersion: number;
  hasMaterials: boolean;
  onSessionExpired: () => void;
  onVersionAdvanced: (caseId: string, version: number) => void;
}) {
  const [plan, setPlan] = useState<WebDynamicCasePlan | null>(null);
  const [currentRun, setCurrentRun] = useState<WebCaseAgentRun | null>(null);
  const [loading, setLoading] = useState(canReview);
  const [busyItemId, setBusyItemId] = useState<string | null>(null);
  const [activating, setActivating] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [activationReceipt, setActivationReceipt] = useState<string | null>(null);
  const [unknownWrite, setUnknownWrite] = useState<UnknownPlanWrite | null>(null);
  const [unknownExecution, setUnknownExecution] = useState<UnknownExecution | null>(null);
  const ledgerReviewScope = `${caseId}:${caseVersion}`;
  const [ledgerReviewGate, setLedgerReviewGate] = useState<LedgerReviewGateState>({
    scope: "",
    status: "LOADING",
    pendingBatchCount: 0,
  });
  const operationKeys = useRef(new Map<string, string>());
  const [error, setError] = useState<string | null>(null);

  const read = useCallback(async (signal?: AbortSignal) => {
    if (!canReview) {
      setPlan(null);
      setLoading(false);
      setError(null);
      return;
    }
    setLoading(true);
    try {
      const [current, run] = await Promise.all([
        readWebDynamicCasePlan(caseId, signal),
        canReviewCaseAgent
          ? readCurrentWebCaseAgentRun(caseId, signal)
          : Promise.resolve(null),
      ]);
      if (!signal?.aborted) {
        setPlan(current);
        setCurrentRun(run);
        if (current && current.currentMatterVersion !== caseVersion) {
          onVersionAdvanced(caseId, current.currentMatterVersion);
        }
        setError(null);
      }
    } catch (reason: unknown) {
      if (signal?.aborted) return;
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError(reason instanceof Error ? reason.message : "暂不能读取本案的办案清单。");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [canReview, canReviewCaseAgent, caseId, caseVersion, onSessionExpired, onVersionAdvanced]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void read(controller.signal), 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [read]);

  useEffect(() => {
    if (!currentRun) return;
    const executionInProgress = currentRun.activePlanExecution
      && ["CREATED", "PLANNING", "WAITING_APPROVAL", "EXECUTING", "WAITING_INPUT", "VERIFYING"].includes(currentRun.status);
    const ordinaryRunStillProducingPlan = !currentRun.activePlanExecution
      && (
        ["CREATED", "PLANNING", "EXECUTING", "VERIFYING", "STALE"].includes(currentRun.status)
        || (
          ["READY_FOR_REVIEW", "COMPLETED"].includes(currentRun.status)
          && (!plan || plan.status === "STALE")
        )
      );
    if (!executionInProgress && !ordinaryRunStillProducingPlan) return;
    const timer = window.setInterval(() => void read(), 3000);
    return () => window.clearInterval(timer);
  }, [currentRun, plan, read]);

  useEffect(() => {
    if (!canReviewAgentLedgerExtractions) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setLedgerReviewGate({ scope: ledgerReviewScope, status: "LOADING", pendingBatchCount: 0 });
      void readWebAgentLedgerExtractionBatches(caseId, controller.signal)
        .then((batches) => {
          if (controller.signal.aborted) return;
          const pending = batches.filter((batch) =>
            batch.status === "REVIEW_READY"
            || batch.status === "EXCEPTIONS_ONLY"
            || batch.status === "EXCEPTIONS_PARTIALLY_RESOLVED"
            || batch.status === "LOW_RISK_CONFIRMED_EXCEPTIONS_OPEN"
            || batch.status === "LOW_RISK_CONFIRMED_EXCEPTIONS_PARTIAL");
          setLedgerReviewGate({
            scope: ledgerReviewScope,
            status: pending.length > 0 ? "PENDING" : "CLEAR",
            pendingBatchCount: pending.length,
          });
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          if (isWebLoginRequired(reason)) {
            onSessionExpired();
            return;
          }
          setLedgerReviewGate({ scope: ledgerReviewScope, status: "UNAVAILABLE", pendingBatchCount: 0 });
        });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [canReviewAgentLedgerExtractions, caseId, ledgerReviewScope, onSessionExpired]);

  const groups = useMemo(() => GROUPS.map((group) => ({
    ...group,
    items: plan?.items.filter((item) => group.kinds.includes(item.category)) ?? [],
  })), [plan]);

  async function decide(item: WebDynamicCasePlanItem, decision: WebDynamicCasePlanDecision) {
    if (!plan || plan.status === "STALE" || !plan.inputsCurrent || item.status !== "CANDIDATE" || busyItemId || unknownWrite) return;
    const operationScope = `item:${caseId}:${plan.planId}:${item.itemId}:${plan.currentMatterVersion}:${JSON.stringify(decision)}`;
    const idempotencyKey = stableOperationKey(operationScope);
    setBusyItemId(item.itemId);
    setError(null);
    try {
      const receipt = await decideWebDynamicCasePlanItem(caseId, plan.planId, item.itemId, plan.currentMatterVersion, decision, idempotencyKey);
      onVersionAdvanced(caseId, receipt.matterVersion);
      setActivationReceipt(receipt.requiresReplanning ? "已记录调整意见；当前候选已停止使用，系统会按更新后的案件信息重新研判。" : "这项建议已记录为律师复核通过。");
      await read();
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (isUnknownWriteOutcome(reason)) {
        setUnknownWrite({ operation: "ITEM_REVIEW", planId: plan.planId, itemId: item.itemId, expectedVersion: plan.currentMatterVersion, idempotencyKey });
        setError("本次事项复核的服务端结果未知。请勿重复点击，先核验当前计划状态。");
      } else {
        setError(reason instanceof Error ? reason.message : "这项建议没有完成复核，请刷新案件后核对状态。");
      }
    } finally {
      setBusyItemId(null);
    }
  }

  async function activatePlan() {
    if (
      !plan?.canActivate
      || effectiveLedgerReviewGate === "LOADING"
      || effectiveLedgerReviewGate === "PENDING"
      || effectiveLedgerReviewGate === "UNAVAILABLE"
      || activating
      || busyItemId
      || unknownWrite
    ) return;
    const operationScope = `activate:${caseId}:${plan.planId}:${plan.currentMatterVersion}`;
    const idempotencyKey = stableOperationKey(operationScope);
    setActivating(true);
    setError(null);
    setActivationReceipt(null);
    try {
      const receipt = await activateWebDynamicCasePlan(caseId, plan.currentMatterVersion, idempotencyKey);
      if (receipt.planId !== plan.planId) throw new Error("服务端激活的计划与当前显示计划不一致，请刷新案件。");
      onVersionAdvanced(caseId, receipt.matterVersion);
      setActivationReceipt(`本案办案清单已由主办律师确认（案件版本 ${receipt.matterVersion}）。后续处理仍受每项审批和当前案件来源约束。`);
      await read();
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (isUnknownWriteOutcome(reason)) {
        setUnknownWrite({ operation: "ACTIVATE", planId: plan.planId, itemId: null, expectedVersion: plan.currentMatterVersion, idempotencyKey });
        setError("本次整案确认的服务端结果未知。请勿再次确认，先核验当前计划状态。");
      } else {
        setError(reason instanceof Error ? reason.message : "整案计划没有完成激活，请刷新案件后核对状态。");
      }
    } finally {
      setActivating(false);
    }
  }

  async function executeActivePlan() {
    if (
      !plan
      || activePlanExecutionUiState({
        planStatus: plan.status,
        canExecuteActivePlan,
        run: currentRun,
      }) !== "OFFER_EXECUTION"
      || executing
      || unknownExecution
    ) return;
    const expectedVersion = plan.currentMatterVersion;
    const operationScope = `execute-active-plan:${caseId}:${plan.planId}:${expectedVersion}`;
    const idempotencyKey = stableOperationKey(operationScope, "CASE_AGENT");
    setExecuting(true);
    setError(null);
    setActivationReceipt(null);
    try {
      const run = await executeWebActivePlan(caseId, expectedVersion, idempotencyKey);
      setCurrentRun(run);
      setActivationReceipt("已开始按当前已确认的办案清单处理。本轮只会形成供审阅的案件意见和收付款核对表，不会自动对外提交。");
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      if (isUnknownWriteOutcome(reason)) {
        setUnknownExecution({ planId: plan.planId, expectedVersion, idempotencyKey });
        setError("本次开始处理的结果暂未确认。系统已禁止重复开始，请先刷新核对当前进展。");
      } else {
        setError(reason instanceof Error ? reason.message : "已激活计划未开始执行；请按页面提示处理，系统不会自动重试。");
      }
    } finally {
      setExecuting(false);
    }
  }

  async function verifyUnknownExecution() {
    if (!unknownExecution || !canReviewCaseAgent) return;
    setLoading(true);
    setError(null);
    try {
      const run = await reconcileWebActivePlanExecution(
        caseId,
        unknownExecution.planId,
        unknownExecution.expectedVersion,
        unknownExecution.idempotencyKey,
      );
      if (run) {
        setCurrentRun(run);
        setActivationReceipt("已核验：服务端已按原请求编号、原计划和原案件版本建立执行任务，无需再次提交。");
        setUnknownExecution(null);
        return;
      }
      setError("仍无法证明本次执行是否已提交。为避免重复花费或生成重复成果，当前不允许重试，需由管理员核验。");
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError("仍无法核验计划执行状态。请勿重复执行，需由管理员处理。");
    } finally {
      setLoading(false);
    }
  }

  function stableOperationKey(scope: string, kind: "DYNAMIC_PLAN" | "CASE_AGENT" = "DYNAMIC_PLAN"): string {
    const existing = operationKeys.current.get(scope);
    if (existing) return existing;
    const created = kind === "CASE_AGENT"
      ? createWebCaseAgentIdempotencyKey()
      : createWebDynamicCasePlanIdempotencyKey();
    operationKeys.current.set(scope, created);
    return created;
  }

  const effectiveLedgerReviewGate = !canReviewAgentLedgerExtractions
    ? "UNAVAILABLE"
    : ledgerReviewGate.scope === ledgerReviewScope
      ? ledgerReviewGate.status
      : "LOADING";
  const ledgerActivationBlocker = effectiveLedgerReviewGate === "LOADING"
    ? "正在核验是否还有待确认的材料整理批次"
    : effectiveLedgerReviewGate === "PENDING"
      ? `本案还有 ${ledgerReviewGate.pendingBatchCount} 个材料整理批次待复核；请先到“案件要点”处理，随后系统会基于更新后的台账重新安排工作`
      : effectiveLedgerReviewGate === "UNAVAILABLE"
        ? "暂不能核验材料整理批次状态；为避免使用旧案情，当前不能确认办案清单"
        : null;
  const executionState = unknownExecution
    ? "RECONCILIATION_REQUIRED"
    : activePlanExecutionUiState({
        planStatus: plan?.status ?? null,
        canExecuteActivePlan,
        run: currentRun,
      });

  async function verifyUnknownWrite() {
    if (!unknownWrite) return;
    setLoading(true);
    setError(null);
    try {
      const current = await readWebDynamicCasePlan(caseId);
      setPlan(current);
      if (current && current.currentMatterVersion !== caseVersion) {
        onVersionAdvanced(caseId, current.currentMatterVersion);
      }
      if (!current || current.planId !== unknownWrite.planId) {
        setActivationReceipt("计划状态已变化，原请求不会自动重试；请按当前页面重新判断。");
        setUnknownWrite(null);
        return;
      }
      if (unknownWrite.operation === "ACTIVATE") {
        if (current.status === "ACTIVE") {
          setActivationReceipt("已核验：整案计划已经激活，无需再次提交。");
          setUnknownWrite(null);
          return;
        }
      } else {
        const reviewed = current.items.find((item) => item.itemId === unknownWrite.itemId);
        if (reviewed && reviewed.status !== "CANDIDATE") {
          setActivationReceipt(reviewed.status === "APPROVED" ? "已核验：本项复核已经记录。" : "已核验：调整意见已经记录，当前候选等待重新研判。");
          setUnknownWrite(null);
          return;
        }
      }
      if (current.currentMatterVersion !== unknownWrite.expectedVersion || current.status !== "CANDIDATE") {
        setActivationReceipt("计划或案件版本已经变化，原请求不会自动重试。");
      } else {
        setActivationReceipt("已核验：服务端仍显示原候选未变。若再次提交，系统会沿用原请求编号核验同一意图。");
      }
      setUnknownWrite(null);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) return onSessionExpired();
      setError("仍无法核验服务端状态。请保持当前页面，不要重复提交。");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className={styles.dynamicPlanPanel} aria-labelledby="dynamic-case-plan-title">
      <header>
        <div>
          <p className={styles.eyebrow}>动态办案研判</p>
          <h3 id="dynamic-case-plan-title">让 Agent 判断本案下一步，而不是套固定流程</h3>
          <p>建议必须同时受本案事实、证据、程序事件和已核验法源约束。代理角色只是研判输入之一，不会自动生成固定材料清单或预置答辩状。</p>
        </div>
        <div className={styles.webAgentActions}><button className={styles.webLawyerSecondaryAction} disabled={loading} onClick={() => void read()} type="button">刷新研判</button><span className={styles.webAgentMode}>{plan?.status === "ACTIVE" ? "当前计划" : plan?.status === "STALE" ? "需重新研判" : "律师确认后生效"}</span></div>
      </header>

      {!hasMaterials ? <p className={styles.webAgentEmpty}>接收案件材料后，Agent 才能结合本案输入形成动态建议。</p> : null}
      {hasMaterials && !canReview ? <div className={styles.webAgentStart}><div><strong>动态办案计划服务尚未接通</strong><span>当前不会按原告或被告显示预制清单，也不会把材料接收状态冒充 Agent 研判。</span></div><span className={styles.webAgentMode}>当前不伪造建议</span></div> : null}
      {hasMaterials && canReview && loading ? <p className={styles.webAgentEmpty}>正在核对本案当前的 Agent 研判…</p> : null}
      {error ? <p className={styles.webAgentError} role="alert">{error}</p> : null}
      {unknownWrite ? <div className={styles.dynamicPlanUnknown}><span>写操作结果尚未确认，系统不会自动重试。</span><button onClick={() => void verifyUnknownWrite()} type="button">先核验当前计划状态</button></div> : null}
      {unknownExecution ? <div className={styles.dynamicPlanUnknown}><span>执行请求结果未知，不得重复启动 Agent 或重复产生模型费用。</span><button disabled={!canReviewCaseAgent || loading} onClick={() => void verifyUnknownExecution()} type="button">核验当前 Agent 任务</button></div> : null}
      {hasMaterials && canReview && !loading && !plan ? <div className={styles.webAgentStart}><div><strong>尚未形成可复核的本案计划</strong><span>先确认代理身份、程序阶段、诉讼目标和诉请范围，并完成材料分析与官方法源核验；缺少这些输入时 Agent 不应猜测交付物。</span></div><span className={styles.webAgentMode}>等待本案输入</span></div> : null}

      {plan?.status === "STALE" ? (
        <div className={styles.dynamicPlanStale} role="status">
          <strong>案件输入已经变化，旧建议不能继续作为当前办案依据</strong>
          <span>{plan.staleReasons.join("；")}。需要 Agent 按新材料、事实、程序或法源重新研判；旧建议仅保留审计记录。</span>
        </div>
      ) : null}

      {plan ? (
        <>
          <div className={styles.dynamicPlanSummary}>
            <div><span>本案动态建议</span><strong>{plan.items.length} 项</strong></div>
            <div><span>已逐项复核</span><strong>{plan.reviewedItemCount} 项</strong></div>
            <div><span>需补研究/信息</span><strong>{plan.items.filter((item) => item.readiness !== "ACTIONABLE").length} 项</strong></div>
            <div><span>本案输入</span><strong>{plan.inputsCurrent ? "当前有效" : "已变化"}</strong></div>
          </div>
          {plan.status === "CANDIDATE" ? (
            <div className={styles.dynamicPlanActivation}>
              <div>
                <strong>主办律师整案确认</strong>
                <span>可以直接确认服务端当前完整候选；逐项复核用于处理例外，不要求把每一项手工点一遍。提出调整或驳回后，本候选会停止激活并等待 Agent 重新研判。</span>
                {!plan.canActivate ? <small>{plan.activationBlockers.join("；")}</small> : null}
                {ledgerActivationBlocker ? <small>{ledgerActivationBlocker}</small> : null}
              </div>
              <button disabled={!plan.canActivate || Boolean(ledgerActivationBlocker) || activating || Boolean(busyItemId) || Boolean(unknownWrite)} onClick={() => void activatePlan()} type="button">
                {activating ? "正在核对并激活…" : "确认整案计划"}
              </button>
            </div>
          ) : null}
          {plan.status === "ACTIVE" ? (
            <div className={styles.dynamicPlanActivation}>
              <div>
                <strong>{executionState === "OFFER_EXECUTION" ? "已激活计划可以开始执行" : "已激活计划执行状态"}</strong>
                <span>{activePlanExecutionMessage(executionState, currentRun)}</span>
              </div>
              {executionState === "OFFER_EXECUTION" ? <button disabled={executing || Boolean(unknownExecution)} onClick={() => void executeActivePlan()} type="button">{executing ? "正在建立执行任务…" : "执行已激活计划"}</button> : null}
              {executionState === "READY_FOR_FINAL_REVIEW" ? <a className={styles.webLawyerSecondaryAction} href={`/?case=${encodeURIComponent(caseId)}`}>返回办案首页复核成果</a> : null}
            </div>
          ) : null}
          {activationReceipt ? <p className={styles.dynamicPlanReceipt} role="status">{activationReceipt}</p> : null}
          <div className={styles.dynamicPlanGroups}>
            {groups.map((group) => (
              <section className={styles.dynamicPlanGroup} key={group.label}>
                <header><strong>{group.label}</strong><span>{group.items.length} 项</span></header>
                {group.items.length === 0 ? <p>{group.empty}</p> : group.items.map((item) => (
                  <DynamicPlanItem
                    busy={busyItemId === item.itemId}
                    disabled={Boolean(busyItemId) || Boolean(unknownWrite) || plan.status === "STALE" || !plan.inputsCurrent}
                    item={item}
                    key={item.itemId}
                    onDecide={(decision) => void decide(item, decision)}
                  />
                ))}
              </section>
            ))}
          </div>
        </>
      ) : null}
    </section>
  );
}

function DynamicPlanItem({ item, busy, disabled, onDecide }: { item: WebDynamicCasePlanItem; busy: boolean; disabled: boolean; onDecide: (decision: WebDynamicCasePlanDecision) => void }) {
  const [editing, setEditing] = useState(false);
  const [readiness, setReadiness] = useState<WebDynamicCasePlanItem["readiness"]>(item.readiness);
  const [required, setRequired] = useState(item.requiredForDelivery);
  const candidate = item.status === "CANDIDATE";
  return (
    <article className={styles.dynamicPlanItem}>
      <div className={styles.dynamicPlanItemHeading}>
        <div><span className={statusClass(item.status)}>{statusLabel(item.status)}</span><strong>{item.title}</strong></div>
        <small>{Math.round(item.confidence * 100)}% · {readinessLabel(item.readiness)}</small>
      </div>
      <p>{item.rationale}</p>
      <div className={styles.dynamicPlanPurpose}><strong>这项工作的目的</strong><span>{item.purpose}</span></div>
      <div className={styles.dynamicPlanRisk}><strong>不处理的风险</strong><span>{item.riskIfOmitted}</span></div>
      <dl className={styles.dynamicPlanCounts}>
        <div><dt>前置条件</dt><dd>{item.prerequisiteCount}</dd></div>
        <div><dt>事实</dt><dd>{item.sourceCounts.fact}</dd></div>
        <div><dt>证据</dt><dd>{item.sourceCounts.evidence}</dd></div>
        <div><dt>程序</dt><dd>{item.sourceCounts.procedure}</dd></div>
        <div><dt>官方法源</dt><dd>{item.sourceCounts.officialAuthority}</dd></div>
      </dl>
      <div className={styles.dynamicPlanMeta}><span>复核门槛：{reviewGateLabel(item.reviewGate)}</span>{item.deliverableKind ? <span>交付候选：{item.deliverableKind}</span> : null}</div>
      <details className={styles.dynamicPlanSources}><summary>展开查看 {item.sources.length} 条来源</summary>{item.sources.length === 0 ? <p>当前项没有可展示来源，因此不能据此形成正式结论。</p> : <ul>{item.sources.map((source) => <li key={`${source.sourceKind}:${source.sourceId}`}><strong>{source.label}</strong><span>{source.locator ?? source.sourceKind}</span></li>)}</ul>}</details>

      {candidate && !disabled ? <div className={styles.dynamicPlanActions}><button disabled={busy} onClick={() => onDecide({ decision: "APPROVE", reasonCode: "VERIFIED_BY_COUNSEL" })} type="button">本项复核通过</button><button disabled={busy} onClick={() => setEditing((value) => !value)} type="button">提出调整并重新研判</button><button disabled={busy} onClick={() => onDecide({ decision: "REJECT", reasonCode: "NOT_APPLICABLE" })} type="button">驳回并重新研判</button></div> : null}
      {candidate && editing && !disabled ? <div className={styles.dynamicPlanEdit}><label><span>调整为</span><select onChange={(event) => setReadiness(event.target.value as WebDynamicCasePlanItem["readiness"])} value={readiness}><option value="ACTIONABLE">可以执行</option><option value="NEEDS_RESEARCH">需要法律研究</option><option value="NEEDS_INFORMATION">需要补充信息</option></select></label>{item.category === "DOCUMENT_CANDIDATE" ? <label><input checked={required} onChange={(event) => setRequired(event.target.checked)} type="checkbox" />重新判断是否属于法院交付必要候选</label> : null}<button disabled={busy || (readiness === item.readiness && required === item.requiredForDelivery)} onClick={() => onDecide({ decision: "MODIFY", reasonCode: readiness === "NEEDS_RESEARCH" ? "REQUIRES_FURTHER_RESEARCH" : "VERIFIED_BY_COUNSEL", ...(readiness !== item.readiness ? { readinessOverride: readiness } : {}), ...(required !== item.requiredForDelivery ? { requiredForDeliveryOverride: required } : {}) })} type="button">保存调整</button></div> : null}
    </article>
  );
}

function activePlanExecutionMessage(
  state: ReturnType<typeof activePlanExecutionUiState>,
  run: WebCaseAgentRun | null,
): string {
  if (state === "INPUTS_CHANGED") {
    return "案件输入已变化，历史任务和文件仍保留，但不能继续生成或终审为当前成果。请回办案首页，基于新案情重新研判并确认新计划。";
  }
  if (state === "OFFER_EXECUTION") {
    return "首轮 Agent 已进入律师复核阶段。点击后才会建立唯一的二次任务，按当前计划生成案件审阅意见 Word/PDF 和收付款核对表 Excel/PDF。";
  }
  if (state === "RUNTIME_UNAVAILABLE") {
    return "办案计划已确认，但生成候选文书的前置条件尚未齐备；当前不会启动可能失败的生成。";
  }
  if (state === "WAITING_SOURCE_REVIEW") {
    return "计划已激活，但首轮 Agent 尚未到达人工复核或完成状态；需先完成当前任务，不会自动执行。";
  }
  if (state === "EXECUTING") {
    return `唯一的计划执行任务已建立，当前状态：${run?.phaseLabel ?? "正在执行"}。页面会读取实际进度，不会再次启动。`;
  }
  if (state === "READY_FOR_FINAL_REVIEW") {
    return "案件审阅意见和收付款核对表已通过服务器核验。请回到办案首页，逐项查看来源并确认可下载文件后完成律师终审。";
  }
  if (state === "COMPLETED") {
    return "本次成果已由律师完成终审。已核验的 Word、Excel 和 PDF 文件仍可在办案首页查看与下载。";
  }
  if (state === "FAILED") {
    return "已知计划执行任务失败，需由管理员核验并处理；当前计划不能再次执行。";
  }
  if (state === "TERMINATED") {
    return `本计划唯一的执行任务已处于“${run?.phaseLabel ?? "已终止"}”状态，不能再次执行。请先完成案件输入更新与重新研判，或由管理员核验。`;
  }
  if (state === "RECONCILIATION_REQUIRED") {
    return "执行结果未知或正在等待管理员对账。系统不会自动恢复、自动重试或重复生成成果。";
  }
  return "当前没有可执行的已激活计划。";
}

function statusLabel(status: WebDynamicCasePlanItem["status"]): string {
  return ({ CANDIDATE: "待确认", APPROVED: "已复核", CHANGE_REQUESTED: "已提调整 · 待重研判", REJECTED: "已驳回 · 待重研判", SUPERSEDED: "已失效" })[status];
}

function statusClass(status: WebDynamicCasePlanItem["status"]): string {
  return `${styles.dynamicPlanStatus} ${status === "CANDIDATE" ? styles.dynamicPlanStatusCandidate : status === "APPROVED" ? styles.dynamicPlanStatusAccepted : styles.dynamicPlanStatusMuted}`;
}

function readinessLabel(value: WebDynamicCasePlanItem["readiness"]): string {
  return ({ ACTIONABLE: "可执行", NEEDS_RESEARCH: "需法律研究", NEEDS_INFORMATION: "需补充信息" })[value];
}

function reviewGateLabel(value: WebDynamicCasePlanItem["reviewGate"]): string {
  return ({ LEAD_LAWYER_CONFIRMATION: "主办律师确认", EVIDENCE_REVIEW: "证据核验", LEGAL_AUTHORITY_REVIEW: "法源核验", PROCEDURE_REVIEW: "程序核验", CALCULATION_REVIEW: "计算复核" })[value];
}

function isUnknownWriteOutcome(reason: unknown): boolean {
  if (!(reason instanceof WebLawyerApiError)) return true;
  return reason.status === null || reason.status === 408 || reason.status >= 500 || (reason.status >= 200 && reason.status < 300);
}
