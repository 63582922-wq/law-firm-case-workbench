"use client";

import { useEffect, useState } from "react";
import {
  caseDataSourceConfig,
  approveLegalFactBinding,
  approveCaseLegalEvent,
  approveCaseLegalBundle,
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
import { readDesktopRuntimeStatus } from "@/lib/desktop-bridge";
import type { DesktopRuntimeStatus } from "@/lib/desktop-bridge";
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
  const [desktopRuntime, setDesktopRuntime] = useState<DesktopRuntimeStatus | null>(null);
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
  const [bundleSegments, setBundleSegments] = useState([newBundleSegment()]);
  const [bundleApproved, setBundleApproved] = useState(false);
  const [bundleBusy, setBundleBusy] = useState(false);
  const [bundleNotice, setBundleNotice] = useState<string | null>(null);
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
        if (active) setError(reason instanceof Error ? reason.message : "暂时无法读取本案适用依据");
      });
    loadOfficialSourceCaptureReview()
      .then((result) => {
        if (active) setCaptureReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setCaptureError(reason instanceof Error ? reason.message : "暂时无法读取官方原文核验记录");
      });
    loadCaseReview()
      .then((result) => {
        if (active) setCaseReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setBindingNotice(reason instanceof Error ? reason.message : "暂时无法读取已确认案件事实");
      });
    loadEvidenceReview()
      .then((result) => {
        if (active) setEvidenceReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setEventNotice(reason instanceof Error ? reason.message : "暂时无法读取本案证据页");
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    readDesktopRuntimeStatus()
      .then((status) => {
        if (active) setDesktopRuntime(status);
      })
      .catch(() => {
        if (active) setDesktopRuntime(null);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!captureReview?.runs.some((run) => run.status === "QUEUED" || run.status === "RUNNING")) return;
    let active = true;
    const timer = window.setInterval(() => {
      loadOfficialSourceCaptureReview()
        .then((refreshed) => {
          if (!active) return;
          setCaptureReview(refreshed);
          setCaptureError(null);
        })
        .catch(() => {
          // Preserve the last durable queue view.  A transient local-worker
          // restart must never make a capture appear to have completed.
        });
    }, 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [captureReview]);

  if (error) {
    return (
      <section className={styles.legalArea} aria-label="利息适用依据">
        <div className={styles.calculationBlocked} role="alert">
          <p className={styles.eyebrow}>暂不能核对适用依据</p>
          <h3>{caseDataSourceConfig.kind === "persistent-disabled" ? "案件资料库尚未启用" : "暂时无法读取本案适用依据"}</h3>
          <p>请检查桌面工作台是否仍在运行、当前案件是否已打开，然后重新打开本页。</p>
          <small>本案依据未就绪时，系统不会以演示规则或未经核对的利率代替。</small>
          <button className={styles.candidateAction} onClick={() => window.location.reload()} type="button">重新载入本页</button>
        </div>
      </section>
    );
  }
  if (!review) {
    return <section className={styles.legalArea}><div className={styles.calculationLoading}>正在读取本案适用依据、关键日期和利率资料…</div></section>;
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
      setCaptureNotice("请先确认本次仅访问公开官方网站，且不发送案件材料。");
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
      setCaptureNotice(
        desktopRuntime?.officialSourceCaptureWorkerPhase === "ASSEMBLED"
          ? `官方原文已加入本次核验；案件版本更新为 ${receipt.matterVersion}。本机保存服务已就绪，状态会自动刷新。`
          : `官方原文已加入本次核验；案件版本更新为 ${receipt.matterVersion}。当前机器的保存服务尚未就绪，本次任务会保持等待。`,
      );
    } catch (reason: unknown) {
      setCaptureNotice(reason instanceof Error ? reason.message : "官方原文核验任务未建立");
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
      setCaptureNotice(`${decision === "REJECT" ? "驳回" : "待登记"}结论已保存；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setCaptureNotice(reason instanceof Error ? reason.message : "官方原文复核未保存");
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
      setCaptureNotice(`已保存为本案可用的官方原文；案件版本更新为 ${receipt.matterVersion}。适用口径和利率仍须在下一步由律师确认。`);
    } catch (reason: unknown) {
      setCaptureNotice(reason instanceof Error ? reason.message : "官方原文未登记到本案");
    } finally {
      setCaptureBusy(null);
    }
  }

  async function approveLprRule() {
    if (!review || review.status !== "reviewable" || review.matterVersion === null) return;
    if (!ruleApproved) {
      setRuleNotice("请先确认：基准利率仅从已核验的官方记录读取，不能手工填入或修改。");
      return;
    }
    const priority = Number(lprRule.priority);
    if (!Number.isInteger(priority) || priority < 0 || priority > 1_000_000) {
      setRuleNotice("口径优先级必须是 0 至 1000000 的整数。");
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
      setRuleNotice(`LPR 利息口径已确认；案件版本更新为 ${receipt.matterVersion}。基准值仍只来自官方记录。`);
      setRuleApproved(false);
    } catch (reason: unknown) {
      setRuleNotice(reason instanceof Error ? reason.message : "LPR 利息口径未确认");
    } finally {
      setRuleBusy(false);
    }
  }

  async function bindLegalFact() {
    if (!review || review.status !== "reviewable" || review.matterVersion === null) return;
    if (!factBinding.approved) {
      setBindingNotice("请先确认：只有本案已确认的事实才可作为适用依据的前提。");
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
      setBindingNotice(`适用依据所需事实已关联；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setBindingNotice(reason instanceof Error ? reason.message : "适用依据所需事实未关联");
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
      setEventNotice(`关键日期及对应证据已确认；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setEventNotice(reason instanceof Error ? reason.message : "关键日期及对应证据未确认");
    } finally {
      setEventBusy(false);
    }
  }

  function updateBundleSegment(segmentId: string, patch: Partial<BundleSegmentDraft>) {
    setBundleSegments((prior) => prior.map((segment) => segment.segmentId === segmentId ? { ...segment, ...patch } : segment));
  }

  async function approveRuleBundle() {
    if (!review || review.status !== "reviewable" || review.matterVersion === null) return;
    if (!bundleApproved) {
      setBundleNotice("请先确认：各适用期间连续无空档，每段依据与关键日期相符，适用说明已完成法律核对。");
      return;
    }
    setBundleBusy(true);
    setBundleNotice(null);
    try {
      const receipt = await approveCaseLegalBundle({
        expectedVersion: review.matterVersion,
        segments: bundleSegments,
      });
      const refreshed = await loadLegalReview();
      setReview(refreshed);
      setBundleApproved(false);
      setBundleNotice(`本案利息适用口径已确认；案件版本更新为 ${receipt.matterVersion}。资料变化后，原测算将不能继续作为本案依据。`);
    } catch (reason: unknown) {
      setBundleNotice(reason instanceof Error ? reason.message : "本案利息适用口径未确认");
    } finally {
      setBundleBusy(false);
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
  const approvedRuleVersions = review.ruleVersions.filter((rule) => rule.status === "APPROVED");
  const approvedLegalEvents = review.legalEvents.filter((event) => event.status === "APPROVED");

  return (
    <section className={styles.legalArea} aria-label="利息适用依据">
      <header className={styles.calculationHeading}>
        <div>
          <p className={styles.eyebrow}>利息争点依据</p>
          <h2>核对本案适用依据、关键日期和利率口径</h2>
          <p>先核对官方原文和本案事实，再由律师确认适用期间与利率口径；系统不会自行作出法律结论。</p>
        </div>
        <div className={styles.calculationStatus}>
          <span>{review.sourceLabel}</span>
          <strong>{review.status === "reviewable" ? `案件版本 ${review.matterVersion}` : "待建立本案依据"}</strong>
          <small>{review.snapshotHash ? `已保存核对记录 ${shortHash(review.snapshotHash)}` : "尚未形成本案依据"}</small>
        </div>
      </header>

      <div className={review.status === "discovery-only" ? styles.legalDiscoveryNotice : styles.legalReviewNotice}>
        <strong>{review.status === "discovery-only" ? "尚未建立本案适用依据" : "当前依据仅供核对"}</strong>
        <span>{review.statusReason}</span>
      </div>

      <div className={styles.legalSummary}>
        <SummaryCell label="官方依据" value={`${review.sources.length} 项`} note={`${readySources} 项已核验可用`} />
        <SummaryCell label="利息口径" value={`${review.ruleVersions.length} 项`} note="利率不在本页手工录入" />
        <SummaryCell label="关键事实" value={`${approvedBindings} 项`} note="仅关联已确认事实" />
        <SummaryCell label="本案适用期间" value={review.currentBundle ? `v${review.currentBundle.version}` : "未确认"} note={review.bundleSegments.length ? `${review.bundleSegments.length} 个连续期间` : "暂不能开始利息测算"} />
      </div>

      <div className={styles.legalGrid}>
        <section className={styles.legalPanel} aria-labelledby="official-sources-title">
          <div className={styles.legalPanelHeading}>
            <div><p className={styles.eyebrow}>第一步</p><h3 id="official-sources-title">核对官方依据与利率资料</h3></div>
            <span>{readySources}/{review.sources.length} 项可用于本案口径</span>
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
                    {source.verificationStatus !== "VERIFIED" ? "待保存并核对" : source.licenseBasis && source.licenseReviewHash ? "原文与使用依据已核对" : "使用依据待补充"}
                  </span>
                  <small>{source.contentSha256 ? `核对编号 ${shortHash(source.contentSha256)}` : "尚未保存原文"}</small>
                  {captureReview?.status === "persistent" && source.verificationStatus !== "VERIFIED" && (
                    <>
                      {candidates.length > 1 && (
                        <label className={styles.legalCaptureTarget}>
                          <span>本次官方来源</span>
                          <select
                            aria-label={`${source.publisher}的本次原文来源`}
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
                        {captureBusy === `queue:${source.sourceId}` ? "正在保存…" : "保存官方原文供复核"}
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
          <p className={styles.eyebrow}>办案核对清单</p>
          <h3>开始利息测算前，请完成以下核对</h3>
          <ol>
            <li>官方网页或 PDF 原始字节已加密保存并绑定 SHA-256。</li>
            <li>具体条文位置、现行效力和本案使用依据已经核对。</li>
            <li>合同成立、起诉、受理、付款等关键日期均有本案证据支持。</li>
            <li>适用口径所需事实均已关联到本案已确认事实。</li>
            <li>各连续期间的适用口径均已确认，测算复核一致。</li>
          </ol>
          <p>案件事实、证据、适用依据或来源发生变化后，本次测算和提交材料都需要重新核对。</p>
        </aside>
      </div>

      <section className={styles.legalPanel} aria-labelledby="official-case-catalog-title">
        <div className={styles.legalPanelHeading}>
          <div><p className={styles.eyebrow}>类案参考</p><h3 id="official-case-catalog-title">可供律师研判的官方类案</h3></div>
          <span>{officialCaseResearchCatalog.candidates.length} 项索引 · 核验于 {officialCaseResearchCatalog.verifiedOn}</span>
        </div>
        <div className={styles.officialCasePolicy}>
          <div>
            <strong>用于检索和类案比较，不替代本案法律判断</strong>
            <p>这里仅保存官方页面索引。类案不会自动成为本案依据、案件事实或利息结论；律师须阅读全文、核对现行效力和本案使用条件后，方可作为研判参考。</p>
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
                  <span>需律师阅读全文并结合本案判断</span>
                </div>
                <div className={styles.officialCaseTags} aria-label="争点标签">
                  {candidate.issueTags.map((tag) => <span key={tag}>{tag}</span>)}
                </div>
                <p>可用于：{candidate.evaluationUses.join("、")}</p>
                <a href={candidate.officialUrl} rel="noreferrer" target="_blank">打开最高人民法院官方页面</a>
              </div>
              <div className={styles.officialCaseState}>
                <strong>供律师研判</strong>
                <small>{candidate.acquisitionMode}</small>
                <small>未作为本案依据</small>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className={styles.legalPanel} aria-labelledby="official-capture-title">
        <div className={styles.legalPanelHeading}>
          <div><p className={styles.eyebrow}>第二步</p><h3 id="official-capture-title">保存官方原文并完成律师复核</h3></div>
          <span>{captureReview ? `${captureReview.sourceLabel} · ${desktopRuntime?.officialSourceCaptureWorkerPhase === "ASSEMBLED" ? "本机保存服务已就绪" : "本机保存服务未就绪"}` : "正在读取本次原文核验状态"}</span>
        </div>
        {captureError ? (
          <div className={styles.legalCaptureBlocked} role="alert">
            <strong>官方原文保存服务未连接</strong>
            <span>{captureError}</span>
            <small>仍可查看已保存资料；系统不会把测试结果或网页链接当作本案已核对的官方原文。</small>
          </div>
        ) : !captureReview ? (
          <div className={styles.legalEmpty}>正在读取本次官方原文、核对记录和律师复核状态…</div>
        ) : (
          <>
            <div className={captureReview.status === "probe-only" ? styles.legalCaptureProbe : styles.legalCapturePersistent}>
              <strong>{captureReview.status === "probe-only" ? "演示资料，不可用于本案" : `案件版本 ${captureReview.matterVersion}`}</strong>
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
                <span><strong>我确认本次仅保存页面列明的公开官方网站</strong><small>请求不会携带案卷、当事人信息、浏览器登录信息、模型密钥或律所账号；本次授权仅尝试一次。</small></span>
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
                        <a href={run.targetUrl} rel="noreferrer" target="_blank">查看本次保存的官方原文</a>
                        <span>保存尝试 {run.attemptCount} 次</span>
                        <span>{run.contentMediaType ?? "尚无响应媒体类型"}</span>
                        <span>{run.contentBytes ? formatBytes(run.contentBytes) : "尚未保存原文"}</span>
                      </div>
                      <div className={styles.legalCaptureHashes}>
                        <code>原文核对编号 {run.contentSha256 ? shortHash(run.contentSha256) : "—"}</code>
                        <code>整理记录 {run.parsedOutputHash ? shortHash(run.parsedOutputHash) : "—"}</code>
                        <code>保存回执 {run.captureVerificationHash ? shortHash(run.captureVerificationHash) : "—"}</code>
                      </div>
                      {run.parsedSummary && <ParsedSummary summary={run.parsedSummary} />}
                      {(run.failureCode || run.staleReason) && (
                        <p className={styles.legalCaptureFailure}>{run.failureCode ? `${run.failureCode}：` : ""}{run.staleReason}</p>
                      )}
                      {recordedReview ? (
                        <>
                          <div className={`${styles.legalCapturedReview} ${recordedReview.decision === "REJECT" ? styles.legalCapturedReviewRejected : ""}`}>
                            <strong>{recordedReview.decision === "REJECT" ? "律师未采纳本次原文" : registeredSource ? "已保存为本案可用官方原文" : "律师已同意保存到本案"}</strong>
                            <span>{recordedReview.provisionLocator}</span>
                            <code>{registeredSource?.contentSha256 ? `原文核对编号 ${shortHash(registeredSource.contentSha256)}` : `复核记录 ${shortHash(recordedReview.reviewHash)}`}</code>
                          </div>
                          {recordedReview.decision === "APPROVE_FOR_REGISTRATION" && !registeredSource && captureReview.status === "persistent" && (
                            <div className={styles.legalRegistrationActions}>
                              <label htmlFor={`license-${run.runId}`}>本案保存和使用依据</label>
                              <textarea
                                id={`license-${run.runId}`}
                                onChange={(event) => setLicenseBases((prior) => ({ ...prior, [run.runId]: event.target.value }))}
                                placeholder="记录公开访问、保存范围、本案研判或诉讼引用用途及禁止再分发等核对结论"
                                value={licenseBases[run.runId] ?? ""}
                              />
                              <button disabled={captureBusy !== null} onClick={() => registerCapture(run, recordedReview.reviewHash)} type="button">
                                {captureBusy === `register:${run.runId}` ? "正在核对并保存…" : "保存为本案官方原文"}
                              </button>
                              <small>系统会再次核对原文；保存后仍不会自动确定适用口径、选取 LPR 或开始利息测算。</small>
                            </div>
                          )}
                        </>
                      ) : run.status === "REVIEW_REQUIRED" && captureReview.status === "persistent" ? (
                        <div className={styles.legalReviewActions}>
                          <label htmlFor={`locator-${run.runId}`}>本案使用的条文或数据位置</label>
                          <input
                            id={`locator-${run.runId}`}
                            onChange={(event) => setProvisionLocators((prior) => ({ ...prior, [run.runId]: event.target.value }))}
                            placeholder="例如：第二十五条、第三十一条；或 LPR records[0]"
                            value={provisionLocators[run.runId] ?? ""}
                          />
                          <div>
                            <button disabled={captureBusy !== null} onClick={() => recordCaptureReview(run, "REJECT")} type="button">不采纳本次原文</button>
                            <button disabled={captureBusy !== null} onClick={() => recordCaptureReview(run, "APPROVE_FOR_REGISTRATION")} type="button">确认可保存到本案</button>
                          </div>
                          <small>确认只保存律师复核结论，不会自动确定适用口径、利率或启动利息测算。</small>
                        </div>
                      ) : null}
                    </article>
                  );
                })}
              </div>
            ) : <div className={styles.legalEmpty}>尚未保存官方原文。先确认公开网络访问范围，再从上方清单选择需要核对的具体原文。</div>}

            {captureNotice && <div className={styles.legalCaptureNotice} role="status">{captureNotice}</div>}
          </>
        )}
      </section>

      <section className={styles.legalPanel} aria-labelledby="rule-versions-title">
        <div className={styles.legalPanelHeading}>
          <div><p className={styles.eyebrow}>第三步</p><h3 id="rule-versions-title">确认利息适用口径与本案事实</h3></div>
          <span>{review.status === "reviewable" ? "所有确认均需对应当前案件资料和律师判断" : "请先建立本案适用依据"}</span>
        </div>
        {review.status === "reviewable" && (
          <form className={styles.legalFactBindingForm} onSubmit={(event) => { event.preventDefault(); void bindLegalFact(); }}>
            <div><strong>关联适用依据所需事实</strong><small>先在“案件要点”确认事实；这里仅将适用条件关联到本案事实，不生成事实或法律结论。</small></div>
            <label><span>适用条件</span><input required value={factBinding.factKey} onChange={(event) => setFactBinding((prior) => ({ ...prior, factKey: event.target.value }))} placeholder="例如：合同订立于 2020年8月20日前" /></label>
            <label><span>已确认案件事实</span><select required value={factBinding.factId} onChange={(event) => setFactBinding((prior) => ({ ...prior, factId: event.target.value }))}><option value="">选择同案已确认事实</option>{confirmedFacts.map((fact) => <option key={fact.factId} value={fact.factId}>{fact.text}</option>)}</select></label>
            <label className={styles.legalFactBindingCheck}><input checked={factBinding.approved} onChange={(event) => setFactBinding((prior) => ({ ...prior, approved: event.target.checked }))} type="checkbox" /><span>我确认该事实已审阅，且确实是本案适用此项依据所需的事实。</span></label>
            <button disabled={bindingBusy || !confirmedFacts.length} type="submit">{bindingBusy ? "正在关联…" : "关联本案事实"}</button>
            {bindingNotice && <p role="status">{bindingNotice}</p>}
          </form>
        )}
        {review.status === "reviewable" && (
          <form className={styles.legalEventForm} onSubmit={(event) => { event.preventDefault(); void approveLegalEvent(); }}>
            <div><strong>确认关键日期及对应证据</strong><small>日期不会从文件时间自动推定；必须选择已纳入本案的证据页。</small></div>
            <label><span>事件类型</span><select value={legalEvent.eventKind} onChange={(event) => setLegalEvent((prior) => ({ ...prior, eventKind: event.target.value as typeof prior.eventKind }))}>{["CONTRACT_SIGNED", "DISBURSEMENT", "PAYMENT", "DEFAULT", "CLAIM_FILED", "CASE_ACCEPTED", "JUDGMENT"].map((item) => <option key={item} value={item}>{eventLabel(item)}</option>)}</select></label>
            <label><span>关键日期</span><input required type="date" value={legalEvent.localDate} onChange={(event) => setLegalEvent((prior) => ({ ...prior, localDate: event.target.value }))} /></label>
            <fieldset><legend>已纳入本案的证据页</legend>{includedEvidencePages.length ? includedEvidencePages.map((page) => <label key={page.pageId}><input checked={legalEvent.evidenceIds.includes(page.pageId)} onChange={(event) => setLegalEvent((prior) => ({ ...prior, evidenceIds: event.target.checked ? [...prior.evidenceIds, page.pageId] : prior.evidenceIds.filter((item) => item !== page.pageId) }))} type="checkbox" />{page.originalLabel} · 第 {page.pageNumber} 页</label>) : <small>当前页没有已纳入的证据页；请先在证据核验台完成页面取舍。</small>}</fieldset>
            <label className={styles.legalEventCheck}><input checked={legalEvent.approved} onChange={(event) => setLegalEvent((prior) => ({ ...prior, approved: event.target.checked }))} type="checkbox" /><span>我确认事件日期、类型与所选证据页的关联已经核对。</span></label>
            <button disabled={eventBusy || !includedEvidencePages.length} type="submit">{eventBusy ? "正在确认…" : "确认关键日期"}</button>
            {eventNotice && <p role="status">{eventNotice}</p>}
          </form>
        )}
        {review.status === "reviewable" && (
          <form className={styles.lprRuleForm} onSubmit={(event) => { event.preventDefault(); void approveLprRule(); }}>
            <div className={styles.lprRuleFormHeading}>
              <div><strong>确认 LPR 利息口径</strong><small>仅用于已完成官方原文核对的案件；这不是利息结论，也不会自动开始测算。</small></div>
              <span>基准利率仅从官方记录读取</span>
            </div>
            <div className={styles.lprRuleFields}>
              <label><span>口径编号</span><input required value={lprRule.ruleId} onChange={(event) => setLprRule((prior) => ({ ...prior, ruleId: event.target.value }))} placeholder="例如：民间借贷 LPR 上限" /></label>
              <label><span>口径版本</span><input required value={lprRule.ruleVersion} onChange={(event) => setLprRule((prior) => ({ ...prior, ruleVersion: event.target.value }))} placeholder="例如：2020年8月起适用" /></label>
              <label><span>对应争点</span><input required value={lprRule.issueKey} onChange={(event) => setLprRule((prior) => ({ ...prior, issueKey: event.target.value }))} placeholder="例如：2020年8月20日后利息上限" /></label>
              <label><span>法律依据</span><select required value={lprRule.sourceSnapshotId} onChange={(event) => setLprRule((prior) => ({ ...prior, sourceSnapshotId: event.target.value }))}><option value="">选择已核对的法律或司法解释</option>{legalFormulaSources.map((source) => <option key={source.snapshotId} value={source.snapshotId!}>{source.publisher} · {source.provisionLocator}</option>)}</select></label>
              <label><span>官方 LPR 资料</span><select required value={lprRule.parameterSourceSnapshotId} onChange={(event) => setLprRule((prior) => ({ ...prior, parameterSourceSnapshotId: event.target.value }))}><option value="">选择已保存的中国货币网资料</option>{lprParameterSources.map((source) => <option key={source.snapshotId} value={source.snapshotId!}>{source.publisher} · {shortHash(source.contentSha256 ?? "")}</option>)}</select></label>
              <label><span>官方记录位置</span><input required value={lprRule.parameterEvidenceLocator} onChange={(event) => setLprRule((prior) => ({ ...prior, parameterEvidenceLocator: event.target.value }))} placeholder="例如：对应期间的一年期 LPR 记录" /></label>
              <label><span>生效起日</span><input required type="date" value={lprRule.effectiveFrom} onChange={(event) => setLprRule((prior) => ({ ...prior, effectiveFrom: event.target.value }))} /></label>
              <label><span>生效止日（可空）</span><input type="date" value={lprRule.effectiveTo} onChange={(event) => setLprRule((prior) => ({ ...prior, effectiveTo: event.target.value }))} /></label>
              <label><span>适用起点</span><select value={lprRule.triggerEventKind} onChange={(event) => setLprRule((prior) => ({ ...prior, triggerEventKind: event.target.value as typeof prior.triggerEventKind }))}>{["CONTRACT_SIGNED", "DISBURSEMENT", "PAYMENT", "DEFAULT", "CLAIM_FILED", "CASE_ACCEPTED", "JUDGMENT"].map((item) => <option key={item} value={item}>{eventLabel(item)}</option>)}</select></label>
              <label><span>LPR 倍数</span><input required inputMode="decimal" value={lprRule.rateMultiplier} onChange={(event) => setLprRule((prior) => ({ ...prior, rateMultiplier: event.target.value }))} /><small>只输入倍数；没有基准利率输入框。</small></label>
              <label><span>不并用口径组（可空）</span><input value={lprRule.conflictSet} onChange={(event) => setLprRule((prior) => ({ ...prior, conflictSet: event.target.value }))} placeholder="例如：民间借贷利息上限" /></label>
              <label><span>适用顺序</span><input required inputMode="numeric" value={lprRule.priority} onChange={(event) => setLprRule((prior) => ({ ...prior, priority: event.target.value }))} /></label>
            </div>
            <fieldset className={styles.lprFactKeys}>
              <legend>本项口径需要的已确认事实</legend>
              {availableFactKeys.length ? availableFactKeys.map((factKey) => <label key={factKey}><input type="checkbox" checked={lprRule.requiredFactKeys.includes(factKey)} onChange={(event) => setLprRule((prior) => ({ ...prior, requiredFactKeys: event.target.checked ? [...prior.requiredFactKeys, factKey] : prior.requiredFactKeys.filter((item) => item !== factKey) }))} />{factKey}</label>) : <small>尚未关联适用所需事实，不能确认本项口径。</small>}
            </fieldset>
            <label className={styles.lprApprovalCheck}><input checked={ruleApproved} onChange={(event) => setRuleApproved(event.target.checked)} type="checkbox" /><span>我确认：本次仅确认利息口径、法律依据、官方 LPR 资料、记录位置和倍数；系统从已核验官方记录读取一年期 LPR，不接受人工利率。</span></label>
            <div className={styles.lprRuleActions}><button disabled={ruleBusy || !legalFormulaSources.length || !lprParameterSources.length || !availableFactKeys.length} type="submit">{ruleBusy ? "正在确认口径…" : "确认 LPR 利息口径"}</button><small>官方原文、使用依据、记录位置、关联事实或案件资料不一致时，系统不会保存本项口径。</small></div>
            {ruleNotice && <p className={styles.lprRuleNotice} role="status">{ruleNotice}</p>}
          </form>
        )}
        {review.ruleVersions.length ? (
          <div className={styles.legalRuleTable} role="table" aria-label="利息适用口径">
            <div className={`${styles.legalRuleRow} ${styles.legalRuleHead}`} role="row"><span>争点 / 口径</span><span>适用起点</span><span>计算方式</span><span>年利率</span><span>所需事实</span></div>
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
        ) : <div className={styles.legalEmpty}>当前没有可确认的利息口径。仅发现到的网页或类案不会被当作本案依据。</div>}

        <div className={styles.legalAnchors}>
          <div><strong>已确认关键日期</strong><span>{review.legalEvents.length} 项</span><small>{review.legalEvents.map((item) => `${eventLabel(item.eventKind)} ${item.localDate}`).join("；") || "尚未建立"}</small></div>
          <div><strong>已关联关键事实</strong><span>{approvedBindings} 项</span><small>{review.factBindings.filter((item) => item.status === "APPROVED").map((item) => item.factKey).join("；") || "尚未建立"}</small></div>
        </div>
      </section>

      <section className={styles.legalPanel} aria-labelledby="legal-bundle-title">
        <div className={styles.legalPanelHeading}>
          <div><p className={styles.eyebrow}>第四步</p><h3 id="legal-bundle-title">确认本案利息适用期间</h3></div>
          <span>{review.currentBundle ? `核对编号 ${shortHash(review.currentBundle.bundleHash)}` : "尚未确认"}</span>
        </div>
        {review.status === "reviewable" && (
          <form className={styles.legalBundleForm} onSubmit={(event) => { event.preventDefault(); void approveRuleBundle(); }}>
            <div className={styles.legalBundleFormHeading}><div><strong>确认连续适用期间</strong><small>每一期间选择已确认口径及其对应的关键日期；系统会再次核对口径效力、关联事实、原文使用依据和期间连续性。</small></div><button disabled={bundleBusy || !approvedRuleVersions.length || !approvedLegalEvents.length} type="button" onClick={() => setBundleSegments((prior) => [...prior, newBundleSegment()])}>添加适用期间</button></div>
            <div className={styles.legalBundleSegments}>
              {bundleSegments.map((segment, index) => (
                <article key={segment.segmentId}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <label><span>利息口径</span><select required value={segment.ruleVersionId} onChange={(event) => { const rule = approvedRuleVersions.find((item) => item.ruleVersionId === event.target.value); updateBundleSegment(segment.segmentId, { ruleVersionId: event.target.value, issueKey: rule?.issueKey ?? "" }); }}><option value="">选择已确认口径</option>{approvedRuleVersions.map((rule) => <option key={rule.ruleVersionId} value={rule.ruleVersionId}>{rule.issueKey} · {rule.ruleVersion}</option>)}</select></label>
                  <label><span>对应关键日期</span><select required value={segment.triggerEventId} onChange={(event) => updateBundleSegment(segment.segmentId, { triggerEventId: event.target.value })}><option value="">选择已确认日期</option>{approvedLegalEvents.map((legalEvent) => <option key={legalEvent.legalEventId} value={legalEvent.legalEventId}>{eventLabel(legalEvent.eventKind)} · {legalEvent.localDate}</option>)}</select></label>
                  <label><span>开始 / 结束</span><div><input required type="date" value={segment.startDate} onChange={(event) => updateBundleSegment(segment.segmentId, { startDate: event.target.value })} /><input required type="date" value={segment.endDate} onChange={(event) => updateBundleSegment(segment.segmentId, { endDate: event.target.value })} /></div></label>
                  <label><span>适用说明</span><input required value={segment.applicabilityAnchor} onChange={(event) => updateBundleSegment(segment.segmentId, { applicabilityAnchor: event.target.value })} placeholder="例如：起诉时的司法保护标准" /></label>
                  <button aria-label={`移除第 ${index + 1} 个适用期间`} disabled={bundleBusy || bundleSegments.length === 1} type="button" onClick={() => setBundleSegments((prior) => prior.filter((item) => item.segmentId !== segment.segmentId))}>移除</button>
                </article>
              ))}
            </div>
            <label className={styles.legalBundleCheck}><input checked={bundleApproved} onChange={(event) => setBundleApproved(event.target.checked)} type="checkbox" /><span>我确认每一期间连续无空档、关键日期与利息口径相符，且已核对相应法律依据、本案事实和官方利率资料。</span></label>
            <div className={styles.legalBundleActions}><button disabled={bundleBusy || !approvedRuleVersions.length || !approvedLegalEvents.length} type="submit">{bundleBusy ? "正在确认适用期间…" : "确认本案适用期间"}</button><small>确认后，如案件资料或利息口径发生变化，原测算不能继续作为当前提交依据。</small></div>
            {bundleNotice && <p className={styles.legalBundleNotice} role="status">{bundleNotice}</p>}
          </form>
        )}
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
        ) : <div className={styles.legalEmpty}>尚无经律师确认的连续适用期间，暂不能开始正式利息测算。</div>}
      </section>
    </section>
  );
}

type BundleSegmentDraft = {
  segmentId: string;
  issueKey: string;
  ruleVersionId: string;
  triggerEventId: string;
  startDate: string;
  endDate: string;
  applicabilityAnchor: string;
};

function newBundleSegment(): BundleSegmentDraft {
  return {
    segmentId: crypto.randomUUID(),
    issueKey: "",
    ruleVersionId: "",
    triggerEventId: "",
    startDate: "",
    endDate: "",
    applicabilityAnchor: "",
  };
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
    QUEUED: "等待保存原文",
    RUNNING: "正在保存原文",
    REVIEW_REQUIRED: "待律师复核",
    FAILED: "原文保存失败",
    STALE: "已失效",
    PROBE_CAPTURE_AND_PARSE_OK: "演示验证已完成",
    PROBE_FAILED: "演示验证未完成",
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
  if (typeof value === "object") return "已整理的原文记录（详情待提供）";
  return String(value).slice(0, 240);
}

function formatBytes(value: number) {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
