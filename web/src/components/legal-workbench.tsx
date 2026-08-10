"use client";

import { useEffect, useState } from "react";
import {
  caseDataSourceConfig,
  approveLegalFactBinding,
  approveCaseLegalEvent,
  loadLegalReview,
  loadCaseReview,
  loadEvidenceReview,
  loadOfficialSourceCaptureReview,
  approveLprMultipleRuleVersion,
  queueOfficialSourceCapture,
  registerReviewedOfficialSourceCapture,
  reviewOfficialSourceCapture,
  type LegalReviewView,
  type CaseReviewView,
  type EvidenceReviewView,
  type OfficialSourceCaptureView,
} from "@/lib/case-data-source";
import { officialCasePolicyLinks, officialCaseResearchCatalog } from "@/lib/official-case-catalog";
import officialSourceCatalog from "../../../knowledge/official_sources/registry.json";
import styles from "./case-workbench.module.css";

export function LegalWorkbench() {
  const [review, setReview] = useState<LegalReviewView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [captureReview, setCaptureReview] = useState<OfficialSourceCaptureView | null>(null);
  const [captureError, setCaptureError] = useState<string | null>(null);
  const [captureBusy, setCaptureBusy] = useState<string | null>(null);
  const [captureNotice, setCaptureNotice] = useState<string | null>(null);
  const [publicSourceConfirmed, setPublicSourceConfirmed] = useState(false);
  const [captureTargets, setCaptureTargets] = useState<Record<string, string>>({});
  const [provisionLocators, setProvisionLocators] = useState<Record<string, string>>({});
  const [licenseBases, setLicenseBases] = useState<Record<string, string>>({});
  const [ruleBusy, setRuleBusy] = useState(false);
  const [ruleNotice, setRuleNotice] = useState<string | null>(null);
  const [ruleApproved, setRuleApproved] = useState(false);
  const [caseReview, setCaseReview] = useState<CaseReviewView | null>(null);
  const [factBinding, setFactBinding] = useState({ factKey: "", factId: "", approved: false });
  const [bindingBusy, setBindingBusy] = useState(false);
  const [bindingNotice, setBindingNotice] = useState<string | null>(null);
  const [evidenceReview, setEvidenceReview] = useState<EvidenceReviewView | null>(null);
  const [legalEvent, setLegalEvent] = useState({
    eventKind: "CLAIM_FILED" as const,
    localDate: "",
    evidenceIds: [] as string[],
    approved: false,
  });
  const [eventBusy, setEventBusy] = useState(false);
  const [eventNotice, setEventNotice] = useState<string | null>(null);
  const [lprRule, setLprRule] = useState({
    ruleId: "",
    ruleVersion: "",
    issueKey: "",
    sourceSnapshotId: "",
    parameterSourceSnapshotId: "",
    parameterEvidenceLocator: "",
    effectiveFrom: "",
    effectiveTo: "",
    triggerEventKind: "CLAIM_FILED" as const,
    rateMultiplier: "4",
    conflictSet: "",
    priority: "100",
    requiredFactKeys: [] as string[],
  });

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
    loadCaseReview()
      .then((result) => {
        if (active) setCaseReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setBindingNotice(reason instanceof Error ? reason.message : "案件事实快照读取失败");
      });
    loadEvidenceReview()
      .then((result) => {
        if (active) setEvidenceReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setEventNotice(reason instanceof Error ? reason.message : "案件证据快照读取失败");
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
    (source) => source.verificationStatus === "VERIFIED"
      && source.licenseStatus === "ACTIVE"
      && source.licenseBasis
      && source.licenseReviewHash,
  ).length;
  const approvedBindings = review.factBindings.filter((item) => item.status === "APPROVED").length;

  async function refreshCaptureReview() {
    const refreshed = await loadOfficialSourceCaptureReview();
    setCaptureReview(refreshed);
    setCaptureError(null);
    return refreshed;
  }

  async function queueCapture(
    source: LegalReviewView["sources"][number],
    targetUrl: string,
  ) {
    if (!captureReview || captureReview.matterVersion === null) return;
    if (!publicSourceConfirmed) {
      setCaptureNotice("请先确认本次只访问公开官方网站且不发送案件材料。");
      return;
    }
    setCaptureBusy(`queue:${source.sourceId}`);
    setCaptureNotice(null);
    try {
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

  async function registerCapture(run: OfficialSourceCaptureView["runs"][number], captureReviewHash: string) {
    if (!captureReview || captureReview.matterVersion === null || !run.contentSha256 || !run.parsedOutputHash) return;
    setCaptureBusy(`register:${run.runId}`);
    setCaptureNotice(null);
    try {
      const receipt = await registerReviewedOfficialSourceCapture({
        runId: run.runId,
        expectedVersion: captureReview.matterVersion,
        contentSha256: run.contentSha256,
        parsedOutputHash: run.parsedOutputHash,
        captureReviewHash,
        licenseBasis: licenseBases[run.runId] ?? "",
      });
      const [refreshedCapture, refreshedLegal] = await Promise.all([
        loadOfficialSourceCaptureReview(),
        loadLegalReview(),
      ]);
      setCaptureReview(refreshedCapture);
      setReview(refreshedLegal);
      setCaptureNotice(`正式法源快照已登记；案件版本更新为 ${receipt.matterVersion}。规则和利率仍须在后续独立审批中建立。`);
    } catch (reason: unknown) {
      setCaptureNotice(reason instanceof Error ? reason.message : "正式法源快照未登记");
    } finally {
      setCaptureBusy(null);
    }
  }

  async function approveLprRule() {
    if (!review || review.status !== "reviewable" || review.matterVersion === null) return;
    if (!ruleApproved) {
      setRuleNotice("请先确认：基准利率由已认证官方观察记录自动读取，不能手工填入或修改。");
      return;
    }
    const priority = Number(lprRule.priority);
    if (!Number.isInteger(priority) || priority < 0 || priority > 1_000_000) {
      setRuleNotice("规则优先级必须是 0 至 1000000 的整数。");
      return;
    }
    setRuleBusy(true);
    setRuleNotice(null);
    try {
      const receipt = await approveLprMultipleRuleVersion({
        expectedVersion: review.matterVersion,
        ruleId: lprRule.ruleId,
        ruleVersion: lprRule.ruleVersion,
        issueKey: lprRule.issueKey,
        sourceSnapshotId: lprRule.sourceSnapshotId,
        parameterSourceSnapshotId: lprRule.parameterSourceSnapshotId,
        parameterEvidenceLocator: lprRule.parameterEvidenceLocator,
        effectiveFrom: lprRule.effectiveFrom,
        effectiveTo: lprRule.effectiveTo || null,
        triggerEventKind: lprRule.triggerEventKind,
        rateMultiplier: lprRule.rateMultiplier,
        requiredFactKeys: lprRule.requiredFactKeys,
        transitionRuleVersions: [],
        conflictSet: lprRule.conflictSet || null,
        priority,
      });
      const refreshed = await loadLegalReview();
      setReview(refreshed);
      setRuleNotice(`LPR 规则已进入审批链；案件版本更新为 ${receipt.matterVersion}。基准值仍仅由官方观察记录派生。`);
      setRuleApproved(false);
    } catch (reason: unknown) {
      setRuleNotice(reason instanceof Error ? reason.message : "LPR 规则未获批准");
    } finally {
      setRuleBusy(false);
    }
  }

  async function bindLegalFact() {
    if (!review || review.status !== "reviewable" || review.matterVersion === null) return;
    if (!factBinding.approved) {
      setBindingNotice("请先确认：只有同案且已确认的事实可以成为法律规则锚点。");
      return;
    }
    setBindingBusy(true);
    setBindingNotice(null);
    try {
      const receipt = await approveLegalFactBinding({
        expectedVersion: review.matterVersion,
        factKey: factBinding.factKey,
        factId: factBinding.factId,
      });
      const [refreshedLegal, refreshedCase] = await Promise.all([loadLegalReview(), loadCaseReview()]);
      setReview(refreshedLegal);
      setCaseReview(refreshedCase);
      setFactBinding((prior) => ({ ...prior, approved: false }));
      setBindingNotice(`法律规则事实锚点已建立；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setBindingNotice(reason instanceof Error ? reason.message : "法律规则事实锚点未建立");
    } finally {
      setBindingBusy(false);
    }
  }

  async function approveLegalEvent() {
    if (!review || review.status !== "reviewable" || review.matterVersion === null) return;
    if (!legalEvent.approved) {
      setEventNotice("请先确认：事件日期与所选证据页已经由律师核对，系统不会从文件时间自动推定日期。");
      return;
    }
    setEventBusy(true);
    setEventNotice(null);
    try {
      const receipt = await approveCaseLegalEvent({
        expectedVersion: review.matterVersion,
        eventKind: legalEvent.eventKind,
        localDate: legalEvent.localDate,
        evidenceIds: legalEvent.evidenceIds,
      });
      const [refreshedLegal, refreshedEvidence] = await Promise.all([loadLegalReview(), loadEvidenceReview()]);
      setReview(refreshedLegal);
      setEvidenceReview(refreshedEvidence);
      setLegalEvent((prior) => ({ ...prior, approved: false, evidenceIds: [] }));
      setEventNotice(`案件法律事件已建立；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setEventNotice(reason instanceof Error ? reason.message : "案件法律事件未建立");
    } finally {
      setEventBusy(false);
    }
  }

  const legalFormulaSources = review.sources.filter(
    (source) => source.snapshotId
      && source.verificationStatus === "VERIFIED"
      && source.licenseStatus === "ACTIVE"
      && source.licenseBasis
      && source.licenseReviewHash
      && ["PRIMARY_LAW", "JUDICIAL_INTERPRETATION"].includes(source.authorityLevel),
  );
  const lprParameterSources = review.sources.filter(
    (source) => source.snapshotId
      && source.sourceId === "CFETS-LPR-HISTORY"
      && source.authorityLevel === "OFFICIAL_RATE_DATA"
      && source.verificationStatus === "VERIFIED"
      && source.licenseStatus === "ACTIVE"
      && source.licenseBasis
      && source.licenseReviewHash,
  );
  const availableFactKeys = [...new Set(review.factBindings
    .filter((binding) => binding.status === "APPROVED")
    .map((binding) => binding.factKey))];
  const confirmedFacts = caseReview?.facts.filter((fact) => fact.status === "CONFIRMED") ?? [];
  const includedEvidencePages = evidenceReview?.pages.filter(
    (page) => page.disposition === "INCLUDE" && page.decisionId !== null,
  ) ?? [];

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
            {review.sources.map((source) => {
              const candidates = officialCaptureTargets(source.sourceId, source.officialUrl);
              const targetUrl = captureTargets[source.sourceId] ?? candidates[0].url;
              return (
              <article className={styles.legalSourceRow} key={`${source.sourceId}-${source.snapshotId ?? "discovery"}`}>
                <div>
                  <strong>{source.publisher}</strong>
                  <small>{authorityLabel(source.authorityLevel)} · {source.provisionLocator}</small>
                  <a href={source.officialUrl} rel="noreferrer" target="_blank">打开官方原文</a>
                </div>
                <div className={styles.legalSourceState}>
                  <span className={source.verificationStatus === "VERIFIED" && source.licenseBasis && source.licenseReviewHash ? styles.verified : styles.pending}>
                    {source.verificationStatus !== "VERIFIED" ? "待正式捕获" : source.licenseBasis && source.licenseReviewHash ? "来源与许可已核验" : "许可依据待补核"}
                  </span>
                  <small>{source.contentSha256 ? shortHash(source.contentSha256) : "无内容哈希"}</small>
                  {captureReview?.status === "persistent" && source.verificationStatus !== "VERIFIED" && (
                    <>
                      {candidates.length > 1 && (
                        <label className={styles.legalCaptureTarget}>
                          <span>本次来源</span>
                          <select
                            aria-label={`${source.publisher}的本次抓取来源`}
                            onChange={(event) => setCaptureTargets((prior) => ({ ...prior, [source.sourceId]: event.target.value }))}
                            value={targetUrl}
                          >
                            {candidates.map((candidate) => <option key={candidate.url} value={candidate.url}>{candidate.label}</option>)}
                          </select>
                        </label>
                      )}
                      <button
                        className={styles.legalCaptureButton}
                        disabled={!publicSourceConfirmed || captureBusy !== null}
                        onClick={() => queueCapture(source, targetUrl)}
                        type="button"
                      >
                        {captureBusy === `queue:${source.sourceId}` ? "正在入队…" : "授权抓取官方原文"}
                      </button>
                    </>
                  )}
                </div>
              </article>
              );
            })}
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

      <section className={styles.legalPanel} aria-labelledby="official-case-catalog-title">
        <div className={styles.legalPanelHeading}>
          <div><p className={styles.eyebrow}>案例研究层</p><h3 id="official-case-catalog-title">官方真实案例研究线索</h3></div>
          <span>{officialCaseResearchCatalog.candidates.length} 项元数据 · 核验于 {officialCaseResearchCatalog.verifiedOn}</span>
        </div>
        <div className={styles.officialCasePolicy}>
          <div>
            <strong>只用于检索、类案比较和测试题设计</strong>
            <p>当前目录只保存官方页面元数据。案例不会自动成为法源、裁判依据、案件事实或利息结论；律师阅读全文、核对时效与取得案内使用许可前，不能进入正式规则包。</p>
          </div>
          <nav aria-label="官方案例库规则">
            {officialCasePolicyLinks.map((link) => <a href={link.url} key={link.url} rel="noreferrer" target="_blank">{link.label}</a>)}
          </nav>
        </div>
        <div className={styles.officialCaseList}>
          {officialCaseResearchCatalog.candidates.map((candidate, index) => (
            <article className={styles.officialCaseRow} key={candidate.candidateId}>
              <span className={styles.officialCaseIndex}>{String(index + 1).padStart(2, "0")}</span>
              <div className={styles.officialCaseBody}>
                <div className={styles.officialCaseTitleLine}>
                  <strong>{candidate.title}</strong>
                  <span>需律师阅读全文</span>
                </div>
                <div className={styles.officialCaseTags} aria-label="争点标签">
                  {candidate.issueTags.map((tag) => <span key={tag}>{tag}</span>)}
                </div>
                <p>可用于：{candidate.evaluationUses.join("、")}</p>
                <a href={candidate.officialUrl} rel="noreferrer" target="_blank">打开最高人民法院官方页面</a>
              </div>
              <div className={styles.officialCaseState}>
                <strong>仅研究线索</strong>
                <small>{candidate.acquisitionMode}</small>
                <small>未进入规则包</small>
              </div>
            </article>
          ))}
        </div>
      </section>

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
                  const registeredSource = review.sources.find(
                    (source) => source.captureRunId === run.runId,
                  ) ?? null;
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
                        <>
                          <div className={`${styles.legalCapturedReview} ${recordedReview.decision === "REJECT" ? styles.legalCapturedReviewRejected : ""}`}>
                            <strong>{recordedReview.decision === "REJECT" ? "律师已驳回" : registeredSource ? "已登记为正式法源快照" : "律师已批准进入登记步骤"}</strong>
                            <span>{recordedReview.provisionLocator}</span>
                            <code>{registeredSource?.contentSha256 ? `正式内容 ${shortHash(registeredSource.contentSha256)}` : `复核 ${shortHash(recordedReview.reviewHash)}`}</code>
                          </div>
                          {recordedReview.decision === "APPROVE_FOR_REGISTRATION" && !registeredSource && captureReview.status === "persistent" && (
                            <div className={styles.legalRegistrationActions}>
                              <label htmlFor={`license-${run.runId}`}>公开访问与案内使用依据</label>
                              <textarea
                                id={`license-${run.runId}`}
                                onChange={(event) => setLicenseBases((prior) => ({ ...prior, [run.runId]: event.target.value }))}
                                placeholder="记录官方网站公开访问、加密保存范围、律所内部研究/诉讼引用用途及禁止再分发等核验结论"
                                value={licenseBases[run.runId] ?? ""}
                              />
                              <button disabled={captureBusy !== null} onClick={() => registerCapture(run, recordedReview.reviewHash)} type="button">
                                {captureBusy === `register:${run.runId}` ? "正在核验并登记…" : "登记为正式法源快照"}
                              </button>
                              <small>系统会重新解密并核对原字节哈希；登记后仍不会自动建立规则、选取 LPR 或启动计算。</small>
                            </div>
                          )}
                        </>
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
          <span>{review.status === "reviewable" ? "写入动作受案件版本与律师确认约束" : "发现模式不允许写入"}</span>
        </div>
        {review.status === "reviewable" && (
          <form className={styles.legalFactBindingForm} onSubmit={(event) => { event.preventDefault(); void bindLegalFact(); }}>
            <div><strong>建立规则所需事实锚点</strong><small>先在“事实与争点”确认事实；此处只把规则键绑定到同案已确认事实，不生成事实或法律结论。</small></div>
            <label><span>规则事实键</span><input required value={factBinding.factKey} onChange={(event) => setFactBinding((prior) => ({ ...prior, factKey: event.target.value }))} placeholder="例如 contract_before_2020_08_20" /></label>
            <label><span>已确认案件事实</span><select required value={factBinding.factId} onChange={(event) => setFactBinding((prior) => ({ ...prior, factId: event.target.value }))}><option value="">选择同案已确认事实</option>{confirmedFacts.map((fact) => <option key={fact.factId} value={fact.factId}>{fact.text}</option>)}</select></label>
            <label className={styles.legalFactBindingCheck}><input checked={factBinding.approved} onChange={(event) => setFactBinding((prior) => ({ ...prior, approved: event.target.checked }))} type="checkbox" /><span>我确认该事实已被审阅，并且确实是本规则适用所需的同案事实。</span></label>
            <button disabled={bindingBusy || !confirmedFacts.length} type="submit">{bindingBusy ? "正在绑定…" : "建立事实锚点"}</button>
            {bindingNotice && <p role="status">{bindingNotice}</p>}
          </form>
        )}
        {review.status === "reviewable" && (
          <form className={styles.legalEventForm} onSubmit={(event) => { event.preventDefault(); void approveLegalEvent(); }}>
            <div><strong>建立案件法律事件</strong><small>事件日期不从文件时间自动推定；必须选择已纳入本案的证据页。</small></div>
            <label><span>事件类型</span><select value={legalEvent.eventKind} onChange={(event) => setLegalEvent((prior) => ({ ...prior, eventKind: event.target.value as typeof prior.eventKind }))}>{["CONTRACT_SIGNED", "DISBURSEMENT", "PAYMENT", "DEFAULT", "CLAIM_FILED", "CASE_ACCEPTED", "JUDGMENT"].map((item) => <option key={item} value={item}>{eventLabel(item)}</option>)}</select></label>
            <label><span>法律事件日期</span><input required type="date" value={legalEvent.localDate} onChange={(event) => setLegalEvent((prior) => ({ ...prior, localDate: event.target.value }))} /></label>
            <fieldset><legend>已纳入本案的证据页</legend>{includedEvidencePages.length ? includedEvidencePages.map((page) => <label key={page.pageId}><input checked={legalEvent.evidenceIds.includes(page.pageId)} onChange={(event) => setLegalEvent((prior) => ({ ...prior, evidenceIds: event.target.checked ? [...prior.evidenceIds, page.pageId] : prior.evidenceIds.filter((item) => item !== page.pageId) }))} type="checkbox" />{page.originalLabel} · 第 {page.pageNumber} 页</label>) : <small>当前页没有已纳入的证据页；请先在证据核验台完成页面取舍。</small>}</fieldset>
            <label className={styles.legalEventCheck}><input checked={legalEvent.approved} onChange={(event) => setLegalEvent((prior) => ({ ...prior, approved: event.target.checked }))} type="checkbox" /><span>我确认事件日期、类型与所选证据页的关联已经核对。</span></label>
            <button disabled={eventBusy || !includedEvidencePages.length} type="submit">{eventBusy ? "正在建立…" : "批准法律事件"}</button>
            {eventNotice && <p role="status">{eventNotice}</p>}
          </form>
        )}
        {review.status === "reviewable" && (
          <form className={styles.lprRuleForm} onSubmit={(event) => { event.preventDefault(); void approveLprRule(); }}>
            <div className={styles.lprRuleFormHeading}>
              <div><strong>建立 LPR 倍数规则</strong><small>仅用于已完成法源登记的案件；这不是利息结论，也不会启动计算。</small></div>
              <span>基准利率：系统从官方记录读取</span>
            </div>
            <div className={styles.lprRuleFields}>
              <label><span>规则标识</span><input required value={lprRule.ruleId} onChange={(event) => setLprRule((prior) => ({ ...prior, ruleId: event.target.value }))} placeholder="例如 private-lending-lpr-cap" /></label>
              <label><span>规则版本</span><input required value={lprRule.ruleVersion} onChange={(event) => setLprRule((prior) => ({ ...prior, ruleVersion: event.target.value }))} placeholder="例如 PRIVATE-LENDING-LPR-2020-08" /></label>
              <label><span>争点标识</span><input required value={lprRule.issueKey} onChange={(event) => setLprRule((prior) => ({ ...prior, issueKey: event.target.value }))} placeholder="例如 interest_cap_after_2020_08_20" /></label>
              <label><span>法律公式依据</span><select required value={lprRule.sourceSnapshotId} onChange={(event) => setLprRule((prior) => ({ ...prior, sourceSnapshotId: event.target.value }))}><option value="">选择已核验法律/司法解释快照</option>{legalFormulaSources.map((source) => <option key={source.snapshotId} value={source.snapshotId!}>{source.publisher} · {source.provisionLocator}</option>)}</select></label>
              <label><span>官方 LPR 数据快照</span><select required value={lprRule.parameterSourceSnapshotId} onChange={(event) => setLprRule((prior) => ({ ...prior, parameterSourceSnapshotId: event.target.value }))}><option value="">选择已登记中国货币网快照</option>{lprParameterSources.map((source) => <option key={source.snapshotId} value={source.snapshotId!}>{source.publisher} · {shortHash(source.contentSha256 ?? "")}</option>)}</select></label>
              <label><span>官方记录精确定位</span><input required value={lprRule.parameterEvidenceLocator} onChange={(event) => setLprRule((prior) => ({ ...prior, parameterEvidenceLocator: event.target.value }))} placeholder="例如 records[0]；必须与该快照匹配" /></label>
              <label><span>生效起日</span><input required type="date" value={lprRule.effectiveFrom} onChange={(event) => setLprRule((prior) => ({ ...prior, effectiveFrom: event.target.value }))} /></label>
              <label><span>生效止日（可空）</span><input type="date" value={lprRule.effectiveTo} onChange={(event) => setLprRule((prior) => ({ ...prior, effectiveTo: event.target.value }))} /></label>
              <label><span>适用触发事件</span><select value={lprRule.triggerEventKind} onChange={(event) => setLprRule((prior) => ({ ...prior, triggerEventKind: event.target.value as typeof prior.triggerEventKind }))}>{["CONTRACT_SIGNED", "DISBURSEMENT", "PAYMENT", "DEFAULT", "CLAIM_FILED", "CASE_ACCEPTED", "JUDGMENT"].map((item) => <option key={item} value={item}>{eventLabel(item)}</option>)}</select></label>
              <label><span>LPR 倍数</span><input required inputMode="decimal" value={lprRule.rateMultiplier} onChange={(event) => setLprRule((prior) => ({ ...prior, rateMultiplier: event.target.value }))} /><small>只输入倍数；没有基准利率输入框。</small></label>
              <label><span>冲突集合（可空）</span><input value={lprRule.conflictSet} onChange={(event) => setLprRule((prior) => ({ ...prior, conflictSet: event.target.value }))} placeholder="例如 private-lending-interest-cap" /></label>
              <label><span>优先级</span><input required inputMode="numeric" value={lprRule.priority} onChange={(event) => setLprRule((prior) => ({ ...prior, priority: event.target.value }))} /></label>
            </div>
            <fieldset className={styles.lprFactKeys}>
              <legend>规则所需事实锚点</legend>
              {availableFactKeys.length ? availableFactKeys.map((factKey) => <label key={factKey}><input type="checkbox" checked={lprRule.requiredFactKeys.includes(factKey)} onChange={(event) => setLprRule((prior) => ({ ...prior, requiredFactKeys: event.target.checked ? [...prior.requiredFactKeys, factKey] : prior.requiredFactKeys.filter((item) => item !== factKey) }))} />{factKey}</label>) : <small>没有已批准事实锚点，不能建立正式规则。</small>}
            </fieldset>
            <label className={styles.lprApprovalCheck}><input checked={ruleApproved} onChange={(event) => setRuleApproved(event.target.checked)} type="checkbox" /><span>我确认：本次只批准规则结构、法律依据快照、官方 LPR 快照、定位与倍数；系统将从认证的官方观察记录读取一年期 LPR，且不接受人工利率。</span></label>
            <div className={styles.lprRuleActions}><button disabled={ruleBusy || !legalFormulaSources.length || !lprParameterSources.length || !availableFactKeys.length} type="submit">{ruleBusy ? "正在提交规则审批…" : "批准 LPR 规则"}</button><small>任一快照、许可、定位、事实锚点或案件版本不匹配，服务端会拒绝写入。</small></div>
            {ruleNotice && <p className={styles.lprRuleNotice} role="status">{ruleNotice}</p>}
          </form>
        )}
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

function officialCaptureTargets(sourceId: string, defaultUrl: string) {
  const source = officialSourceCatalog.sources.find((item) => item.source_id === sourceId);
  if (!source) return [{ label: "登记官方原文", url: defaultUrl }];
  if ("official_data_api" in source && source.official_data_api) {
    return [{ label: "官方历史数据接口", url: source.official_data_api }];
  }
  const parserCompatibleFallbacks = new Set([
    "CN-CIVIL-CODE-680",
    "SPC-PRIVATE-LENDING-2020-SECOND-REVISION",
  ]);
  return [
    { label: "登记官方原文", url: defaultUrl },
    ...(
      parserCompatibleFallbacks.has(sourceId) && "fallback_official_url" in source && source.fallback_official_url
        ? [{ label: "已登记官方备用页", url: source.fallback_official_url }]
        : []
    ),
  ];
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
