"use client";

import { useEffect, useState } from "react";
import {
  caseDataSourceConfig,
  loadLegalReview,
  loadOfficialSourceCaptureReview,
  queueOfficialSourceCapture,
  reviewOfficialSourceCapture,
  type LegalReviewView,
  type OfficialSourceCaptureView,
} from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

export function LegalWorkbench() {
  const [review, setReview] = useState<LegalReviewView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [captureReview, setCaptureReview] = useState<OfficialSourceCaptureView | null>(null);
  const [captureError, setCaptureError] = useState<string | null>(null);
  const [captureBusy, setCaptureBusy] = useState<string | null>(null);
  const [captureNotice, setCaptureNotice] = useState<string | null>(null);
  const [publicSourceConfirmed, setPublicSourceConfirmed] = useState(false);
  const [provisionLocators, setProvisionLocators] = useState<Record<string, string>>({});

  useEffect(() => {
    let active = true;
    loadLegalReview()
      .then((result) => {
        if (active) setReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "法律依据审查快照读取失败");
      });
    loadOfficialSourceCaptureReview()
      .then((result) => {
        if (active) setCaptureReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setCaptureError(reason instanceof Error ? reason.message : "官方法源抓取快照读取失败");
      });
    return () => {
      active = false;
    };
  }, []);

  if (error) {
    return (
      <section className={styles.legalArea} aria-label="法律规则">
        <div className={styles.calculationBlocked} role="alert">
          <p className={styles.eyebrow}>法律依据数据源已阻断</p>
          <h3>{caseDataSourceConfig.kind === "persistent-disabled" ? "持久化模式未启用" : "法律依据快照未连接"}</h3>
          <p>{error}</p>
          <small>系统没有回退到演示规则，也没有显示任何未经服务端批准的利率。</small>
        </div>
      </section>
    );
  }
  if (!review) {
    return <section className={styles.legalArea}><div className={styles.calculationLoading}>正在读取官方来源与案件规则包…</div></section>;
  }

  const readySources = review.sources.filter(
    (source) => source.verificationStatus === "VERIFIED" && source.licenseStatus === "ACTIVE",
  ).length;
  const approvedBindings = review.factBindings.filter((item) => item.status === "APPROVED").length;

  async function refreshCaptureReview() {
    const refreshed = await loadOfficialSourceCaptureReview();
    setCaptureReview(refreshed);
    setCaptureError(null);
    return refreshed;
  }

  async function queueCapture(source: LegalReviewView["sources"][number]) {
    if (!captureReview || captureReview.matterVersion === null) return;
    if (!publicSourceConfirmed) {
      setCaptureNotice("请先确认本次只访问公开官方网站且不发送案件材料。");
      return;
    }
    setCaptureBusy(`queue:${source.sourceId}`);
    setCaptureNotice(null);
    try {
      const targetUrl = officialCaptureTarget(source.sourceId, source.officialUrl);
      const receipt = await queueOfficialSourceCapture({
        sourceId: source.sourceId,
        targetUrl,
        expectedVersion: captureReview.matterVersion,
      });
      await refreshCaptureReview();
      setCaptureNotice(`抓取任务已进入受控队列；案件版本更新为 ${receipt.matterVersion}。系统不会在回执未知时自动重试。`);
    } catch (reason: unknown) {
      setCaptureNotice(reason instanceof Error ? reason.message : "官方法源抓取任务未建立");
    } finally {
      setCaptureBusy(null);
    }
  }

  async function recordCaptureReview(
    run: OfficialSourceCaptureView["runs"][number],
    decision: "APPROVE_FOR_REGISTRATION" | "REJECT",
  ) {
    if (!captureReview || captureReview.matterVersion === null) return;
    setCaptureBusy(`review:${run.runId}`);
    setCaptureNotice(null);
    try {
      const receipt = await reviewOfficialSourceCapture({
        runId: run.runId,
        expectedVersion: captureReview.matterVersion,
        decision,
        provisionLocator: provisionLocators[run.runId] ?? "",
        contentSha256: run.contentSha256,
        parsedOutputHash: run.parsedOutputHash,
      });
      await refreshCaptureReview();
      setCaptureNotice(`${decision === "REJECT" ? "驳回" : "待登记批准"}决定已写入审计链；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setCaptureNotice(reason instanceof Error ? reason.message : "官方法源复核未记录");
    } finally {
      setCaptureBusy(null);
    }
  }

  return (
    <section className={styles.legalArea} aria-label="法律规则">
      <header className={styles.calculationHeading}>
        <div>
          <p className={styles.eyebrow}>法律依据与适用规则</p>
          <h2>把法条、时间节点和利率公式锁成可追溯规则包</h2>
          <p>来源必须来自登记的官方域名；规则参数由服务端计算；案件关键事实与触发日期必须经过律师批准。</p>
        </div>
        <div className={styles.calculationStatus}>
          <span>{review.sourceLabel}</span>
          <strong>{review.status === "reviewable" ? `案件版本 ${review.matterVersion}` : "仅作来源发现"}</strong>
          <small>{review.snapshotHash ? `快照 ${shortHash(review.snapshotHash)}` : "未形成正式快照"}</small>
        </div>
      </header>

      <div className={review.status === "discovery-only" ? styles.legalDiscoveryNotice : styles.legalReviewNotice}>
        <strong>{review.status === "discovery-only" ? "未进入正式规则链" : "版本化只读审查"}</strong>
        <span>{review.statusReason}</span>
      </div>

      <div className={styles.legalSummary}>
        <SummaryCell label="官方来源" value={`${review.sources.length} 项`} note={`${readySources} 项已核验可用`} />
        <SummaryCell label="规则版本" value={`${review.ruleVersions.length} 项`} note="利率不由浏览器输入" />
        <SummaryCell label="关键事实锚点" value={`${approvedBindings} 项`} note="只绑定已确认事实" />
        <SummaryCell label="当前规则包" value={review.currentBundle ? `v${review.currentBundle.version}` : "未建立"} note={review.bundleSegments.length ? `${review.bundleSegments.length} 个连续分段` : "不能启动正式计算"} />
      </div>

      <div className={styles.legalGrid}>
        <section className={styles.legalPanel} aria-labelledby="official-sources-title">
          <div className={styles.legalPanelHeading}>
            <div><p className={styles.eyebrow}>来源层</p><h3 id="official-sources-title">官方来源快照</h3></div>
            <span>{readySources}/{review.sources.length} 可进入规则</span>
          </div>
          <div className={styles.legalSourceList}>
            {review.sources.map((source) => (
              <article className={styles.legalSourceRow} key={`${source.sourceId}-${source.snapshotId ?? "discovery"}`}>
                <div>
                  <strong>{source.publisher}</strong>
                  <small>{authorityLabel(source.authorityLevel)} · {source.provisionLocator}</small>
                  <a href={source.officialUrl} rel="noreferrer" target="_blank">打开官方原文</a>
                </div>
                <div className={styles.legalSourceState}>
                  <span className={source.verificationStatus === "VERIFIED" ? styles.verified : styles.pending}>{source.verificationStatus === "VERIFIED" ? "已核验" : "待正式捕获"}</span>
                  <small>{source.contentSha256 ? shortHash(source.contentSha256) : "无内容哈希"}</small>
                  {captureReview?.status === "persistent" && source.verificationStatus !== "VERIFIED" && (
                    <button
                      className={styles.legalCaptureButton}
                      disabled={!publicSourceConfirmed || captureBusy !== null}
                      onClick={() => queueCapture(source)}
                      type="button"
                    >
                      {captureBusy === `queue:${source.sourceId}` ? "正在入队…" : "授权抓取官方原文"}
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>

        <aside className={styles.legalGuardrail}>
          <p className={styles.eyebrow}>强制门禁</p>
          <h3>正式计算前必须同时满足</h3>
          <ol>
            <li>官方网页或 PDF 原始字节已加密保存并绑定 SHA-256。</li>
            <li>具体条文位置、版本效力和使用许可经授权人员核验。</li>
            <li>合同成立、起诉、受理、付款等日期有案件证据锚点。</li>
            <li>规则所需事实键已绑定到“已确认”的案件事实。</li>
            <li>所有连续期间均使用同一已批准规则包，独立复算一致。</li>
          </ol>
          <p>任一上游事实、证据、规则或来源失效，当前计算及提交材料自动转为失效。</p>
        </aside>
      </div>

      <section className={styles.legalPanel} aria-labelledby="official-capture-title">
        <div className={styles.legalPanelHeading}>
          <div><p className={styles.eyebrow}>抓取与复核层</p><h3 id="official-capture-title">官方法源抓取与律师复核</h3></div>
          <span>{captureReview ? captureReview.sourceLabel : "正在读取独立抓取状态"}</span>
        </div>
        {captureError ? (
          <div className={styles.legalCaptureBlocked} role="alert">
            <strong>抓取服务未连接</strong>
            <span>{captureError}</span>
            <small>法律规则只读页仍可审查；页面没有把开发冒烟或发现链接替代为正式法源。</small>
          </div>
        ) : !captureReview ? (
          <div className={styles.legalEmpty}>正在读取抓取队列、内容哈希和复核记录…</div>
        ) : (
          <>
            <div className={captureReview.status === "probe-only" ? styles.legalCaptureProbe : styles.legalCapturePersistent}>
              <strong>{captureReview.status === "probe-only" ? "仅为开发验证" : `案件版本 ${captureReview.matterVersion}`}</strong>
              <span>{captureReview.statusReason}</span>
              {captureReview.snapshotHash && <code>{shortHash(captureReview.snapshotHash)}</code>}
            </div>

            {captureReview.status === "persistent" && (
              <label className={styles.legalCaptureConsent}>
                <input
                  checked={publicSourceConfirmed}
                  onChange={(event) => setPublicSourceConfirmed(event.target.checked)}
                  type="checkbox"
                />
                <span><strong>我确认本次只访问页面列明的公开官方网站</strong><small>请求不会携带案卷、当事人姓名、Cookie、API Key 或律所账号；授权 15 分钟内只尝试一次。</small></span>
              </label>
            )}

            {captureReview.runs.length ? (
              <div className={styles.legalCaptureRuns}>
                {captureReview.runs.map((run) => {
                  const recordedReview = captureReview.reviews.find((item) => item.runId === run.runId);
                  return (
                    <article key={run.runId} className={styles.legalCaptureRun}>
                      <div className={styles.legalCaptureRunHeading}>
                        <div>
                          <strong>{run.publisher}</strong>
                          <small>{run.sourceId} · {captureStatusLabel(run.status)}</small>
                        </div>
                        <span className={captureStatusClass(run.status)}>{captureStatusLabel(run.status)}</span>
                      </div>
                      <div className={styles.legalCaptureMeta}>
                        <a href={run.targetUrl} rel="noreferrer" target="_blank">先打开本次授权的官方原文</a>
                        <span>尝试 {run.attemptCount} 次</span>
                        <span>{run.contentMediaType ?? "尚无响应媒体类型"}</span>
                        <span>{run.contentBytes ? formatBytes(run.contentBytes) : "尚无归档字节"}</span>
                      </div>
                      <div className={styles.legalCaptureHashes}>
                        <code>内容 {run.contentSha256 ? shortHash(run.contentSha256) : "—"}</code>
                        <code>解析 {run.parsedOutputHash ? shortHash(run.parsedOutputHash) : "—"}</code>
                        <code>捕获回执 {run.captureVerificationHash ? shortHash(run.captureVerificationHash) : "—"}</code>
                      </div>
                      {run.parsedSummary && <ParsedSummary summary={run.parsedSummary} />}
                      {(run.failureCode || run.staleReason) && (
                        <p className={styles.legalCaptureFailure}>{run.failureCode ? `${run.failureCode}：` : ""}{run.staleReason}</p>
                      )}
                      {recordedReview ? (
                        <div className={`${styles.legalCapturedReview} ${recordedReview.decision === "REJECT" ? styles.legalCapturedReviewRejected : ""}`}>
                          <strong>{recordedReview.decision === "REJECT" ? "律师已驳回" : "律师已批准进入登记步骤"}</strong>
                          <span>{recordedReview.provisionLocator}</span>
                          <code>{shortHash(recordedReview.reviewHash)}</code>
                        </div>
                      ) : run.status === "REVIEW_REQUIRED" && captureReview.status === "persistent" ? (
                        <div className={styles.legalReviewActions}>
                          <label htmlFor={`locator-${run.runId}`}>官方原文定位</label>
                          <input
                            id={`locator-${run.runId}`}
                            onChange={(event) => setProvisionLocators((prior) => ({ ...prior, [run.runId]: event.target.value }))}
                            placeholder="例如：第二十五条、第三十一条；或 LPR records[0]"
                            value={provisionLocators[run.runId] ?? ""}
                          />
                          <div>
                            <button disabled={captureBusy !== null} onClick={() => recordCaptureReview(run, "REJECT")} type="button">驳回本次结果</button>
                            <button disabled={captureBusy !== null} onClick={() => recordCaptureReview(run, "APPROVE_FOR_REGISTRATION")} type="button">批准进入登记步骤</button>
                          </div>
                          <small>批准只记录律师复核结论，不会自动登记规则、选择适用利率或启动利息计算。</small>
                        </div>
                      ) : null}
                    </article>
                  );
                })}
              </div>
            ) : <div className={styles.legalEmpty}>尚无抓取运行。先勾选公开网络授权，再从上方来源清单选择具体官方原文。</div>}

            {captureNotice && <div className={styles.legalCaptureNotice} role="status">{captureNotice}</div>}
          </>
        )}
      </section>

      <section className={styles.legalPanel} aria-labelledby="rule-versions-title">
        <div className={styles.legalPanelHeading}>
          <div><p className={styles.eyebrow}>规则层</p><h3 id="rule-versions-title">规则版本与案件事实锚点</h3></div>
          <span>审批动作不在只读页执行</span>
        </div>
        {review.ruleVersions.length ? (
          <div className={styles.legalRuleTable} role="table" aria-label="法律规则版本">
            <div className={`${styles.legalRuleRow} ${styles.legalRuleHead}`} role="row"><span>争点 / 版本</span><span>触发事件</span><span>公式</span><span>服务端年利率</span><span>所需事实</span></div>
            {review.ruleVersions.map((rule) => (
              <div className={styles.legalRuleRow} role="row" key={rule.ruleVersionId}>
                <span><strong>{rule.issueKey}</strong><small>{rule.ruleVersion}</small></span>
                <span>{eventLabel(rule.triggerEventKind)}</span>
                <span>{formulaLabel(rule)}<small>{rule.parameterEvidenceLocator ?? "无外部利率参数"}</small></span>
                <span>{formatPercent(rule.derivedAnnualRate)}</span>
                <span>{rule.requiredFactKeys.length ? rule.requiredFactKeys.join("、") : "无"}</span>
              </div>
            ))}
          </div>
        ) : <div className={styles.legalEmpty}>当前没有可批准的规则版本。来源发现记录不会被当作规则使用。</div>}

        <div className={styles.legalAnchors}>
          <div><strong>已批准法律事件</strong><span>{review.legalEvents.length} 项</span><small>{review.legalEvents.map((item) => `${eventLabel(item.eventKind)} ${item.localDate}`).join("；") || "尚未建立"}</small></div>
          <div><strong>已绑定关键事实</strong><span>{approvedBindings} 项</span><small>{review.factBindings.filter((item) => item.status === "APPROVED").map((item) => item.factKey).join("；") || "尚未建立"}</small></div>
        </div>
      </section>

      <section className={styles.legalPanel} aria-labelledby="legal-bundle-title">
        <div className={styles.legalPanelHeading}>
          <div><p className={styles.eyebrow}>计算入口</p><h3 id="legal-bundle-title">当前案件法律规则包</h3></div>
          <span>{review.currentBundle ? `哈希 ${shortHash(review.currentBundle.bundleHash)}` : "未批准"}</span>
        </div>
        {review.bundleSegments.length ? (
          <div className={styles.legalTimeline}>
            {review.bundleSegments.map((segment, index) => (
              <article key={segment.segmentId}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div><strong>{segment.startDate} 至 {segment.endDate}</strong><small>{segment.issueKey} · {segment.applicabilityAnchor}</small></div>
                <em>{formatPercent(segment.annualRate)}</em>
              </article>
            ))}
          </div>
        ) : <div className={styles.legalEmpty}>尚无经律师批准的连续规则分段，正式利息计算保持阻断。</div>}
      </section>
    </section>
  );
}

function SummaryCell({ label, value, note }: { label: string; value: string; note: string }) {
  return <div><span>{label}</span><strong>{value}</strong><small>{note}</small></div>;
}

function shortHash(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}

function formatPercent(value: string) {
  return `${(Number(value) * 100).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}%`;
}

function authorityLabel(value: string) {
  if (value === "PRIMARY_LAW") return "法律";
  if (value === "JUDICIAL_INTERPRETATION") return "司法解释";
  if (value === "OFFICIAL_RATE_DATA") return "官方利率数据";
  return "官方案例";
}

function eventLabel(value: string) {
  const labels: Record<string, string> = {
    CONTRACT_SIGNED: "合同成立",
    DISBURSEMENT: "借款交付",
    PAYMENT: "付款",
    DEFAULT: "逾期",
    CLAIM_FILED: "起诉",
    CASE_ACCEPTED: "法院受理",
    JUDGMENT: "裁判",
  };
  return labels[value] ?? value;
}

function formulaLabel(rule: LegalReviewView["ruleVersions"][number]) {
  if (rule.formulaKind === "NO_INTEREST") return "不计息";
  if (rule.formulaKind === "LPR_MULTIPLE") return `${formatPercent(rule.baseAnnualRate ?? "0")} × ${rule.rateMultiplier}`;
  return `固定 ${formatPercent(rule.baseAnnualRate ?? rule.derivedAnnualRate)}`;
}

function officialCaptureTarget(sourceId: string, defaultUrl: string) {
  if (sourceId === "CFETS-LPR-HISTORY") {
    return "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?lang=CN";
  }
  return defaultUrl;
}

function captureStatusLabel(status: string) {
  const labels: Record<string, string> = {
    QUEUED: "等待本机抓取",
    RUNNING: "正在抓取",
    REVIEW_REQUIRED: "待律师复核",
    FAILED: "抓取失败",
    STALE: "已失效",
    PROBE_CAPTURE_AND_PARSE_OK: "开发冒烟通过",
    PROBE_FAILED: "开发冒烟未通过",
  };
  return labels[status] ?? status;
}

function captureStatusClass(status: string) {
  if (status === "REVIEW_REQUIRED") return styles.legalCaptureReady;
  if (["FAILED", "STALE", "PROBE_FAILED"].includes(status)) return styles.legalCaptureFailed;
  if (status === "PROBE_CAPTURE_AND_PARSE_OK") return styles.legalCaptureProbeState;
  return styles.legalCapturePending;
}

function ParsedSummary({ summary }: { summary: Record<string, unknown> }) {
  const entries = Object.entries(summary).slice(0, 8);
  return (
    <dl className={styles.legalParsedSummary}>
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{summaryLabel(key)}</dt>
          <dd>{displaySummaryValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function summaryLabel(value: string) {
  const labels: Record<string, string> = {
    located_articles: "定位条文",
    result: "解析结果",
    record_count: "记录数",
    period: "覆盖期间",
    latest_one_year_lpr: "最新一年期 LPR",
  };
  return labels[value] ?? value.replaceAll("_", " ");
}

function displaySummaryValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (Array.isArray(value)) return value.map((item) => String(item)).join("、").slice(0, 240);
  if (typeof value === "object") return "结构化解析记录（展开功能待接入）";
  return String(value).slice(0, 240);
}

function formatBytes(value: number) {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
