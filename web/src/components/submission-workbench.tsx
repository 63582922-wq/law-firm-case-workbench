"use client";

import { useEffect, useState } from "react";
import {
  caseDataSourceConfig,
  fetchSubmissionExport,
  loadDocumentConsistencyReview,
  loadSubmissionReview,
  type DocumentConsistencyReviewView,
  type SubmissionReviewView,
} from "@/lib/case-data-source";
import { ReviewableOfficeDrafts } from "@/components/reviewable-office-drafts";
import styles from "./case-workbench.module.css";

const requiredFlow = [
  ["答辩文书", "DEFENCE_STATEMENT", "正文须与已确认的定稿一致"],
  ["证据目录", "EVIDENCE_INDEX", "仅列入本次实际提交的证据"],
  ["证据材料", "EVIDENCE_MATERIAL", "只含已核验相关页；红框版本单独命名"],
  ["利息测算表", "INTEREST_CALCULATION", "人民币 / CNY，逐期本金与冲抵可复算"],
] as const;

export function SubmissionWorkbench() {
  const [review, setReview] = useState<SubmissionReviewView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [consistency, setConsistency] = useState<DocumentConsistencyReviewView | null>(null);
  const [consistencyError, setConsistencyError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    loadSubmissionReview()
      .then((result) => {
        if (active) setReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "暂时无法读取提交材料清单");
      });
    loadDocumentConsistencyReview()
      .then((result) => {
        if (active) setConsistency(result);
      })
      .catch((reason: unknown) => {
        if (active) setConsistencyError(reason instanceof Error ? reason.message : "暂时无法读取文书核对记录");
      });
    return () => {
      active = false;
    };
  }, []);

  if (error) {
    return (
      <section className={styles.submissionArea} aria-label="提交材料">
        <div className={styles.calculationBlocked} role="alert">
          <p className={styles.eyebrow}>暂不能整理提交材料</p>
          <h3>{caseDataSourceConfig.kind === "persistent-disabled" ? "案件资料库尚未启用" : "暂时无法读取提交材料"}</h3>
          <p>请检查桌面工作台是否仍在运行、当前案件是否已打开，然后重新打开本页。</p>
          <small>材料未就绪时，系统不会生成空文件包或以演示文件代替。</small>
          <button className={styles.candidateAction} onClick={() => window.location.reload()} type="button">重新载入本页</button>
        </div>
      </section>
    );
  }
  if (!review) {
    return <section className={styles.submissionArea}><div className={styles.calculationLoading}>正在核对本次提交文件及其依据…</div></section>;
  }

  const approvedCourtProducts = review.workProducts.filter(
    (item) => item.status === "APPROVED" && item.audience === "COURT_SUBMISSION",
  );
  const supersededCourtProducts = review.workProducts.filter(
    (item) => item.status === "STALE" && item.audience === "COURT_SUBMISSION",
  );
  const currentBundle = review.currentBundleId
    ? review.bundles.find((item) => item.bundleId === review.currentBundleId) ?? null
    : null;

  async function downloadCourtZip() {
    setDownloading(true);
    setDownloadError(null);
    try {
      const delivery = await fetchSubmissionExport(review!);
      const url = URL.createObjectURL(delivery.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = delivery.fileName;
      link.rel = "noopener";
      link.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setDownloadError(reason instanceof Error ? reason.message : "法院提交包下载失败");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <section className={styles.submissionArea} aria-label="提交材料">
      <header className={styles.calculationHeading}>
        <div>
          <p className={styles.eyebrow}>提交法院</p>
          <h2>整理一套可直接提交法院的材料</h2>
          <p>本页只保留本次需要提交的文件；每份材料、排列顺序和正式名称均由律师确认，生成前逐份核对。</p>
        </div>
        <div className={styles.calculationStatus}>
          <span>{review.sourceLabel}</span>
          <strong>{statusLabel(review.status)}</strong>
          <small>{review.snapshotHash ? `核对编号 ${shortHash(review.snapshotHash)}` : "尚未形成可提交清单"}</small>
        </div>
      </header>

      <div className={review.status === "blocked" ? styles.submissionBlockedNotice : styles.submissionReviewNotice}>
        <strong>{review.status === "blocked" ? "暂不能生成提交材料" : `案件版本 ${review.matterVersion}`}</strong>
        <span>{review.statusReason}</span>
      </div>

      <div className={styles.submissionSummary}>
        <Summary label="已确认法院材料" value={`${approvedCourtProducts.length} 份`} note="内部工作底稿不会混入" />
        <Summary label="当前提交版" value={currentBundle ? "1 份" : "无"} note="每案仅保留一个" />
        <Summary label="不可提交旧版" value={`${supersededCourtProducts.length} 份`} note="不会再进入材料包" />
        <Summary label="币种" value={currentBundle?.currency ?? "CNY"} note="金额文件必须明确" />
        <Summary label="材料包" value={review.currentExport ? "已生成" : "未生成"} note={review.currentExport ? `${review.currentExport.componentCount} 个文件` : "暂不能上传法院"} />
      </div>

      <div className={styles.submissionGrid}>
        <section className={styles.submissionPanel} aria-labelledby="submission-files-title">
          <div className={styles.submissionPanelHeading}>
            <div><p className={styles.eyebrow}>本次材料</p><h3 id="submission-files-title">提交法院的文件</h3></div>
            <span>{review.currentComponents.length ? "来自当前提交版" : "等待核对提交清单"}</span>
          </div>
          {review.currentComponents.length ? (
            <div className={styles.submissionFileList}>
              {review.currentComponents.map((item) => (
                <article key={item.workProductId}>
                  <span>{String(item.sequence).padStart(2, "0")}</span>
                  <div><strong>{item.courtFilename}</strong><small>{documentKindLabel(item.documentKind)} · {formatBytes(item.byteSize)} · PDF</small></div>
                  <code>{shortHash(item.artifactSha256)}</code>
                </article>
              ))}
            </div>
          ) : (
            <div className={styles.submissionChecklist}>
              {requiredFlow.map(([label, kind, note], index) => {
                const matches = approvedCourtProducts.filter((item) => item.documentKind === kind);
                return (
                  <article key={kind}>
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <div><strong>{label}</strong><small>{note}</small></div>
                    <em>{matches.length ? `已批准 ${matches.length} 份` : "未进入当前清单"}</em>
                  </article>
                );
              })}
            </div>
          )}
        </section>

        <aside className={styles.submissionBoundary}>
          <p className={styles.eyebrow}>材料边界</p>
          <h3>法院材料与内部工作底稿分开</h3>
          <dl>
            <div><dt>法院材料包</dt><dd>只含已确认的 PDF</dd></div>
            <div><dt>内部清单</dt><dd>材料来源、确认记录和核对依据</dd></div>
            <div><dt>原始案卷</dt><dd>永不改写、永不混入</dd></div>
            <div><dt>文件名</dt><dd>不使用“最新 / 最终 / V2”</dd></div>
          </dl>
          <p>委托手续、律所函和身份材料是否必需，应由具体法院要求与本案代理关系决定，并纳入同一提交前核对清单。</p>
        </aside>
      </div>

      {supersededCourtProducts.length > 0 && (
        <section className={styles.submissionPanel} aria-labelledby="superseded-document-title">
          <div className={styles.submissionPanelHeading}>
            <div><p className={styles.eyebrow}>旧版材料</p><h3 id="superseded-document-title">不可再提交的法院材料</h3></div>
            <span>仅供追溯，不可重新选入</span>
          </div>
          <div className={styles.submissionChecklist}>
            {supersededCourtProducts.map((item, index) => (
              <article key={item.workProductId}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>{documentKindLabel(item.documentKind)}</strong>
                  <small>{item.staleReason ?? "该材料已失效，不能进入本次提交材料包。"}</small>
                </div>
                <em>{shortHash(item.artifactSha256)}</em>
              </article>
            ))}
          </div>
        </section>
      )}

      <ReviewableOfficeDrafts />

      <DocumentConsistencyPanel review={consistency} error={consistencyError} />

      <section className={styles.submissionPanel} aria-labelledby="submission-lineage-title">
        <div className={styles.submissionPanelHeading}>
          <div><p className={styles.eyebrow}>提交前核对</p><h3 id="submission-lineage-title">本次材料必须对应的四项依据</h3></div>
          <span>{currentBundle ? `核对编号 ${shortHash(currentBundle.inputHash)}` : "尚未确认"}</span>
        </div>
        <div className={styles.submissionDependencyGrid}>
          <Dependency label="证据材料范围" hash={currentBundle?.evidenceManifestHash ?? null} />
          <Dependency label="利息适用口径" hash={currentBundle?.legalBundleHash ?? null} />
          <Dependency label="利息测算结果" hash={currentBundle?.calculationOutputHash ?? null} />
          <Dependency label="文书定稿确认" hash={currentBundle?.finalTextHash ?? null} />
        </div>
      </section>

      <section className={styles.submissionExportState} aria-label="导出核验状态">
        <div>
          <p className={styles.eyebrow}>材料包状态</p>
          <h3>{review.currentExport ? "提交材料包已逐份核对" : "尚无可交付的提交材料包"}</h3>
          <p>{review.currentExport ? `提交材料包 ${formatBytes(review.currentExport.courtZipBytes)}；内部核对清单已单独保存。` : "只有当前有效提交版才能生成材料包；页面不会用临时文件或演示数据代替。"}</p>
        </div>
        {review.currentExport ? (
          <div className={styles.submissionDownloadBlock}>
            <dl>
              <div><dt>材料包核对编号</dt><dd>{shortHash(review.currentExport.courtZipSha256)}</dd></div>
              <div><dt>内部清单编号</dt><dd>{shortHash(review.currentExport.internalManifestSha256)}</dd></div>
              <div><dt>核对回执</dt><dd>{shortHash(review.currentExport.verificationHash)}</dd></div>
            </dl>
            <button disabled={downloading} onClick={downloadCourtZip} type="button">
              {downloading ? "正在核对并下载…" : "下载法院提交材料.zip"}
            </button>
            {downloadError && <small role="alert">{downloadError}</small>}
          </div>
        ) : <span className={styles.submissionGate}>等待：确认文件 → 提交前核对 → 确认唯一提交版 → 生成材料包</span>}
      </section>
    </section>
  );
}

function DocumentConsistencyPanel({
  review,
  error,
}: {
  review: DocumentConsistencyReviewView | null;
  error: string | null;
}) {
  const latestFindings = review?.latest
    ? review.findings.filter((item) => item.reviewId === review.latest?.reviewId)
    : [];
  const state = review?.latest?.status === "PASS" ? "已通过" : review?.latest?.status === "BLOCKED" ? "存在阻断项" : "尚未形成审查";
  return (
    <section className={styles.submissionPanel} aria-labelledby="document-consistency-title">
      <div className={styles.submissionPanelHeading}>
        <div><p className={styles.eyebrow}>文书核对</p><h3 id="document-consistency-title">提交前逐项核对</h3></div>
        <span>{state}</span>
      </div>
      {error ? (
        <div className={styles.submissionBlockedNotice} role="alert">
          <strong>暂时无法读取文书核对记录</strong><span>请稍后重新打开本页；已生成的材料不会被更改。</span>
        </div>
      ) : !review?.latest ? (
        <div className={styles.submissionReviewNotice}>
          <strong>尚未完成本次核对</strong>
          <span>提交前核对需要覆盖本次全部已确认 PDF，且不得存在阻断问题；系统不会把旧核对结果当作本次有效结果。</span>
        </div>
      ) : (
        <div className={styles.submissionChecklist}>
          <article><span>01</span><div><strong>核对结果：{state}</strong><small>阻断 {review.latest.blockingCount} 项，提示 {review.latest.warningCount} 项；对应案件版本 {review.latest.reviewedMatterVersion}</small></div><em>{shortHash(review.latest.outputHash)}</em></article>
          <article><span>02</span><div><strong>当前核对提示</strong><small>为保护当事人信息，本页只显示提示编号，不重复展示文书正文或确认内容。</small></div><em>{latestFindings.length ? latestFindings.map((item) => item.code).join(" · ") : "无"}</em></article>
        </div>
      )}
    </section>
  );
}

function Summary({ label, value, note }: { label: string; value: string; note: string }) {
  return <div><span>{label}</span><strong>{value}</strong><small>{note}</small></div>;
}

function Dependency({ label, hash }: { label: string; hash: string | null }) {
  return <div><span>{label}</span><strong>{hash ? "已绑定" : "未绑定"}</strong><code>{hash ? shortHash(hash) : "—"}</code></div>;
}

function statusLabel(status: SubmissionReviewView["status"]) {
  if (status === "exported") return "已核验导出";
  if (status === "locked") return "已锁定提交版";
  if (status === "reviewable") return "待锁定";
  return "流程阻断";
}

function documentKindLabel(kind: string) {
  const labels: Record<string, string> = {
    DEFENCE_STATEMENT: "民事答辩状",
    EVIDENCE_INDEX: "证据目录",
    EVIDENCE_MATERIAL: "证据材料",
    INTEREST_CALCULATION: "利息测算表",
    AUTHORIZATION_LETTER: "授权委托书",
    LAW_FIRM_LETTER: "律所函",
  };
  return labels[kind] ?? kind;
}

function shortHash(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}

function formatBytes(value: number) {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
