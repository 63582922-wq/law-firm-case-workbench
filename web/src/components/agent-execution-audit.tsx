"use client";

import { useEffect, useState } from "react";
import { loadAgentExecutionAudit, type AgentExecutionAuditView } from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

export function AgentExecutionAudit() {
  const [review, setReview] = useState<AgentExecutionAuditView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    loadAgentExecutionAudit()
      .then((result) => {
        if (active) setReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "Agent 审计快照读取失败");
      });
    return () => {
      active = false;
    };
  }, []);

  const currentRun = review?.runs[0] ?? null;
  const currentProposals = currentRun ? review?.proposals.filter((item) => item.runId === currentRun.runId) ?? [] : [];
  return (
    <section className={styles.agentAudit} aria-label="Agent 执行审计">
      <header className={styles.agentAuditHeading}>
        <div>
          <p className={styles.eyebrow}>案件级执行审计</p>
          <h3>每一步都先留计划，再留工具回执</h3>
          <p>这里不显示提示词和案卷正文，只显示执行所依据的版本、哈希、Skill、范围和结果。</p>
        </div>
        <span>{review?.sourceLabel ?? "正在读取"}</span>
      </header>
      {error ? <p className={styles.agentAuditBlocked} role="alert">{error}</p> : !review ? <div className={styles.calculationLoading}>正在读取 Agent 审计账本…</div> : currentRun ? (
        <>
          <div className={styles.agentAuditSummary}>
            <div><span>当前 Agent</span><strong>{currentRun.agentId}</strong><small>v{currentRun.agentVersion}</small></div>
            <div><span>输入案件版本</span><strong>v{currentRun.inputMatterVersion}</strong><small>{shortHash(currentRun.inputHash)}</small></div>
            <div><span>计划操作</span><strong>{currentProposals.length} 项</strong><small>策略 {shortHash(currentRun.policyManifestHash)}</small></div>
            <div><span>工具回执</span><strong>{review.receipts.length} 项</strong><small>追加记录，不可覆盖</small></div>
          </div>
          <div className={styles.agentAuditList}>
            {currentProposals.map((proposal) => {
              const receipt = review.receipts.find((item) => item.proposalId === proposal.proposalId) ?? null;
              return <article key={proposal.proposalId}>
                <span>{String(proposal.sequence).padStart(2, "0")}</span>
                <div><strong>{proposal.skillId}</strong><small>{proposal.toolId} · {approvalLabel(proposal.approvalGate)}</small></div>
                <code>{proposal.requiredScopes.join(" · ")}</code>
                <em className={receipt?.status === "SUCCEEDED" ? styles.agentReceiptReady : receipt ? styles.agentReceiptBlocked : undefined}>{receipt ? receipt.status : "等待受控执行"}</em>
              </article>;
            })}
          </div>
        </>
      ) : (
        <div className={styles.agentAuditEmpty}>
          <strong>{review.sourceKind === "synthetic-alpha" ? "合成模式没有真实 Agent 执行" : "当前案件尚未产生 Agent 执行计划"}</strong>
          <span>模型计划、工具调用和外部请求都不能绕过本机会话、案件权限、Skill 门禁及律师审批。</span>
        </div>
      )}
    </section>
  );
}

function shortHash(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}

function approvalLabel(value: string) {
  const labels: Record<string, string> = {
    NONE: "无需额外确认",
    MATERIAL_SCOPE: "材料范围已批准",
    LAWYER_REVIEW: "须律师复核",
    RELEASE_LOCK: "须锁定提交版",
  };
  return labels[value] ?? value;
}
