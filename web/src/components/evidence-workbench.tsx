"use client";

import { useEffect, useMemo, useState } from "react";
import {
  caseDataSourceConfig,
  fetchEvidenceDerivative,
  loadEvidenceReview,
  type EvidenceDerivative,
  type EvidenceReviewPage,
  type EvidenceReviewView,
} from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

type DuplicateDecision = "pending" | "exclude" | "keep";

function pageStatus(page: EvidenceReviewPage): string {
  if (!page.decisionId) return "待律师逐页处置";
  return page.disposition === "INCLUDE" ? "已批准纳入" : "已批准排除";
}

function statusClass(page: EvidenceReviewPage): string {
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
        setSelectedPageId(result.pages[0]?.pageId ?? null);
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
              onClick={() => setSelectedPageId(item.pageId)}
              type="button"
            >
              <span className={styles.pageNumber}>第 {item.pageNumber} 页</span>
              <strong>{item.disposition === "INCLUDE" ? "纳入" : item.disposition === "EXCLUDE" ? "排除" : "待审"}</strong>
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

          {selected.annotations.length > 0 && (
            <div className={styles.coordinateList}>
              <strong>红框坐标</strong>
              {selected.annotations.map((annotation) => (
                <span key={annotation.annotationId}>{annotation.label} · ({annotation.x0}, {annotation.y0})—({annotation.x1}, {annotation.y1})</span>
              ))}
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
            <small>派生件：{review.derivatives.length ? review.derivatives.map((item) => `${item.artifactType === "ANNOTATED_RELATED_PAGES_PDF" ? "红框版" : "相关页版"} ${item.status}`).join("；") : "尚未生成"}</small>
          </div>
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
          {review.sourceKind === "synthetic-alpha" && <button className={styles.disabledAction} disabled type="button">生成提交材料（合成模式不生成正式文件）</button>}
        </aside>
      </div>
    </section>
  );
}
