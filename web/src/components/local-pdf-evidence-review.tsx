"use client";

import { useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import styles from "./case-workbench.module.css";

type AsyncAction = () => void | Promise<void>;

/**
 * The local folder is deliberately represented only by a display label.  An
 * absolute path, desktop grant, or file handle must stay outside the WebView.
 */
export interface LocalPdfEvidenceMaterialFolder {
  displayName: string;
  accessState: "RESELECT_REQUIRED" | "AUTHORIZED" | "UNAVAILABLE";
  message?: string;
}

export type LocalPdfEvidenceReviewState = "LOADING" | "READY" | "UNAVAILABLE" | "ERROR";
export type LocalPdfEvidencePageDisposition = "UNREVIEWED" | "INCLUDE" | "EXCLUDE";
export type LocalPdfEvidenceSelectionState = "DRAFT" | "LOCKED" | "STALE";
export type LocalPdfEvidencePdfKind = "RELATED_PAGES" | "RELATED_PAGES_WITH_BOXES";
export type LocalPdfEvidencePdfStatus = "NOT_REQUESTED" | "QUEUED" | "RUNNING" | "VERIFIED" | "FAILED" | "UNAVAILABLE";

export interface LocalPdfEvidenceBox {
  boxId: string;
  /** Normalized coordinates in the source page: 0 <= x0 < x1 <= 1, 0 <= y0 < y1 <= 1. */
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  label?: string;
}

export interface LocalPdfEvidencePreview {
  /** The parent supplies a CSP-safe single-page preview slot, not an embedded PDF. */
  status: "READY" | "LOADING" | "UNAVAILABLE" | "ERROR";
  imageUrl?: string;
  alt?: string;
  message?: string;
}

export interface LocalPdfEvidencePage {
  pageId: string;
  documentId: string;
  documentLabel: string;
  pageNumber: number;
  disposition: LocalPdfEvidencePageDisposition;
  boxes: readonly LocalPdfEvidenceBox[];
  preview?: LocalPdfEvidencePreview;
}

/** An exact-duplicate warning must come from verified parent data, never filename heuristics. */
export interface LocalPdfEvidenceExactDuplicate {
  duplicateId: string;
  documentLabel: string;
  matchingDocumentLabel: string;
  resolved: boolean;
  message?: string;
}

export interface LocalPdfEvidenceSelection {
  state: LocalPdfEvidenceSelectionState;
  lockBlockedReason?: string;
}

export interface LocalPdfEvidencePdfOutput {
  kind: LocalPdfEvidencePdfKind;
  status: LocalPdfEvidencePdfStatus;
  message?: string;
}

export interface LocalPdfEvidenceReviewProps {
  state: LocalPdfEvidenceReviewState;
  materialFolder?: LocalPdfEvidenceMaterialFolder;
  pages?: readonly LocalPdfEvidencePage[];
  activePageId?: string | null;
  exactDuplicates?: readonly LocalPdfEvidenceExactDuplicate[];
  selection: LocalPdfEvidenceSelection;
  pdfOutputs?: readonly LocalPdfEvidencePdfOutput[];
  unavailableMessage?: string;
  errorMessage?: string;
  onRetry?: AsyncAction;
  onReselectMaterialFolder?: AsyncAction;
  onSelectPage?: (pageId: string) => void;
  onSetPageDisposition?: (input: {
    pageId: string;
    disposition: Exclude<LocalPdfEvidencePageDisposition, "UNREVIEWED">;
  }) => void | Promise<void>;
  onAddBox?: (input: {
    pageId: string;
    box: Omit<LocalPdfEvidenceBox, "boxId">;
  }) => void | Promise<void>;
  onRemoveBox?: (input: { pageId: string; boxId: string }) => void | Promise<void>;
  onLockSelection?: AsyncAction;
  onGeneratePdf?: (kind: LocalPdfEvidencePdfKind) => void | Promise<void>;
}

type NormalizedPoint = { x: number; y: number };
type DraftBox = Omit<LocalPdfEvidenceBox, "boxId" | "label">;

const outputKinds: readonly LocalPdfEvidencePdfKind[] = ["RELATED_PAGES", "RELATED_PAGES_WITH_BOXES"];

export function LocalPdfEvidenceReview({
  state,
  materialFolder,
  pages = [],
  activePageId,
  exactDuplicates = [],
  selection,
  pdfOutputs = [],
  unavailableMessage,
  errorMessage,
  onRetry,
  onReselectMaterialFolder,
  onSelectPage,
  onSetPageDisposition,
  onAddBox,
  onRemoveBox,
  onLockSelection,
  onGeneratePdf,
}: LocalPdfEvidenceReviewProps) {
  const [internalPageId, setInternalPageId] = useState<string | null>(null);
  const [draftBox, setDraftBox] = useState<DraftBox | null>(null);
  const [draftPageId, setDraftPageId] = useState<string | null>(null);
  const [draftLabel, setDraftLabel] = useState("");
  const [lockConfirmed, setLockConfirmed] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const dragStart = useRef<NormalizedPoint | null>(null);

  const selectedPage = useMemo(() => {
    const targetId = activePageId ?? internalPageId;
    return pages.find((page) => page.pageId === targetId) ?? pages[0] ?? null;
  }, [activePageId, internalPageId, pages]);

  const outputByKind = useMemo(
    () => new Map(pdfOutputs.map((output) => [output.kind, output])),
    [pdfOutputs],
  );
  const unresolvedPageCount = pages.filter((page) => page.disposition === "UNREVIEWED").length;
  const unresolvedDuplicateCount = exactDuplicates.filter((duplicate) => !duplicate.resolved).length;
  const folderAuthorized = materialFolder?.accessState === "AUTHORIZED";
  const canEdit = state === "READY" && folderAuthorized && selection.state === "DRAFT";
  const currentDraftBox = draftPageId === selectedPage?.pageId ? draftBox : null;
  const lockBlockedReason = selection.lockBlockedReason
    ?? (!folderAuthorized
      ? "需要先重新选择并确认当前材料文件夹，才能锁定或生成派生件。"
      : unresolvedPageCount > 0
        ? `还有 ${unresolvedPageCount} 页尚未决定纳入或排除。`
        : unresolvedDuplicateCount > 0
          ? `还有 ${unresolvedDuplicateCount} 份完全重复的 PDF 未处理。`
          : null);

  async function runAction(actionKey: string, action: AsyncAction | undefined) {
    if (!action) return;
    setBusyAction(actionKey);
    setActionError(null);
    try {
      await action();
    } catch (reason: unknown) {
      setActionError(reason instanceof Error ? reason.message : "本次操作未完成；请先核对当前材料状态后再试。");
    } finally {
      setBusyAction(null);
    }
  }

  function selectPage(pageId: string) {
    setInternalPageId(pageId);
    setDraftBox(null);
    setDraftPageId(null);
    setDraftLabel("");
    setActionError(null);
    onSelectPage?.(pageId);
  }

  function normalizedPoint(event: ReactPointerEvent<HTMLDivElement>): NormalizedPoint {
    const rect = event.currentTarget.getBoundingClientRect();
    return {
      x: clamp((event.clientX - rect.left) / rect.width),
      y: clamp((event.clientY - rect.top) / rect.height),
    };
  }

  function beginBox(event: ReactPointerEvent<HTMLDivElement>) {
    if (!canEdit || !selectedPage?.preview?.imageUrl || !onAddBox) return;
    event.preventDefault();
    const point = normalizedPoint(event);
    dragStart.current = point;
    setDraftBox({ x0: point.x, y0: point.y, x1: point.x, y1: point.y });
    setDraftPageId(selectedPage.pageId);
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
    const nextBox = normalizeBox({ x0: start.x, y0: start.y, x1: point.x, y1: point.y });
    if (nextBox.x1 - nextBox.x0 < 0.012 || nextBox.y1 - nextBox.y0 < 0.012) {
      setDraftBox(null);
      setDraftPageId(null);
      setActionError("请拖拽出一个可见范围后再添加红框。");
      return;
    }
    setDraftBox(nextBox);
  }

  async function addDraftBox() {
    if (!selectedPage || !currentDraftBox || !onAddBox) return;
    const actionKey = `add-box:${selectedPage.pageId}`;
    await runAction(actionKey, async () => {
      await onAddBox({
        pageId: selectedPage.pageId,
        box: { ...currentDraftBox, label: draftLabel.trim() || undefined },
      });
      setDraftBox(null);
      setDraftPageId(null);
      setDraftLabel("");
    });
  }

  if (state === "LOADING") {
    return <ReviewState title="正在读取本机 PDF 审阅状态" description="正在等待上层提供当前材料范围与单页预览；不会以示例页面替代原件。" />;
  }

  if (state === "UNAVAILABLE") {
    return (
      <ReviewState
        title="本机 PDF 审阅暂不可用"
        description={unavailableMessage ?? "当前没有可用的本机材料范围或页级预览，因此不会展示任何替代内容。"}
        actionLabel={onReselectMaterialFolder ? "重新选择当前材料文件夹" : undefined}
        onAction={onReselectMaterialFolder}
      />
    );
  }

  if (state === "ERROR") {
    return (
      <ReviewState
        title="无法读取本机 PDF 审阅状态"
        description={errorMessage ?? "材料状态读取失败。系统没有改写原件，也不会以其他材料或演示内容代替。"}
        actionLabel={onRetry ? "重新读取" : undefined}
        onAction={onRetry}
      />
    );
  }

  return (
    <section className={styles.localPdfEvidenceReview} aria-label="本机 PDF 证据审阅">
      <header className={styles.localPdfEvidenceHeading}>
        <div>
          <p className={styles.localPdfEvidenceEyebrow}>本机 PDF 证据整理</p>
          <h2>逐页确认后，再生成派生 PDF</h2>
          <p>原始 PDF 始终保留在律师选择的材料文件夹中；本页只处理“纳入、排除与红框”的派生选择。</p>
        </div>
        <dl className={styles.localPdfEvidenceSummary}>
          <div><dt>已纳入</dt><dd>{pages.filter((page) => page.disposition === "INCLUDE").length} 页</dd></div>
          <div><dt>待处置</dt><dd>{unresolvedPageCount} 页</dd></div>
          <div><dt>选择状态</dt><dd>{selectionLabel(selection.state)}</dd></div>
        </dl>
      </header>

      <FolderNotice
        busy={busyAction === "reselect-folder"}
        folder={materialFolder}
        onReselect={onReselectMaterialFolder ? () => runAction("reselect-folder", onReselectMaterialFolder) : undefined}
      />

      {exactDuplicates.length > 0 && (
        <section className={styles.localPdfEvidenceDuplicateWarning} aria-label="完全重复 PDF 提醒">
          <div>
            <p className={styles.localPdfEvidenceEyebrow}>重复风险</p>
            <h3>发现由上层确认的完全重复 PDF</h3>
          </div>
          <ul>
            {exactDuplicates.map((duplicate) => (
              <li key={duplicate.duplicateId}>
                <strong>{duplicate.documentLabel}</strong>
                <span>与“{duplicate.matchingDocumentLabel}”的完整文件一致</span>
                <small>{duplicate.message ?? (duplicate.resolved ? "已在上层工作流中完成处理。" : "请在锁定前核对并明确处理；原件不会被删除。")}</small>
              </li>
            ))}
          </ul>
        </section>
      )}

      {actionError && <p className={styles.localPdfEvidenceActionError} role="alert">{actionError}</p>}

      {pages.length === 0 ? (
        <section className={styles.localPdfEvidenceEmpty}>
          <p className={styles.localPdfEvidenceEyebrow}>尚无页级材料</p>
          <h3>当前材料范围还没有可审阅的 PDF 页面</h3>
          <p>请先重新选择并确认当前材料文件夹，或等待上层完成本机页面盘点。组件不会补造页面、文件名或案件内容。</p>
        </section>
      ) : selectedPage ? (
        <div className={styles.localPdfEvidenceLayout}>
          <aside className={styles.localPdfEvidencePageList} aria-label="PDF 页面列表">
            <header>
              <strong>页面</strong>
              <span>{pages.length} 页</span>
            </header>
            <div>
              {pages.map((page) => {
                const isSelected = page.pageId === selectedPage.pageId;
                return (
                  <button
                    aria-current={isSelected ? "page" : undefined}
                    className={`${styles.localPdfEvidencePageItem} ${isSelected ? styles.localPdfEvidencePageItemSelected : ""}`}
                    key={page.pageId}
                    onClick={() => selectPage(page.pageId)}
                    type="button"
                  >
                    <span>第 {page.pageNumber} 页</span>
                    <strong title={page.documentLabel}>{page.documentLabel}</strong>
                    <em className={pageDispositionClass(page.disposition, styles)}>{pageDispositionLabel(page.disposition)}</em>
                  </button>
                );
              })}
            </div>
          </aside>

          <article className={styles.localPdfEvidencePreview} aria-label={`第 ${selectedPage.pageNumber} 页预览`}>
            <header>
              <div>
                <p className={styles.localPdfEvidenceEyebrow}>原始页面预览</p>
                <h3>{selectedPage.documentLabel}</h3>
              </div>
              <span>第 {selectedPage.pageNumber} 页</span>
            </header>
            <PreviewCanvas
              canPreview={folderAuthorized}
              page={selectedPage}
              canDraw={canEdit && Boolean(onAddBox)}
              draftBox={currentDraftBox}
              onPointerDown={beginBox}
              onPointerMove={updateBox}
              onPointerUp={finishBox}
              onPointerCancel={() => {
                dragStart.current = null;
                setDraftBox(null);
                setDraftPageId(null);
              }}
            />
            <footer>
              {canEdit && onAddBox
                ? "在本页预览上拖拽可框选红框；坐标会以本页 0—1 相对比例交给上层工作流。"
                : "当前页为只读状态；在材料文件夹授权、页级选择和锁定状态允许前，不会创建红框。"}
            </footer>
          </article>

          <aside className={styles.localPdfEvidenceInspector} aria-label="当前页面处理">
            <section>
              <p className={styles.localPdfEvidenceEyebrow}>本页处理</p>
              <h3>第 {selectedPage.pageNumber} 页</h3>
              <dl>
                <div><dt>来源文件</dt><dd title={selectedPage.documentLabel}>{selectedPage.documentLabel}</dd></div>
                <div><dt>当前选择</dt><dd>{pageDispositionLabel(selectedPage.disposition)}</dd></div>
                <div><dt>已标红</dt><dd>{selectedPage.boxes.length} 处</dd></div>
              </dl>
              <div className={styles.localPdfEvidenceDispositionActions}>
                <button
                  aria-pressed={selectedPage.disposition === "INCLUDE"}
                  className={selectedPage.disposition === "INCLUDE" ? styles.localPdfEvidenceDispositionActive : ""}
                  disabled={!canEdit || !onSetPageDisposition || busyAction !== null}
                  onClick={() => void runAction(`include:${selectedPage.pageId}`, () => onSetPageDisposition?.({ pageId: selectedPage.pageId, disposition: "INCLUDE" }))}
                  type="button"
                >
                  {busyAction === `include:${selectedPage.pageId}` ? "正在提交…" : "纳入派生 PDF"}
                </button>
                <button
                  aria-pressed={selectedPage.disposition === "EXCLUDE"}
                  className={selectedPage.disposition === "EXCLUDE" ? styles.localPdfEvidenceDispositionExclude : ""}
                  disabled={!canEdit || !onSetPageDisposition || busyAction !== null}
                  onClick={() => void runAction(`exclude:${selectedPage.pageId}`, () => onSetPageDisposition?.({ pageId: selectedPage.pageId, disposition: "EXCLUDE" }))}
                  type="button"
                >
                  {busyAction === `exclude:${selectedPage.pageId}` ? "正在提交…" : "排除本页"}
                </button>
              </div>
              <small>“排除”只影响后续派生 PDF；原始 PDF 的页序、内容和文件名均不改变。</small>
            </section>

            <section className={styles.localPdfEvidenceBoxes}>
              <div>
                <strong>红框标注</strong>
                <span>{selectedPage.boxes.length} 处</span>
              </div>
              {currentDraftBox && (
                <div className={styles.localPdfEvidenceDraftBoxForm}>
                  <label htmlFor={`local-pdf-box-label-${selectedPage.pageId}`}>
                    <span>标注说明（可选）</span>
                    <input
                      id={`local-pdf-box-label-${selectedPage.pageId}`}
                      maxLength={500}
                      onChange={(event) => setDraftLabel(event.target.value)}
                      placeholder="例如：需要律师关注的位置"
                      value={draftLabel}
                    />
                  </label>
                  <div>
                    <button disabled={!onAddBox || busyAction !== null} onClick={() => void addDraftBox()} type="button">
                      {busyAction === `add-box:${selectedPage.pageId}` ? "正在提交…" : "添加红框"}
                    </button>
                    <button className={styles.localPdfEvidenceSecondaryAction} disabled={busyAction !== null} onClick={() => { setDraftBox(null); setDraftPageId(null); setDraftLabel(""); }} type="button">取消</button>
                  </div>
                </div>
              )}
              {selectedPage.boxes.length === 0 && !currentDraftBox ? <p>尚未在本页记录红框。</p> : null}
              <ul>
                {selectedPage.boxes.map((box, index) => (
                  <li key={box.boxId}>
                    <span>{box.label || `红框 ${index + 1}`}</span>
                    <button
                      aria-label={`移除${box.label || `红框 ${index + 1}`}`}
                      disabled={!canEdit || !onRemoveBox || busyAction !== null}
                      onClick={() => void runAction(`remove-box:${box.boxId}`, () => onRemoveBox?.({ pageId: selectedPage.pageId, boxId: box.boxId }))}
                      type="button"
                    >
                      移除
                    </button>
                  </li>
                ))}
              </ul>
            </section>

            <LockAndGenerateControls
              busyAction={busyAction}
              folderAuthorized={folderAuthorized}
              lockBlockedReason={lockBlockedReason}
              lockConfirmed={lockConfirmed}
              onGenerate={onGeneratePdf}
              onLock={onLockSelection}
              onLockConfirmedChange={setLockConfirmed}
              outputByKind={outputByKind}
              selection={selection}
            />
          </aside>
        </div>
      ) : null}
    </section>
  );
}

function FolderNotice({
  folder,
  onReselect,
  busy,
}: {
  folder?: LocalPdfEvidenceMaterialFolder;
  onReselect?: AsyncAction;
  busy: boolean;
}) {
  const reselectAction = async () => {
    if (!onReselect) return;
    await onReselect();
  };
  const isAuthorized = folder?.accessState === "AUTHORIZED";
  const title = isAuthorized ? "当前材料文件夹已获得临时只读授权" : "需要重新选择当前材料文件夹";
  const description = folder?.message
    ?? (isAuthorized
      ? `当前会话可只读检查“${folder?.displayName}”。应用重启或授权到期后，仍需重新选择；绝对路径不会在此显示。`
      : folder?.accessState === "RESELECT_REQUIRED"
        ? `为保护原件，需要在本机会话中重新选择“${folder.displayName}”后才可读取原页预览。`
        : "当前没有可用的本机材料文件夹授权；不会显示或替换为其他文件。");

  return (
    <section className={styles.localPdfEvidenceFolderNotice} role={isAuthorized ? "status" : "alert"}>
      <div>
        <p className={styles.localPdfEvidenceEyebrow}>本机资料根</p>
        <strong>{title}</strong>
        <span>{description}</span>
      </div>
      <button disabled={!onReselect || busy} onClick={() => void reselectAction()} type="button">
        {busy ? "正在打开选择器…" : "重新选择当前文件夹"}
      </button>
    </section>
  );
}

function PreviewCanvas({
  page,
  canPreview,
  canDraw,
  draftBox,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onPointerCancel,
}: {
  page: LocalPdfEvidencePage;
  canPreview: boolean;
  canDraw: boolean;
  draftBox: DraftBox | null;
  onPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onPointerMove: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onPointerUp: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onPointerCancel: () => void;
}) {
  const preview = page.preview;
  if (!canPreview) {
    return (
      <div className={styles.localPdfEvidencePreviewState} role="status">
        <strong>请先重新选择当前材料文件夹</strong>
        <span>本组件不会继续展示可能已失效的本机原页预览。</span>
      </div>
    );
  }
  if (preview?.status === "LOADING") {
    return <div className={styles.localPdfEvidencePreviewState}>正在等待本机提供第 {page.pageNumber} 页预览…</div>;
  }
  if (preview?.status !== "READY" || !preview.imageUrl) {
    return (
      <div className={styles.localPdfEvidencePreviewState} role={preview?.status === "ERROR" ? "alert" : "status"}>
        <strong>{preview?.status === "ERROR" ? "当前页预览读取失败" : "当前页预览尚不可用"}</strong>
        <span>{preview?.message ?? "上层未提供可在当前 CSP 下显示的单页预览，因此组件不会尝试嵌入原始 PDF。"}</span>
      </div>
    );
  }

  return (
    <div className={styles.localPdfEvidenceImageStage}>
      <div className={styles.localPdfEvidenceImageFrame}>
        {/* The desktop parent supplies a short-lived local preview URL, which must not be routed through an image optimizer. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img alt={preview.alt ?? `PDF 第 ${page.pageNumber} 页预览`} draggable={false} src={preview.imageUrl} />
        {page.boxes.map((box) => <BoxOverlay box={box} key={box.boxId} />)}
        {draftBox && <BoxOverlay box={draftBox} draft />}
        <div
          aria-label={canDraw ? "在当前 PDF 页面上拖拽创建红框" : undefined}
          className={`${styles.localPdfEvidenceGestureLayer} ${canDraw ? styles.localPdfEvidenceGestureEnabled : ""}`}
          onPointerCancel={onPointerCancel}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          role={canDraw ? "presentation" : undefined}
        />
      </div>
    </div>
  );
}

function BoxOverlay({ box, draft = false }: { box: DraftBox | LocalPdfEvidenceBox; draft?: boolean }) {
  const normalized = normalizeBox(box);
  return (
    <span
      aria-hidden="true"
      className={`${styles.localPdfEvidenceBox} ${draft ? styles.localPdfEvidenceBoxDraft : ""}`}
      style={{
        left: `${normalized.x0 * 100}%`,
        top: `${normalized.y0 * 100}%`,
        width: `${(normalized.x1 - normalized.x0) * 100}%`,
        height: `${(normalized.y1 - normalized.y0) * 100}%`,
      }}
    />
  );
}

function LockAndGenerateControls({
  busyAction,
  folderAuthorized,
  lockBlockedReason,
  lockConfirmed,
  onGenerate,
  onLock,
  onLockConfirmedChange,
  outputByKind,
  selection,
}: {
  busyAction: string | null;
  folderAuthorized: boolean;
  lockBlockedReason: string | null | undefined;
  lockConfirmed: boolean;
  onGenerate?: (kind: LocalPdfEvidencePdfKind) => void | Promise<void>;
  onLock?: AsyncAction;
  onLockConfirmedChange: (value: boolean) => void;
  outputByKind: Map<LocalPdfEvidencePdfKind, LocalPdfEvidencePdfOutput>;
  selection: LocalPdfEvidenceSelection;
}) {
  const [actionError, setActionError] = useState<string | null>(null);
  const [localBusy, setLocalBusy] = useState<string | null>(null);
  const locked = selection.state === "LOCKED";
  const canLock = selection.state === "DRAFT" && folderAuthorized && !lockBlockedReason && lockConfirmed && Boolean(onLock);

  async function invoke(actionKey: string, action: AsyncAction | undefined) {
    if (!action) return;
    setLocalBusy(actionKey);
    setActionError(null);
    try {
      await action();
    } catch (reason: unknown) {
      setActionError(reason instanceof Error ? reason.message : "本次请求未完成；请重新读取当前材料状态后再试。");
    } finally {
      setLocalBusy(null);
    }
  }

  const busy = busyAction !== null || localBusy !== null;

  return (
    <section className={styles.localPdfEvidenceLockPanel}>
      <p className={styles.localPdfEvidenceEyebrow}>派生结果</p>
      <h3>{selectionLabel(selection.state)}</h3>
      {selection.state === "STALE" ? <p>上游材料或页级选择已经变化；此前锁定内容不能继续生成或使用。</p> : null}
      {selection.state === "DRAFT" ? (
        <>
          <label className={styles.localPdfEvidenceConfirmLine}>
            <input checked={lockConfirmed} disabled={Boolean(lockBlockedReason) || busy} onChange={(event) => onLockConfirmedChange(event.target.checked)} type="checkbox" />
            <span>我确认本次逐页选择将构成后续派生 PDF 的页集；原始 PDF 不会被修改。</span>
          </label>
          {lockBlockedReason ? <small>{lockBlockedReason}</small> : null}
          <button disabled={!canLock || busy} onClick={() => void invoke("lock", onLock)} type="button">
            {localBusy === "lock" ? "正在锁定…" : "锁定当前选择"}
          </button>
        </>
      ) : null}
      {locked ? (
        <div className={styles.localPdfEvidenceGenerateList}>
          {outputKinds.map((kind) => {
            const output = outputByKind.get(kind) ?? { kind, status: "NOT_REQUESTED" as const };
            const isRunning = output.status === "QUEUED" || output.status === "RUNNING";
            const actionKey = `generate:${kind}`;
            return (
              <div key={kind}>
                <span>{pdfKindLabel(kind)}</span>
                <small>{output.message ?? pdfStatusLabel(output.status)}</small>
                <button disabled={!folderAuthorized || !onGenerate || busy || isRunning || output.status === "UNAVAILABLE"} onClick={() => void invoke(actionKey, () => onGenerate?.(kind))} type="button">
                  {localBusy === actionKey ? "正在提交…" : isRunning ? pdfStatusLabel(output.status) : output.status === "VERIFIED" ? "重新生成" : "生成 PDF"}
                </button>
              </div>
            );
          })}
        </div>
      ) : null}
      {actionError ? <p className={styles.localPdfEvidenceActionError} role="alert">{actionError}</p> : null}
    </section>
  );
}

function ReviewState({
  title,
  description,
  actionLabel,
  onAction,
}: {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: AsyncAction;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function act() {
    if (!onAction) return;
    setBusy(true);
    setError(null);
    try {
      await onAction();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "本次请求未完成；请稍后重新读取。");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className={styles.localPdfEvidenceState} aria-label={title}>
      <p className={styles.localPdfEvidenceEyebrow}>本机 PDF 证据整理</p>
      <h2>{title}</h2>
      <p>{description}</p>
      {actionLabel && <button disabled={busy} onClick={() => void act()} type="button">{busy ? "正在处理…" : actionLabel}</button>}
      {error ? <small role="alert">{error}</small> : null}
    </section>
  );
}

function normalizeBox(box: DraftBox | LocalPdfEvidenceBox): DraftBox {
  const x0 = clamp(Math.min(box.x0, box.x1));
  const x1 = clamp(Math.max(box.x0, box.x1));
  const y0 = clamp(Math.min(box.y0, box.y1));
  const y1 = clamp(Math.max(box.y0, box.y1));
  return { x0, y0, x1, y1 };
}

function clamp(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

function pageDispositionLabel(disposition: LocalPdfEvidencePageDisposition): string {
  if (disposition === "INCLUDE") return "已标记纳入";
  if (disposition === "EXCLUDE") return "已标记排除";
  return "待处置";
}

function pageDispositionClass(disposition: LocalPdfEvidencePageDisposition, css: typeof styles): string {
  if (disposition === "INCLUDE") return css.localPdfEvidencePageIncluded;
  if (disposition === "EXCLUDE") return css.localPdfEvidencePageExcluded;
  return css.localPdfEvidencePagePending;
}

function selectionLabel(state: LocalPdfEvidenceSelectionState): string {
  if (state === "LOCKED") return "已锁定";
  if (state === "STALE") return "已失效";
  return "待锁定";
}

function pdfKindLabel(kind: LocalPdfEvidencePdfKind): string {
  return kind === "RELATED_PAGES" ? "相关页 PDF" : "含红框相关页 PDF";
}

function pdfStatusLabel(status: LocalPdfEvidencePdfStatus): string {
  if (status === "QUEUED") return "等待本机生成";
  if (status === "RUNNING") return "正在本机生成";
  if (status === "VERIFIED") return "已生成并核验";
  if (status === "FAILED") return "上次生成失败";
  if (status === "UNAVAILABLE") return "当前不可生成";
  return "尚未生成";
}
