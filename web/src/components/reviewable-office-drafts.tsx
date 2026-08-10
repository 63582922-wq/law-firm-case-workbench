"use client";

import { useEffect, useState } from "react";
import {
  approveReviewableOfficeDraft,
  fetchReviewableOfficeDraft,
  loadReviewableOfficeDrafts,
  type ReviewableOfficeDraftReviewView,
} from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

export function ReviewableOfficeDrafts() {
  const [review, setReview] = useState<ReviewableOfficeDraftReviewView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyPairId, setBusyPairId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = async () => {
    setError(null);
    try {
      setReview(await loadReviewableOfficeDrafts());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文书审阅快照读取失败");
    }
  };

  useEffect(() => {
    let active = true;
    loadReviewableOfficeDrafts()
      .then((result) => {
        if (active) setReview(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "文书审阅快照读取失败");
      });
    return () => {
      active = false;
    };
  }, []);

  async function viewPdf(pair: NonNullable<ReviewableOfficeDraftReviewView>["pairs"][number]) {
    setBusyPairId(pair.pairId);
    setNotice(null);
    try {
      const delivery = await fetchReviewableOfficeDraft(pair, "REVIEW_PDF");
      const url = URL.createObjectURL(delivery.blob);
      const link = document.createElement("a");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
      setNotice("已打开一次性取得的 PDF 审阅稿。请核对内容、页数和版式后再确认。 ");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "PDF 审阅稿读取失败");
    } finally {
      setBusyPairId(null);
    }
  }

  async function downloadEditable(pair: NonNullable<ReviewableOfficeDraftReviewView>["pairs"][number]) {
    setBusyPairId(pair.pairId);
    setNotice(null);
    try {
      const delivery = await fetchReviewableOfficeDraft(pair, "DOWNLOAD_EDITABLE");
      const url = URL.createObjectURL(delivery.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = delivery.fileName;
      link.rel = "noopener";
      link.click();
      URL.revokeObjectURL(url);
      setNotice("已下载可编辑内部草稿。它不是法院提交件；修改后必须重新生成并审阅。 ");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "可编辑文书下载失败");
    } finally {
      setBusyPairId(null);
    }
  }

  async function approve(pair: NonNullable<ReviewableOfficeDraftReviewView>["pairs"][number]) {
    if (!review || !window.confirm("确认前请已审阅对应 PDF。确认仅批准这一份绑定哈希的内部草稿，不会直接生成法院提交文件。是否继续？")) return;
    setBusyPairId(pair.pairId);
    setNotice(null);
    try {
      await approveReviewableOfficeDraft(review, pair);
      setNotice("已确认该内部草稿对；法院 PDF 成品、QA 与提交锁定仍须分别完成。 ");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文书确认失败");
    } finally {
      setBusyPairId(null);
    }
  }

  return (
    <section className={styles.officeDraftArea} aria-label="内部文书审阅">
      <div className={styles.officeDraftHeading}>
        <div>
          <p className={styles.eyebrow}>内部文书审阅</p>
          <h3>先看 PDF，再确认对应 Word 或 Excel 草稿</h3>
          <p>候选件与 PDF 预览、可编辑源件、审批输入和渲染回执使用同一复核哈希绑定；内部草稿不会直接进入法院提交包。</p>
        </div>
        <button onClick={() => void refresh()} type="button">刷新候选件</button>
      </div>
      {error && <p className={styles.officeDraftError} role="alert">{error}</p>}
      {notice && <p className={styles.officeDraftNotice} role="status">{notice}</p>}
      {!review ? <div className={styles.calculationLoading}>正在读取可审阅文书候选件…</div> : review.pairs.length === 0 ? (
        <div className={styles.officeDraftEmpty}>
          <strong>{review.sourceKind === "synthetic-alpha" ? "合成模式不生成可编辑文书" : "当前没有可审阅的内部 Word / Excel 草稿"}</strong>
          <span>{review.sourceKind === "synthetic-alpha" ? "不会以示例文书替代真实案件材料。" : "系统生成后必须先形成“可编辑源件 + PDF 审阅稿”的核验对，才会出现在这里。"}</span>
        </div>
      ) : (
        <div className={styles.officeDraftList}>
          {review.pairs.map((pair) => {
            const busy = busyPairId === pair.pairId;
            return (
              <article key={pair.pairId}>
                <div className={styles.officeDraftMeta}>
                  <strong>{documentKindLabel(pair.documentKind)}</strong>
                  <span>{pair.editableMediaType.endsWith("document") ? "Word 草稿" : "Excel 草稿"} · PDF {pair.reviewPdfPageCount} 页</span>
                  <code>复核 {shortHash(pair.reviewInputHash)}</code>
                </div>
                <span className={pair.status === "APPROVED" ? styles.officeDraftApproved : styles.officeDraftCandidate}>
                  {pair.status === "APPROVED" ? "已确认" : "待确认"}
                </span>
                <div className={styles.officeDraftActions}>
                  <button disabled={busy} onClick={() => void viewPdf(pair)} type="button">审阅 PDF</button>
                  <button disabled={busy} onClick={() => void downloadEditable(pair)} type="button">下载 {pair.editableMediaType.endsWith("document") ? "Word" : "Excel"}</button>
                  {pair.status === "CANDIDATE" && <button className={styles.officeDraftApprove} disabled={busy} onClick={() => void approve(pair)} type="button">确认此草稿</button>}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function documentKindLabel(kind: string) {
  const labels: Record<string, string> = {
    DEFENCE_STATEMENT: "民事答辩状",
    INTEREST_CALCULATION: "利息测算表",
    EVIDENCE_INDEX: "证据目录",
  };
  return labels[kind] ?? kind;
}

function shortHash(value: string) {
  return `${value.slice(0, 8)}…${value.slice(-6)}`;
}
