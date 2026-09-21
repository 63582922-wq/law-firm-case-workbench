"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  approveEvidenceAnnotation,
  approveEvidencePageDecision,
  approveLocalFolderScan,
  authorizeSinglePageQwenOcr,
  caseDataSourceConfig,
  enqueueEvidenceDerivativeRun,
  fetchEvidenceDerivative,
  fetchOriginalPagePreview,
  createLocalFolderScan,
  createPersistentFactCandidate,
  createPersistentTransactionCandidate,
  enqueueEvidenceIntakeRun,
  inspectLocalFolderSelection,
  issueLocalFolderGrant,
  loadEvidenceReview,
  loadLocalFolderIntake,
  loadMoreEvidenceIntakeItems,
  loadMoreEvidencePages,
  loadMoreLocalFolderFiles,
  loadOcrReviewCandidates,
  lockEvidenceManifest,
  proposeEvidenceAnnotation,
  proposeEvidencePageDecision,
  readOcrReviewCandidateText,
  reviewOcrReviewCandidate,
  resolveEvidenceDuplicateGroup,
  type EvidenceDerivative,
  type EvidenceReviewPage,
  type EvidenceReviewView,
  type LocalFolderGrant,
  type LocalFolderIntakeView,
  type LocalFolderSelection,
  type OcrReviewCandidateSnapshot,
} from "@/lib/case-data-source";
import { executeAuthorizedQwenOcr, readDesktopRuntimeStatus } from "@/lib/desktop-bridge";
import type { DesktopRuntimeStatus } from "@/lib/desktop-bridge";
import styles from "./case-workbench.module.css";

type DuplicateDecision = "pending" | "exclude" | "keep";
type DraftBox = { x0: number; y0: number; x1: number; y1: number };

function pageStatus(page: EvidenceReviewPage): string {
  if (page.pendingDecision) return page.pendingDecision.disposition === "INCLUDE" ? "待确认保留" : "待确认不纳入";
  if (!page.decisionId) return "待判断是否有关";
  return page.disposition === "INCLUDE" ? "已保留到提交 PDF" : "不纳入提交 PDF";
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
  const [auditNotice, setAuditNotice] = useState("尚未记录新的演示操作。");
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
  const [pageLoadBusy, setPageLoadBusy] = useState(false);
  const [folderSelection, setFolderSelection] = useState<LocalFolderSelection | null>(null);
  const [folderGrant, setFolderGrant] = useState<LocalFolderGrant | null>(null);
  const [folderBusy, setFolderBusy] = useState<"select" | "grant" | null>(null);
  const [folderNotice, setFolderNotice] = useState<string | null>(null);
  const [folderIntake, setFolderIntake] = useState<LocalFolderIntakeView | null>(null);
  const [intakeBusy, setIntakeBusy] = useState<"scan" | "approve" | "enqueue" | "more" | "intake-items" | null>(null);
  const [intakeConfirmed, setIntakeConfirmed] = useState(false);
  const [intakeRunConfirmed, setIntakeRunConfirmed] = useState(false);
  const [intakeNotice, setIntakeNotice] = useState<string | null>(null);
  const [originalPreview, setOriginalPreview] = useState<{
    pageId: string;
    url: string;
    sha256: string;
    width: number;
    height: number;
  } | null>(null);
  const [previewedPageIds, setPreviewedPageIds] = useState<string[]>([]);
  const [originalPreviewBusy, setOriginalPreviewBusy] = useState(false);
  const [desktopRuntime, setDesktopRuntime] = useState<DesktopRuntimeStatus | null>(null);
  const [draftBox, setDraftBox] = useState<DraftBox | null>(null);
  const [draftLabel, setDraftLabel] = useState("");
  const [ocrBusy, setOcrBusy] = useState(false);
  const [ocrNotice, setOcrNotice] = useState<string | null>(null);
  const [ocrRetentionPolicy, setOcrRetentionPolicy] = useState("");
  const [ocrTrainingPolicy, setOcrTrainingPolicy] = useState("");
  const [ocrCostCap, setOcrCostCap] = useState("100");
  const [ocrRegion, setOcrRegion] = useState<"cn-beijing" | "ap-southeast-1">("cn-beijing");
  const [ocrConfirmed, setOcrConfirmed] = useState(false);
  const [ocrCandidates, setOcrCandidates] = useState<OcrReviewCandidateSnapshot | null>(null);
  const [ocrText, setOcrText] = useState<string | null>(null);
  const [ocrReviewReason, setOcrReviewReason] = useState("");
  const [ocrFactText, setOcrFactText] = useState("");
  const [ocrTransactionDraft, setOcrTransactionDraft] = useState({
    localDate: "",
    datePrecision: "EXACT_DATE" as "EXACT_DATE" | "MONTH_ONLY" | "YEAR_ONLY" | "UNKNOWN",
    amount: "",
    currency: "CNY",
    direction: "UNKNOWN" as "OUTGOING" | "INCOMING" | "UNKNOWN",
    payerLabel: "",
    payeeLabel: "",
    channel: "WECHAT" as "WECHAT" | "BANK" | "CASH" | "CHAT_RECORD" | "LOAN_INSTRUMENT" | "OTHER",
    transactionReference: "",
    confirmed: false,
  });
  const dragStart = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    return () => {
      if (artifactPreview) URL.revokeObjectURL(artifactPreview.url);
    };
  }, [artifactPreview]);

  useEffect(() => {
    return () => {
      if (originalPreview) URL.revokeObjectURL(originalPreview.url);
    };
  }, [originalPreview]);

  useEffect(() => {
    let active = true;
    loadEvidenceReview()
      .then(async (result) => {
        if (!active) return;
        setReview(result);
        const firstPage = result.pages[0] ?? null;
        setSelectedPageId(firstPage?.pageId ?? null);
        setPageDisposition(firstPage?.pendingDecision?.disposition ?? firstPage?.disposition ?? "INCLUDE");
        setPageReason(firstPage?.pendingDecision?.reason ?? firstPage?.reason ?? "");
        const firstGroup = result.duplicateGroups.find((item) => firstPage && item.pageIds.includes(firstPage.pageId));
        setCanonicalPageId(firstGroup?.canonicalPageId ?? firstGroup?.pageIds[0] ?? null);
        setError(null);
        if (result.sourceKind === "persistent-preview") {
          try {
            const [intake, candidates] = await Promise.all([loadLocalFolderIntake(), loadOcrReviewCandidates()]);
            if (active) {
              setFolderIntake(intake);
              setOcrCandidates(candidates);
            }
          } catch (reason: unknown) {
            if (active) setIntakeNotice(reason instanceof Error ? reason.message : "案卷盘点摘要读取失败");
          }
        }
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : "证据快照读取失败");
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

  const selected = useMemo(
    () => review?.pages.find((page) => page.pageId === selectedPageId) ?? review?.pages[0] ?? null,
    [review, selectedPageId],
  );
  const activeFolderScan = folderIntake?.candidateScan ?? folderIntake?.approvedScan ?? null;
  const activeIntakeRun = folderIntake?.intakeRun ?? null;
  const currentOcrCandidate = ocrCandidates?.candidates.find((item) => item.evidencePageId === selected?.pageId) ?? null;
  const folderStep = folderGrant ? "材料范围" : folderSelection ? "第二步" : "第一步";
  const folderTitle = folderGrant
    ? `已选择材料文件夹：${folderGrant.displayName}`
    : folderSelection
      ? `确认只读访问：${folderSelection.displayName}`
      : "选择本案材料文件夹";
  const folderDescription = folderGrant
    ? `本机只读访问至 ${new Date(folderGrant.expiresAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}；原件不会被修改。`
    : folderSelection
      ? "确认后只在本机读取该文件夹；绝对路径不会写入案卷。"
      : "将微信交易记录、银行流水、借条和聊天记录放在同一文件夹后，从这里开始。系统只读原件。";

  useEffect(() => {
    if (
      review?.sourceKind !== "persistent-preview"
      || !activeIntakeRun
      || !["QUEUED", "RUNNING"].includes(activeIntakeRun.status)
    ) {
      return;
    }
    let active = true;
    const refresh = async () => {
      try {
        const [nextIntake, nextReview] = await Promise.all([
          loadLocalFolderIntake(),
          loadEvidenceReview(),
        ]);
        if (!active) return;
        setFolderIntake(nextIntake);
        setReview(nextReview);
      } catch {
        // An in-flight local worker may be restarting or the desktop session
        // may be expiring.  Keep the last durable queue snapshot visible and
        // let an explicit subsequent user action surface a recoverable error.
      }
    };
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [activeIntakeRun, review?.sourceKind]);

  if (error) {
    return (
      <section className={styles.evidenceArea} aria-label="收集材料与还款证据">
        <div className={styles.evidenceBlocked} role="alert">
          <p className={styles.eyebrow}>材料暂不能打开</p>
          <h2>尚未连接到本案材料</h2>
          <p>请检查桌面工作台是否仍在运行、当前案件是否已打开，然后重新打开本页。</p>
          <button className={styles.candidateAction} onClick={() => window.location.reload()} type="button">重新载入材料</button>
        </div>
      </section>
    );
  }

  if (!review) {
    return <section className={styles.evidenceArea}><div className={styles.evidenceLoading}>正在读取本案材料页面…</div></section>;
  }

  if (!selected) {
    return (
      <section className={styles.evidenceArea} aria-label="收集材料与还款证据">
        <header className={styles.evidenceHeading}>
          <div>
            <p className={styles.eyebrow}>收集材料与还款证据</p>
            <h2>先选择本案材料文件夹</h2>
          </div>
          <div className={styles.evidenceSnapshotState}>
            <strong>{review.sourceLabel}</strong>
            <span>尚未登记材料页面</span>
            <small>不会用演示材料替代本案文件</small>
          </div>
        </header>
        <div className={styles.evidenceBlocked} role="status">
          <p className={styles.eyebrow}>开始整理</p>
          <h2>本案还没有可筛选的材料页面</h2>
          <p>先选择本案文件夹。系统只读盘点，不会上传、删除、改名或覆盖原件。</p>
        </div>
        {review.sourceKind === "persistent-preview" && (
          <>
            <div className={styles.folderAccessBar}>
              <div>
                <p className={styles.folderStep}>{folderStep}</p>
                <strong>{folderTitle}</strong>
                <span>{folderDescription}</span>
              </div>
              <div>
                <button disabled={folderBusy !== null} onClick={() => void selectCaseFolder()} type="button">
                  {folderBusy === "select" ? "正在选择…" : folderGrant ? "更换材料文件夹" : folderSelection ? "重新选择文件夹" : "第一步：选择材料文件夹"}
                </button>
                {folderSelection && !folderGrant && (
                  <button disabled={folderBusy !== null} onClick={() => void confirmCaseFolder()} type="button">
                    {folderBusy === "grant" ? "正在确认…" : "第二步：确认只读访问"}
                  </button>
                )}
                {folderGrant && folderIntake && (
                  <button disabled={folderBusy !== null || intakeBusy !== null} onClick={() => void scanCaseFolder()} type="button">
                    {intakeBusy === "scan" ? "正在读取文件清单…" : activeFolderScan ? "重新检查材料文件夹" : "检查材料文件夹"}
                  </button>
                )}
              </div>
              {folderNotice && <p role="status">{folderNotice}</p>}
            </div>
            {folderIntake && activeFolderScan && (
              <section className={styles.intakePanel} aria-label="首次案卷文件盘点">
                <div className={styles.intakeHeading}>
                  <div>
                    <p className={styles.eyebrow}>材料收集</p>
                    <h3>{activeFolderScan.status === "CANDIDATE" ? "待确认的材料范围" : "当前可整理的材料范围"}</h3>
                  </div>
                  <div>
                    <strong>{activeFolderScan.totalFiles} 个文件 · {formatBytes(activeFolderScan.totalBytes)}</strong>
                    <small>盘点哈希 {activeFolderScan.manifestHash.slice(0, 16)}…</small>
                  </div>
                </div>
                {folderIntake.candidateScan && (
                  <div className={styles.intakeApproval}>
                    <label className={styles.confirmLine}>
                      <input checked={intakeConfirmed} onChange={(event) => setIntakeConfirmed(event.target.checked)} type="checkbox" />
                      我已核对本次文件范围，确认以此作为本案材料整理范围
                    </label>
                    <button disabled={!intakeConfirmed || intakeBusy !== null} onClick={() => void approveCaseFolderScan()} type="button">
                      {intakeBusy === "approve" ? "正在确认…" : "确认材料范围"}
                    </button>
                  </div>
                )}
                {!folderIntake.candidateScan && folderIntake.approvedScan && !folderIntake.intakeRun && (
                  <div className={styles.intakeApproval}>
                    <label className={styles.confirmLine}>
                      <input checked={intakeRunConfirmed} onChange={(event) => setIntakeRunConfirmed(event.target.checked)} type="checkbox" />
                      我确认从当前材料范围开始整理；系统只读原件并逐文件进行安全检查
                    </label>
                    <button disabled={!folderGrant || !intakeRunConfirmed || intakeBusy !== null} onClick={() => void enqueueFolderIntake()} type="button">
                      {intakeBusy === "enqueue" ? "正在开始整理…" : "开始整理材料"}
                    </button>
                  </div>
                )}
                {folderIntake.intakeRun && (
                  <div className={styles.intakeRunPanel}>
                    <div>
                      <strong>{intakeRunStatusLabel(folderIntake.intakeRun.status)}</strong>
                      <small>每 3 秒刷新一次持久化处理状态；关闭本页不会中断已经写入队列的安全检查。</small>
                    </div>
                    <div className={styles.intakeRunCounts}>
                      <span><strong>{folderIntake.intakeRun.registeredItems}</strong>已登记</span>
                      <span><strong>{folderIntake.intakeRun.queuedItems + folderIntake.intakeRun.runningItems}</strong>等待/处理中</span>
                      <span><strong>{folderIntake.intakeRun.reviewRequiredItems}</strong>需转换/人工查看</span>
                      <span><strong>{folderIntake.intakeRun.blockedItems + folderIntake.intakeRun.failedItems}</strong>已阻断/失败</span>
                    </div>
                  </div>
                )}
                {intakeNotice && <p className={styles.auditNotice} role="status">{intakeNotice}</p>}
              </section>
            )}
          </>
        )}
      </section>
    );
  }

  const unresolvedCount = review.unresolvedPageCount;
  const pendingDecisionCount = review.pendingDecisionCount;
  const unresolvedDuplicateCount = review.unresolvedDuplicateCount;
  const duplicateGroup = review.duplicateGroups.find((group) => group.pageIds.includes(selected.pageId));
  const source = review.originals.find((item) => item.fileId === selected.fileId);
  const hasCurrentOriginalPreview = originalPreview?.pageId === selected.pageId;
  const duplicatePagesPreviewed = duplicateGroup
    ? duplicateGroup.pageIds.every((pageId) => previewedPageIds.includes(pageId))
    : true;
  const duplicatePagesLoaded = duplicateGroup
    ? duplicateGroup.pageIds.every((pageId) => review.pages.some((page) => page.pageId === pageId))
    : true;

  function recordSyntheticDecision() {
    if (duplicateDecision === "pending") {
      setAuditNotice("请先选择律师决定；系统不会替代律师作出取舍。");
      return;
    }
    const action = duplicateDecision === "exclude" ? "不把重复页放入提交 PDF" : "按不同页面分别处理";
    setAuditNotice(`已保存演示处理结果：${action}。原始页不会被删除；真实案件仍需逐页确认。`);
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
        setArtifactNotice(`已保存提交版证据 PDF：${delivery.fileName}`);
      } else {
        const url = URL.createObjectURL(delivery.blob);
        setArtifactPreview((prior) => {
          if (prior) URL.revokeObjectURL(prior.url);
          return {
            url,
            label: derivative.artifactType === "ANNOTATED_RELATED_PAGES_PDF" ? "标红还款证据 PDF" : "筛选后的证据 PDF",
            sha256: delivery.artifactSha256,
          };
        });
        setArtifactNotice("提交版 PDF 仅在本页临时预览；关闭后释放，不会写回原件文件夹。");
      }
    } catch (reason: unknown) {
      setArtifactNotice(reason instanceof Error ? reason.message : "提交版证据 PDF 读取失败");
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
      setFolderIntake(await loadLocalFolderIntake());
      setArtifactNotice(`提交版证据 PDF 已开始生成；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setArtifactNotice(reason instanceof Error ? reason.message : "提交版证据 PDF 未能开始生成");
    } finally {
      setArtifactBusy(null);
    }
  }

  async function loadNextEvidencePages() {
    if (!review || pageLoadBusy || !review.pagePage.hasMore) return;
    setPageLoadBusy(true);
    setReviewNotice(null);
    try {
      const refreshed = await loadMoreEvidencePages(review);
      setReview(refreshed);
      setReviewNotice(`已载入 ${refreshed.pagePage.loadedCount} / ${refreshed.pagePage.totalCount} 页；已做的筛选和当前页面都保留。`);
    } catch (reason: unknown) {
      setReviewNotice(reason instanceof Error ? `${reason.message} 已载入页面仍保留，可再次续载。` : "证据后续页载入失败；已载入页面仍保留，可再次续载。");
    } finally {
      setPageLoadBusy(false);
    }
  }

  async function selectCaseFolder() {
    if (caseDataSourceConfig.kind !== "persistent-preview") return;
    setFolderBusy("select");
    setFolderNotice(null);
    try {
      if (!window.lawCaseDesktop) throw new Error("当前网页壳层尚未连接本机文件夹选择器；请从桌面版打开本案。");
      const picked = await window.lawCaseDesktop.selectCaseFolder({ matterId: caseDataSourceConfig.matterId });
      if (!picked) {
        setFolderNotice("已取消选择，没有读取任何文件。");
        return;
      }
      const selection = await inspectLocalFolderSelection(picked.selectedRoot);
      setFolderSelection(selection);
      setFolderGrant(null);
      clearOriginalPagePreview();
      setPreviewedPageIds([]);
      setOriginalCompared(false);
      setFolderNotice(`已选择“${selection.displayName}”，尚未授权读取。请核对名称后确认。`);
    } catch (reason: unknown) {
      setFolderNotice(reason instanceof Error ? reason.message : "材料文件夹选择失败");
    } finally {
      setFolderBusy(null);
    }
  }

  async function confirmCaseFolder() {
    if (!folderSelection) return;
    setFolderBusy("grant");
    setFolderNotice(null);
    try {
      const grant = await issueLocalFolderGrant(folderSelection);
      setFolderGrant(grant);
      setPreviewedPageIds([]);
      setFolderNotice(`“${grant.displayName}”已获得本机会话内的短时只读授权。`);
    } catch (reason: unknown) {
      setFolderNotice(reason instanceof Error ? reason.message : "材料文件夹确认失败");
    } finally {
      setFolderBusy(null);
    }
  }

  async function scanCaseFolder() {
    if (!folderGrant || !folderIntake) {
      setIntakeNotice("请先选择并确认本案材料文件夹。");
      return;
    }
    setIntakeBusy("scan");
    setIntakeNotice(null);
    try {
      const receipt = await createLocalFolderScan(folderIntake, folderGrant.grantId);
      const [refreshedReview, refreshedIntake] = await Promise.all([
        loadEvidenceReview(),
        loadLocalFolderIntake(),
      ]);
      setReview(refreshedReview);
      setFolderIntake(refreshedIntake);
      setIntakeConfirmed(false);
      setIntakeRunConfirmed(false);
      setIntakeNotice(`已整理出待确认的材料清单；案件版本更新为 ${receipt.matterVersion}。确认前不会改变本案材料范围。`);
    } catch (reason: unknown) {
      setIntakeNotice(reason instanceof Error ? reason.message : "材料文件夹检查未完成");
    } finally {
      setIntakeBusy(null);
    }
  }

  async function approveCaseFolderScan() {
    if (!folderIntake?.candidateScan || !intakeConfirmed) return;
    setIntakeBusy("approve");
    setIntakeNotice(null);
    try {
      const receipt = await approveLocalFolderScan(folderIntake);
      const [refreshedReview, refreshedIntake] = await Promise.all([
        loadEvidenceReview(),
        loadLocalFolderIntake(),
      ]);
      setReview(refreshedReview);
      setFolderIntake(refreshedIntake);
      setIntakeConfirmed(false);
      setIntakeRunConfirmed(false);
      setManifestConfirmed(false);
      setIntakeNotice(`材料范围已确认；案件版本更新为 ${receipt.matterVersion}。如材料范围变动，已生成的提交材料会按规则失效。`);
    } catch (reason: unknown) {
      setIntakeNotice(reason instanceof Error ? reason.message : "材料范围未能确认");
    } finally {
      setIntakeBusy(null);
    }
  }

  async function loadNextFolderFiles() {
    if (!folderIntake || intakeBusy !== null) return;
    setIntakeBusy("more");
    setIntakeNotice(null);
    try {
      const refreshed = await loadMoreLocalFolderFiles(folderIntake);
      setFolderIntake(refreshed);
      setIntakeNotice(`已载入 ${refreshed.filePage.loadedCount} / ${refreshed.filePage.totalCount} 个文件记录。`);
    } catch (reason: unknown) {
      setIntakeNotice(reason instanceof Error ? `${reason.message} 已载入记录仍保留。` : "后续文件记录载入失败；已载入记录仍保留。");
    } finally {
      setIntakeBusy(null);
    }
  }

  async function enqueueFolderIntake() {
    if (!folderIntake?.approvedScan || folderIntake.candidateScan || !folderGrant || !intakeRunConfirmed) return;
    setIntakeBusy("enqueue");
    setIntakeNotice(null);
    try {
      const receipt = await enqueueEvidenceIntakeRun(folderIntake, folderGrant.grantId);
      const [refreshedReview, refreshedIntake] = await Promise.all([
        loadEvidenceReview(),
        loadLocalFolderIntake(),
      ]);
      setReview(refreshedReview);
      setFolderIntake(refreshedIntake);
      setIntakeRunConfirmed(false);
      setIntakeNotice(
        desktopRuntime?.evidenceIntakeWorkerPhase === "ASSEMBLED"
          ? `材料整理已开始；案件版本更新为 ${receipt.matterVersion}。本机会继续处理，状态会自动刷新。`
          : `材料整理已开始；案件版本更新为 ${receipt.matterVersion}。当前机器的处理服务尚未就绪，任务会保持等待。`,
      );
    } catch (reason: unknown) {
      setIntakeNotice(reason instanceof Error ? reason.message : "材料整理未能开始");
    } finally {
      setIntakeBusy(null);
    }
  }

  async function loadNextEvidenceIntakeItems() {
    if (!folderIntake || intakeBusy !== null) return;
    setIntakeBusy("intake-items");
    setIntakeNotice(null);
    try {
      const refreshed = await loadMoreEvidenceIntakeItems(folderIntake);
      setFolderIntake(refreshed);
      setIntakeNotice(`已载入 ${refreshed.intakeItemPage.loadedCount} / ${refreshed.intakeItemPage.totalCount} 条材料处理记录。`);
    } catch (reason: unknown) {
      setIntakeNotice(reason instanceof Error ? `${reason.message} 已载入记录仍保留。` : "后续处理记录载入失败；已载入记录仍保留。");
    } finally {
      setIntakeBusy(null);
    }
  }

  async function readOriginalPage() {
    if (!folderGrant || !selected) {
      setFolderNotice("请先选择并确认本案材料文件夹。");
      return;
    }
    setOriginalPreviewBusy(true);
    setFolderNotice(null);
    const currentPage = selected;
    try {
      const delivery = await fetchOriginalPagePreview(currentPage.pageId, folderGrant.grantId);
      const url = URL.createObjectURL(delivery.blob);
      setOriginalPreview((prior) => {
        if (prior) URL.revokeObjectURL(prior.url);
        return {
          pageId: delivery.pageId,
          url,
          sha256: delivery.contentSha256,
          width: delivery.width,
          height: delivery.height,
        };
      });
      setPreviewedPageIds((prior) => prior.includes(currentPage.pageId) ? prior : [...prior, currentPage.pageId]);
      setOriginalCompared(false);
      setDraftBox(null);
      setDraftLabel("");
      setOcrText(null);
      setOcrFactText("");
      setFolderNotice(`已在本机打开第 ${currentPage.pageNumber} 页原件；整份原件不会传出。`);
    } catch (reason: unknown) {
      setFolderNotice(reason instanceof Error ? reason.message : "本页原件打开失败");
    } finally {
      setOriginalPreviewBusy(false);
    }
  }

  async function executePageOcr() {
    if (!review || review.sourceKind !== "persistent-preview" || !folderGrant || !selected || !hasCurrentOriginalPreview || !originalPreview) {
      setOcrNotice("请先在本机打开并核对当前原件，再确认识别本页文字。");
      return;
    }
    const costCapMinor = Number(ocrCostCap);
    if (!Number.isInteger(costCapMinor) || costCapMinor < 0) {
      setOcrNotice("请填写本次 OCR 的人民币分级成本上限（整数）。");
      return;
    }
    if (!ocrConfirmed) {
      setOcrNotice("请先明确确认仅向已选服务发送当前这一页。 ");
      return;
    }
    setOcrBusy(true);
    setOcrNotice(null);
    try {
      const authorization = await authorizeSinglePageQwenOcr({
        review,
        evidencePageId: selected.pageId,
        renderedPageSha256: originalPreview.sha256,
        processorRegion: ocrRegion,
        retentionPolicy: ocrRetentionPolicy,
        trainingPolicy: ocrTrainingPolicy,
        costCapMinor,
        expiresAt: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
        confirmation: "CONFIRM_SINGLE_PAGE_QWEN_OCR",
      });
      const result = await executeAuthorizedQwenOcr({
        matterId: caseDataSourceConfig.kind === "persistent-preview" ? caseDataSourceConfig.matterId : "",
        evidencePageId: selected.pageId,
        folderGrantId: folderGrant.grantId,
        externalRequestId: authorization.requestId,
        expectedVersion: authorization.matterVersion,
      });
      const [refreshedReview, candidates] = await Promise.all([loadEvidenceReview(), loadOcrReviewCandidates()]);
      setReview(refreshedReview);
      setOcrCandidates(candidates);
      const candidate = candidates.candidates.find((item) => item.candidateId === result.candidateId);
      if (candidate) setOcrText(await readOcrReviewCandidateText({ candidateId: candidate.candidateId }));
      setOcrConfirmed(false);
      setOcrNotice(`本页文字识别结果已安全保存，等待你核对；案件版本更新为 ${result.matterVersion}。识别文字尚未成为事实或提交材料。`);
    } catch (reason: unknown) {
      setOcrNotice(reason instanceof Error ? reason.message : "本页文字识别未完成；系统没有自动重试。");
    } finally {
      setOcrBusy(false);
    }
  }

  async function decideOcrCandidate(decision: "ACCEPTED" | "REJECTED") {
    if (!selected) return;
    const candidate = ocrCandidates?.candidates.find((item) => item.evidencePageId === selected.pageId && item.status === "CANDIDATE");
    if (!ocrCandidates || !candidate || !ocrReviewReason.trim()) {
      setOcrNotice("请填写对本页识别文字的核对说明。 ");
      return;
    }
    setOcrBusy(true);
    try {
      await reviewOcrReviewCandidate({ snapshot: ocrCandidates, candidate, decision, reason: ocrReviewReason });
      const [candidates, refreshedReview] = await Promise.all([loadOcrReviewCandidates(), loadEvidenceReview()]);
      setOcrCandidates(candidates);
      setReview(refreshedReview);
      setOcrReviewReason("");
      setOcrNotice(decision === "ACCEPTED" ? "识别文字已确认保留；如需使用，请另行整理为待确认事实。" : "识别文字已标记为不采用，并保留本次处理记录。 ");
    } catch (reason: unknown) {
      setOcrNotice(reason instanceof Error ? reason.message : "识别文字尚未完成核对。 ");
    } finally {
      setOcrBusy(false);
    }
  }

  async function createFactFromAcceptedOcr() {
    if (!review || review.sourceKind !== "persistent-preview" || !selected || !currentOcrCandidate || currentOcrCandidate.status !== "ACCEPTED") return;
    const source = review.originals.find((item) => item.fileId === selected.fileId);
    if (!source || !ocrFactText.trim()) {
      setOcrNotice("请先将识别文字整理为一项明确、可与本页原件核对的事实。 ");
      return;
    }
    setOcrBusy(true);
    try {
      const receipt = await createPersistentFactCandidate({
        expectedVersion: review.matterVersion ?? 0,
        originalText: ocrFactText,
        origin: "AGENT_CANDIDATE",
        evidenceLink: {
          evidenceId: selected.pageId,
          originalFileSha256: source.originalFileSha256,
          pageNumber: selected.pageNumber,
          originalLabel: selected.originalLabel,
        },
      });
      setReview(await loadEvidenceReview());
      setOcrFactText("");
      setOcrNotice(`已保存一项待确认事实（案件版本 ${receipt.matterVersion}）；请在“事实与争点”中确认、争议或否认。`);
    } catch (reason: unknown) {
      setOcrNotice(reason instanceof Error ? reason.message : "待确认事实未能保存。 ");
    } finally {
      setOcrBusy(false);
    }
  }

  async function createTransactionFromReviewedPage() {
    if (!review || review.sourceKind !== "persistent-preview" || !selected || !hasCurrentOriginalPreview) return;
    const source = review.originals.find((item) => item.fileId === selected.fileId);
    if (!source || !ocrTransactionDraft.confirmed) {
      setOcrNotice("请先核对本页原件中的日期、金额、币种、收付款方向和当事人，并确认本次记录。 ");
      return;
    }
    setOcrBusy(true);
    try {
      const receipt = await createPersistentTransactionCandidate({
        expectedVersion: review.matterVersion ?? 0,
        localDate: ocrTransactionDraft.datePrecision === "EXACT_DATE" ? ocrTransactionDraft.localDate : null,
        datePrecision: ocrTransactionDraft.datePrecision,
        amount: ocrTransactionDraft.amount,
        currency: ocrTransactionDraft.currency,
        direction: ocrTransactionDraft.direction,
        payerLabel: ocrTransactionDraft.payerLabel,
        payeeLabel: ocrTransactionDraft.payeeLabel,
        channel: ocrTransactionDraft.channel,
        transactionReference: ocrTransactionDraft.transactionReference,
        evidenceLink: {
          evidenceId: selected.pageId,
          originalFileSha256: source.originalFileSha256,
          pageNumber: selected.pageNumber,
          originalLabel: selected.originalLabel,
        },
      });
      setReview(await loadEvidenceReview());
      setOcrTransactionDraft((prior) => ({ ...prior, amount: "", transactionReference: "", confirmed: false }));
      setOcrNotice(`已保存一笔待确认交易（案件版本 ${receipt.matterVersion}）；请在“事实与争点”中确认，并再单独判断付款性质。`);
    } catch (reason: unknown) {
      setOcrNotice(reason instanceof Error ? reason.message : "待确认交易未能保存。 ");
    } finally {
      setOcrBusy(false);
    }
  }

  function previewCoordinate(event: ReactPointerEvent<HTMLDivElement>): { x: number; y: number } {
    const bounds = event.currentTarget.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
      y: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)),
    };
  }

  function beginRedBox(event: ReactPointerEvent<HTMLDivElement>) {
    if (!review || !hasCurrentOriginalPreview || review.lockedManifest) return;
    dragStart.current = previewCoordinate(event);
    event.currentTarget.setPointerCapture(event.pointerId);
    setDraftBox(null);
  }

  function finishRedBox(event: ReactPointerEvent<HTMLDivElement>) {
    const start = dragStart.current;
    dragStart.current = null;
    if (!review || !start || !hasCurrentOriginalPreview || review.lockedManifest) return;
    const end = previewCoordinate(event);
    const next = {
      x0: Math.min(start.x, end.x),
      y0: Math.min(start.y, end.y),
      x1: Math.max(start.x, end.x),
      y1: Math.max(start.y, end.y),
    };
    if (next.x1 - next.x0 < 0.005 || next.y1 - next.y0 < 0.005) {
      setReviewNotice("红框范围太小，请在页图上重新拖选。");
      return;
    }
    setDraftBox(next);
    setReviewNotice("已框出红框；填写说明后保存，仍需确认才会用于提交版 PDF。 ");
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
      setFolderIntake(await loadLocalFolderIntake());
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
    if (page) return `${page.originalLabel} · 第 ${page.pageNumber} 页`;
    return review?.duplicateGroups.find((group) => group.pageIds.includes(pageId))?.pageLabels[pageId] ?? "尚未载入的材料页面";
  }

  function clearOriginalPagePreview() {
    setOriginalPreview((prior) => {
      if (prior) URL.revokeObjectURL(prior.url);
      return null;
    });
    setDraftBox(null);
    setDraftLabel("");
  }

  return (
    <section className={styles.evidenceArea} aria-label="收集材料与还款证据">
      <header className={styles.evidenceHeading}>
        <div>
          <p className={styles.eyebrow}>收集材料与还款证据</p>
          <h2>筛出与对方当事人有关的页面，整理还款证据</h2>
        </div>
        <div className={styles.evidenceSnapshotState}>
          <strong>{review.sourceLabel}</strong>
          <span>已载入 {review.pagePage.loadedCount} / {review.totalPages} 页 · 还有 {unresolvedCount} 页待筛选</span>
          <small>材料版本 {review.matterVersion ?? "演示"} · 每次处理都会留痕</small>
        </div>
      </header>

      <section className={styles.evidenceWorkflow} aria-label="还款证据办理顺序">
        <div>
          <span>1</span>
          <strong>选择材料</strong>
          <small>微信流水、银行流水、借条、聊天记录</small>
        </div>
        <div>
          <span>2</span>
          <strong>逐页筛选</strong>
          <small>找出对方当事人、转账、还款相关内容</small>
        </div>
        <div>
          <span>3</span>
          <strong>标红并确认</strong>
          <small>框出关键姓名、金额、日期、交易信息</small>
        </div>
        <div>
          <span>4</span>
          <strong>生成 PDF</strong>
          <small>只生成已经确认保留的提交版材料</small>
        </div>
      </section>

      {review.sourceKind === "persistent-preview" && (
        <div className={styles.folderAccessBar}>
          <div>
            <p className={styles.folderStep}>{folderStep}</p>
            <strong>{folderTitle}</strong>
            <span>{folderDescription}</span>
          </div>
          <div>
            <button disabled={folderBusy !== null} onClick={() => void selectCaseFolder()} type="button">
              {folderBusy === "select" ? "正在选择…" : folderGrant ? "更换材料文件夹" : folderSelection ? "重新选择文件夹" : "第一步：选择材料文件夹"}
            </button>
            {folderSelection && !folderGrant && (
              <button disabled={folderBusy !== null} onClick={() => void confirmCaseFolder()} type="button">
                {folderBusy === "grant" ? "正在确认…" : "第二步：确认只读访问"}
              </button>
            )}
            {folderGrant && folderIntake && (
              <button disabled={folderBusy !== null || intakeBusy !== null} onClick={() => void scanCaseFolder()} type="button">
                {intakeBusy === "scan" ? "正在读取文件清单…" : activeFolderScan ? "重新检查材料文件夹" : "检查材料文件夹"}
              </button>
            )}
          </div>
          {folderNotice && <p role="status">{folderNotice}</p>}
        </div>
      )}

      {review.sourceKind === "persistent-preview" && folderIntake && activeFolderScan && (
        <section className={styles.intakePanel} aria-label="案卷文件盘点">
          <div className={styles.intakeHeading}>
            <div>
              <p className={styles.eyebrow}>材料收集</p>
              <h3>{activeFolderScan.status === "CANDIDATE" ? "待确认的材料范围" : "当前可整理的材料范围"}</h3>
            </div>
            <div>
              <strong>{activeFolderScan.totalFiles} 个文件 · {formatBytes(activeFolderScan.totalBytes)}</strong>
              <small>盘点哈希 {activeFolderScan.manifestHash.slice(0, 16)}…</small>
            </div>
          </div>
          <div className={styles.intakeCounts}>
            <span><strong>{activeFolderScan.newCount}</strong>新增</span>
            <span><strong>{activeFolderScan.modifiedCount}</strong>内容修改</span>
            <span><strong>{activeFolderScan.movedCount}</strong>移动</span>
            <span><strong>{activeFolderScan.missingCount}</strong>缺失</span>
            <span><strong>{activeFolderScan.unchangedCount}</strong>未变化</span>
            <span><strong>{activeFolderScan.duplicateContentCount}</strong>同内容副本</span>
          </div>
          {(activeFolderScan.skippedSymlinks > 0 || activeFolderScan.duplicateContentCount > 0) && (
            <p className={styles.intakeWarning}>
              {activeFolderScan.skippedSymlinks > 0 ? `已安全跳过 ${activeFolderScan.skippedSymlinks} 个符号链接；` : ""}
              {activeFolderScan.duplicateContentCount > 0 ? `发现 ${activeFolderScan.duplicateContentCount} 个同内容副本，系统不会自动删除。` : ""}
            </p>
          )}
          <div className={styles.intakeFileList}>
            {folderIntake.files.map((file) => (
              <div className={styles.intakeFile} key={`${file.changeKind}:${file.relativePath}`}>
                <span className={styles.intakeChange}>{folderChangeLabel(file.changeKind)}</span>
                <div>
                  <strong title={file.relativePath}>{file.relativePath}</strong>
                  <small>
                    {folderKindLabel(file.detectedKind)} · {formatBytes(file.byteSize)} · {file.fileSha256.slice(0, 12)}…
                    {file.previousRelativePath && file.previousRelativePath !== file.relativePath ? ` · 原位置 ${file.previousRelativePath}` : ""}
                  </small>
                </div>
              </div>
            ))}
          </div>
          <div className={styles.intakeActions}>
            <small>已载入 {folderIntake.filePage.loadedCount} / {folderIntake.filePage.totalCount} 项；这里只建立只读范围，不上传、删除、改名或覆盖原件。</small>
            {folderIntake.filePage.hasMore && (
              <button disabled={intakeBusy !== null} onClick={() => void loadNextFolderFiles()} type="button">
                {intakeBusy === "more" ? "正在载入…" : "继续载入 100 项"}
              </button>
            )}
          </div>
          {folderIntake.candidateScan && (
            <div className={styles.intakeApproval}>
              <label className={styles.confirmLine}>
                <input checked={intakeConfirmed} onChange={(event) => setIntakeConfirmed(event.target.checked)} type="checkbox" />
                我已核对本次材料范围及新增、修改、移动、缺失和重复提示，确认以此作为本案材料整理范围
              </label>
              <button disabled={!intakeConfirmed || intakeBusy !== null} onClick={() => void approveCaseFolderScan()} type="button">
                {intakeBusy === "approve" ? "正在确认…" : "确认材料范围"}
              </button>
            </div>
          )}
          {!folderIntake.candidateScan && folderIntake.approvedScan && !folderIntake.intakeRun && (
            <div className={styles.intakeApproval}>
              <label className={styles.confirmLine}>
                <input checked={intakeRunConfirmed} onChange={(event) => setIntakeRunConfirmed(event.target.checked)} type="checkbox" />
                我确认从当前材料范围开始整理；系统只读原件，逐文件检查文件是否完整、安全、可用
              </label>
              <button disabled={!folderGrant || !intakeRunConfirmed || intakeBusy !== null} onClick={() => void enqueueFolderIntake()} type="button">
                {intakeBusy === "enqueue" ? "正在开始整理…" : "开始整理材料"}
              </button>
            </div>
          )}
          {folderIntake.intakeRun && (
            <div className={styles.intakeRunPanel}>
              <div>
                <strong>{intakeRunStatusLabel(folderIntake.intakeRun.status)}</strong>
                <small>材料处理批次已建立；每份文件的处理结果都会保留。</small>
              </div>
              <div className={styles.intakeRunCounts}>
                <span><strong>{folderIntake.intakeRun.registeredItems}</strong>已登记 PDF</span>
                <span><strong>{folderIntake.intakeRun.queuedItems + folderIntake.intakeRun.runningItems}</strong>等待/处理中</span>
                <span><strong>{folderIntake.intakeRun.reviewRequiredItems}</strong>需转换/人工查看</span>
                <span><strong>{folderIntake.intakeRun.blockedItems + folderIntake.intakeRun.failedItems}</strong>已阻断/失败</span>
              </div>
              <p>非 PDF 文件不会被当作页面材料。系统会先检查真实格式与安全性，再决定是否登记、需要转换，或提示不能处理。</p>
              {folderIntake.intakeItems.length > 0 && (
                <div className={styles.intakeItemList}>
                  <div className={styles.intakeItemHeading}>
                    <strong>材料整理结果</strong>
                    <span>已显示 {folderIntake.intakeItemPage.loadedCount} / {folderIntake.intakeItemPage.totalCount}</span>
                  </div>
                  {folderIntake.intakeItems.map((item) => (
                    <div className={styles.intakeItemRow} key={item.itemId}>
                      <span className={styles.intakeItemState}>{intakeItemStatusLabel(item.status)}</span>
                      <div>
                        <strong>{item.relativePath}</strong>
                        <small>{folderKindLabel(item.detectedKind)} · {intakeOutcomeLabel(item.outcomeCode)}</small>
                      </div>
                      <span>第 {item.attemptCount || 0} 次处理</span>
                    </div>
                  ))}
                  {folderIntake.intakeItemPage.hasMore && (
                    <button disabled={intakeBusy !== null} onClick={() => void loadNextEvidenceIntakeItems()} type="button">
                      {intakeBusy === "intake-items" ? "正在载入…" : "继续载入 100 条处理记录"}
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
          {intakeNotice && <div className={styles.auditNotice} role="status">{intakeNotice}</div>}
        </section>
      )}

      {review.sourceKind === "persistent-preview" && intakeNotice && !activeFolderScan && (
        <div className={styles.auditNotice} role="status">{intakeNotice}</div>
      )}

      <div className={styles.evidenceColumns}>
        <aside className={styles.pageList}>
          <div className={styles.listHeading}><span>逐页筛选</span><small>找出与对方、还款有关的页面</small></div>
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
                clearOriginalPagePreview();
                const group = review.duplicateGroups.find((candidate) => candidate.pageIds.includes(item.pageId));
                setCanonicalPageId(group?.canonicalPageId ?? group?.pageIds[0] ?? null);
              }}
              type="button"
            >
              <span className={styles.pageNumber}>第 {item.pageNumber} 页</span>
              <strong>{item.pendingDecision ? "待确认" : item.disposition === "INCLUDE" ? "已保留" : item.disposition === "EXCLUDE" ? "不纳入" : "待判断"}</strong>
              <small title={item.originalLabel}>{item.originalLabel}</small>
              <em className={statusClass(item)}>{pageStatus(item)}</em>
            </button>
          ))}
          <div className={styles.ledgerPagination} aria-live="polite">
            <small>已载入 {review.pagePage.loadedCount} / {review.pagePage.totalCount} 页</small>
            {review.pagePage.hasMore && (
              <button className={styles.candidateAction} disabled={pageLoadBusy} onClick={() => void loadNextEvidencePages()} type="button">
                {pageLoadBusy ? "正在载入…" : "继续载入 50 页"}
              </button>
            )}
          </div>
        </aside>

        <article className={styles.documentStage}>
          <div className={styles.documentToolbar}>
            <span>正在查看原件 · 第 {selected.pageNumber} 页</span>
            <span>{review.sourceKind === "synthetic-alpha" ? "演示预览" : hasCurrentOriginalPreview ? "已打开本页原件" : "请先打开本页原件"}</span>
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
          ) : hasCurrentOriginalPreview && originalPreview ? (
            <div className={styles.originalPreviewFrame}>
              <div className={styles.originalPreviewHeader}>
                <div><strong>{selected.originalLabel} · 第 {selected.pageNumber} 页</strong><small>PNG {originalPreview.width} × {originalPreview.height} · {originalPreview.sha256.slice(0, 16)}…</small></div>
                <button onClick={clearOriginalPagePreview} type="button">关闭原件</button>
              </div>
              <div
                aria-label={`原件第 ${selected.pageNumber} 页，可拖动标出红框`}
                className={styles.originalPageCanvas}
                onPointerDown={beginRedBox}
                onPointerUp={finishRedBox}
                role="img"
              >
                {/* eslint-disable-next-line @next/next/no-img-element -- authenticated in-memory Blob has no stable Next image URL */}
                <img alt={`原始证据 ${selected.originalLabel} 第 ${selected.pageNumber} 页`} draggable={false} src={originalPreview.url} />
                {selected.annotations.map((annotation) => (
                  <span
                    aria-label={`${annotation.status === "APPROVED" ? "已确认" : "待确认"}红框：${annotation.label}`}
                    className={annotation.status === "APPROVED" ? styles.approvedBox : styles.candidateBox}
                    key={annotation.annotationId}
                    style={{
                      left: `${annotation.x0 * 100}%`,
                      top: `${annotation.y0 * 100}%`,
                      width: `${(annotation.x1 - annotation.x0) * 100}%`,
                      height: `${(annotation.y1 - annotation.y0) * 100}%`,
                    }}
                  />
                ))}
                {draftBox && (
                  <span
                    aria-label="红框草稿"
                    className={styles.draftBox}
                    style={{
                      left: `${draftBox.x0 * 100}%`,
                      top: `${draftBox.y0 * 100}%`,
                      width: `${(draftBox.x1 - draftBox.x0) * 100}%`,
                      height: `${(draftBox.y1 - draftBox.y0) * 100}%`,
                    }}
                  />
                )}
              </div>
              {!review.lockedManifest && <p>在页图上按住并拖动，框出对方姓名、交易金额、日期或还款信息。红框不会改动原件，确认后才会用于提交版 PDF。</p>}
            </div>
          ) : selected.syntheticPreview ? (
            <div className={styles.documentPaper} aria-label={`演示交易记录第 ${selected.pageNumber} 页`}>
              <div className={styles.documentBrand}>微信支付 <small>演示页面</small></div>
              <div className={styles.documentTitle}>交易明细证明</div>
              <div className={styles.documentMeta}><span>交易时间</span><strong>{selected.syntheticPreview.date} 10:16</strong></div>
              <div className={`${styles.transactionRow} ${selected.annotations.length ? styles.redBox : ""}`}>
                <div><span>转账给</span><strong>{selected.syntheticPreview.counterpart}</strong></div>
                <b>{selected.syntheticPreview.amount}</b>
              </div>
              <div className={styles.documentMeta}><span>交易编号</span><strong>示例-{String(selected.pageNumber).padStart(4, "0")}</strong></div>
              <p className={styles.documentFootnote}>红框仅为经批准的页内坐标示意，不修改原件，也不自动完成法律定性。</p>
            </div>
          ) : (
            <div className={styles.sourcePreviewUnavailable}>
              <p className={styles.eyebrow}>请先查看本页原件</p>
              <h3>{selected.originalLabel}</h3>
              <p>{folderGrant ? "点击下方按钮后，系统只会在本机打开当前一页，供你判断它是否与对方当事人、还款或争点有关。" : "请先通过上方按钮选择并确认本案材料文件夹；未确认前不会读取任何原件。"}</p>
              <dl>
                <div><dt>原件校验</dt><dd>{source?.originalFileSha256.slice(0, 18) ?? "—"}…</dd></div>
                <div><dt>材料页码</dt><dd>第 {selected.pageNumber} 页</dd></div>
                <div><dt>已确认红框</dt><dd>{selected.annotations.filter((item) => item.status === "APPROVED").length} 个</dd></div>
              </dl>
              <button className={styles.originalPreviewAction} disabled={!folderGrant || originalPreviewBusy} onClick={() => void readOriginalPage()} type="button">
                {originalPreviewBusy ? "正在打开原件…" : "打开本页原件并开始筛选"}
              </button>
            </div>
          )}
          <p className={styles.sourceNote}>原件和页码不可修改。筛选后的页面、标红页面和提交版 PDF，只会从你确认的材料清单生成。</p>
          {review.sourceKind === "persistent-preview" && !review.lockedManifest && (
            <section className={styles.decisionPanel} aria-label="识别本页文字">
              <strong>识别本页文字（可选）</strong>
              <small>如需读取图片或扫描件文字，可在确认后仅发送当前这一页给已配置的识别服务。结果只是待你核对的文字，不会自动写入案件事实。</small>
              {!currentOcrCandidate && (
                <>
                  <label htmlFor="ocr-region">服务地域</label>
                  <select id="ocr-region" disabled={ocrBusy} value={ocrRegion} onChange={(event) => setOcrRegion(event.target.value as "cn-beijing" | "ap-southeast-1") }>
                    <option value="cn-beijing">中国（北京）</option>
                    <option value="ap-southeast-1">新加坡</option>
                  </select>
                  <label htmlFor="ocr-retention">本所确认的服务数据保留说明</label>
                  <input id="ocr-retention" disabled={ocrBusy} maxLength={240} onChange={(event) => setOcrRetentionPolicy(event.target.value)} placeholder="例如：供应商仅按本所已确认期限保留本次输入" value={ocrRetentionPolicy} />
                  <label htmlFor="ocr-training">本所确认的服务训练使用说明</label>
                  <input id="ocr-training" disabled={ocrBusy} maxLength={240} onChange={(event) => setOcrTrainingPolicy(event.target.value)} placeholder="例如：供应商不得将本次材料用于模型训练" value={ocrTrainingPolicy} />
                  <label htmlFor="ocr-cost">本次成本上限（人民币分）</label>
                  <input id="ocr-cost" disabled={ocrBusy} inputMode="numeric" onChange={(event) => setOcrCostCap(event.target.value)} value={ocrCostCap} />
                  <label className={styles.confirmLine}>
                    <input checked={ocrConfirmed} disabled={!hasCurrentOriginalPreview || ocrBusy} onChange={(event) => setOcrConfirmed(event.target.checked)} type="checkbox" />
                    我确认仅向上述服务发送当前这一页，并仅将结果作为待我核对的文字，不自动写入事实或文书
                  </label>
                  <button disabled={!hasCurrentOriginalPreview || !ocrConfirmed || !ocrRetentionPolicy.trim() || !ocrTrainingPolicy.trim() || ocrBusy} onClick={() => void executePageOcr()} type="button">
                    {ocrBusy ? "正在识别本页…" : "确认并识别本页"}
                  </button>
                </>
              )}
              {currentOcrCandidate && (
                <>
                  <p>本页已有文字识别结果：{currentOcrCandidate.status === "CANDIDATE" ? "等待核对" : currentOcrCandidate.status === "ACCEPTED" ? "已确认保留" : "已确认不采用"} · {currentOcrCandidate.contentSha256.slice(0, 16)}…</p>
                  <button disabled={ocrBusy} onClick={() => void readOcrReviewCandidateText({ candidateId: currentOcrCandidate.candidateId }).then(setOcrText).catch((reason: unknown) => setOcrNotice(reason instanceof Error ? reason.message : "文字识别结果读取失败"))} type="button">查看识别文字</button>
                  {ocrText && <pre className={styles.ocrCandidateText}>{ocrText}</pre>}
                  {currentOcrCandidate.status === "CANDIDATE" && (
                    <>
                      <label htmlFor="ocr-review-reason">核对说明</label>
                      <textarea id="ocr-review-reason" disabled={ocrBusy} maxLength={480} onChange={(event) => setOcrReviewReason(event.target.value)} placeholder="说明与本页原件比对后的保留或不采用理由" value={ocrReviewReason} />
                      <div className={styles.inlineActions}>
                        <button disabled={ocrBusy || !ocrReviewReason.trim()} onClick={() => void decideOcrCandidate("ACCEPTED")} type="button">确认采用文字</button>
                        <button className={styles.dangerAction} disabled={ocrBusy || !ocrReviewReason.trim()} onClick={() => void decideOcrCandidate("REJECTED")} type="button">不采用文字</button>
                      </div>
                    </>
                  )}
                  {currentOcrCandidate.status === "ACCEPTED" && (
                    <>
                      <label htmlFor="ocr-fact-text">从本页整理一项待确认事实</label>
                      <textarea id="ocr-fact-text" disabled={ocrBusy} maxLength={10_000} onChange={(event) => setOcrFactText(event.target.value)} placeholder="例如：某日向对方支付人民币某元。请写成可与原件逐项核对的陈述。" value={ocrFactText} />
                      <button disabled={ocrBusy || !ocrFactText.trim()} onClick={() => void createFactFromAcceptedOcr()} type="button">保存待确认事实</button>
                      <small>系统会固定本页原件和页码；仍须到“事实与争点”中确认这项事实。</small>
                    </>
                  )}
                </>
              )}
              {ocrNotice && <p className={styles.inspectorNote} role="status">{ocrNotice}</p>}
            </section>
          )}
          {review.sourceKind === "persistent-preview" && !review.lockedManifest && hasCurrentOriginalPreview && (
            <section className={styles.decisionPanel} aria-label="记录本页交易信息">
              <strong>记录本页交易信息</strong>
              <small>无需识别文字。请以已打开的原件为准，手工记录日期、金额和付款方向；系统不会自动把转账认定为借款、还款、本金或利息。</small>
              <div className={styles.ocrTransactionEntry}>
                <div className={styles.paymentClassificationFields}>
                  <label><span>日期精度</span><select disabled={ocrBusy} value={ocrTransactionDraft.datePrecision} onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, datePrecision: event.target.value as typeof prior.datePrecision, localDate: event.target.value === "EXACT_DATE" ? prior.localDate : "" }))}><option value="EXACT_DATE">确切日期</option><option value="MONTH_ONLY">仅知月份</option><option value="YEAR_ONLY">仅知年份</option><option value="UNKNOWN">日期不明</option></select></label>
                  {ocrTransactionDraft.datePrecision === "EXACT_DATE" && <label><span>交易日期</span><input disabled={ocrBusy} onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, localDate: event.target.value }))} type="date" value={ocrTransactionDraft.localDate} /></label>}
                  <label><span>金额</span><input disabled={ocrBusy} id="ocr-transaction-amount" inputMode="decimal" onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, amount: event.target.value }))} placeholder="例如：800.00" value={ocrTransactionDraft.amount} /></label>
                  <label><span>币种</span><select disabled={ocrBusy} value={ocrTransactionDraft.currency} onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, currency: event.target.value }))}><option value="CNY">CNY（人民币）</option><option value="USD">USD</option><option value="HKD">HKD</option></select></label>
                  <label><span>相对当事人的方向</span><select disabled={ocrBusy} value={ocrTransactionDraft.direction} onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, direction: event.target.value as typeof prior.direction }))}><option value="UNKNOWN">暂不确定</option><option value="OUTGOING">我方支出</option><option value="INCOMING">我方收入</option></select></label>
                  <label><span>付款人（可选）</span><input disabled={ocrBusy} maxLength={500} onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, payerLabel: event.target.value }))} value={ocrTransactionDraft.payerLabel} /></label>
                  <label><span>收款人（可选）</span><input disabled={ocrBusy} maxLength={500} onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, payeeLabel: event.target.value }))} value={ocrTransactionDraft.payeeLabel} /></label>
                  <label><span>渠道</span><select disabled={ocrBusy} value={ocrTransactionDraft.channel} onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, channel: event.target.value as typeof prior.channel }))}><option value="WECHAT">微信</option><option value="BANK">银行</option><option value="CASH">现金</option><option value="CHAT_RECORD">聊天记录</option><option value="LOAN_INSTRUMENT">借据/合同</option><option value="OTHER">其他</option></select></label>
                  <label><span>交易号/备注（可选）</span><input disabled={ocrBusy} maxLength={500} onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, transactionReference: event.target.value }))} value={ocrTransactionDraft.transactionReference} /></label>
                </div>
                <label className={styles.confirmLine}><input checked={ocrTransactionDraft.confirmed} disabled={ocrBusy} onChange={(event) => setOcrTransactionDraft((prior) => ({ ...prior, confirmed: event.target.checked }))} type="checkbox" /><span>我已逐项与当前原件核对。本次只记录交易信息，尚未判断是借款、还款、本金或利息。</span></label>
                <button disabled={ocrBusy || !ocrTransactionDraft.confirmed} onClick={() => void createTransactionFromReviewedPage()} type="button">保存待确认交易</button>
              </div>
            </section>
          )}
        </article>

        <aside className={styles.inspector}>
          <p className={styles.eyebrow}>本页处理</p>
          <h3>第 {selected.pageNumber} 页</h3>
          <dl className={styles.inspectorFacts}>
            <div><dt>原始文件</dt><dd title={selected.originalLabel}>{selected.originalLabel}</dd></div>
            <div><dt>筛选结果</dt><dd className={statusClass(selected)}>{pageStatus(selected)}</dd></div>
            <div><dt>已确认红框</dt><dd>{selected.annotations.filter((item) => item.status === "APPROVED").length} 个</dd></div>
            <div><dt>重复页面</dt><dd>{duplicateGroup ? duplicateGroup.status : "无"}</dd></div>
          </dl>
          <p className={styles.inspectorNote}>{selected.reason ?? selected.syntheticPreview?.note ?? "请先判断：本页是否出现对方当事人、转账、还款、借条或与本案争点有关的内容。"}</p>

          {review.sourceKind === "persistent-preview" && !review.lockedManifest && (
            <div className={styles.decisionPanel}>
              <strong>筛选本页</strong>
              {selected.pendingDecision ? (
                <>
                  <div className={styles.pendingDecisionSummary}>
                    <span>待确认：{selected.pendingDecision.disposition === "INCLUDE" ? "保留到提交 PDF" : "不纳入提交 PDF"}</span>
                    <small>{selected.pendingDecision.reason}</small>
                  </div>
                  <label className={styles.confirmLine}>
                    <input checked={originalCompared} disabled={!hasCurrentOriginalPreview} onChange={(event) => setOriginalCompared(event.target.checked)} type="checkbox" />
                    我已打开并核对本页原件，确认上述处理
                  </label>
                  <button
                    disabled={!hasCurrentOriginalPreview || !originalCompared || reviewBusy !== null}
                    type="button"
                    onClick={() => void refreshAfterEvidenceMutation(
                      `approve-page:${selected.pageId}`,
                      () => approveEvidencePageDecision({ review, page: selected }),
                      "本页筛选结果已确认",
                    )}
                  >
                    {reviewBusy === `approve-page:${selected.pageId}` ? "正在确认…" : "确认本页筛选"}
                  </button>
                </>
              ) : (
                <>
                  <label htmlFor="page-disposition">本页处理方式</label>
                  <select id="page-disposition" value={pageDisposition} onChange={(event) => setPageDisposition(event.target.value as "INCLUDE" | "EXCLUDE") }>
                    <option value="INCLUDE">与对方/还款/争点有关，保留到提交 PDF</option>
                    <option value="EXCLUDE">无关或仅为重复内容，不纳入提交 PDF</option>
                  </select>
                  <label htmlFor="page-reason">说明</label>
                  <textarea id="page-reason" maxLength={2000} onChange={(event) => setPageReason(event.target.value)} placeholder="例如：出现对方微信名及向其转账记录；或本页与本案无关。" value={pageReason} />
                  <button
                    disabled={!hasCurrentOriginalPreview || !pageReason.trim() || reviewBusy !== null}
                    type="button"
                    onClick={() => void refreshAfterEvidenceMutation(
                      `propose-page:${selected.pageId}`,
                      () => proposeEvidencePageDecision({ review, pageId: selected.pageId, disposition: pageDisposition, reason: pageReason }),
                      "本页筛选已保存，等待确认",
                    )}
                  >
                    {reviewBusy === `propose-page:${selected.pageId}` ? "正在保存…" : "保存本页筛选（待确认）"}
                  </button>
                </>
              )}
            </div>
          )}

          {review.sourceKind === "persistent-preview" && !review.lockedManifest && hasCurrentOriginalPreview && draftBox && (
            <div className={styles.decisionPanel}>
              <strong>为提交 PDF 标红</strong>
              <small>已框出本页一处内容。请说明它与对方当事人、还款或争点的关系。</small>
              <label htmlFor="draft-box-label">红框说明</label>
              <input id="draft-box-label" maxLength={500} onChange={(event) => setDraftLabel(event.target.value)} placeholder="例如：对方微信名；向对方支付的转账记录" value={draftLabel} />
              <button
                disabled={!draftLabel.trim() || reviewBusy !== null}
                onClick={() => void refreshAfterEvidenceMutation(
                  `propose-annotation:${selected.pageId}`,
                  () => proposeEvidenceAnnotation({ review, pageId: selected.pageId, ...draftBox, label: draftLabel }),
                  "红框已保存，等待确认",
                )}
                type="button"
              >
                {reviewBusy === `propose-annotation:${selected.pageId}` ? "正在保存红框…" : "保存红框（待确认）"}
              </button>
            </div>
          )}

          {selected.annotations.length > 0 && (
            <div className={styles.coordinateList}>
              <strong>已标红的内容</strong>
              {review.sourceKind === "persistent-preview" && !review.lockedManifest && selected.annotations.some((item) => item.status === "CANDIDATE") && !selected.pendingDecision && duplicateGroup?.status !== "CANDIDATE" && (
                <label className={styles.confirmLine}>
                  <input checked={originalCompared} disabled={!hasCurrentOriginalPreview} onChange={(event) => setOriginalCompared(event.target.checked)} type="checkbox" />
                  我已打开并核对本页原件，确认这些红框标出的内容
                </label>
              )}
              {selected.annotations.map((annotation) => (
                <div className={styles.coordinateItem} key={annotation.annotationId}>
                  <span>{annotation.label} · {annotation.status === "APPROVED" ? "已确认用于提交 PDF" : "等待确认"}</span>
                  {review.sourceKind === "persistent-preview" && !review.lockedManifest && annotation.status === "CANDIDATE" && (
                    <button
                      disabled={!hasCurrentOriginalPreview || !originalCompared || reviewBusy !== null}
                      type="button"
                      onClick={() => void refreshAfterEvidenceMutation(
                        `approve-annotation:${annotation.annotationId}`,
                        () => approveEvidenceAnnotation({ review, page: selected, annotationId: annotation.annotationId }),
                        "红框已确认用于提交 PDF",
                      )}
                    >
                      {reviewBusy === `approve-annotation:${annotation.annotationId}` ? "正在确认…" : "确认红框"}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          {review.sourceKind === "persistent-preview" && !review.lockedManifest && duplicateGroup?.status === "CANDIDATE" && (
            <div className={styles.decisionPanel}>
              <strong>处理重复页面</strong>
              <label htmlFor="persistent-duplicate-decision">这些页面是否是同一内容？</label>
              <select id="persistent-duplicate-decision" value={duplicateResolution} onChange={(event) => setDuplicateResolution(event.target.value as "same" | "distinct") }>
                <option value="same">是同一内容，只保留一页进入提交 PDF</option>
                <option value="distinct">不是重复内容，分别判断是否保留</option>
              </select>
              {duplicateResolution === "same" && (
                <>
                  <label htmlFor="canonical-page">保留哪一页到提交 PDF</label>
                  <select id="canonical-page" value={canonicalPageId ?? ""} onChange={(event) => setCanonicalPageId(event.target.value)}>
                    {duplicateGroup.pageIds.map((pageId) => <option key={pageId} value={pageId}>{pageLabel(pageId)}</option>)}
                  </select>
                </>
              )}
              <label className={styles.confirmLine}>
                <input checked={originalCompared} disabled={!duplicatePagesLoaded || !duplicatePagesPreviewed} onChange={(event) => setOriginalCompared(event.target.checked)} type="checkbox" />
                我已逐页打开原件并比对本组全部 {duplicateGroup.pageIds.length} 页
              </label>
              {!duplicatePagesLoaded && <small>本组还有页面未载入；请先在左侧继续载入。</small>}
              {duplicatePagesLoaded && !duplicatePagesPreviewed && <small>请从左侧依次打开本组每一页原件后再确认。</small>}
              <button
                disabled={!duplicatePagesLoaded || !duplicatePagesPreviewed || !originalCompared || reviewBusy !== null}
                type="button"
                onClick={() => void refreshAfterEvidenceMutation(
                  `resolve-duplicate:${duplicateGroup.groupId}`,
                  () => resolveEvidenceDuplicateGroup({
                    review,
                    groupId: duplicateGroup.groupId,
                    sameSourcePage: duplicateResolution === "same",
                    canonicalPageId: duplicateResolution === "same" ? canonicalPageId : null,
                  }),
                  "重复页面处理已确认",
                )}
              >
                {reviewBusy === `resolve-duplicate:${duplicateGroup.groupId}` ? "正在确认…" : "确认重复页面处理"}
              </button>
            </div>
          )}

          {review.sourceKind === "synthetic-alpha" && duplicateGroup?.status === "CANDIDATE" && (
            <div className={styles.decisionPanel}>
              <label htmlFor="duplicate-decision">演示：处理重复页面</label>
              <select id="duplicate-decision" value={duplicateDecision} onChange={(event) => setDuplicateDecision(event.target.value as DuplicateDecision)}>
                <option value="pending">尚未决定</option>
                <option value="exclude">排除重复派生引用</option>
                <option value="keep">保留为不同页</option>
              </select>
              <button type="button" onClick={recordSyntheticDecision}>保存演示处理结果</button>
            </div>
          )}

          {review.sourceKind === "synthetic-alpha" && <div className={styles.auditNotice} role="status">{auditNotice}</div>}
          <div className={styles.manifestState}>
            <span>提交材料清单</span>
            <strong>{review.lockedManifest ? "已确认，可以生成 PDF" : "尚未确认"}</strong>
            <small>{review.lockedManifest ? `${review.lockedManifest.includedPages} 页保留 / ${review.lockedManifest.excludedPages} 页不纳入` : `还有 ${unresolvedCount} 页需要筛选`}</small>
            {!review.lockedManifest && <small>待确认筛选 {pendingDecisionCount} 项 · 待处理重复页面 {unresolvedDuplicateCount} 组</small>}
            <small>提交版 PDF：{review.derivatives.length ? review.derivatives.map((item) => `${item.artifactType === "ANNOTATED_RELATED_PAGES_PDF" ? "标红还款证据" : "筛选后的材料"} ${runStatusLabel(item.status)}`).join("；") : "尚未生成"}</small>
            <small>生成状态：{review.derivativeRuns.length ? review.derivativeRuns.map((item) => `${runStatusLabel(item.status)}（第 ${item.attemptCount}/3 次${item.failureCode ? ` · ${item.failureCode}` : ""}）`).join("；") : "尚未开始"}</small>
          </div>
          {review.sourceKind === "persistent-preview" && !review.lockedManifest && unresolvedCount === 0 && pendingDecisionCount === 0 && unresolvedDuplicateCount === 0 && (
            <div className={styles.lockPanel}>
              <label className={styles.confirmLine}>
                <input checked={manifestConfirmed} onChange={(event) => setManifestConfirmed(event.target.checked)} type="checkbox" />
                我确认已完成逐页筛选、重复页面处理和红框确认；这些内容构成本案当前的提交版证据材料
              </label>
              <button
                disabled={!manifestConfirmed || reviewBusy !== null}
                type="button"
                onClick={() => void refreshAfterEvidenceMutation(
                  "lock-manifest",
                  () => lockEvidenceManifest(review),
                  "提交材料清单已确认",
                )}
              >
                {reviewBusy === "lock-manifest" ? "正在确认…" : "确认筛选完成"}
              </button>
            </div>
          )}
          {review.sourceKind === "persistent-preview" && review.lockedManifest && !review.derivativeRuns.some((item) => ["QUEUED", "RUNNING", "SUCCEEDED"].includes(item.status)) && (
            <button className={styles.primaryArtifactAction} disabled={artifactBusy !== null} type="button" onClick={() => void enqueueDerivativeRun()}>
              {artifactBusy === "enqueue" ? "正在准备 PDF…" : review.derivativeRuns.some((item) => item.status === "FAILED") ? "重新生成提交版证据 PDF" : "生成提交版证据 PDF"}
            </button>
          )}
          {review.sourceKind === "persistent-preview" && review.derivatives.some((item) => item.status === "VERIFIED") && (
            <div className={styles.artifactActions}>
              {review.derivatives.filter((item) => item.status === "VERIFIED").map((item) => (
                <div key={item.derivativeId}>
                  <strong>{item.artifactType === "ANNOTATED_RELATED_PAGES_PDF" ? "标红还款证据 PDF" : "筛选后的证据 PDF"}</strong>
                  <span>{item.pageCount} 页 · {item.artifactSha256.slice(0, 12)}…</span>
                  <button disabled={artifactBusy !== null} type="button" onClick={() => void readDerivative(item, "INLINE_PREVIEW")}>
                    {artifactBusy === `${item.derivativeId}:INLINE_PREVIEW` ? "正在打开…" : "查看 PDF"}
                  </button>
                  <button disabled={artifactBusy !== null} type="button" onClick={() => void readDerivative(item, "DOWNLOAD")}>
                    {artifactBusy === `${item.derivativeId}:DOWNLOAD` ? "正在准备…" : "保存 PDF"}
                  </button>
                </div>
              ))}
            </div>
          )}
          {artifactNotice && <div className={styles.auditNotice} role="status">{artifactNotice}</div>}
          {reviewNotice && <div className={styles.auditNotice} role="status">{reviewNotice}</div>}
          {review.sourceKind === "synthetic-alpha" && <button className={styles.disabledAction} disabled type="button">生成提交版证据 PDF（演示模式不能生成正式文件）</button>}
        </aside>
      </div>
    </section>
  );
}

function runStatusLabel(status: string): string {
  if (status === "QUEUED") return "等待本机生成";
  if (status === "RUNNING") return "正在生成 PDF";
  if (status === "SUCCEEDED" || status === "VERIFIED") return "已生成";
  if (status === "FAILED") return "生成失败";
  return status;
}

function intakeRunStatusLabel(status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "PARTIAL"): string {
  if (status === "QUEUED") return "等待开始整理材料";
  if (status === "RUNNING") return "本机正在整理材料";
  if (status === "SUCCEEDED") return "当前范围的材料已整理完成";
  return "材料已整理完成，但仍有需要人工查看的文件";
}

function intakeItemStatusLabel(status: LocalFolderIntakeView["intakeItems"][number]["status"]): string {
  const labels: Record<LocalFolderIntakeView["intakeItems"][number]["status"], string> = {
    QUEUED: "等待处理",
    RUNNING: "正在检查",
    REGISTERED: "已登记",
    REVIEW_REQUIRED: "待转换",
    BLOCKED: "已阻断",
    FAILED: "处理失败",
  };
  return labels[status];
}

function intakeOutcomeLabel(code: string | null): string {
  if (!code) return "尚无处理结论";
  const labels: Record<string, string> = {
    IMAGE_CONVERSION_REQUIRED: "图片结构已核验，等待生成安全 PDF",
    HEIC_CONVERSION_REQUIRED: "HEIC 签名已核验，等待隔离转换",
    WORD_CONVERSION_REQUIRED: "Word 容器已核验，等待隔离转换",
    SPREADSHEET_CONVERSION_REQUIRED: "表格容器已核验，等待隔离转换",
    LEGACY_SPREADSHEET_CONVERSION_REQUIRED: "旧版表格等待隔离转换",
    TEXT_CONVERSION_REQUIRED: "文本结构已核验，等待排版",
    EMAIL_CONVERSION_REQUIRED: "邮件结构已核验，等待安全展开",
    ARCHIVE_EXPANSION_REQUIRES_APPROVAL: "压缩包结构已核验，展开前需批准",
    UNSUPPORTED_FILE_TYPE: "暂不支持自动处理",
    MALWARE_DETECTED: "本机扫描发现恶意内容",
    MALWARE_SCAN_INDETERMINATE: "本机扫描未得出可信结论",
    EMPTY_FILE: "空文件",
    FILE_SIGNATURE_MISMATCH: "扩展名与真实格式不一致",
    IMAGE_FORMAT_UNSUPPORTED: "图片格式不受支持",
    IMAGE_PIXEL_LIMIT: "图片像素规模超过安全上限",
    IMAGE_MULTIFRAME_UNSUPPORTED: "多帧图片暂不自动处理",
    IMAGE_MALFORMED: "图片结构损坏",
    OFFICE_ACTIVE_CONTENT: "Office 文件含宏、ActiveX 或嵌入对象",
    OFFICE_EXTERNAL_RELATIONSHIP: "Office 文件含外部链接",
    OFFICE_UNSAFE_RELATIONSHIP: "Office 文件包含越界关系路径",
    OFFICE_CONTAINER_MALFORMED: "Office 文件结构损坏",
    OFFICE_RELATIONSHIP_MALFORMED: "Office 关系文件损坏",
    OFFICE_XML_ACTIVE_CONTENT: "Office XML 含主动内容声明",
    ARCHIVE_UNSAFE_PATH: "压缩包含越界路径",
    ARCHIVE_DUPLICATE_PATH: "压缩包含冲突路径",
    ARCHIVE_ENCRYPTED: "压缩包已加密",
    ARCHIVE_SYMBOLIC_LINK: "压缩包含符号链接",
    ARCHIVE_ENTRY_LIMIT: "压缩包条目过多",
    ARCHIVE_ENTRY_SIZE_LIMIT: "压缩包单项过大",
    ARCHIVE_TOTAL_SIZE_LIMIT: "压缩包展开规模过大",
    ARCHIVE_COMPRESSION_RATIO_LIMIT: "压缩比例异常",
    ARCHIVE_MALFORMED: "压缩包结构损坏",
    TEXT_BINARY_CONTENT: "文件含二进制内容，不是可信文本",
    TEXT_ENCODING_UNSUPPORTED: "文本编码不受支持",
    TEXT_CONTROL_CONTENT: "文本含异常控制字符",
    EMAIL_MALFORMED: "邮件结构损坏",
    EMAIL_PART_LIMIT: "邮件组成部分过多",
    EMAIL_UNSAFE_ATTACHMENT_NAME: "邮件附件名包含危险路径",
    RECOVERY_ATTEMPTS_EXHAUSTED: "自动恢复次数已用尽",
  };
  return labels[code] ?? `需要管理员复核（${code}）`;
}

function folderChangeLabel(kind: LocalFolderIntakeView["files"][number]["changeKind"]): string {
  if (kind === "NEW") return "新增";
  if (kind === "MODIFIED") return "已修改";
  if (kind === "MOVED") return "已移动";
  if (kind === "MISSING") return "已缺失";
  return "未变化";
}

function folderKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    PDF: "PDF",
    IMAGE: "图片",
    WORD_DOCUMENT: "Word 文档",
    SPREADSHEET: "表格",
    TEXT: "文本",
    EMAIL: "邮件",
    ARCHIVE: "压缩包",
    OTHER: "其他文件",
  };
  return labels[kind] ?? "其他文件";
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
