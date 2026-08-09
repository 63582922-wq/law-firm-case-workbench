"use client";

import { useEffect, useMemo, useState } from "react";
import {
  approveEvidenceAnnotation,
  approveEvidencePageDecision,
  caseDataSourceConfig,
  enqueueEvidenceDerivativeRun,
  fetchEvidenceDerivative,
  loadEvidenceReview,
  lockEvidenceManifest,
  proposeEvidencePageDecision,
  resolveEvidenceDuplicateGroup,
  type EvidenceDerivative,
  type EvidenceReviewPage,
  type EvidenceReviewView,
} from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

type DuplicateDecision = "pending" | "exclude" | "keep";

function pageStatus(page: EvidenceReviewPage): string {
  if (page.pendingDecision) return `待批准${page.pendingDecision.disposition === "INCLUDE" ? "纳入" : "排除"}`;
  if (!page.decisionId) return "待律师逐页处置";
  return page.disposition === "INCLUDE" ? "已批准纳入" : "已批准排除";
}

function statusClass(page: EvidenceReviewPage): string {
  if (page.pendingDecision) return styles.pending;
  if (!page.decisionId) return styles.pending;
  return page.disposition === "INCLUDE" ? styles.verified : styles.needsMaterial;
}

export function EvidenceWorkbench() {
  const [review, setReview] = useState<EvidenceReviewView | null>(null);
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(
    caseDataSourceConfig.kind === "persistent-disabled" ? caseDataSourceConfig.reason : null,
  );
  const [duplicateDecision, setDuplicateDecision] = useState<DuplicateDecision>("pending");
  const [auditNotice, setAuditNotice] = useState("尚未记录新的合成审计决定。");
  const [artifactNotice, setArtifactNotice] = useState<string | null>(null);
  const [artifactBusy, setArtifactBusy] = useState<string | null>(null);
  const [artifactPreview, setArtifactPreview] = useState<{ url: string; label: string; sha256: string } | null>(null);
  const [pageDisposition, setPageDisposition] = useState<"INCLUDE" | "EXCLUDE">("INCLUDE");
  const [pageReason, setPageReason] = useState("");
  const [originalCompared, setOriginalCompared] = useState(false);
  const [duplicateResolution, setDuplicateResolution] = useState<"same" | "distinct">("same");
  const [canonicalPageId, setCanonicalPageId] = useState<string | null>(null);
  const [manifestConfirmed, setManifestConfirmed] = useState(false);
  const [reviewBusy, setReviewBusy] = useState<string | null>(null);
  const [reviewNotice, setReviewNotice] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      if (artifactPreview) URL.revokeObjectURL(artifactPreview.url);
    };
  }, [artifactPreview]);

  useEffect(() => {
    let active = true;
    loadEvidenceReview()
      .then((result) => {
        if (!active) return;
        setReview(result);
        const firstPage = result.pages[0] ?? null;
        setSelectedPageId(firstPage?.pageId ?? null);
        setPageDisposition(firstPage?.pendingDecision?.disposition ?? firstPage?.disposition ?? "INCLUDE");
        setPageReason(firstPage?.pendingDecision?.reason ?? firstPage?.reason ?? "");
        const firstGroup = result.duplicateGroups.find((item) => firstPage && item.pageIds.includes(firstPage.pageId));
        setCanonicalPageId(firstGroup?.canonicalPageId ?? firstGroup?.pageIds[0] ?? null);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "证据快照读取失败");
      });
    return () => {
      active = false;
    };
  }, []);

  const selected = useMemo(
    () => review?.pages.find((page) => page.pageId === selectedPageId) ?? review?.pages[0] ?? null,
    [review, selectedPageId],
  );

  if (error) {
    return (
      <section className={styles.evidenceArea} aria-label="证据核验台">
        <div className={styles.evidenceBlocked} role="alert">
          <p className={styles.eyebrow}>证据数据源已阻断</p>
          <h2>未展示任何合成案卷替代内容</h2>
          <p>{error}</p>
        </div>
      </section>
    );
  }

  if (!review || !selected) {
    return <section className={styles.evidenceArea}><div className={styles.evidenceLoading}>正在读取页级证据快照…</div></section>;
  }

  const unresolvedCount = review.pages.filter((page) => !page.decisionId).length;
  const pendingDecisionCount = review.pages.filter((page) => page.pendingDecision).length;
  const unresolvedDuplicateCount = review.duplicateGroups.filter((group) => group.status === "CANDIDATE").length;
  const duplicateGroup = review.duplicateGroups.find((group) => group.pageIds.includes(selected.pageId));
  const source = review.originals.find((item) => item.fileId === selected.fileId);

  function recordSyntheticDecision() {
    if (duplicateDecision === "pending") {
      setAuditNotice("请先选择律师决定；系统不会替代律师作出取舍。");
      return;
    }
    const action = duplicateDecision === "exclude" ? "排除重复页的派生提交引用" : "保留为不同来源页";
    setAuditNotice(`已记录合成界面动作：${action}。原始页未删除；持久化模式必须通过版本化 API 审批。`);
  }

  async function readDerivative(derivative: EvidenceDerivative, purpose: "INLINE_PREVIEW" | "DOWNLOAD") {
    setArtifactBusy(`${derivative.derivativeId}:${purpose}`);
    setArtifactNotice(null);
    try {
      const delivery = await fetchEvidenceDerivative(derivative, purpose);
      if (purpose === "DOWNLOAD") {
        const url = URL.createObjectURL(delivery.blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = delivery.fileName;
        anchor.click();
        URL.revokeObjectURL(url);
        setArtifactNotice(`已下载经核验派生件：${delivery.fileName}`);
      } else {
        const url = URL.createObjectURL(delivery.blob);
        setArtifactPreview((prior) => {
          if (prior) URL.revokeObjectURL(prior.url);
          return {
            url,
            label: derivative.artifactType === "ANNOTATED_RELATED_PAGES_PDF" ? "红框相关页" : "相关页",
            sha256: delivery.artifactSha256,
          };
        });
        setArtifactNotice("派生件仅在本页内存中短时预览；关闭后释放，不写回原件文件夹。");
      }
    } catch (reason: unknown) {
      setArtifactNotice(reason instanceof Error ? reason.message : "证据派生件读取失败");
    } finally {
      setArtifactBusy(null);
    }
  }

  async function enqueueDerivativeRun() {
    if (!review) return;
    setArtifactBusy("enqueue");
    setArtifactNotice(null);
    try {
      const receipt = await enqueueEvidenceDerivativeRun(review);
      const refreshed = await loadEvidenceReview();
      setReview(refreshed);
      setArtifactNotice(`证据派生任务已进入受控队列；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setArtifactNotice(reason instanceof Error ? reason.message : "证据派生任务未建立");
    } finally {
      setArtifactBusy(null);
    }
  }

  async function refreshAfterEvidenceMutation(
    actionKey: string,
    action: () => Promise<{ matterVersion: number }>,
    success: string,
  ) {
    setReviewBusy(actionKey);
    setReviewNotice(null);
    try {
      const receipt = await action();
      const refreshed = await loadEvidenceReview();
      setReview(refreshed);
      const current = refreshed.pages.find((page) => page.pageId === selectedPageId) ?? refreshed.pages[0] ?? null;
      setPageDisposition(current?.pendingDecision?.disposition ?? current?.disposition ?? "INCLUDE");
      setPageReason(current?.pendingDecision?.reason ?? current?.reason ?? "");
      setOriginalCompared(false);
      setManifestConfirmed(false);
      const group = refreshed.duplicateGroups.find((item) => current && item.pageIds.includes(current.pageId));
      setCanonicalPageId(group?.canonicalPageId ?? group?.pageIds[0] ?? null);
      setReviewNotice(`${success}；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setReviewNotice(reason instanceof Error ? reason.message : "证据决定未记录");
    } finally {
      setReviewBusy(null);
    }
  }

  function pageLabel(pageId: string): string {
    const page = review?.pages.find((item) => item.pageId === pageId);
    return page ? `${page.originalLabel} · 第 ${page.pageNumber} 页` : pageId;
  }

  return (
    <section className={styles.evidenceArea} aria-label="证据核验台">
      <header className={styles.evidenceHeading}>
        <div>
          <p className={styles.eyebrow}>证据工作台</p>
          <h2>原始页逐页核验</h2>
        </div>
        <div className={styles.evidenceSnapshotState}>
          <strong>{review.sourceLabel}</strong>
          <span>{review.pages.length} 页来源页 · {unresolvedCount} 页待处置</span>
          <small>版本 {review.matterVersion ?? "合成"} · 快照 {review.snapshotHash.slice(0, 12)}</small>
        </div>
      </header>

      <div className={styles.evidenceColumns}>
        <aside className={styles.pageList}>
          <div className={styles.listHeading}><span>全部来源页</span><small>零静默排除</small></div>
          {review.pages.map((item) => (
            <button
              className={`${styles.pageItem} ${item.pageId === selected.pageId ? styles.pageSelected : ""}`}
              key={item.pageId}
              onClick={() => {
                setSelectedPageId(item.pageId);
                setPageDisposition(item.pendingDecision?.disposition ?? item.disposition ?? "INCLUDE");
                setPageReason(item.pendingDecision?.reason ?? item.reason ?? "");
                setOriginalCompared(false);
                setManifestConfirmed(false);
                const group = review.duplicateGroups.find((candidate) => candidate.pageIds.includes(item.pageId));
                setCanonicalPageId(group?.canonicalPageId ?? group?.pageIds[0] ?? null);
              }}
              type="button"
            >
              <span className={styles.pageNumber}>第 {item.pageNumber} 页</span>
              <strong>{item.pendingDecision ? "候选" : item.disposition === "INCLUDE" ? "纳入" : item.disposition === "EXCLUDE" ? "排除" : "待审"}</strong>
              <small title={item.originalLabel}>{item.originalLabel}</small>
              <em className={statusClass(item)}>{pageStatus(item)}</em>
            </button>
          ))}
        </aside>

        <article className={styles.documentStage}>
          <div className={styles.documentToolbar}>
            <span>原始页定位 · 第 {selected.pageNumber} 页</span>
            <span>{review.sourceKind === "synthetic-alpha" ? "合成预览" : "对象预览待安全接入"}</span>
          </div>
          {artifactPreview ? (
            <div className={styles.artifactPreview}>
              <div className={styles.artifactPreviewHeader}>
                <div><strong>{artifactPreview.label}</strong><small>SHA-256 {artifactPreview.sha256.slice(0, 18)}…</small></div>
                <button type="button" onClick={() => setArtifactPreview((prior) => {
                  if (prior) URL.revokeObjectURL(prior.url);
                  return null;
                })}>关闭预览</button>
              </div>
              <iframe src={artifactPreview.url} title={`${artifactPreview.label} PDF 预览`} sandbox="" />
            </div>
          ) : selected.syntheticPreview ? (
            <div className={styles.documentPaper} aria-label={`合成交易记录第 ${selected.pageNumber} 页`}>
              <div className={styles.documentBrand}>微信支付 <small>合成示例</small></div>
              <div className={styles.documentTitle}>交易明细证明</div>
              <div className={styles.documentMeta}><span>交易时间</span><strong>{selected.syntheticPreview.date} 10:16</strong></div>
              <div className={`${styles.transactionRow} ${selected.annotations.length ? styles.redBox : ""}`}>
                <div><span>转账给</span><strong>{selected.syntheticPreview.counterpart}</strong></div>
                <b>{selected.syntheticPreview.amount}</b>
              </div>
              <div className={styles.documentMeta}><span>交易单号</span><strong>ALPHA-TRX-{String(selected.pageNumber).padStart(4, "0")}</strong></div>
              <p className={styles.documentFootnote}>红框仅为经批准的页内坐标示意，不修改原件，也不自动完成法律定性。</p>
            </div>
          ) : (
            <div className={styles.sourcePreviewUnavailable}>
              <p className={styles.eyebrow}>原件影像未传到浏览器</p>
              <h3>{selected.originalLabel}</h3>
              <p>当前持久化快照只返回页标识、处置、重复组和红框坐标。对象存储签名读取与原件影像渲染尚未通过安全门，因此这里不会伪造预览。</p>
              <dl>
                <div><dt>文件哈希</dt><dd>{source?.originalFileSha256.slice(0, 18) ?? "—"}…</dd></div>
                <div><dt>来源页</dt><dd>第 {selected.pageNumber} 页</dd></div>
                <div><dt>批准标注</dt><dd>{selected.annotations.filter((item) => item.status === "APPROVED").length} 个</dd></div>
              </dl>
            </div>
          )}
          <p className={styles.sourceNote}>来源层：原始文件与来源页不可修改；相关页 PDF、红框 PDF 和提交件只能从锁定 Manifest 派生。</p>
        </article>

        <aside className={styles.inspector}>
          <p className={styles.eyebrow}>核验说明</p>
          <h3>第 {selected.pageNumber} 页</h3>
          <dl className={styles.inspectorFacts}>
            <div><dt>原始文件</dt><dd title={selected.originalLabel}>{selected.originalLabel}</dd></div>
            <div><dt>页级处置</dt><dd className={statusClass(selected)}>{pageStatus(selected)}</dd></div>
            <div><dt>批准红框</dt><dd>{selected.annotations.filter((item) => item.status === "APPROVED").length} 个</dd></div>
            <div><dt>重复组</dt><dd>{duplicateGroup ? duplicateGroup.status : "无"}</dd></div>
          </dl>
          <p className={styles.inspectorNote}>{selected.reason ?? selected.syntheticPreview?.note ?? "该页尚无律师批准的纳入/排除理由。"}</p>

          {review.sourceKind === "persistent-preview" && !review.lockedManifest && (
            <div className={styles.decisionPanel}>
              <strong>本页正式处置</strong>
              {selected.pendingDecision ? (
                <>
                  <div className={styles.pendingDecisionSummary}>
                    <span>待批准：{selected.pendingDecision.disposition === "INCLUDE" ? "纳入相关页 PDF" : "从派生提交件排除"}</span>
                    <small>{selected.pendingDecision.reason}</small>
                  </div>
                  <label className={styles.confirmLine}>
                    <input checked={originalCompared} onChange={(event) => setOriginalCompared(event.target.checked)} type="checkbox" />
                    我已对照原始文件第 {selected.pageNumber} 页核验该决定
                  </label>
                  <button
                    disabled={!originalCompared || reviewBusy !== null}
                    type="button"
                    onClick={() => void refreshAfterEvidenceMutation(
                      `approve-page:${selected.pageId}`,
                      () => approveEvidencePageDecision({ review, page: selected }),
                      "本页处置已由律师批准",
                    )}
                  >
                    {reviewBusy === `approve-page:${selected.pageId}` ? "正在记录批准…" : "律师批准本页处置"}
                  </button>
                </>
              ) : (
                <>
                  <label htmlFor="page-disposition">拟定处置</label>
                  <select id="page-disposition" value={pageDisposition} onChange={(event) => setPageDisposition(event.target.value as "INCLUDE" | "EXCLUDE") }>
                    <option value="INCLUDE">纳入相关页 PDF</option>
                    <option value="EXCLUDE">排除，仅保留原件审计记录</option>
                  </select>
                  <label htmlFor="page-reason">处置理由</label>
                  <textarea id="page-reason" maxLength={2000} onChange={(event) => setPageReason(event.target.value)} placeholder="例如：与目标主体的微信交易相关；或该页与本案无关。" value={pageReason} />
                  <button
                    disabled={!pageReason.trim() || reviewBusy !== null}
                    type="button"
                    onClick={() => void refreshAfterEvidenceMutation(
                      `propose-page:${selected.pageId}`,
                      () => proposeEvidencePageDecision({ review, pageId: selected.pageId, disposition: pageDisposition, reason: pageReason }),
                      "本页处置候选已建立，尚未批准",
                    )}
                  >
                    {reviewBusy === `propose-page:${selected.pageId}` ? "正在建立候选…" : "提交本页处置候选"}
                  </button>
                </>
              )}
            </div>
          )}

          {selected.annotations.length > 0 && (
            <div className={styles.coordinateList}>
              <strong>红框坐标</strong>
              {review.sourceKind === "persistent-preview" && !review.lockedManifest && selected.annotations.some((item) => item.status === "CANDIDATE") && !selected.pendingDecision && duplicateGroup?.status !== "CANDIDATE" && (
                <label className={styles.confirmLine}>
                  <input checked={originalCompared} onChange={(event) => setOriginalCompared(event.target.checked)} type="checkbox" />
                  我已对照原始文件第 {selected.pageNumber} 页核验候选红框
                </label>
              )}
              {selected.annotations.map((annotation) => (
                <div className={styles.coordinateItem} key={annotation.annotationId}>
                  <span>{annotation.label} · ({annotation.x0}, {annotation.y0})—({annotation.x1}, {annotation.y1}) · {annotation.status === "APPROVED" ? "已批准" : "待批准"}</span>
                  {review.sourceKind === "persistent-preview" && !review.lockedManifest && annotation.status === "CANDIDATE" && (
                    <button
                      disabled={!originalCompared || reviewBusy !== null}
                      type="button"
                      onClick={() => void refreshAfterEvidenceMutation(
                        `approve-annotation:${annotation.annotationId}`,
                        () => approveEvidenceAnnotation({ review, page: selected, annotationId: annotation.annotationId }),
                        "红框坐标已由律师批准",
                      )}
                    >
                      {reviewBusy === `approve-annotation:${annotation.annotationId}` ? "正在批准…" : "批准红框"}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          {review.sourceKind === "persistent-preview" && !review.lockedManifest && duplicateGroup?.status === "CANDIDATE" && (
            <div className={styles.decisionPanel}>
              <strong>重复页裁决</strong>
              <label htmlFor="persistent-duplicate-decision">页面关系</label>
              <select id="persistent-duplicate-decision" value={duplicateResolution} onChange={(event) => setDuplicateResolution(event.target.value as "same" | "distinct") }>
                <option value="same">确为同一来源页，只保留一页</option>
                <option value="distinct">不是重复页，均保留独立判断</option>
              </select>
              {duplicateResolution === "same" && (
                <>
                  <label htmlFor="canonical-page">唯一保留页</label>
                  <select id="canonical-page" value={canonicalPageId ?? ""} onChange={(event) => setCanonicalPageId(event.target.value)}>
                    {duplicateGroup.pageIds.map((pageId) => <option key={pageId} value={pageId}>{pageLabel(pageId)}</option>)}
                  </select>
                </>
              )}
              <label className={styles.confirmLine}>
                <input checked={originalCompared} onChange={(event) => setOriginalCompared(event.target.checked)} type="checkbox" />
                我已逐页对照原始文件核验该重复页结论
              </label>
              <button
                disabled={!originalCompared || reviewBusy !== null}
                type="button"
                onClick={() => void refreshAfterEvidenceMutation(
                  `resolve-duplicate:${duplicateGroup.groupId}`,
                  () => resolveEvidenceDuplicateGroup({
                    review,
                    groupId: duplicateGroup.groupId,
                    sameSourcePage: duplicateResolution === "same",
                    canonicalPageId: duplicateResolution === "same" ? canonicalPageId : null,
                  }),
                  "重复页结论已由律师批准",
                )}
              >
                {reviewBusy === `resolve-duplicate:${duplicateGroup.groupId}` ? "正在记录裁决…" : "律师确认重复页结论"}
              </button>
            </div>
          )}

          {review.sourceKind === "synthetic-alpha" && duplicateGroup?.status === "CANDIDATE" && (
            <div className={styles.decisionPanel}>
              <label htmlFor="duplicate-decision">律师决定（仅合成界面动作）</label>
              <select id="duplicate-decision" value={duplicateDecision} onChange={(event) => setDuplicateDecision(event.target.value as DuplicateDecision)}>
                <option value="pending">尚未决定</option>
                <option value="exclude">排除重复派生引用</option>
                <option value="keep">保留为不同页</option>
              </select>
              <button type="button" onClick={recordSyntheticDecision}>记录合成界面动作</button>
            </div>
          )}

          {review.sourceKind === "synthetic-alpha" && <div className={styles.auditNotice} role="status">{auditNotice}</div>}
          <div className={styles.manifestState}>
            <span>当前 Manifest</span>
            <strong>{review.lockedManifest ? "已锁定" : "尚未锁定"}</strong>
            <small>{review.lockedManifest ? `${review.lockedManifest.includedPages} 页纳入 / ${review.lockedManifest.excludedPages} 页排除` : `仍有 ${unresolvedCount} 页待律师处置`}</small>
            {!review.lockedManifest && <small>待批准处置 {pendingDecisionCount} 项 · 未裁决重复组 {unresolvedDuplicateCount} 组</small>}
            <small>派生件：{review.derivatives.length ? review.derivatives.map((item) => `${item.artifactType === "ANNOTATED_RELATED_PAGES_PDF" ? "红框版" : "相关页版"} ${item.status}`).join("；") : "尚未生成"}</small>
            <small>任务：{review.derivativeRuns.length ? review.derivativeRuns.map((item) => `${runStatusLabel(item.status)}（尝试 ${item.attemptCount}/3${item.failureCode ? ` · ${item.failureCode}` : ""}）`).join("；") : "尚未建立"}</small>
          </div>
          {review.sourceKind === "persistent-preview" && !review.lockedManifest && unresolvedCount === 0 && pendingDecisionCount === 0 && unresolvedDuplicateCount === 0 && (
            <div className={styles.lockPanel}>
              <label className={styles.confirmLine}>
                <input checked={manifestConfirmed} onChange={(event) => setManifestConfirmed(event.target.checked)} type="checkbox" />
                我确认全部来源页处置、重复页结论和已批准红框构成当前提交依据
              </label>
              <button
                disabled={!manifestConfirmed || reviewBusy !== null}
                type="button"
                onClick={() => void refreshAfterEvidenceMutation(
                  "lock-manifest",
                  () => lockEvidenceManifest(review),
                  "证据清单已锁定",
                )}
              >
                {reviewBusy === "lock-manifest" ? "正在锁定…" : "律师锁定证据清单"}
              </button>
            </div>
          )}
          {review.sourceKind === "persistent-preview" && review.lockedManifest && !review.derivativeRuns.some((item) => ["QUEUED", "RUNNING", "SUCCEEDED"].includes(item.status)) && (
            <button className={styles.primaryArtifactAction} disabled={artifactBusy !== null} type="button" onClick={() => void enqueueDerivativeRun()}>
              {artifactBusy === "enqueue" ? "正在建立任务…" : review.derivativeRuns.some((item) => item.status === "FAILED") ? "重新生成相关页 PDF" : "生成相关页 PDF"}
            </button>
          )}
          {review.sourceKind === "persistent-preview" && review.derivatives.some((item) => item.status === "VERIFIED") && (
            <div className={styles.artifactActions}>
              {review.derivatives.filter((item) => item.status === "VERIFIED").map((item) => (
                <div key={item.derivativeId}>
                  <strong>{item.artifactType === "ANNOTATED_RELATED_PAGES_PDF" ? "红框相关页" : "相关页"}</strong>
                  <span>{item.pageCount} 页 · {item.artifactSha256.slice(0, 12)}…</span>
                  <button disabled={artifactBusy !== null} type="button" onClick={() => void readDerivative(item, "INLINE_PREVIEW")}>
                    {artifactBusy === `${item.derivativeId}:INLINE_PREVIEW` ? "正在核验…" : "本机预览"}
                  </button>
                  <button disabled={artifactBusy !== null} type="button" onClick={() => void readDerivative(item, "DOWNLOAD")}>
                    {artifactBusy === `${item.derivativeId}:DOWNLOAD` ? "正在准备…" : "下载 PDF"}
                  </button>
                </div>
              ))}
            </div>
          )}
          {artifactNotice && <div className={styles.auditNotice} role="status">{artifactNotice}</div>}
          {reviewNotice && <div className={styles.auditNotice} role="status">{reviewNotice}</div>}
          {review.sourceKind === "synthetic-alpha" && <button className={styles.disabledAction} disabled type="button">生成提交材料（合成模式不生成正式文件）</button>}
        </aside>
      </div>
    </section>
  );
}

function runStatusLabel(status: string): string {
  if (status === "QUEUED") return "等待本机 Worker";
  if (status === "RUNNING") return "正在生成并核验";
  if (status === "SUCCEEDED") return "生成完成";
  if (status === "FAILED") return "生成失败";
  return status;
}
