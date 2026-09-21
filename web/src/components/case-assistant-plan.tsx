"use client";

import { useEffect, useMemo, useState } from "react";
import {
  authorizeDeepSeekCasePlan,
  loadAgentExecutionAudit,
  loadCasePlanningPreflight,
  type AgentExecutionAuditView,
  type CaseDataSourceConfig,
  type CasePlanningPreflight,
} from "@/lib/case-data-source";
import {
  executeAuthorizedDeepSeekCasePlan,
  readDesktopModelProviderStatuses,
  type CasePlanTaskKind,
  type DesktopModelProviderStatus,
} from "@/lib/desktop-bridge";
import styles from "./case-workbench.module.css";

type PlanTask = {
  kind: CasePlanTaskKind;
  label: string;
  detail: string;
};

const PLAN_TASKS: readonly PlanTask[] = [
  { kind: "case_intake", label: "整理材料", detail: "安排材料接收、文件梳理和需确认事项。" },
  { kind: "evidence_review", label: "核对还款证据", detail: "安排交易页筛选、重复页判断和还款核对。" },
  { kind: "legal_research", label: "核对法律依据", detail: "安排官方法源核验和适用规则确认。" },
  { kind: "interest_review", label: "核算还款与利息", detail: "安排还款用途、抵扣顺序和测算前核对。" },
  { kind: "document_review", label: "准备应诉材料", detail: "安排答辩材料、证据目录和提交前复核。" },
] as const;

const RETENTION_CONFIRMATION = "主办律师确认：本次依本所与服务商已生效的数据保留政策处理最小案件快照";
const TRAINING_CONFIRMATION = "主办律师确认：本次依本所与服务商已生效的训练使用政策处理最小案件快照";

export function CaseAssistantPlan({ sourceConfig }: { sourceConfig: CaseDataSourceConfig }) {
  const persistent = sourceConfig.kind === "persistent-preview";
  const [preflight, setPreflight] = useState<CasePlanningPreflight | null>(null);
  const [audit, setAudit] = useState<AgentExecutionAuditView | null>(null);
  const [providers, setProviders] = useState<DesktopModelProviderStatus[] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "blocked">(persistent ? "loading" : "ready");
  const [message, setMessage] = useState<string | null>(null);
  const [taskKind, setTaskKind] = useState<CasePlanTaskKind>("case_intake");
  const [consented, setConsented] = useState(false);
  const [costCapYuan, setCostCapYuan] = useState("1.00");
  const [busy, setBusy] = useState(false);
  const [createdRunId, setCreatedRunId] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    if (!persistent) {
      return () => { active = false; };
    }
    void Promise.resolve().then(() => {
      if (!active) return null;
      setState("loading");
      setMessage(null);
      return Promise.all([
        loadCasePlanningPreflight(sourceConfig),
        loadAgentExecutionAudit(sourceConfig),
        readDesktopModelProviderStatuses(),
      ]);
    }).then((result) => {
      if (!active || result === null) return;
      const [nextPreflight, nextAudit, nextProviders] = result;
      setPreflight(nextPreflight);
      setAudit(nextAudit);
      setProviders(nextProviders);
      setState("ready");
    }).catch((reason: unknown) => {
      if (!active) return;
      setState("blocked");
      setMessage(reason instanceof Error ? reason.message : "本案 AI 办案助手暂时不可用。 ");
    });
    return () => { active = false; };
  }, [persistent, sourceConfig]);

  const deepSeekProvider = providers?.find((item) => item.providerId === "deepseek");
  const deepSeekConfigurationState = deepSeekProvider?.configurationState ?? "NOT_CHECKED";
  const selectedTask = PLAN_TASKS.find((item) => item.kind === taskKind) ?? PLAN_TASKS[0];
  const parsedCostCapYuan = Number(costCapYuan);
  const costCapMinor = Math.round(parsedCostCapYuan * 100);
  const createdRun = useMemo(
    () => createdRunId ? audit?.runs.find((item) => item.runId === createdRunId) ?? null : null,
    [audit, createdRunId],
  );
  const displayedRun = createdRun ?? audit?.runs.find((item) => item.agentId === "deepseek-case-planner") ?? null;
  const displayedProposals = displayedRun
    ? audit?.proposals.filter((item) => item.runId === displayedRun.runId).sort((left, right) => left.sequence - right.sequence) ?? []
    : [];

  async function createPlan() {
    if (!persistent || !preflight || !consented) return;
    if (!costCapYuan.trim() || !Number.isFinite(parsedCostCapYuan) || !Number.isInteger(costCapMinor) || costCapMinor < 0 || costCapMinor > 10_000_000) {
      setMessage("请填写 0 至 100,000 元之间的本次费用上限。 ");
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const expiresAt = new Date(Date.now() + 10 * 60 * 1000).toISOString();
      const authorization = await authorizeDeepSeekCasePlan({
        preflight,
        retentionPolicy: RETENTION_CONFIRMATION,
        trainingPolicy: TRAINING_CONFIRMATION,
        costCapMinor,
        expiresAt,
        confirmation: "CONFIRM_MINIMAL_CASE_PLAN",
        config: sourceConfig,
      });
      const result = await executeAuthorizedDeepSeekCasePlan({
        matterId: sourceConfig.matterId,
        externalRequestId: authorization.requestId,
        expectedVersion: authorization.matterVersion,
        taskKind,
      });
      const [nextAudit, nextPreflight] = await Promise.all([
        loadAgentExecutionAudit(sourceConfig),
        loadCasePlanningPreflight(sourceConfig),
      ]);
      setAudit(nextAudit);
      setPreflight(nextPreflight);
      setCreatedRunId(result.runId);
      setConsented(false);
      setMessage(`已生成 ${result.proposalCount} 个受控办案步骤。它们尚未自动执行，请逐项确认后再进入对应工作页。`);
    } catch (reason: unknown) {
      setMessage(reason instanceof Error ? reason.message : "AI 办案计划未生成；系统没有自动重试。 ");
    } finally {
      setBusy(false);
    }
  }

  if (!persistent) {
    return (
      <section className={styles.assistantPlanner} aria-label="AI 办案助手">
        <div className={styles.assistantPlannerHeading}>
          <div><p className={styles.eyebrow}>AI 办案助手</p><h2>先把下一步工作排出来</h2></div>
          <span>演示模式</span>
        </div>
        <p>演示案件不会连接模型或发送任何材料。建立真实案件并选择资料文件夹后，AI 才能根据已登记的办理进度生成受控步骤清单。</p>
      </section>
    );
  }

  return (
    <section className={styles.assistantPlanner} aria-label="AI 办案助手">
      <div className={styles.assistantPlannerHeading}>
        <div>
          <p className={styles.eyebrow}>AI 办案助手</p>
          <h2>让 AI 先排出本案下一步</h2>
          <p>AI 只根据案件当前阶段和已登记项目数量列出可用工作步骤；不读取原始材料、不作法律结论，也不会自动执行。</p>
        </div>
        <span>{state === "loading" ? "正在准备" : displayedRun ? "已有步骤清单" : "等待选择任务"}</span>
      </div>

      {state === "loading" ? <div className={styles.assistantPlannerLoading}>正在读取本案可用的办案动作…</div> : null}
      {state === "blocked" ? <p className={styles.assistantPlannerBlocked} role="alert">{message}</p> : null}

      {state === "ready" && preflight ? (
        <div className={styles.assistantPlannerBody}>
          <div className={styles.assistantTaskChoices} role="radiogroup" aria-label="选择要推进的事项">
            {PLAN_TASKS.map((task) => (
              <label className={task.kind === taskKind ? styles.assistantTaskActive : ""} key={task.kind}>
                <input checked={task.kind === taskKind} name="case-plan-task" onChange={() => setTaskKind(task.kind)} type="radio" value={task.kind} />
                <span><strong>{task.label}</strong><small>{task.detail}</small></span>
              </label>
            ))}
          </div>

          <div className={styles.assistantPlanAction}>
            <div className={styles.assistantScopeNote}>
              <strong>这一次只发送什么</strong>
              <span>案件阶段、已登记事实/交易/诉请/争点的数量和状态；不发送文件、姓名、金额、日期、原文或律师意见。</span>
            </div>
            {providers === null ? (
              <p className={styles.assistantPlannerBlocked}>请在桌面应用中使用 AI 办案助手；浏览器预览不能读取本机模型配置。</p>
            ) : (
              <>
                {deepSeekConfigurationState !== "VALIDATED_FOR_CURRENT_SESSION" ? (
                  <p className={styles.assistantPlannerBlocked}>
                    {deepSeekConfigurationState === "NOT_CONFIGURED"
                      ? <>尚未记录 DeepSeek 配置。你仍可在下方完成本次授权；系统会在发送前验证密钥。若验证失败，<a href="/security">前往工作台设置</a>配置或更换服务密钥。</>
                      : <>此页不会读取系统钥匙串中的密钥。你确认本次授权后，系统会在发送前验证 DeepSeek 密钥；若尚未配置或密钥不可用，<a href="/security">前往工作台设置</a>处理。</>}
                  </p>
                ) : null}
                <label className={styles.assistantCostField}>
                  <span>本次费用授权上限（元）</span>
                  <input inputMode="decimal" min="0" onChange={(event) => setCostCapYuan(event.target.value)} required step="0.01" type="number" value={costCapYuan} />
                </label>
                <label className={styles.assistantConsent}>
                  <input checked={consented} onChange={(event) => setConsented(event.target.checked)} type="checkbox" />
                  <span>我确认本次仅发送上述最小快照，并适用本所与服务商已生效的数据处理和费用政策；AI 只生成步骤清单，不自动执行任何动作。</span>
                </label>
                <button disabled={busy || !consented} onClick={() => void createPlan()} type="button">
                  {busy ? "正在生成步骤清单…" : `为“${selectedTask.label}”生成步骤清单`}
                </button>
              </>
            )}
            {message ? <p className={styles.assistantPlanMessage} role="status">{message}</p> : null}
          </div>
        </div>
      ) : null}

      {displayedRun ? (
        <div className={styles.assistantPlanResult} aria-live="polite">
          <div><p className={styles.eyebrow}>本次步骤清单</p><h3>先确认，再逐项办理</h3><span>AI 没有替你执行任何一步。</span></div>
          {displayedProposals.length ? (
            <ol>
              {displayedProposals.map((proposal) => <li key={proposal.proposalId}><span>{String(proposal.sequence).padStart(2, "0")}</span><div><strong>{friendlyToolName(proposal.skillId, proposal.toolId)}</strong><small>{approvalHint(proposal.approvalGate)}</small></div></li>)}
            </ol>
          ) : <p>步骤清单已保存，正在等待刷新。</p>}
        </div>
      ) : null}
    </section>
  );
}

function friendlyToolName(skillId: string, toolId: string): string {
  const labels: Record<string, string> = {
    "material_inventory:register_source_file": "整理已确认的材料清单",
    "material_inventory:inspect_pdf_structure": "检查 PDF 材料结构",
    "material_inventory:inspect_non_pdf_structure": "检查 Word、Excel 或图片材料",
    "pdf_reading:extract_pdf_text": "提取已确认 PDF 的可核对文字",
    "evidence_pdf_normalization:normalize_image_or_text_pdf": "生成可核对的证据 PDF 副本",
    "evidence_pdf_normalization:render_registered_page": "查看材料中的指定页面",
    "office_reading:parse_office_document": "读取已确认的 Word 或 Excel 材料",
    "document_consistency_review:review_document_consistency": "核对文书内容与案件资料是否一致",
    "legal_rule_research:search_authoritative_rules": "准备官方法律依据核对清单",
    "interest_calculation:plan_private_lending_transition": "梳理利率适用期间与过渡节点",
    "interest_calculation:calculate_interest_schedule": "按已确认资料生成利息测算",
    "document_drafting:create_reviewable_docx_draft": "生成可编辑的文书草稿",
    "document_drafting:create_pdf_derivative": "生成供核对的文书 PDF",
    "spreadsheet_ledger:create_reviewable_xlsx_ledger": "生成可编辑的交易或核算表",
    "submission_bundle_validation:validate_submission_bundle": "核对法院提交包是否完整",
  };
  return labels[`${skillId}:${toolId}`] ?? "安排一项已受控的办案动作";
}

function approvalHint(gate: string): string {
  const labels: Record<string, string> = {
    NONE: "可以在对应工作页查看结果",
    MATERIAL_SCOPE: "先确认材料范围，再在对应工作页办理",
    LAWYER_REVIEW: "须由律师确认后才能继续",
    RELEASE_LOCK: "须在提交前锁定材料后才能继续",
  };
  return labels[gate] ?? "请在对应工作页确认后继续";
}
