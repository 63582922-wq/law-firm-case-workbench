"use client";

import { useEffect, useState } from "react";
import { loadExternalRequestAudit, type ExternalRequestAuditView } from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

export function ExternalRequestAudit() {
  const [review, setReview] = useState<ExternalRequestAuditView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    loadExternalRequestAudit()
      .then((result) => { if (active) setReview(result); })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "外部调用授权账本读取失败");
      });
    return () => { active = false; };
  }, []);

  return (
    <section className={styles.externalRequestAudit} aria-label="外部调用预授权">
      <header className={styles.externalRequestHeading}>
        <div>
          <p className={styles.eyebrow}>外部研究与处理边界</p>
          <h3>先固定可发送范围，再允许受控 Worker 调用</h3>
          <p>授权只记录字段标识、供应商政策和哈希；不会在这里显示案卷正文、提示词、密钥或完整外部请求。</p>
        </div>
        <span>{review?.sourceLabel ?? "正在读取"}</span>
      </header>
      {error ? <p className={styles.externalRequestBlocked} role="alert">{error}</p> : !review ? <div className={styles.calculationLoading}>正在读取外部调用预授权账本…</div> : review.authorizations.length === 0 ? (
        <div className={styles.externalRequestEmpty}>
          <strong>{review.sourceKind === "synthetic-alpha" ? "合成模式不发送任何材料到外部服务" : "当前案件尚无外部调用预授权"}</strong>
          <span>网络检索、模型、OCR 或 MCP 不能因为 Agent 计划存在而自动发起；必须先由主办或复核律师固定范围、供应商、地域、保留政策、次数和成本上限。</span>
        </div>
      ) : (
        <div className={styles.externalRequestList}>
          {review.authorizations.map((authorization) => {
            const latest = review.attempts
              .filter((attempt) => attempt.requestId === authorization.requestId)
              .sort((left, right) => right.sequence - left.sequence)[0] ?? null;
            return (
              <article key={authorization.requestId}>
                <div className={styles.externalRequestTitle}>
                  <span>{kindLabel(authorization.requestKind)}</span>
                  <div>
                    <strong>{authorization.purpose}</strong>
                    <small>{authorization.providerId} · {authorization.serviceId} · {authorization.processorRegion}</small>
                  </div>
                </div>
                <dl>
                  <div><dt>允许字段</dt><dd>{authorization.selectedFieldIds.join("、")}</dd></div>
                  <div><dt>保留 / 训练</dt><dd>{authorization.retentionPolicy} / {authorization.trainingPolicy}</dd></div>
                  <div><dt>次数 / 成本</dt><dd>{authorization.callCap} 次 / {authorization.costCurrency} {authorization.costCapMinor} 最小单位</dd></div>
                  <div><dt>授权至</dt><dd>{formatTime(authorization.expiresAt)}</dd></div>
                </dl>
                <footer>
                  <code>范围 {shortHash(authorization.inputHash)} · 授权 {shortHash(authorization.authorizationHash)}</code>
                  <em className={attemptClass(latest?.status)}>{latest ? attemptLabel(latest.status) : "仅获授权，尚未提交"}</em>
                </footer>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function kindLabel(value: "MODEL" | "OCR" | "MCP") {
  return ({ MODEL: "模型", OCR: "识别", MCP: "受控连接器" })[value];
}

function attemptLabel(value: NonNullable<ExternalRequestAuditView["attempts"][number]["status"]>) {
  return ({
    SUBMISSION_STARTED: "已提交，等待结果核对",
    SUCCEEDED: "已完成并留存回执",
    FAILED: "调用失败，已留存原因",
    UNKNOWN_SUBMISSION: "状态未知，自动重试已阻断",
    CANCELLED: "已取消",
    EXPIRED: "授权已到期",
  })[value];
}

function attemptClass(value: ExternalRequestAuditView["attempts"][number]["status"] | undefined) {
  if (value === "SUCCEEDED") return styles.externalRequestSucceeded;
  if (value === "FAILED" || value === "UNKNOWN_SUBMISSION" || value === "EXPIRED") return styles.externalRequestStopped;
  return undefined;
}

function shortHash(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}
