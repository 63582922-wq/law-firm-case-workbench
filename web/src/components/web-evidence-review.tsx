"use client";

/* eslint-disable @next/next/no-img-element -- the preview is a protected, same-origin one-page PNG; Next image optimization must not proxy it. */

import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import {
  confirmWebEvidenceAnnotation,
  confirmWebEvidenceDecision,
  confirmWebEvidenceDecisionsBatch,
  createWebEvidenceAnnotationCandidate,
  createWebEvidenceDecisionCandidate,
  enqueueWebEvidenceDerivativeRun,
  listWebEvidencePages,
  lockWebEvidenceManifest,
  readWebEvidenceSummary,
  webEvidenceDerivativeDownloadUrl,
  webEvidencePagePreviewUrl,
  type WebEvidenceAnnotation,
  type WebEvidencePage,
  type WebEvidenceSummary,
} from "@/lib/web-evidence-api";
import { isWebLoginRequired } from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";

const isLocalWebMode = process.env.NEXT_PUBLIC_WEB_API_PREFIX === "/api/local/v1";

type Box = Readonly<{ x0: number; y0: number; x1: number; y1: number }>;

export function WebEvidenceReview({
  caseId,
  focusPageIds = null,
  onClearFocus,
  onSessionExpired,
  onVersionAdvanced,
}: {
  caseId: string;
  focusPageIds?: readonly string[] | null;
  onClearFocus?: () => void;
  onSessionExpired: () => void;
  onVersionAdvanced: (caseId: string, version: number) => void;
}) {
  const [summary, setSummary] = useState<WebEvidenceSummary | null>(null);
  const [pages, setPages] = useState<readonly WebEvidencePage[]>([]);
  const [activePageId, setActivePageId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draftBox, setDraftBox] = useState<Box | null>(null);
  const [draftLabel, setDraftLabel] = useState("与原告相关的还款记录");
  const [lockConfirmed, setLockConfirmed] = useState(false);
  const dragStart = useRef<{ x: number; y: number } | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setRefreshing(true);
    try {
      const [nextSummary, nextPages] = await Promise.all([
        readWebEvidenceSummary(caseId, signal),
        listWebEvidencePages(caseId, { limit: 100, signal }),
      ]);
      if (signal?.aborted) return;
      setSummary(nextSummary);
      setPages(nextPages.items);
      onVersionAdvanced(caseId, Math.max(nextSummary.matterVersion, nextPages.matterVersion));
      setActivePageId((current) => current && nextPages.items.some((item) => item.evidencePageId === current)
        ? current
        : nextPages.items[0]?.evidencePageId ?? null);
      setError(null);
    } catch (reason: unknown) {
      if (signal?.aborted) return;
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      setError(reason instanceof Error ? reason.message : "无法读取证据页面；服务端没有返回可核验结果。");
    } finally {
      if (!signal?.aborted) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [caseId, onSessionExpired, onVersionAdvanced]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void refresh(controller.signal), 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [refresh]);

  const visiblePages = useMemo(() => {
    if (focusPageIds === null) return pages;
    const focusSet = new Set(focusPageIds);
    return pages.filter((page) => focusSet.has(page.evidencePageId));
  }, [focusPageIds, pages]);

  const activePage = useMemo(
    () => visiblePages.find((page) => page.evidencePageId === activePageId) ?? visiblePages[0] ?? null,
    [activePageId, visiblePages],
  );
  const focusedPendingDecisions = useMemo(() => {
    if (focusPageIds === null) return [];
    return visiblePages.flatMap((page) => page.pendingDecision?.status === "CANDIDATE" ? [page.pendingDecision] : []);
  }, [focusPageIds, visiblePages]);

  async function runAction(actionKey: string, action: () => Promise<void>) {
    if (busy) return;
    setBusy(actionKey);
    setNotice(null);
    setError(null);
    try {
      await action();
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      const detail = reason instanceof Error ? reason.message : "服务端没有返回可核验结果。";
      setError(`${detail} 系统不会自动重试；请先刷新核验案件版本和待确认决定，再决定是否重新操作。`);
    } finally {
      setBusy(null);
    }
  }

  function setPageDisposition(disposition: "INCLUDE" | "EXCLUDE") {
    if (!activePage || !summary) return;
    void runAction(`candidate:${activePage.evidencePageId}`, async () => {
      const receipt = await createWebEvidenceDecisionCandidate(caseId, activePage.evidencePageId, {
        expectedVersion: summary.matterVersion,
        disposition,
        reason: disposition === "INCLUDE" ? "律师确认：与本案争议或还款事实相关" : "律师确认：与本案提交范围无关",
      });
      onVersionAdvanced(caseId, receipt.matterVersion);
      setNotice("页面处理候选已保存。请核对右侧内容后点击“确认此决定”，才会成为正式审阅记录。");
      await refresh();
    });
  }

  function confirmDecision() {
    const pending = activePage?.pendingDecision;
    if (!pending || !summary) return;
    void runAction(`confirm-decision:${pending.decisionId}`, async () => {
      const receipt = await confirmWebEvidenceDecision(caseId, pending.decisionId, summary.matterVersion);
      onVersionAdvanced(caseId, receipt.matterVersion);
      setNotice("页面处理决定已确认，并写入案件审计记录。");
      await refresh();
    });
  }

  function confirmFocusedDecisionsBatch() {
    if (!summary || focusPageIds === null || focusedPendingDecisions.length < 1) return;
    const decisionIds = focusedPendingDecisions.map((decision) => decision.decisionId);
    const previousVersion = summary.matterVersion;
    void runAction("confirm-focused-batch", async () => {
      const receipt = await confirmWebEvidenceDecisionsBatch(caseId, decisionIds, previousVersion);
      onVersionAdvanced(caseId, receipt.matterVersion);
      setNotice(`已一次确认 ${decisionIds.length} 项现有待确认决定；案件版本由 ${previousVersion} 更新为 ${receipt.matterVersion}。此前锁定的证据清单、证据 PDF 和提交材料如存在均已失效，须按新版本重新生成。`);
      await refresh();
    });
  }

  function beginBox(event: ReactPointerEvent<HTMLDivElement>) {
    if (busy || !activePage || activePage.pendingDecision || activePage.decision?.status === "APPROVED") return;
    event.preventDefault();
    const point = normalizedPoint(event);
    dragStart.current = point;
    setDraftBox({ x0: point.x, y0: point.y, x1: point.x, y1: point.y });
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function updateBox(event: ReactPointerEvent<HTMLDivElement>) {
    const start = dragStart.current;
    if (!start) return;
    const point = normalizedPoint(event);
    setDraftBox(normalizeBox({ x0: start.x, y0: start.y, x1: point.x, y1: point.y }));
  }

  function finishBox(event: ReactPointerEvent<HTMLDivElement>) {
    const start = dragStart.current;
    dragStart.current = null;
    if (!start) return;
    const point = normalizedPoint(event);
    const box = normalizeBox({ x0: start.x, y0: start.y, x1: point.x, y1: point.y });
    if (box.x1 - box.x0 < 0.012 || box.y1 - box.y0 < 0.012) {
      setDraftBox(null);
      setError("请拖拽出一个可见范围后再添加红框。");
      return;
    }
    setDraftBox(box);
  }

  function addDraftBox() {
    if (!activePage || !summary || !draftBox) return;
    void runAction(`annotation:${activePage.evidencePageId}`, async () => {
      const receipt = await createWebEvidenceAnnotationCandidate(caseId, activePage.evidencePageId, {
        expectedVersion: summary.matterVersion,
        ...draftBox,
        label: draftLabel,
      });
      onVersionAdvanced(caseId, receipt.matterVersion);
      setDraftBox(null);
      setNotice("红框候选已保存。请在右侧核对后确认，红框只用于标识提交材料中的位置，不会改写原件。");
      await refresh();
    });
  }

  function confirmAnnotation(annotation: WebEvidenceAnnotation) {
    if (!summary) return;
    void runAction(`confirm-annotation:${annotation.annotationId}`, async () => {
      const receipt = await confirmWebEvidenceAnnotation(caseId, annotation.annotationId, summary.matterVersion);
      onVersionAdvanced(caseId, receipt.matterVersion);
      setNotice("红框已确认，并写入案件审计记录。");
      await refresh();
    });
  }

  function lockManifest() {
    if (!summary || !lockConfirmed || unresolved > 0 || pending > 0 || summary.unresolvedDuplicateCount > 0 || summary.lockedManifest) return;
    void runAction("manifest-lock", async () => {
      const receipt = await lockWebEvidenceManifest(caseId, summary.matterVersion, summary.manifestReadinessHash);
      onVersionAdvanced(caseId, receipt.matterVersion);
      setNotice("证据清单已锁定。后续 PDF 派生件将严格按照这份清单生成，不会改写原始材料。");
      setLockConfirmed(false);
      await refresh();
    });
  }

  function enqueueDerivativeRun() {
    if (!summary?.lockedManifest) return;
    if (summary.derivativeRuns.some((run) => ["QUEUED", "RUNNING", "SUCCEEDED"].includes(run.status))) return;
    void runAction("derivative-enqueue", async () => {
      const receipt = await enqueueWebEvidenceDerivativeRun(caseId, summary.matterVersion, summary.lockedManifest!.manifestId);
      onVersionAdvanced(caseId, receipt.matterVersion);
      setNotice("证据 PDF 已开始生成。完成后会在这里显示下载入口；请勿重复提交。");
      await refresh();
    });
  }

  if (loading) {
    return <section className={styles.webEvidenceState}><p className={styles.eyebrow}>第二步 · 逐页审阅</p><h2>正在准备材料页面</h2><p>请稍候，页面准备完成后可逐页核对。</p></section>;
  }
  if (error && !summary) {
    return <section className={styles.webEvidenceState} role="alert"><p className={styles.eyebrow}>证据审阅</p><h2>暂时不能读取页面</h2><p>{error}</p><button className={styles.webLawyerPrimaryAction} onClick={() => void refresh()} type="button">重新读取</button></section>;
  }

  const unresolved = summary?.unresolvedPageCount ?? pages.filter((page) => !page.decision).length;
  const pending = summary?.pendingDecisionCount ?? pages.filter((page) => page.pendingDecision).length;

  return (
    <section className={styles.webEvidenceReview} aria-labelledby="web-evidence-review-title">
      <header className={styles.webEvidenceHeading}>
        <div>
          <p className={styles.eyebrow}>第二步 · 逐页审阅</p>
          <h2 id="web-evidence-review-title">核对材料页面与还款证据</h2>
          <p>{focusPageIds !== null ? "已定位到相关页面；请优先处理这些关键页和例外。" : "可先浏览全部页面。"} 需要强调付款、聊天或身份信息时，在页面上拖出红框。原件保持不变，页面上的操作只会形成可复核标记。</p>
        </div>
        <dl className={styles.webEvidenceSummary}>
          <div><dt>材料页数</dt><dd>{summary?.totalPages ?? pages.length}</dd></div>
          <div><dt>尚未决定</dt><dd>{unresolved}</dd></div>
          <div><dt>待确认</dt><dd>{pending}</dd></div>
        </dl>
      </header>

      {error ? <p className={styles.webEvidenceError} role="alert">{error}</p> : null}
      {notice ? <p className={styles.webEvidenceNotice} role="status">{notice}</p> : null}
      {focusPageIds !== null ? (
        <div className={styles.webEvidenceFocusNotice} role="status">
          <div>
            <strong>{focusedPendingDecisions.length > 0 ? "正在核对待确认页面" : "正在核对重点页面"}</strong>
            <span>当前只显示与该组绑定的 {visiblePages.length} 页；其中有 {focusedPendingDecisions.length} 项是已保存、尚待主办律师确认的页面决定候选。</span>
            <small>批量确认只处理已经列出的待确认页面。图片不清、重复、存在冲突或需要进一步判断的页面，会保留给律师单独处理。</small>
          </div>
          <div className={styles.webEvidenceFocusActions}>
            {focusedPendingDecisions.length > 0 && summary?.canBatchConfirmPageDecisions ? <button className={styles.webLawyerPrimaryAction} disabled={Boolean(busy)} onClick={confirmFocusedDecisionsBatch} type="button">{busy === "confirm-focused-batch" ? "正在整批确认…" : `主办律师批量确认（${focusedPendingDecisions.length} 项）`}</button> : null}
            {onClearFocus ? <button className={styles.webLawyerSecondaryAction} disabled={Boolean(busy)} onClick={onClearFocus} type="button">显示全部 {pages.length} 页</button> : null}
          </div>
        </div>
      ) : null}

      {visiblePages.length === 0 ? (
        <div className={styles.webEvidenceEmpty}><strong>{pages.length === 0 ? "材料已接收，但页面尚未准备好" : "这组材料暂未定位到页面"}</strong><span>{pages.length === 0 ? "请稍后刷新；不要重复上传同一 PDF。" : "请显示全部页面后继续核对。"}</span>{pages.length === 0 ? <button className={styles.webLawyerSecondaryAction} disabled={refreshing} onClick={() => void refresh()} type="button">{refreshing ? "读取中" : "刷新页面"}</button> : onClearFocus ? <button className={styles.webLawyerSecondaryAction} onClick={onClearFocus} type="button">显示全部页面</button> : null}</div>
      ) : (
        <div className={styles.webEvidenceLayout}>
          <aside className={styles.webEvidencePageList} aria-label="证据页面列表">
            <header><strong>{focusPageIds === null ? "页面清单" : "候选组页面"}</strong><span>{refreshing ? "刷新中" : `${visiblePages.length} 页`}</span></header>
            <div>
              {visiblePages.map((page) => (
                <button className={page.evidencePageId === activePage?.evidencePageId ? styles.webEvidencePageActive : styles.webEvidencePage} key={page.evidencePageId} onClick={() => { setActivePageId(page.evidencePageId); setDraftBox(null); }} type="button">
                  <span>第 {page.pageNumber} 页</span>
                  <strong>{page.originalLabel}</strong>
                  <em className={page.pendingDecision ? styles.webEvidencePending : page.decision?.disposition === "INCLUDE" ? styles.webEvidenceIncluded : page.decision?.disposition === "EXCLUDE" ? styles.webEvidenceExcluded : undefined}>{page.pendingDecision ? "待确认" : page.decision ? page.decision.disposition === "INCLUDE" ? "已纳入" : "已排除" : "待决定"}</em>
                </button>
              ))}
            </div>
          </aside>

          <section className={styles.webEvidencePreview} aria-label="证据页面预览">
            <header><div><span>材料页面预览</span><h3>{activePage ? `${activePage.originalLabel} · 第 ${activePage.pageNumber} 页` : "未选择页面"}</h3></div><span>原件保持不变</span></header>
            {activePage ? (
              <div className={styles.webEvidenceImageStage}>
                <div className={styles.webEvidenceImageFrame}>
                  {isLocalWebMode ? (
                    <iframe title={`${activePage.originalLabel}第${activePage.pageNumber}页`} src={webEvidencePagePreviewUrl(caseId, activePage.evidencePageId)} className={styles.webEvidenceLocalPdf} />
                  ) : (
                    <>
                      <img alt={`${activePage.originalLabel}第${activePage.pageNumber}页`} src={webEvidencePagePreviewUrl(caseId, activePage.evidencePageId)} />
                      <div className={styles.webEvidenceBoxLayer} onPointerDown={beginBox} onPointerMove={updateBox} onPointerUp={finishBox}>
                        {activePage.annotations.map((annotation) => <span className={styles.webEvidenceBox} key={annotation.annotationId} style={boxStyle(annotation)} />)}
                        {draftBox ? <span className={`${styles.webEvidenceBox} ${styles.webEvidenceBoxDraft}`} style={boxStyle(draftBox)} /> : null}
                      </div>
                    </>
                  )}
                </div>
              </div>
            ) : <div className={styles.webEvidencePreviewState}>请选择左侧页面。</div>}
            <footer>{isLocalWebMode ? "离线开发模式未装配视觉红框和派生 PDF；完整服务恢复后会在同一案件工作台开放。" : "红框仅是页面级标注；必须另行确认后，才会进入后续提交材料清单。"}</footer>
          </section>

          <aside className={styles.webEvidenceInspector} aria-label="页面审阅操作">
            <section>
              <p className={styles.eyebrow}>页面处理</p>
              <h3>{activePage ? `第 ${activePage.pageNumber} 页` : "未选择页面"}</h3>
              <p className={styles.webEvidenceSmallText}>先保存候选，再进行人工确认。候选不会自动成为正式证据决定。</p>
              <div className={styles.webEvidenceActions}>
                <button className={activePage?.decision?.disposition === "INCLUDE" ? styles.webEvidenceActionActive : styles.webEvidenceAction} disabled={!activePage || Boolean(busy)} onClick={() => setPageDisposition("INCLUDE")} type="button">纳入本案</button>
                <button className={activePage?.decision?.disposition === "EXCLUDE" ? styles.webEvidenceActionActive : styles.webEvidenceAction} disabled={!activePage || Boolean(busy)} onClick={() => setPageDisposition("EXCLUDE")} type="button">排除本页</button>
              </div>
              {activePage?.pendingDecision ? <div className={styles.webEvidenceConfirmCard}><strong>页面有待确认决定</strong><span>{activePage.pendingDecision.reason}</span><button className={styles.webLawyerPrimaryAction} disabled={Boolean(busy)} onClick={confirmDecision} type="button">确认此决定</button></div> : null}
            </section>
            <section>
              <p className={styles.eyebrow}>红框标识</p>
              <h3>框出需要强调的位置</h3>
              <p className={styles.webEvidenceSmallText}>{isLocalWebMode ? "离线开发模式未装配视觉拖拽红框；当前不显示假红框结果。" : "在预览上拖动即可形成候选红框；请确认页面纳入后再做标识。"}</p>
              {!isLocalWebMode && draftBox ? <div className={styles.webEvidenceDraftForm}><label><span>红框说明</span><input maxLength={500} onChange={(event) => setDraftLabel(event.target.value)} value={draftLabel} /></label><button className={styles.webLawyerPrimaryAction} disabled={Boolean(busy)} onClick={addDraftBox} type="button">保存红框候选</button></div> : null}
              {!isLocalWebMode && activePage?.annotations.length ? <ul className={styles.webEvidenceAnnotationList}>{activePage.annotations.map((annotation) => <li key={annotation.annotationId}><span>{annotation.label} · {annotation.status === "CANDIDATE" ? "待确认" : "已确认"}</span>{annotation.status === "CANDIDATE" ? <button disabled={Boolean(busy)} onClick={() => confirmAnnotation(annotation)} type="button">确认</button> : null}</li>)}</ul> : <span className={styles.webEvidenceSmallText}>{isLocalWebMode ? "本机模式未开启红框功能。" : "本页尚无红框。"}</span>}
            </section>
            <section className={styles.webEvidenceNextStep}>
              <strong>{summary?.lockedManifest ? "证据清单已锁定" : "第三步 · 锁定清单"}</strong>
              <span>{summary?.lockedManifest ? "这份清单已绑定当前案件版本；原始 PDF 仍保持不变。" : "所有相关页面完成决定并确认红框后，锁定清单才可生成。"}</span>
              {!summary?.lockedManifest ? <label className={styles.webEvidenceLockLine}><input checked={lockConfirmed} onChange={(event) => setLockConfirmed(event.target.checked)} type="checkbox" disabled={unresolved > 0 || pending > 0 || (summary?.unresolvedDuplicateCount ?? 0) > 0 || Boolean(busy)} /><span>我已核对每一页的纳入/排除决定，并确认红框位置。</span></label> : null}
              {!summary?.lockedManifest ? <button className={styles.webLawyerPrimaryAction} disabled={!lockConfirmed || unresolved > 0 || pending > 0 || (summary?.unresolvedDuplicateCount ?? 0) > 0 || Boolean(busy)} onClick={lockManifest} type="button">{busy === "manifest-lock" ? "正在锁定…" : "锁定当前证据清单"}</button> : null}
              {!summary?.lockedManifest && unresolved === 0 && pending === 0 && (summary?.unresolvedDuplicateCount ?? 0) === 0 ? <small>锁定前请完成上方复核；锁定操作会写入案件审计记录。</small> : null}
              {summary?.lockedManifest ? <div className={styles.webEvidenceDerivativePanel}><strong>提交版 PDF</strong><span>{summary.derivativeRuns.length ? summary.derivativeRuns.map((run) => `${run.status}${run.failureCode ? `（${run.failureCode}）` : ""}`).join("；") : "尚未提交生成任务"}</span><button className={styles.webLawyerPrimaryAction} disabled={Boolean(busy) || summary.derivativeRuns.some((run) => ["QUEUED", "RUNNING", "SUCCEEDED"].includes(run.status))} onClick={enqueueDerivativeRun} type="button">{busy === "derivative-enqueue" ? "正在提交…" : summary.derivativeRuns.some((run) => run.status === "FAILED") ? "重新提交生成任务" : "生成两份证据 PDF"}</button><small>生成完成后才会开放受管下载；浏览器不会直接读取原件。</small>{summary.derivatives.filter((item) => item.status === "VERIFIED").length ? <div className={styles.webEvidenceDerivativeLinks}>{summary.derivatives.filter((item) => item.status === "VERIFIED").map((item) => <a href={webEvidenceDerivativeDownloadUrl(caseId, item.derivativeId)} key={item.derivativeId} rel="noreferrer">{item.artifactType === "ANNOTATED_RELATED_PAGES_PDF" ? "下载红框版 PDF" : "下载相关页面 PDF"}</a>)}</div> : null}</div> : null}
            </section>
          </aside>
        </div>
      )}
    </section>
  );

  function normalizedPoint(event: ReactPointerEvent<HTMLDivElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    return { x: clamp((event.clientX - rect.left) / rect.width), y: clamp((event.clientY - rect.top) / rect.height) };
  }
}

function normalizeBox(input: Box): Box {
  return { x0: Math.min(input.x0, input.x1), y0: Math.min(input.y0, input.y1), x1: Math.max(input.x0, input.x1), y1: Math.max(input.y0, input.y1) };
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function boxStyle(box: Pick<Box, "x0" | "y0" | "x1" | "y1">) {
  return { left: `${box.x0 * 100}%`, top: `${box.y0 * 100}%`, width: `${(box.x1 - box.x0) * 100}%`, height: `${(box.y1 - box.y0) * 100}%` };
}
