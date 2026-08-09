"use client";

import { useEffect, useState } from "react";
import {
  caseDataSourceConfig,
  fetchSubmissionExport,
  loadSubmissionReview,
  type SubmissionReviewView,
} from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

const requiredFlow = [
  ["答辩文书", "DEFENCE_STATEMENT", "正文哈希须与当前最终文本审批一致"],
  ["证据目录", "EVIDENCE_INDEX", "仅列入本次实际提交的证据"],
  ["证据材料", "EVIDENCE_MATERIAL", "只含已核验相关页；红框版本单独命名"],
  ["利息测算表", "INTEREST_CALCULATION", "人民币 / CNY，逐期本金与冲抵可复算"],
] as const;

export function SubmissionWorkbench() {
  const [review, setReview] = useState<SubmissionReviewView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    loadSubmissionReview()
      .then((result) => {
        if (active) setReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "提交材料快照读取失败");
      });
    return () => {
      active = false;
    };
  }, []);

  if (error) {
    return (
      <section className={styles.submissionArea} aria-label="提交材料">
        <div className={styles.calculationBlocked} role="alert">
          <p className={styles.eyebrow}>提交链已阻断</p>
          <h3>{caseDataSourceConfig.kind === "persistent-disabled" ? "持久化模式未启用" : "提交材料快照未连接"}</h3>
          <p>{error}</p>
          <small>系统没有生成空 ZIP，也没有回退到合成文件。</small>
        </div>
      </section>
    );
  }
  if (!review) {
    return <section className={styles.submissionArea}><div className={styles.calculationLoading}>正在核对提交文件与全部上游依赖…</div></section>;
  }

  const approvedCourtProducts = review.workProducts.filter(
    (item) => item.status === "APPROVED" && item.audience === "COURT_SUBMISSION",
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
          <p className={styles.eyebrow}>法院提交包</p>
          <h2>只保留要提交的文件，内部审计信息不混入法院 ZIP</h2>
          <p>每份文件、排列顺序、正式名称和依赖哈希都由律师批准；锁定后由本机确定性程序编译并逐文件复核。</p>
        </div>
        <div className={styles.calculationStatus}>
          <span>{review.sourceLabel}</span>
          <strong>{statusLabel(review.status)}</strong>
          <small>{review.snapshotHash ? `快照 ${shortHash(review.snapshotHash)}` : "未形成正式快照"}</small>
        </div>
      </header>

      <div className={review.status === "blocked" ? styles.submissionBlockedNotice : styles.submissionReviewNotice}>
        <strong>{review.status === "blocked" ? "不能生成正式文件" : `案件版本 ${review.matterVersion}`}</strong>
        <span>{review.statusReason}</span>
      </div>

      <div className={styles.submissionSummary}>
        <Summary label="已批准法院文件" value={`${approvedCourtProducts.length} 份`} note="内部底稿不会进入" />
        <Summary label="当前锁定版" value={currentBundle ? "1 份" : "无"} note="每案最多一个" />
        <Summary label="币种" value={currentBundle?.currency ?? "CNY"} note="金额文件必须明确" />
        <Summary label="已核验导出" value={review.currentExport ? "已形成" : "未形成"} note={review.currentExport ? `${review.currentExport.componentCount} 个文件` : "不得上传法院"} />
      </div>

      <div className={styles.submissionGrid}>
        <section className={styles.submissionPanel} aria-labelledby="submission-files-title">
          <div className={styles.submissionPanelHeading}>
            <div><p className={styles.eyebrow}>法院文件区</p><h3 id="submission-files-title">本次提交文件</h3></div>
            <span>{review.currentComponents.length ? "来自当前锁定版" : "等待律师建立 QA 清单"}</span>
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
          <p className={styles.eyebrow}>文件边界</p>
          <h3>法院 ZIP 与内部清单分开</h3>
          <dl>
            <div><dt>法院 ZIP</dt><dd>只有批准的 PDF</dd></div>
            <div><dt>内部清单</dt><dd>哈希、版本、审批与依赖</dd></div>
            <div><dt>原始案卷</dt><dd>永不改写、永不混入</dd></div>
            <div><dt>文件名</dt><dd>不使用“最新/最终/V2”</dd></div>
          </dl>
          <p>委托手续、律所函和身份材料是否必需，应由具体法院要求与本案代理关系决定，并纳入同一 QA 清单。</p>
        </aside>
      </div>

      <section className={styles.submissionPanel} aria-labelledby="submission-lineage-title">
        <div className={styles.submissionPanelHeading}>
          <div><p className={styles.eyebrow}>锁定依据</p><h3 id="submission-lineage-title">四项不可缺少的上游依赖</h3></div>
          <span>{currentBundle ? `输入 ${shortHash(currentBundle.inputHash)}` : "未锁定"}</span>
        </div>
        <div className={styles.submissionDependencyGrid}>
          <Dependency label="证据 Manifest" hash={currentBundle?.evidenceManifestHash ?? null} />
          <Dependency label="法律规则包" hash={currentBundle?.legalBundleHash ?? null} />
          <Dependency label="利息计算输出" hash={currentBundle?.calculationOutputHash ?? null} />
          <Dependency label="最终文本审批" hash={currentBundle?.finalTextHash ?? null} />
        </div>
      </section>

      <section className={styles.submissionExportState} aria-label="导出核验状态">
        <div>
          <p className={styles.eyebrow}>导出状态</p>
          <h3>{review.currentExport ? "法院 ZIP 已完成逐文件核验" : "尚无可交付的法院 ZIP"}</h3>
          <p>{review.currentExport ? `法院 ZIP ${formatBytes(review.currentExport.courtZipBytes)}；内部清单已作为独立加密对象保存。` : "只有当前有效锁定版才能由本机 Worker 编译；页面不会用临时文件或演示数据代替。"}</p>
        </div>
        {review.currentExport ? (
          <div className={styles.submissionDownloadBlock}>
            <dl>
              <div><dt>ZIP 哈希</dt><dd>{shortHash(review.currentExport.courtZipSha256)}</dd></div>
              <div><dt>内部清单哈希</dt><dd>{shortHash(review.currentExport.internalManifestSha256)}</dd></div>
              <div><dt>核验回执</dt><dd>{shortHash(review.currentExport.verificationHash)}</dd></div>
            </dl>
            <button disabled={downloading} onClick={downloadCourtZip} type="button">
              {downloading ? "正在核验并下载…" : "下载法院提交材料.zip"}
            </button>
            {downloadError && <small role="alert">{downloadError}</small>}
          </div>
        ) : <span className={styles.submissionGate}>等待：文件审批 → QA 清单 → 唯一锁定 → 本机编译</span>}
      </section>
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
