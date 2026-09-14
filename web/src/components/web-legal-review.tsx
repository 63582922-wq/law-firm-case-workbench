"use client";

import { useEffect, useState, type FormEvent } from "react";
import { listWebEvidencePages, type WebEvidencePage } from "@/lib/web-evidence-api";
import { approveWebCurrentLegalBundle, confirmWebLegalEvent, isWebLoginRequired, queueWebOfficialSourceCapture, readWebLegalReview, readWebOfficialSourceCaptures, registerWebOfficialSourceCapture, reviewWebOfficialSourceCapture, type WebLegalReview as WebLegalReviewData, type WebOfficialSourceCapture, type WebOfficialSourceCaptureStatus, type WebOfficialSourceCatalogueItem } from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";

export function WebLegalReview({ caseId, canConfirmLegal, onSessionExpired, onVersionAdvanced }: { caseId: string; canConfirmLegal: boolean; onSessionExpired: () => void; onVersionAdvanced: (caseId: string, version: number) => void }) {
  const [review, setReview] = useState<WebLegalReviewData | null>(null);
  const [catalogue, setCatalogue] = useState<readonly WebOfficialSourceCatalogueItem[]>([]);
  const [captures, setCaptures] = useState<WebOfficialSourceCaptureStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submittingSourceId, setSubmittingSourceId] = useState<string | null>(null);
  const [locatorByRunId, setLocatorByRunId] = useState<Record<string, string>>({});
  const [evidencePages, setEvidencePages] = useState<readonly WebEvidencePage[]>([]);
  const [eventKind, setEventKind] = useState<"CONTRACT_SIGNED" | "DISBURSEMENT" | "PAYMENT" | "DEFAULT" | "CLAIM_FILED" | "CASE_ACCEPTED" | "JUDGMENT">("CONTRACT_SIGNED");
  const [eventDate, setEventDate] = useState("");
  const [eventEvidenceId, setEventEvidenceId] = useState("");
  const [savingEvent, setSavingEvent] = useState(false);
  const [bundleRuleId, setBundleRuleId] = useState("");
  const [bundleEventId, setBundleEventId] = useState("");
  const [bundleEndDate, setBundleEndDate] = useState("");
  const [savingBundle, setSavingBundle] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void Promise.all([readWebLegalReview(caseId, controller.signal), readWebOfficialSourceCaptures(caseId, controller.signal).catch(() => null), listWebEvidencePages(caseId, { limit: 100, signal: controller.signal }).catch(() => null)])
        .then(([nextReview, sourceState, evidencePageBatch]) => {
          if (!controller.signal.aborted) {
            setReview(nextReview);
            setCatalogue(sourceState?.catalogue ?? []);
            setCaptures(sourceState?.captures ?? null);
            setEvidencePages(evidencePageBatch?.items ?? []);
            setEventEvidenceId((current) => current || evidencePageBatch?.items[0]?.evidencePageId || "");
            setBundleRuleId((current) => current || nextReview.ruleVersions.find((rule) => rule.status === "APPROVED")?.ruleVersionId || "");
            setBundleEventId((current) => current || nextReview.legalEvents.find((event) => event.status === "APPROVED")?.legalEventId || "");
            setError(null);
          }
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          if (isWebLoginRequired(reason)) {
            onSessionExpired();
            return;
          }
          setError(reason instanceof Error ? reason.message : "无法读取法律依据台账。");
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [caseId, onSessionExpired]);

  if (loading) return <section className={styles.webLawyerEmptyPanel}>正在读取本案已核对的法律依据…</section>;
  if (error) return <section className={styles.webLawyerEmptyPanel} role="alert"><p className={styles.eyebrow}>依据与测算</p><h2>暂不能读取法律依据</h2><p>{error}</p><small>请稍后重试；未核对的网页内容不会被当作本案依据。</small></section>;
  if (!review) return null;
  const currentReview = review;

  async function queueOfficialSource(sourceId: WebOfficialSourceCatalogueItem["sourceId"]) {
    setSubmittingSourceId(sourceId);
    try {
      await queueWebOfficialSourceCapture(caseId, sourceId, currentReview.matterVersion);
      const sourceState = await readWebOfficialSourceCaptures(caseId);
      setCatalogue(sourceState.catalogue);
      setCaptures(sourceState.captures);
      setError(null);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "暂时无法提交官方依据核对。");
    } finally {
      setSubmittingSourceId(null);
    }
  }

  async function reviewAndRegisterCapture(run: WebOfficialSourceCapture) {
    const reviewed = captures?.reviewedRunIds.includes(run.runId) ?? false;
    const locator = locatorByRunId[run.runId]?.trim() ?? "";
    if (!reviewed && !locator) {
      setError("请先填写已核对的条款或页码定位。");
      return;
    }
    setSubmittingSourceId(run.runId);
    try {
      const reviewReceipt = reviewed
        ? { matterVersion: captures?.matterVersion ?? currentReview.matterVersion }
        : await reviewWebOfficialSourceCapture({ caseId, runId: run.runId, expectedVersion: captures?.matterVersion ?? currentReview.matterVersion, decision: "APPROVE_FOR_REGISTRATION", provisionLocator: locator });
      const registrationReceipt = await registerWebOfficialSourceCapture(caseId, run.runId, reviewReceipt.matterVersion);
      onVersionAdvanced(caseId, registrationReceipt.matterVersion);
      const [nextReview, sourceState] = await Promise.all([readWebLegalReview(caseId), readWebOfficialSourceCaptures(caseId).catch(() => null)]);
      setReview(nextReview);
      setCatalogue(sourceState?.catalogue ?? []);
      setCaptures(sourceState?.captures ?? null);
      setError(null);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "暂时无法登记本案依据。");
    } finally {
      setSubmittingSourceId(null);
    }
  }

  async function saveLegalEvent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!review || !eventEvidenceId || !eventDate || savingEvent) return;
    setSavingEvent(true);
    try {
      const receipt = await confirmWebLegalEvent({ caseId, expectedVersion: review.matterVersion, eventKind, localDate: eventDate, evidencePageIds: [eventEvidenceId] });
      onVersionAdvanced(caseId, receipt.matterVersion);
      const [nextReview, sourceState] = await Promise.all([readWebLegalReview(caseId), readWebOfficialSourceCaptures(caseId).catch(() => null)]);
      setReview(nextReview);
      setCatalogue(sourceState?.catalogue ?? []);
      setCaptures(sourceState?.captures ?? null);
      setEventDate("");
      setError(null);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "关键日期未保存，请核对材料和日期后重试。");
    } finally {
      setSavingEvent(false);
    }
  }

  async function saveLegalBundle(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!review || !bundleRuleId || !bundleEventId || !bundleEndDate || savingBundle) return;
    setSavingBundle(true);
    try {
      const receipt = await approveWebCurrentLegalBundle({
        caseId,
        expectedVersion: review.matterVersion,
        ruleVersionId: bundleRuleId,
        triggerEventId: bundleEventId,
        endDate: bundleEndDate,
      });
      onVersionAdvanced(caseId, receipt.matterVersion);
      const [nextReview, sourceState] = await Promise.all([
        readWebLegalReview(caseId),
        readWebOfficialSourceCaptures(caseId).catch(() => null),
      ]);
      setReview(nextReview);
      setCatalogue(sourceState?.catalogue ?? []);
      setCaptures(sourceState?.captures ?? null);
      setError(null);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "本案适用依据未确认，请刷新后重试。");
    } finally {
      setSavingBundle(false);
    }
  }

  return (
    <section className={styles.factsArea} aria-labelledby="web-legal-review-title">
      <header className={styles.webLawyerIntakeHeading}>
        <div><p className={styles.eyebrow}>依据与测算</p><h2 id="web-legal-review-title">核对本案依据与金额条件</h2><p>先核对适用依据和关键日期；确认无误后，系统才会开放金额核对。</p></div>
        <dl><div><dt>已核对依据</dt><dd>{review.sources.length}</dd></div><div><dt>关键日期</dt><dd>{review.legalEvents.length}</dd></div><div><dt>可用规则</dt><dd>{review.ruleVersions.length}</dd></div></dl>
      </header>
      {review.bundleReconfirmation ? <section className={styles.webLegalBundleRefresh} aria-label="当前依据需要重新确认" role="status">
        <div><p className={styles.eyebrow}>当前需要处理</p><strong>案件信息已变化，请重新确认本案依据</strong><small>{review.bundleReconfirmation.reason} 旧依据版本仅保留作历史记录，不能用于当前研判、测算或文书。</small></div>
        <span>第 {review.bundleReconfirmation.version} 版已失效</span>
      </section> : null}
      <div className={styles.factsGrid}>
        <LegalCard title="已核对的法律依据" note="只显示已完成来源核对的依据。">
          {review.sources.length === 0 ? <div><Empty text="暂未核对法律依据。先确认本案诉请和争点，再补齐需要适用的依据。" /><a className={styles.webLawyerSecondaryAction} href={`/facts?case=${encodeURIComponent(caseId)}#case-framing`}>去确认诉请和争点</a></div> : review.sources.map((source) => <div className={styles.ledgerRow} key={source.snapshotId}><strong>{source.publisher}</strong><small>{source.provisionLocator} · 已由律师核对</small><a className={styles.webLegalSourceLink} href={source.officialUrl} rel="noreferrer" target="_blank">查看官方原文</a></div>)}
        </LegalCard>
        {captures ? <LegalCard title="补齐官方依据" note="选择所需依据后，系统只读取对应官方网站，不会发送本案材料。">
          <div className={styles.webOfficialSourceList}>
            {catalogue.map((source) => {
              const run = captures?.runs.find((item) => item.sourceId === source.sourceId);
              if (!run) return <div className={styles.webOfficialSourceRow} key={source.sourceId}><div><strong>{source.title}</strong><small>{source.publisher} · {source.purpose}</small></div><button type="button" className={styles.webLawyerSecondaryAction} disabled={submittingSourceId === source.sourceId} onClick={() => void queueOfficialSource(source.sourceId)}>{submittingSourceId === source.sourceId ? "正在提交…" : "加入核对"}</button></div>;
              const reviewRequired = run.status === "REVIEW_REQUIRED";
              const reviewed = captures?.reviewedRunIds.includes(run.runId) ?? false;
              const registered = review.sources.some((item) => item.sourceId === source.sourceId);
              return <div className={styles.webOfficialSourceRow} key={source.sourceId}><div><strong>{source.title}</strong><small>{source.publisher} · {registered ? "已登记为本案依据" : captureStatusText(run.status, run.failureCode)}</small>{run.provisions.length > 0 ? <small>系统识别到：{run.provisions.join("、")}</small> : null}{reviewRequired && run.officialUrl ? <a className={styles.webLegalSourceLink} href={run.officialUrl} rel="noreferrer" target="_blank">查看官方原文并核对</a> : null}{reviewRequired && !reviewed && !registered ? <label className={styles.webOfficialSourceLocator}>已核对的条款或页码<input value={locatorByRunId[run.runId] ?? ""} onChange={(event) => setLocatorByRunId((current) => ({ ...current, [run.runId]: event.target.value }))} placeholder="例如：第二十五条、第三十一条" /></label> : null}</div>{registered ? <span className={styles.webOfficialSourceStatus}>已登记</span> : reviewRequired ? <button type="button" className={styles.webLawyerSecondaryAction} disabled={submittingSourceId === run.runId || (!reviewed && !(locatorByRunId[run.runId] ?? "").trim())} onClick={() => void reviewAndRegisterCapture(run)}>{submittingSourceId === run.runId ? "正在登记…" : reviewed ? "登记本案依据" : "核对并登记"}</button> : run.status === "FAILED" ? <button type="button" className={styles.webLawyerSecondaryAction} disabled={submittingSourceId === source.sourceId} onClick={() => void queueOfficialSource(source.sourceId)}>{submittingSourceId === source.sourceId ? "正在提交…" : "重新读取"}</button> : <span className={styles.webOfficialSourceStatus}>{captureStatusText(run.status, run.failureCode)}</span>}</div>;
            })}
          </div>
        </LegalCard> : null}
        <LegalCard title="金额与利息规则" note="规则由律师核对后使用；系统不会要求你填写计算公式。">
          {review.ruleVersions.length === 0 ? <Empty text="尚未确定可用于本案的利息规则。完成依据核对与关键日期确认后再继续。" /> : review.ruleVersions.map((rule) => <div className={styles.ledgerRow} key={rule.ruleVersionId}><strong>{lawyerRuleTitle(rule.issueKey)}</strong><small>{lawyerRuleDetail(rule)}</small></div>)}
        </LegalCard>
        <LegalCard title="本案关键日期" note="合同、付款、起诉、受理等日期必须由律师以材料确认。">
          {review.legalEvents.length === 0 ? <Empty text="尚未确认关键日期；系统不会从文件时间自动推定起算日。" /> : review.legalEvents.map((event) => <div className={styles.ledgerRow} key={event.legalEventId}><strong>{lawyerEventTitle(event.eventKind)} · {event.localDate ?? "日期未确认"}</strong><small>已由律师以 {event.evidenceIds.length} 处材料定位核对</small></div>)}
          {evidencePages.length > 0 ? <form className={styles.webLegalEventForm} onSubmit={saveLegalEvent}><strong>确认一项关键日期</strong><label>日期类型<select disabled={!canConfirmLegal} value={eventKind} onChange={(event) => setEventKind(event.target.value as typeof eventKind)}><option value="CONTRACT_SIGNED">合同或借据日期</option><option value="DISBURSEMENT">出借日期</option><option value="PAYMENT">付款日期</option><option value="DEFAULT">逾期或违约日期</option><option value="CLAIM_FILED">起诉日期</option><option value="CASE_ACCEPTED">法院受理日期</option><option value="JUDGMENT">裁判日期</option></select></label><label>确认日期<input disabled={!canConfirmLegal} required type="date" value={eventDate} onChange={(event) => setEventDate(event.target.value)} /></label><label>依据材料<select disabled={!canConfirmLegal} value={eventEvidenceId} onChange={(event) => setEventEvidenceId(event.target.value)}>{evidencePages.map((page) => <option key={page.evidencePageId} value={page.evidencePageId}>{page.originalLabel} · 第 {page.pageNumber} 页</option>)}</select></label>{canConfirmLegal ? <button className={styles.webLawyerSecondaryAction} disabled={savingEvent || !eventDate || !eventEvidenceId} type="submit">{savingEvent ? "正在确认…" : "确认关键日期"}</button> : <small>由主办律师确认关键日期后，才能进入后续金额核对。</small>}</form> : <a className={styles.webLawyerSecondaryAction} href={`/evidence?case=${encodeURIComponent(caseId)}`}>先核对材料页面</a>}
        </LegalCard>
        <LegalCard title={review.bundleReconfirmation ? "重新确认本案适用依据" : "确认本案适用依据"} note={review.bundleReconfirmation ? "争点或案情已经更新；请按当前案情重新确认适用依据和适用区间。" : "选择已核对规则与关键日期，确认本次审阅的适用区间。"}>
          {review.currentBundle ? <div className={styles.ledgerRow}><strong>本案适用依据已确认</strong><small>已形成当前依据版本；上游材料、事实或规则变化后会自动失效。</small></div> : review.ruleVersions.some((rule) => rule.status === "APPROVED") && review.legalEvents.some((event) => event.status === "APPROVED") ? <form className={styles.webLegalEventForm} onSubmit={saveLegalBundle}><strong>确认一项适用依据</strong><label>已核对规则<select disabled={!canConfirmLegal || savingBundle} value={bundleRuleId} onChange={(event) => { setBundleRuleId(event.target.value); setBundleEventId(""); }}><option value="" disabled>请选择规则</option>{review.ruleVersions.filter((rule) => rule.status === "APPROVED").map((rule) => <option key={rule.ruleVersionId} value={rule.ruleVersionId}>{lawyerRuleTitle(rule.issueKey)} · {rule.ruleVersion}</option>)}</select></label><label>对应关键日期<select disabled={!canConfirmLegal || savingBundle} value={bundleEventId} onChange={(event) => setBundleEventId(event.target.value)}><option value="" disabled>请选择关键日期</option>{review.legalEvents.filter((item) => item.status === "APPROVED").map((item) => <option key={item.legalEventId} value={item.legalEventId}>{lawyerEventTitle(item.eventKind)} · {item.localDate ?? "待确认"}</option>)}</select></label><label>适用终点<input disabled={!canConfirmLegal || savingBundle} required type="date" value={bundleEndDate} onChange={(event) => setBundleEndDate(event.target.value)} /></label>{canConfirmLegal ? <button className={styles.webLawyerSecondaryAction} disabled={savingBundle || !bundleRuleId || !bundleEventId || !bundleEndDate} type="submit">{savingBundle ? "正在确认…" : "确认本案适用依据"}</button> : <small>由主办律师确认本案适用依据后，才能进入后续金额核对。</small>}</form> : <Empty text="先确认至少一项关键日期和已核对规则，再确定本案适用依据。" />}
        </LegalCard>
        <LegalCard title="金额核对状态" note="规则、起算日期和付款记录齐全后，才会进入正式计算。">
          {review.currentBundle ? <div className={styles.ledgerRow}><strong>本案金额核对条件已确认</strong><small>已覆盖 {review.bundleSegments.length} 个适用期间；可进入“金额核对”。</small></div> : <Empty text="金额核对仍处于保护状态，待依据、关键日期和适用规则全部确认后开放。" />}
        </LegalCard>
      </div>
      <footer className={styles.webLawyerNotice}><strong>接下来：</strong>核对本案关键日期和收付款记录；信息齐全后，可在“金额核对”中查看可复核的计算结果。</footer>
    </section>
  );
}

function lawyerRuleTitle(issueKey: string) {
  const labels: Record<string, string> = {
    "NO_INTEREST": "未约定利息的处理",
    "PRIVATE_LENDING_RESPONSE_SOURCE_SCOPE": "民间借贷应诉适用范围",
  };
  return labels[issueKey] ?? (/^[\u4e00-\u9fff，、（）()·\s\dA-Za-z_-]{2,80}$/.test(issueKey) ? issueKey : "已核对的适用规则");
}

function lawyerRuleDetail(rule: WebLegalReviewData["ruleVersions"][number]) {
  const period = rule.effectiveTo ? `${rule.effectiveFrom ?? "起始日期待核对"} 至 ${rule.effectiveTo}` : `${rule.effectiveFrom ?? "起始日期待核对"} 起适用`;
  const rate = rule.derivedAnnualRate ? `；已核对年利率 ${rule.derivedAnnualRate}` : "";
  return `适用期间：${period}${rate}`;
}

function lawyerEventTitle(eventKind: string) {
  const labels: Record<string, string> = {
    "CONTRACT_SIGNED": "合同或借据日期",
    "DISBURSEMENT": "出借日期",
    "PAYMENT": "付款日期",
    "DEFAULT": "逾期或违约日期",
    "CLAIM_FILED": "起诉日期",
    "CASE_ACCEPTED": "法院受理日期",
    "JUDGMENT": "裁判日期",
  };
  return labels[eventKind] ?? "本案关键日期";
}

function LegalCard({ title, note, children }: { title: string; note: string; children: React.ReactNode }) {
  return <section className={styles.ledgerCard}><div><strong>{title}</strong><small>{note}</small></div>{children}</section>;
}

function Empty({ text }: { text: string }) {
  return <p className={styles.webLawyerUploadEmpty}>{text}</p>;
}

function captureStatusText(status: string, failureCode: string | null) {
  if (status === "QUEUED") return "等待读取官方原文";
  if (status === "RUNNING") return "正在读取官方原文";
  if (status === "REVIEW_REQUIRED") return "已读取，等待律师核对";
  if (status === "FAILED") return failureCode ? "本次读取未完成，请稍后重新发起" : "本次读取未完成";
  return "正在更新核对状态";
}
