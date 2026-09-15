"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
  type InputHTMLAttributes,
} from "react";
import { useRouter } from "next/navigation";
import {
  WebLawyerApiError,
  WEB_MAX_ARCHIVE_BYTES,
  WEB_MAX_COMMON_MATERIAL_BYTES,
  WEB_MAX_IMAGE_BYTES,
  WEB_MAX_PDF_BYTES,
  createWebCaseIdempotencyKey,
  createWebCommonMaterialUploadSlot,
  createWebLawyerCase,
  createWebMaterialUploadSlot,
  createWebMaterialArchiveSlot,
  isPdfCandidate,
  isImageMaterialCandidate,
  isCommonMaterialCandidate,
  isLegacyCommonMaterialCandidate,
  isZipCandidate,
  isWebLoginRequired,
  isWebMaterialRejected,
  readWebCaseReadiness,
  readWebMaterialArchiveStatus,
  readWebCommonMaterialUploadStatus,
  readWebMaterialUploadStatus,
  listWebLawyerCases,
  normalizeCaseTitle,
  readWebLawyerSession,
  uploadWebMaterialPdf,
  uploadWebCommonMaterial,
  uploadWebMaterialArchive,
  type WebLawyerCase,
  type WebLawyerSession,
  type WebCaseReadiness,
  type WebMaterialReceipt,
  type WebMaterialArchiveReceipt,
  type WebCommonMaterialAdmissionReceipt,
  type WebCommonMaterialUploadStatus,
  type WebMaterialUploadStatus,
} from "@/lib/web-lawyer-api";
import { WebCaseReview } from "@/components/web-case-review";
import { WebLegalReview } from "@/components/web-legal-review";
import { WebDecisionPackage } from "@/components/web-decision-package";
import { WebDefenceBrief } from "@/components/web-defence-brief";
import { WebDeliverableChecklist } from "@/components/web-deliverables";
import { WebCalculationWorkbench } from "@/components/web-calculation-workbench";
import { WebSubmissionReview } from "@/components/web-submission-review";
import { WebEvidenceReview } from "@/components/web-evidence-review";
import { WebCasePosture } from "@/components/web-case-posture";
import { UnifiedCaseAgentPanel } from "@/components/web-agent-panel";
import {
  canOpenWebLawyerView,
  hasRegisteredCaseMaterials,
  type WebLawyerInitialView,
} from "@/lib/web-lawyer-navigation";
import {
  WEB_LOGIN_FAILURE_MESSAGE,
  resolveWebLoginFailureLocation,
} from "@/lib/web-login-failure";
import styles from "./case-workbench.module.css";

const isLocalWebMode = process.env.NEXT_PUBLIC_WEB_API_PREFIX === "/api/local/v1";

export type { WebLawyerInitialView } from "@/lib/web-lawyer-navigation";

type SessionState =
  | { kind: "checking" }
  | { kind: "login-required"; loginFailure: boolean }
  | { kind: "ready"; session: WebLawyerSession }
  | { kind: "unavailable"; message: string; requestId: string | null };

type CaseListState =
  | { kind: "loading" }
  | { kind: "ready"; cases: WebLawyerCase[]; refreshing: boolean }
  | { kind: "unavailable"; message: string; requestId: string | null };

type UploadItemState = "READY" | "CREATING_SLOT" | "UPLOADING" | "RECEIVED" | "ARCHIVE_STORED" | "REJECTED" | "UNCONFIRMED";
type UploadItemKind = "PDF" | "ZIP" | "COMMON";

type UploadItem = {
  id: string;
  file: File | null;
  kind: UploadItemKind;
  /** 图片走与 PDF 相同的接收通道（服务端 kind 仍为 PDF），仅用于界面标签与大小上限。 */
  image: boolean;
  name: string;
  byteSize: number;
  serverId: string | null;
  state: UploadItemState;
  message: string | null;
  receipt: WebMaterialReceipt | WebMaterialArchiveReceipt | WebCommonMaterialAdmissionReceipt | null;
};

type PendingCaseCreation = {
  title: string;
  idempotencyKey: string;
};

type DirectoryInputAttributes = InputHTMLAttributes<HTMLInputElement> & {
  webkitdirectory?: string;
  directory?: string;
};

const sectionLabels: Record<WebLawyerInitialView, string> = {
  overview: "办案首页",
  evidence: "收集材料",
  facts: "核对案情",
  legal: "法律依据",
  calculation: "还款与利息",
  analysis: "决策包",
  brief: "答辩状",
  deliverables: "交付清单",
  bundle: "应诉材料",
  security: "工作台设置",
};

/**
 * Browser-native first entry.  It intentionally starts with a live session
 * probe instead of a synthetic case, desktop path picker, or model settings.
 */
export function WebLawyerWorkbench({ initialView = "overview" }: { initialView?: WebLawyerInitialView }) {
  const [sessionState, setSessionState] = useState<SessionState>({ kind: "checking" });
  const [sessionRefreshKey, setSessionRefreshKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    void readWebLawyerSession(controller.signal)
      .then((session) => {
        if (!active) return;
        const loginFailure = resolveWebLoginFailureLocation(window.location.href);
        if (loginFailure !== null) {
          window.history.replaceState(window.history.state, "", loginFailure.replacementPath);
        }
        setSessionState(session === null
          ? { kind: "login-required", loginFailure: loginFailure !== null }
          : { kind: "ready", session });
      })
      .catch((reason: unknown) => {
        if (!active || isAbortError(reason)) return;
        const issue = operationalIssue(reason, "无法连接律所办案服务。请确认该地址由律所管理员部署，并在恢复后重新尝试。");
        setSessionState({ kind: "unavailable", ...issue });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [sessionRefreshKey]);

  if (sessionState.kind === "ready") {
    return (
      <AuthenticatedWebLawyerWorkbench
        initialView={initialView}
        key={sessionState.session.actor.roles.join(":")}
        onSessionExpired={() => setSessionState({ kind: "login-required", loginFailure: false })}
        session={sessionState.session}
      />
    );
  }

  if (sessionState.kind === "login-required") {
    return <WebLoginRequired loginFailure={sessionState.loginFailure} />;
  }

  if (sessionState.kind === "unavailable") {
    return (
      <WebOperationalError
        message={sessionState.message}
        onRetry={() => {
          setSessionState({ kind: "checking" });
          setSessionRefreshKey((current) => current + 1);
        }}
        requestId={sessionState.requestId}
      />
    );
  }

  return <WebConnectionChecking />;
}

function WebConnectionChecking() {
  return (
    <WebEntryShell status={isLocalWebMode ? "正在准备本机工作台" : "正在确认律所办案服务"}>
      <section className={styles.webLawyerEntryCard} aria-live="polite">
        <p className={styles.eyebrow}>{isLocalWebMode ? "离线开发模式" : "律师办案工作台"}</p>
        <h1>{isLocalWebMode ? "正在准备基础材料环境" : "正在确认受管登录状态"}</h1>
        <p>{isLocalWebMode ? "这是同一套律师界面的基础能力验证，不代表完整 Agent 已部署。" : "系统尚未读取、创建、上传或修改任何案件材料。"}</p>
      </section>
    </WebEntryShell>
  );
}

function WebLoginRequired({ loginFailure }: { loginFailure: boolean }) {
  if (isLocalWebMode) {
    return (
      <WebEntryShell status="本机工作台已就绪">
        <section className={`${styles.webLawyerEntryCard} ${styles.webLawyerLoginCard}`} aria-labelledby="local-web-title">
          <div>
            <p className={styles.eyebrow}>离线开发模式</p>
            <h1 id="local-web-title">基础材料流程已准备</h1>
            <p>该模式用于开发和断网验证，只提供本机 PDF 接收、文本层预处理与人工页级审阅。它不是律师正式商用入口，也不会伪装完整 Agent、视觉/OCR、法律检索、测算或文书能力。</p>
            <button className={styles.webLawyerPrimaryAction} onClick={() => window.location.reload()} type="button">进入基础模式</button>
          </div>
          <dl className={styles.webLawyerLoginFacts}>
            <div><dt>用于验证</dt><dd>建案、PDF 接收、文本层预处理、人工审阅</dd></div>
            <div><dt>明确未装配</dt><dd>完整 Agent、视觉/OCR、联网、测算与文书</dd></div>
          </dl>
        </section>
      </WebEntryShell>
    );
  }
  return (
    <WebEntryShell status={loginFailure ? "需要重新登录" : "需要律所登录"}>
      <section
        className={`${styles.webLawyerEntryCard} ${styles.webLawyerLoginCard} ${loginFailure ? styles.webLawyerErrorCard : ""}`}
        aria-labelledby="web-login-title"
      >
        <div>
          <p className={styles.eyebrow}>律师办案工作台</p>
          <h1 id="web-login-title">{loginFailure ? "登录尚未完成" : "从律所账号进入案件工作台"}</h1>
          {loginFailure ? (
            <p role="alert">{WEB_LOGIN_FAILURE_MESSAGE}</p>
          ) : (
            <p>登录后，系统会由服务端确认你的律所成员身份和案件权限；网页不能自行指定角色或读取电脑上的文件夹。</p>
          )}
          <a className={styles.webLawyerPrimaryAction} href="/api/v1/auth/login">
            {loginFailure ? "重新登录并输入动态验证码" : "使用律所账号登录"}
          </a>
        </div>
        <dl className={styles.webLawyerLoginFacts}>
          <div><dt>材料进入方式</dt><dd>律师明确选择 PDF、Word、Excel 与图片</dd></div>
          <div><dt>材料保存位置</dt><dd>律所受管案卷库</dd></div>
          <div><dt>第一条正式流程</dt><dd>建案后接收材料</dd></div>
        </dl>
      </section>
    </WebEntryShell>
  );
}

function WebOperationalError({
  message,
  requestId,
  onRetry,
}: {
  message: string;
  requestId: string | null;
  onRetry: () => void;
}) {
  return (
    <WebEntryShell status="服务暂不可用">
      <section className={`${styles.webLawyerEntryCard} ${styles.webLawyerErrorCard}`} aria-labelledby="web-service-error-title" role="alert">
        <p className={styles.eyebrow}>服务状态</p>
        <h1 id="web-service-error-title">暂时无法进入案件工作台</h1>
        <p>{message}</p>
        {requestId ? <p className={styles.webLawyerRequestId}>请求编号：<code>{requestId}</code></p> : null}
        <button className={styles.webLawyerPrimaryAction} onClick={onRetry} type="button">重新连接</button>
        <small>此页面没有读取、创建、上传或修改任何案件材料。</small>
      </section>
    </WebEntryShell>
  );
}

function WebEntryShell({ children, status }: { children: React.ReactNode; status: string }) {
  return (
    <main className={`${styles.shell} ${styles.webLawyerShell}`}>
      <header className={styles.topbar}>
        <div className={styles.brand} aria-label="律师办案工作台">
          <span className={styles.brandMark}>案</span>
          <span>律师办案工作台</span>
          <small>Web 工作台</small>
        </div>
        <div className={styles.topbarMeta}>
          <span>{status}</span>
          <span className={styles.dot} aria-hidden="true" />
          <span>{isLocalWebMode ? "离线开发模式" : "律所受管服务"}</span>
        </div>
      </header>
      <div className={styles.webLawyerEntry}>{children}</div>
      <footer className={styles.footer}>
          <span>{isLocalWebMode ? "开发数据只在你明确选择后写入本机工作区。" : "案件材料只在律师明确操作后进入受管案卷库。"}</span>
          <span>浏览器不会扫描个人电脑文件夹。</span>
      </footer>
    </main>
  );
}

function AuthenticatedWebLawyerWorkbench({
  initialView,
  session,
  onSessionExpired,
}: {
  initialView: WebLawyerInitialView;
  session: WebLawyerSession;
  onSessionExpired: () => void;
}) {
  // The first case-list response can return before an effect runs. Resolve the
  // requested case during initial render so a direct link never falls back to
  // the empty “建立案件” screen simply because of that timing race.
  const requestedCaseIdRef = useRef<string | null>(
    typeof window === "undefined"
      ? null
      : new URLSearchParams(window.location.search).get("case"),
  );
  const [caseListState, setCaseListState] = useState<CaseListState>({ kind: "loading" });
  const [caseListRefreshKey, setCaseListRefreshKey] = useState(0);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [caseTitle, setCaseTitle] = useState("");
  const [creatingCase, setCreatingCase] = useState(false);
  const [caseNotice, setCaseNotice] = useState<string | null>(null);
  const [pendingCaseCreation, setPendingCaseCreation] = useState<PendingCaseCreation | null>(null);
  const [hasReceivedMaterials, setHasReceivedMaterials] = useState(false);
  const [postureCurrent, setPostureCurrent] = useState(false);

  const refreshCaseList = useCallback(async (signal?: AbortSignal) => {
    try {
      const cases = await listWebLawyerCases(signal);
      if (signal?.aborted) return;
      setCaseListState({ kind: "ready", cases, refreshing: false });
      setSelectedCaseId((current) => {
        if (current !== null && cases.some((item) => item.caseId === current)) return current;
        const requestedCaseId = requestedCaseIdRef.current;
        return requestedCaseId !== null && cases.some((item) => item.caseId === requestedCaseId) ? requestedCaseId : null;
      });
    } catch (reason: unknown) {
      if (signal?.aborted || isAbortError(reason)) return;
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      const issue = operationalIssue(reason, "无法读取案件列表。系统没有以示例案件替代真实资料。");
      setCaseListState({ kind: "unavailable", ...issue });
    }
  }, [onSessionExpired]);

  const requestCaseListRefresh = useCallback(() => {
    setCaseListState((current) => current.kind === "ready"
      ? { ...current, refreshing: true }
      : { kind: "loading" });
    setCaseListRefreshKey((current) => current + 1);
  }, []);

  const advanceCaseVersion = useCallback((caseId: string, version: number) => {
    setCaseListState((current) => current.kind !== "ready" ? current : {
      ...current,
      cases: current.cases.map((item) => item.caseId === caseId ? { ...item, version } : item),
    });
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    // Defer the first state-producing request by one task so this effect only
    // establishes a subscription/cancellation boundary for the remote service.
    const timer = window.setTimeout(() => void refreshCaseList(controller.signal), 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [caseListRefreshKey, refreshCaseList]);

  const cases = caseListState.kind === "ready" ? caseListState.cases : [];
  const selectedCase = selectedCaseId === null ? null : cases.find((item) => item.caseId === selectedCaseId) ?? null;
  const hasCaseMaterials = Boolean(
    selectedCase
    && hasRegisteredCaseMaterials(selectedCase.materialCount, hasReceivedMaterials),
  );
  const canOpenCurrentView = canOpenWebLawyerView(
    session.capabilities,
    initialView,
    Boolean(selectedCase),
    hasCaseMaterials,
  );

  async function createCase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let title: string;
    try {
      title = normalizeCaseTitle(caseTitle);
    } catch (reason: unknown) {
      setCaseNotice(localMessage(reason, "案件名称无法使用。"));
      return;
    }
    if (!session.capabilities.canCreateCase) {
      setCaseNotice("当前受管角色没有建立案件的权限。请联系案件负责人或律所管理员处理。");
      return;
    }
    setCreatingCase(true);
    setCaseNotice(null);
    try {
      const creation = pendingCaseCreation?.title === title
        ? pendingCaseCreation
        : { title, idempotencyKey: createWebCaseIdempotencyKey() };
      setPendingCaseCreation(creation);
      const createdCase = await createWebLawyerCase(title, creation.idempotencyKey);
      setCaseListState((current) => {
        const currentCases = current.kind === "ready" ? current.cases : [];
        const withoutCurrent = currentCases.filter((item) => item.caseId !== createdCase.caseId);
        return { kind: "ready", cases: [createdCase, ...withoutCurrent], refreshing: false };
      });
      setSelectedCaseId(createdCase.caseId);
      setHasReceivedMaterials(false);
      setPostureCurrent(false);
      setCaseTitle("");
      setPendingCaseCreation(null);
      setCaseNotice("案件已建立。现在可以明确选择需要接收的 PDF、Word、Excel 与图片材料。");
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      const issue = operationalIssue(reason, "建案结果未确认。请先刷新案件列表；如需重试，请保持案件名称不变以沿用同一请求编号。");
      setCaseNotice(reason instanceof WebLawyerApiError && [400, 422].includes(reason.status ?? 0)
        ? issue.message
        : `${issue.message}${issue.requestId ? ` 请求编号：${issue.requestId}` : ""}`);
    } finally {
      setCreatingCase(false);
    }
  }

  const workflowLabel = initialView === "overview" && selectedCase
    ? session.capabilities.canRunCaseAgent ? "可以继续处理" : "暂不能继续处理"
    : !canOpenCurrentView && initialView !== "overview"
    ? isLocalWebMode ? `本机模式未提供${sectionLabels[initialView]}` : `${sectionLabels[initialView]}尚未开放`
    : !selectedCase
    ? "01 / 建立案件"
    : initialView === "analysis" ? "决策包"
    : initialView === "brief" ? "答辩状"
    : initialView === "deliverables" ? "交付清单"
    : initialView === "legal"
      ? "03 / 核对适用依据"
    : initialView === "calculation"
        ? "04 / 核对金额"
        : initialView === "bundle"
          ? "05 / 审阅成果文件"
      : hasCaseMaterials
            ? "材料与证据核对"
            : "接收案件材料";
  const workflowHint = !selectedCase
    ? "先选定案件，材料不会进入未选定的工作区。"
    : hasCaseMaterials
    ? "材料已入卷。可核对页面、标记重点，并回到案件首页继续处理。"
    : "先选择本案材料。接收完成后，再进入材料核对。";

  return (
    <main className={`${styles.shell} ${styles.webLawyerShell}`}>
      <header className={styles.topbar}>
        <div className={styles.brand} aria-label="律师办案工作台">
          <span className={styles.brandMark}>案</span>
          <span>律师办案工作台</span>
          <small>{isLocalWebMode ? "本机审阅" : "我的案件"}</small>
        </div>
        <div className={styles.topbarMeta}>
          <span>{roleSummary(session.actor.roles)}</span>
          <span className={styles.dot} aria-hidden="true" />
          <span className={styles.runtimeState}>{isLocalWebMode ? "本机审阅" : "已登录"}</span>
        </div>
      </header>

      <section className={styles.webLawyerCaseHeader} aria-labelledby="web-case-title">
        <div>
          <p className={styles.eyebrow}>{isLocalWebMode ? "本机审阅案件" : "案件工作区"}</p>
          <h1 id="web-case-title">{selectedCase ? selectedCase.title : "选择或建立案件"}</h1>
          <p>{selectedCase
            ? isLocalWebMode ? "本机审阅 · 可接收材料并进行人工核对" : `${selectedCase.materialCount} 份材料已入卷`
            : "选择左侧案件，或建立新案件。"}</p>
        </div>
        {initialView !== "overview" ? <div className={styles.webLawyerCaseHeaderStatus}>
          <span>下一步</span>
          <strong>{workflowLabel}</strong>
          <small>{initialView === "evidence"
            ? workflowHint
            : initialView === "calculation"
              ? "核对已确认的规则和交易后，再进行正式测算。"
              : initialView === "bundle"
                ? "先审阅文件和依据，确认无误后再导出提交材料。"
                : `完成前一步后，即可继续处理“${sectionLabels[initialView]}”。`}</small>
        </div> : null}
      </section>

      <WebLawyerNavigation capabilities={session.capabilities} caseId={selectedCaseId} currentView={initialView} hasMaterials={hasCaseMaterials} />

      <div className={styles.webLawyerWorkspace}>
        <aside className={styles.webLawyerCaseSidebar} aria-label="案件列表">
          <div className={styles.webLawyerSidebarHeading}>
            <div><p className={styles.sideLabel}>案件</p><strong>我的可访问案件</strong></div>
            <button disabled={caseListState.kind === "loading"} onClick={requestCaseListRefresh} type="button">
              {caseListState.kind === "ready" && caseListState.refreshing ? "刷新中" : "刷新"}
            </button>
          </div>
          {caseListState.kind === "loading" ? (
            <p className={styles.webLawyerSidebarState}>正在读取案件列表…</p>
          ) : caseListState.kind === "unavailable" ? (
            <div className={styles.webLawyerSidebarError} role="alert">
              <strong>案件列表不可用</strong>
              <span>{caseListState.message}</span>
              {caseListState.requestId ? <code>{caseListState.requestId}</code> : null}
              <button onClick={requestCaseListRefresh} type="button">重新读取</button>
            </div>
          ) : cases.length === 0 ? (
            <p className={styles.webLawyerSidebarState}>目前没有可访问案件。可在右侧建立案件。</p>
          ) : (
            <div className={styles.webLawyerCaseRows}>
              {cases.map((item) => (
                <button
                  aria-pressed={selectedCaseId === item.caseId}
                  className={selectedCaseId === item.caseId ? styles.webLawyerCaseRowActive : undefined}
                  key={item.caseId}
                  onClick={() => {
                    if (selectedCaseId === item.caseId) return;
                    setSelectedCaseId(item.caseId);
                    setHasReceivedMaterials(false);
                    setPostureCurrent(false);
                    setCaseNotice(null);
                  }}
                  type="button"
                >
                  <strong>{item.title}</strong>
                  <small>{caseMeta(item)}</small>
                </button>
              ))}
            </div>
          )}
        </aside>

        <section className={styles.webLawyerContent} aria-live="polite">
          {caseListState.kind === "unavailable" ? (
            <CaseListUnavailable onRetry={requestCaseListRefresh} />
          ) : selectedCase ? (
            !canOpenCurrentView ? (
              <WebLockedStage hasMaterials={hasCaseMaterials} localMode={isLocalWebMode} />
            ) : initialView === "overview" ? (
              <WebCaseOverview
                canCompleteCaseAgentRun={session.capabilities.canCompleteCaseAgentRun}
                canReviewEvidence={session.capabilities.canReviewEvidence}
                canRunCaseAgent={session.capabilities.canRunCaseAgent}
                canReviewCaseAgent={session.capabilities.canReviewCaseAgent}
                canReviewCaseAgentDocuments={session.capabilities.canReviewCaseAgentDocuments}
                canReviewCasePosture={session.capabilities.canReviewCasePosture}
                canConfirmCasePosture={session.capabilities.canConfirmCasePosture}
                caseItem={selectedCase}
                hasMaterials={hasCaseMaterials}
                onSessionExpired={onSessionExpired}
                onVersionAdvanced={(version) => advanceCaseVersion(selectedCase.caseId, version)}
                onPostureCurrentChanged={setPostureCurrent}
                postureCurrent={postureCurrent}
              />
            ) : initialView === "evidence" ? (
              <MaterialIntake
                canUpload={session.capabilities.canUploadMaterial}
                canUploadCommon={session.capabilities.canUploadCommonMaterial}
                caseItem={selectedCase}
                key={selectedCase.caseId}
                onCaseVersionAdvanced={advanceCaseVersion}
                onMaterialsReceived={() => setHasReceivedMaterials(true)}
                onSessionExpired={onSessionExpired}
              />
            ) : initialView === "facts" ? (
              <WebCaseReview
                canDecide={session.capabilities.canConfirmFact}
                canReviewAgentLedgerExtractions={
                  session.workspaceMode === "FIRM_MANAGED"
                  && session.capabilities.canReviewAgentLedgerExtractions
                }
                canReviewAgentLedgerExceptionFollowups={
                  session.workspaceMode === "FIRM_MANAGED"
                  && session.capabilities.canReviewAgentLedgerExceptionFollowups
                }
                caseId={selectedCase.caseId}
                onSessionExpired={onSessionExpired}
                onVersionAdvanced={advanceCaseVersion}
              />
            ) : initialView === "legal" ? (
              <WebLegalReview caseId={selectedCase.caseId} canConfirmLegal={session.actor.roles.includes("LEAD_LAWYER")} onSessionExpired={onSessionExpired} onVersionAdvanced={advanceCaseVersion} />
            ) : initialView === "calculation" ? (
              <WebCalculationWorkbench
                canRunCalculation={session.capabilities.canRunCalculation}
                caseId={selectedCase.caseId}
                caseVersion={selectedCase.version}
                onSessionExpired={onSessionExpired}
                onVersionAdvanced={advanceCaseVersion}
              />
            ) : initialView === "analysis" ? (
              <WebDecisionPackage caseId={selectedCase.caseId} caseNumber={selectedCase.title} onSessionExpired={onSessionExpired} />
            ) : initialView === "brief" ? (
              <WebDefenceBrief caseId={selectedCase.caseId} caseNumber={selectedCase.title}
                               onSessionExpired={onSessionExpired} />
            ) : initialView === "deliverables" ? (
              <WebDeliverableChecklist caseId={selectedCase.caseId}
                                       onSessionExpired={onSessionExpired} />
            ) : initialView === "bundle" ? (
              <WebSubmissionReview
                canApprove={session.actor.roles.some((role) => role === "LEAD_LAWYER" || role === "REVIEWER")}
                canLock={session.actor.roles.includes("LEAD_LAWYER")}
                caseId={selectedCase.caseId}
                caseVersion={selectedCase.version}
                onSessionExpired={onSessionExpired}
                onVersionAdvanced={advanceCaseVersion}
              />
            ) : (
              <WebLockedStage hasMaterials={hasCaseMaterials} localMode={isLocalWebMode} />
            )
          ) : (
            <CreateCasePanel
              busy={creatingCase}
              canCreateCase={session.capabilities.canCreateCase}
              notice={caseNotice}
              onSubmit={createCase}
              title={caseTitle}
              onTitleChange={setCaseTitle}
            />
          )}
        </section>
      </div>

      <footer className={styles.footer}>
        <span>原始材料始终保留，所有标记和整理结果都可以追溯到来源。</span>
        <span>只有经律师确认的内容才会进入应诉文件。</span>
      </footer>
    </main>
  );
}

function WebLawyerNavigation({ capabilities, caseId, currentView, hasMaterials }: { capabilities: WebLawyerSession["capabilities"]; caseId: string | null; currentView: WebLawyerInitialView; hasMaterials: boolean }) {
  const items: ReadonlyArray<{ id: WebLawyerInitialView; label: string; href: string }> = [
    { id: "overview", label: "案件首页", href: "/" },
    { id: "evidence", label: "材料与证据", href: "/evidence" },
    { id: "facts", label: "确认案情", href: "/facts" },
    { id: "legal", label: "依据与测算", href: "/legal" },
    { id: "calculation", label: "金额核对", href: "/calculation" },
    { id: "analysis", label: "决策包", href: "/analysis" },
    { id: "brief", label: "答辩状", href: "/brief" },
    { id: "deliverables", label: "交付清单", href: "/deliverables" },
    { id: "bundle", label: "成果文件", href: "/bundle" },
  ];
  return (
    <nav className={styles.webLawyerNavigation} aria-label="案件工作区导航">
      {items.map((item) => {
        const available = canOpenWebLawyerView(capabilities, item.id, Boolean(caseId), hasMaterials);
        if (!available) {
          return <span aria-disabled="true" className={styles.webLawyerNavigationUnavailable} key={item.id} title={caseId ? (isLocalWebMode ? "本机模式未提供该环节；请在律所服务器模式办理" : "完成前一步后可继续处理") : "请先选择案件"}>{item.label}</span>;
        }
        return <a aria-current={item.id === currentView ? "page" : undefined} className={item.id === currentView ? styles.webLawyerNavigationActive : undefined} href={caseId ? `${item.href}?case=${encodeURIComponent(caseId)}` : item.href} key={item.id}>{item.label}</a>;
      })}
    </nav>
  );
}

function WebCaseOverview({
  canCompleteCaseAgentRun,
  canReviewEvidence,
  canReviewCaseAgent,
  canReviewCaseAgentDocuments,
  canReviewCasePosture,
  canConfirmCasePosture,
  canRunCaseAgent,
  caseItem,
  hasMaterials,
  onSessionExpired,
  onVersionAdvanced,
  onPostureCurrentChanged,
  postureCurrent,
}: {
  canCompleteCaseAgentRun: boolean;
  canReviewEvidence: boolean;
  canReviewCaseAgent: boolean;
  canReviewCaseAgentDocuments: boolean;
  canReviewCasePosture: boolean;
  canConfirmCasePosture: boolean;
  canRunCaseAgent: boolean;
  caseItem: WebLawyerCase;
  hasMaterials: boolean;
  onSessionExpired: () => void;
  onVersionAdvanced: (version: number) => void;
  onPostureCurrentChanged: (current: boolean) => void;
  postureCurrent: boolean;
}) {
  const router = useRouter();
  return (
    <div className={styles.webCaseOverviewStack}>
      {isLocalWebMode ? (
        <section className={styles.webCapabilityBoundary} role="status">
          <div><strong>当前为离线模式</strong></div>
          <p>可接收和人工审阅材料，暂不能运行办案任务。</p>
        </section>
      ) : null}

      <WebCaseJourney caseId={caseItem.caseId} onSessionExpired={onSessionExpired} />

      <WebCasePosture
        canConfirm={canConfirmCasePosture}
        canReview={canReviewCasePosture}
        caseId={caseItem.caseId}
        caseVersion={caseItem.version}
        onCurrentChanged={onPostureCurrentChanged}
        onSessionExpired={onSessionExpired}
        onVersionAdvanced={onVersionAdvanced}
      />

      <section className={styles.webCaseInputCard} aria-labelledby="web-case-overview-title">
        <div>
          <h2 id="web-case-overview-title">案件材料</h2>
          <p>{caseItem.materialCount > 0
            ? "查看原件、补充材料，或核对整理结果。"
            : "添加起诉状、证据及其他案件材料。"}</p>
        </div>
        {canReviewEvidence
          ? <a className={styles.webLawyerSecondaryAction} href={`/evidence?case=${encodeURIComponent(caseItem.caseId)}`}>{caseItem.materialCount > 0 ? "查看或补充材料" : "添加案件材料"}</a>
          : <span className={styles.webLawyerNavigationUnavailable} aria-disabled="true">材料暂不能查看</span>}
      </section>

      {postureCurrent && hasMaterials ? <UnifiedCaseAgentPanel
        canCompleteCaseAgentRun={canCompleteCaseAgentRun}
        canReview={canReviewCaseAgent}
        canReviewCaseAgentDocuments={canReviewCaseAgentDocuments}
        canRun={canRunCaseAgent}
        caseId={caseItem.caseId}
        caseVersion={caseItem.version}
        onOpenEvidence={(pageIds) => {
          const focus = pageIds.slice(0, 100).join(",");
          router.push(
            `/evidence?case=${encodeURIComponent(caseItem.caseId)}${focus ? `&focus=${encodeURIComponent(focus)}` : ""}`,
          );
        }}
        onSessionExpired={onSessionExpired}
      /> : null}
    </div>
  );
}

/**
 * The home page is a case cockpit, not an implementation dashboard.  The
 * readiness endpoint already owns the legal gates; this component translates
 * them into one visible route and never exposes versions, hashes or services.
 */
function WebCaseJourney({ caseId, onSessionExpired }: { caseId: string; onSessionExpired: () => void }) {
  const [readiness, setReadiness] = useState<WebCaseReadiness | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void readWebCaseReadiness(caseId, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setReadiness(next);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted && isWebLoginRequired(reason)) onSessionExpired();
      });
    return () => controller.abort();
  }, [caseId, onSessionExpired]);

  if (!readiness) return null;
  const next = readiness.checks.find((item) => item.status === "BLOCKED") ?? null;
  const route = next ? journeyRoute(caseId, next.key) : `/calculation?case=${encodeURIComponent(caseId)}`;
  const action = next ? journeyAction(next.key) : "查看金额核对";
  return (
    <section className={styles.webCaseJourney} aria-labelledby="web-case-journey-title">
      <header>
        <div>
          <p className={styles.eyebrow}>提交前进度</p>
          <h2 id="web-case-journey-title">{next ? "提交前还需完成这一项" : "提交前条件已经齐备"}</h2>
          <p>{next ? next.detail : "可以进入金额核对，并继续审阅应诉材料。办案助手可在材料入卷后协助推进前置核对。"}</p>
        </div>
        <a className={styles.webLawyerPrimaryAction} href={route}>{action}</a>
      </header>
      <ol>
        {readiness.checks.map((item, index) => (
          <li className={item.status === "READY" ? styles.webCaseJourneyDone : item === next ? styles.webCaseJourneyCurrent : undefined} key={item.key}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <strong>{item.label}</strong>
            <small>{item.status === "READY" ? "已完成" : item === next ? "现在处理" : "后续处理"}</small>
          </li>
        ))}
      </ol>
    </section>
  );
}

function journeyRoute(caseId: string, key: string): string {
  const encoded = encodeURIComponent(caseId);
  if (key === "materials") return `/evidence?case=${encoded}`;
  if (key === "case_review") return `/facts?case=${encoded}#case-framing`;
  if (key === "payment_review") return `/facts?case=${encoded}#payment-classification`;
  if (key === "legal_sources" || key === "legal_events" || key === "rule_bundle") return `/legal?case=${encoded}`;
  return `/`;
}

function journeyAction(key: string): string {
  if (key === "materials") return "核对材料";
  if (key === "case_review") return "确认案情";
  if (key === "payment_review") return "核对收付款";
  if (key === "legal_sources") return "核对法律依据";
  if (key === "legal_events") return "确认关键日期";
  if (key === "rule_bundle") return "核对适用规则";
  return "继续办理";
}

function WebLockedStage({ hasMaterials, localMode }: { hasMaterials: boolean; localMode: boolean }) {
  if (localMode) {
    return (
      <section className={styles.webLawyerEmptyPanel} aria-labelledby="web-locked-stage-title">
        <p className={styles.eyebrow}>办案流程</p>
        <h2 id="web-locked-stage-title">本机模式未提供该环节</h2>
        <p>本机离线模式装配的是：案件首页、材料与证据、决策包（确定性核对 + 正式数字 + 可选模型分析）。确认案情、依据与测算、金额核对、成果文件需要律所服务器模式（受管案卷库、法律依据登记与文书服务），本机不会伪造这些环节的结果。</p>
        <small>本机数据不会静默转入律所受管案卷；需要正式成果文件时请改用服务器模式办理。</small>
      </section>
    );
  }
  return (
    <section className={styles.webLawyerEmptyPanel} aria-labelledby="web-locked-stage-title">
      <p className={styles.eyebrow}>办案流程</p>
      <h2 id="web-locked-stage-title">先完成前一步</h2>
      <p>{hasMaterials ? "材料已经保留在案卷中。完成案件要点和相关确认后，即可继续处理本环节。" : "先添加并核对案件材料，再继续处理本环节。"}</p>
      <small>系统不会在信息不完整时把未核对内容当作结论或正式文件。</small>
    </section>
  );
}

function CaseListUnavailable({ onRetry }: { onRetry: () => void }) {
  return (
    <section className={styles.webLawyerEmptyPanel} role="alert">
      <p className={styles.eyebrow}>案件服务</p>
      <h2>暂不能选择或新建案件</h2>
      <p>为了避免把网页临时内容当作真实案卷，案件列表不可用时系统不会显示任何替代案件。</p>
      <button className={styles.webLawyerPrimaryAction} onClick={onRetry} type="button">重新读取案件</button>
    </section>
  );
}

function CreateCasePanel({
  busy,
  canCreateCase,
  notice,
  onSubmit,
  onTitleChange,
  title,
}: {
  busy: boolean;
  canCreateCase: boolean;
  notice: string | null;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onTitleChange: (value: string) => void;
  title: string;
}) {
  return (
    <section className={styles.webLawyerCreatePanel} aria-labelledby="web-create-case-title">
      <div>
        <p className={styles.eyebrow}>第一步</p>
        <h2 id="web-create-case-title">建立案件工作区</h2>
        <p>填写一个便于识别的案件名称。建立后，材料、审阅和成果文件都会归入同一案件。</p>
      </div>
      <form onSubmit={onSubmit}>
        <label>
          <span>案件名称</span>
          <input
            autoComplete="off"
            disabled={busy || !canCreateCase}
            maxLength={160}
            minLength={2}
            onChange={(event) => onTitleChange(event.target.value)}
            placeholder="例如：周雅丽与寒雪青松民间借贷纠纷"
            required
            value={title}
          />
        </label>
        <div className={styles.webLawyerCreateActions}>
          <button className={styles.webLawyerPrimaryAction} disabled={busy || !canCreateCase} type="submit">{busy ? "正在建立…" : "建立案件"}</button>
          <small>{canCreateCase ? "建立案件不会读取或上传电脑中的任何文件。" : "当前账号没有建立案件的权限。"}</small>
        </div>
      </form>
      {notice ? <p className={styles.webLawyerNotice} role="status">{notice}</p> : null}
    </section>
  );
}

function MaterialIntake({
  canUpload,
  canUploadCommon,
  caseItem,
  onCaseVersionAdvanced,
  onMaterialsReceived,
  onSessionExpired,
}: {
  canUpload: boolean;
  canUploadCommon: boolean;
  caseItem: WebLawyerCase;
  onCaseVersionAdvanced: (caseId: string, version: number) => void;
  onMaterialsReceived: () => void;
  onSessionExpired: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const commonInputRef = useRef<HTMLInputElement>(null);
  const archiveInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [focusedEvidencePageIds, setFocusedEvidencePageIds] = useState<readonly string[] | null>(() => {
    if (typeof window === "undefined") return null;
    const raw = new URLSearchParams(window.location.search).get("focus");
    if (!raw) return null;
    const pageIds = raw.split(",").filter((value) => /^[0-9a-f-]{36}$/i.test(value)).slice(0, 100);
    return pageIds.length ? [...new Set(pageIds)] : null;
  });

  const pendingCount = items.filter((item) => item.state === "READY").length;
  const receivedCount = items.filter((item) => item.state === "RECEIVED").length;
  const pdfReceivedCount = items.filter((item) => item.kind === "PDF" && item.state === "RECEIVED").length;
  const visualEvidenceReceivedCount = items.filter((item) => item.kind === "COMMON" && item.state === "RECEIVED" && isVisualEvidenceReceipt(item.receipt)).length;
  const archiveStoredCount = items.filter((item) => item.state === "ARCHIVE_STORED").length;
  // DOCX/XLSX create material-object Agent sources, not page-level evidence.
  // Only PDFs, image evidence-page sources, and previously registered evidence
  // files make the page-review surface available.
  const showEvidenceReview = pdfReceivedCount > 0 || visualEvidenceReceivedCount > 0 || caseItem.materialCount > 0;

  function enqueueFiles(files: FileList | File[]) {
    const selected = Array.from(files);
    const withheldCommonCount = selected.filter((file) => isCommonMaterialCandidate(file) && !canUploadCommon).length;
    const nextItems = selected.flatMap((file): UploadItem[] => {
      const image = isImageMaterialCandidate(file);
      const pdf = isPdfCandidate(file) || image;
      const zip = isZipCandidate(file);
      const common = !pdf && isCommonMaterialCandidate(file);
      if (common && !canUploadCommon) return [];
      const kind: UploadItemKind = pdf ? "PDF" : zip ? "ZIP" : "COMMON";
      const maximum = kind === "ZIP" ? WEB_MAX_ARCHIVE_BYTES
        : kind === "COMMON" ? WEB_MAX_COMMON_MATERIAL_BYTES
        : image ? WEB_MAX_IMAGE_BYTES : WEB_MAX_PDF_BYTES;
      const valid = (pdf || zip || common) && file.size > 0 && file.size <= maximum;
      const legacy = isLegacyCommonMaterialCandidate(file);
      return [{
        id: makeQueueId(),
        file: valid ? file : null,
        kind,
        image,
        serverId: null,
        name: file.name || "未命名文件",
        byteSize: file.size,
        state: valid ? "READY" : "REJECTED",
        message: valid
          ? null
            : file.size <= 0
              ? "文件为空，未上传。"
              : file.size > maximum
                ? `文件超过 ${kind === "COMMON" ? "100 MiB" : image ? "64 MiB" : "256 MiB"} 的受管接收上限，未上传。`
                : legacy
                  ? "当前不接收旧版 DOC / XLS / PPT、MSG 或 OFD；请先转换为受支持格式后重新选择。"
                  : "当前支持 PDF、ZIP、DOCX、XLSX、PPTX、RTF、TXT、CSV、HTML、EML、JPEG 和 PNG；该文件未上传。",
        receipt: null,
      }];
    });
    if (nextItems.length > 0) setItems((current) => [...current, ...nextItems]);
    if (withheldCommonCount > 0) {
      setNotice(nextItems.length > 0
        ? "PDF / ZIP 已加入待接收清单；常见材料入口仅在完整律所服务开放，未加入上传队列。"
        : "常见材料入口仅在完整律所服务开放，未加入上传队列。"
      );
    } else if (nextItems.length > 0) {
      setNotice("已加入待接收清单。请核对后点击“开始接收”。");
    }
  }

  function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    if (event.target.files) enqueueFiles(event.target.files);
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragActive(false);
    if (!canUpload) return;
    if (event.dataTransfer.files.length > 0) enqueueFiles(event.dataTransfer.files);
  }

  async function startUpload() {
    const queuedItems = items.filter((item) => item.state === "READY" && item.file !== null);
    if (queuedItems.length === 0 || uploading || !canUpload) return;
    setUploading(true);
    setNotice(null);
    let receivedInThisRun = 0;
    let interruptedBySession = false;
    let expectedVersion = caseItem.version;

    for (const queuedItem of queuedItems) {
      const file = queuedItem.file;
      if (!file) continue;
      let slotGranted = false;
      setItems((current) => updateUploadItem(current, queuedItem.id, {
        state: "CREATING_SLOT",
        message: "正在准备接收…",
      }));
      try {
        if (queuedItem.kind === "ZIP") {
          const slot = await createWebMaterialArchiveSlot(caseItem.caseId, expectedVersion, file);
          slotGranted = true;
          setItems((current) => updateUploadItem(current, queuedItem.id, { serverId: slot.archiveId }));
          setItems((current) => updateUploadItem(current, queuedItem.id, {
            state: "UPLOADING",
            message: "正在接收材料包并进行安全检查…",
          }));
          const receipt = await uploadWebMaterialArchive(caseItem.caseId, slot.archiveId, file);
          setItems((current) => updateUploadItem(current, queuedItem.id, {
            file: null,
            state: "ARCHIVE_STORED",
            message: "材料包已接收；其中的文件仍需逐份入卷后，才能进入材料核对。请勿重复上传。",
            receipt,
          }));
        } else if (queuedItem.kind === "PDF") {
          const slot = await createWebMaterialUploadSlot(caseItem.caseId, expectedVersion, file);
          slotGranted = true;
          setItems((current) => updateUploadItem(current, queuedItem.id, { serverId: slot.uploadId }));
          setItems((current) => updateUploadItem(current, queuedItem.id, {
            state: "UPLOADING",
            message: "正在接收材料并进行安全检查…",
          }));
          const receipt = await uploadWebMaterialPdf(caseItem.caseId, slot.uploadId, file);
          receivedInThisRun += 1;
          onMaterialsReceived();
          expectedVersion = receipt.matterVersion;
          onCaseVersionAdvanced(caseItem.caseId, receipt.matterVersion);
          setItems((current) => updateUploadItem(current, queuedItem.id, {
            file: null,
            state: "RECEIVED",
            message: "材料已接收并归入本案。",
            receipt,
          }));
        } else if (canUploadCommon) {
          const slot = await createWebCommonMaterialUploadSlot(caseItem.caseId, expectedVersion, file);
          slotGranted = true;
          setItems((current) => updateUploadItem(current, queuedItem.id, { serverId: slot.uploadId }));
          setItems((current) => updateUploadItem(current, queuedItem.id, {
            state: "UPLOADING",
            message: "正在接收材料并进行安全检查…",
          }));
          const receipt = await uploadWebCommonMaterial(caseItem.caseId, slot.uploadId, file);
          receivedInThisRun += 1;
          if (isVisualEvidenceReceipt(receipt)) onMaterialsReceived();
          expectedVersion = receipt.matterVersion;
          onCaseVersionAdvanced(caseItem.caseId, receipt.matterVersion);
          setItems((current) => updateUploadItem(current, queuedItem.id, {
            file: null,
            state: "RECEIVED",
            message: commonMaterialReceiptMessage(receipt),
            receipt,
          }));
        } else {
          setItems((current) => updateUploadItem(current, queuedItem.id, {
            state: "REJECTED",
            message: "常见材料入口仅在完整律所服务开放，未开始上传。",
          }));
        }
      } catch (reason: unknown) {
        if (isWebLoginRequired(reason)) {
          setItems((current) => updateUploadItem(current, queuedItem.id, {
            file: null,
            state: "UNCONFIRMED",
            message: slotGranted
              ? "登录状态已失效，当前材料接收结果未确认。请勿直接重传，先由服务端核验。"
              : "登录状态已失效，未开始正文传输。请重新登录后核验接收记录。",
          }));
          interruptedBySession = true;
          onSessionExpired();
          break;
        }
        const issue = operationalIssue(reason, "材料接收结果未确认。请勿直接重传，先由服务端核验。");
        const rejected = isWebMaterialRejected(reason);
        setItems((current) => updateUploadItem(current, queuedItem.id, {
          file: null,
          state: rejected ? "REJECTED" : "UNCONFIRMED",
          message: rejected
              ? `${slotGranted ? `服务器未接收该 ${queuedItem.kind}。` : "服务器未创建材料接收位。"} ${issue.message}`
            : `${slotGranted ? `${queuedItem.kind} 已开始传输，但` : "接收位创建过程中"}结果未确认。请勿直接重传，先由服务端核验。${issue.requestId ? ` 请求编号：${issue.requestId}` : ""}`,
        }));
      }
    }

    setUploading(false);
    if (interruptedBySession) return;
    if (receivedInThisRun > 0) {
      setNotice(`本次已接收 ${receivedInThisRun} 份材料${archiveStoredCount > 0 ? "；材料包已保存，仍需逐份入卷" : ""}。材料入卷后仍需要核对，才会用于分析或文件。`);
    }
  }

  async function checkStatus(item: UploadItem) {
    if (!item.serverId || uploading) return;
    try {
      const status = item.kind === "ZIP"
        ? await readWebMaterialArchiveStatus(caseItem.caseId, item.serverId)
        : item.kind === "COMMON"
          ? await readWebCommonMaterialUploadStatus(caseItem.caseId, item.serverId)
          : await readWebMaterialUploadStatus(caseItem.caseId, item.serverId);
      applyStatus(item.id, item.kind, status);
    } catch (reason: unknown) {
      if (isWebLoginRequired(reason)) {
        onSessionExpired();
        return;
      }
      const issue = operationalIssue(reason, "暂不能核验该接收编号，请稍后重试。");
      setItems((current) => updateUploadItem(current, item.id, { message: `${issue.message}${issue.requestId ? ` 请求编号：${issue.requestId}` : ""}` }));
    }
  }

  function applyStatus(itemId: string, kind: UploadItemKind, status: WebMaterialUploadStatus | WebCommonMaterialUploadStatus) {
    if (status.kind !== kind) return;
    if (kind === "PDF" && status.receipt && "evidenceFileId" in status.receipt) {
      const receipt = status.receipt;
      onMaterialsReceived();
      onCaseVersionAdvanced(caseItem.caseId, receipt.matterVersion);
      const image = items.find((candidate) => candidate.id === itemId)?.image ?? false;
      setItems((current) => updateUploadItem(current, itemId, { file: null, state: "RECEIVED", message: image ? "图片已接收并归入本案。" : "PDF 已接收并归入本案。", receipt }));
      return;
    }
    if (kind === "ZIP" && status.receipt && "processingStatus" in status.receipt) {
      setItems((current) => updateUploadItem(current, itemId, { file: null, state: "ARCHIVE_STORED", message: "材料包已接收；需要逐份入卷后才能开始核对，请勿重复上传。", receipt: status.receipt }));
      return;
    }
    if (kind === "COMMON" && status.receipt && "agentStatus" in status.receipt) {
      const receipt = status.receipt;
      if (isVisualEvidenceReceipt(receipt)) onMaterialsReceived();
      onCaseVersionAdvanced(caseItem.caseId, receipt.matterVersion);
      setItems((current) => updateUploadItem(current, itemId, {
        file: null,
        state: "RECEIVED",
        message: commonMaterialReceiptMessage(receipt),
        receipt,
      }));
      return;
    }
    const message = status.state === "RECONCILIATION_REQUIRED"
      ? "材料仍在核对中，请勿重复上传。"
      : status.state === "PROCESSING"
        ? "材料正在处理中，请稍后再次查看。"
      : status.state === "EXPIRED"
          ? "本次接收已过期，材料尚未入卷；如需继续，请重新选择原文件。"
          : "材料尚未接收；如需继续，请重新选择原文件。";
    setItems((current) => updateUploadItem(current, itemId, { file: null, state: status.state === "PROCESSING" || status.state === "RECONCILIATION_REQUIRED" ? "UNCONFIRMED" : "REJECTED", message }));
  }

  return (
    <section className={styles.webLawyerIntake} aria-labelledby="web-material-intake-title">
      <header className={styles.webLawyerIntakeHeading}>
        <div>
          <p className={styles.eyebrow}>案件材料</p>
          <h2 id="web-material-intake-title">添加案件材料</h2>
          <p>把材料加入本案后，系统会保留原件并整理可审阅内容。需要你判断的页面、事实或风险会单独列出；未经审阅的内容不会成为正式结论。</p>
        </div>
        <dl>
          <div><dt>当前案件</dt><dd>{caseItem.title}</dd></div>
          <div><dt>已入卷</dt><dd>{caseItem.materialCount} 份</dd></div>
          <div><dt>本次已接收</dt><dd>{receivedCount} 份</dd></div>
          <div><dt>待接收</dt><dd>{pendingCount} 份</dd></div>
        </dl>
      </header>

      <div
        aria-label="选择或拖入案件材料"
        className={`${styles.webLawyerDropzone} ${dragActive ? styles.webLawyerDropzoneActive : ""}`}
        onDragEnter={(event) => {
          event.preventDefault();
          if (canUpload) setDragActive(true);
        }}
        onDragLeave={(event) => {
          event.preventDefault();
          setDragActive(false);
        }}
        onDragOver={(event) => {
          if (canUpload) event.preventDefault();
        }}
        onDrop={handleDrop}
      >
        <input accept="application/pdf,.pdf,.jpg,.jpeg,.png" aria-label="选择 PDF 或图片文件" disabled={!canUpload} hidden multiple onChange={handleFileChange} ref={inputRef} type="file" />
        <input accept=".docx,.xlsx,.pptx,.rtf,.txt,.csv,.html,.htm,.eml,.jpg,.jpeg,.png" aria-label="选择常见案件材料" disabled={!canUploadCommon} hidden multiple onChange={handleFileChange} ref={commonInputRef} type="file" />
        <input accept="application/zip,.zip" aria-label="选择 ZIP 材料包" disabled={!canUpload} hidden multiple onChange={handleFileChange} ref={archiveInputRef} type="file" />
        <input
          {...({ webkitdirectory: "", directory: "" } as DirectoryInputAttributes)}
          aria-label="选择材料文件夹"
          disabled={!canUpload}
          hidden
          multiple
          onChange={handleFileChange}
          ref={folderInputRef}
          type="file"
        />
        <strong>选择案件材料，或拖到这里</strong>
        <span>{canUpload
          ? canUploadCommon
            ? "支持常见办案文件和材料文件夹。选择后先在清单中核对，再点击“开始接收”；不支持的格式会明确提示。"
            : "当前可添加 PDF、ZIP 和已开放的材料类型。"
          : "当前账号不能添加案件材料。"}</span>
        <div className={styles.webLawyerUploadActions}>
          <button className={styles.webLawyerSecondaryAction} disabled={!canUpload} onClick={() => inputRef.current?.click()} type="button">选择 PDF</button>
          <button className={styles.webLawyerSecondaryAction} disabled={!canUploadCommon} onClick={() => commonInputRef.current?.click()} title={canUploadCommon ? undefined : "仅在完整律所服务开放"} type="button">选择常见材料</button>
          <button className={styles.webLawyerSecondaryAction} disabled={!canUpload} onClick={() => archiveInputRef.current?.click()} type="button">选择 ZIP 材料包</button>
          <button className={styles.webLawyerSecondaryAction} disabled={!canUpload} onClick={() => folderInputRef.current?.click()} type="button">选择材料文件夹</button>
        </div>
      </div>

      {notice ? <p className={styles.webLawyerNotice} role="status">{notice}</p> : null}

      <section className={styles.webLawyerUploadQueue} aria-label="材料接收清单">
        <header>
          <div><strong>材料清单</strong><span>确认接收后，材料才会保留在本案中。</span></div>
          <button
            className={styles.webLawyerPrimaryAction}
            disabled={pendingCount === 0 || uploading || !canUpload}
            onClick={() => void startUpload()}
            type="button"
          >
            {uploading ? "正在接收…" : pendingCount > 0 ? `开始接收 ${pendingCount} 份材料` : "等待选择材料"}
          </button>
        </header>
        {items.length === 0 ? (
          <p className={styles.webLawyerUploadEmpty}>尚未选择文件。选择文件本身不会上传，需由律师点击“开始接收”。</p>
        ) : (
          <div className={styles.webLawyerUploadRows}>
            {items.map((item) => (
              <article key={item.id}>
                <div className={styles.webLawyerUploadTitle}>
                  <span className={uploadStateClass(item.state)}>{uploadStateLabel(item.state)}</span>
                  <div><strong>{item.name}</strong><small>{item.image ? "图片" : item.kind} · {formatBytes(item.byteSize)}</small></div>
                </div>
                <div className={styles.webLawyerUploadDetail}>
                  {item.message ? <p>{item.message}</p> : <p>等待律师确认接收。</p>}
                  {item.receipt ? <ReceiptDetails receipt={item.receipt} /> : null}
                </div>
                {canRemoveUploadItem(item) ? (
                  <button className={styles.webLawyerRemoveAction} disabled={uploading} onClick={() => setItems((current) => current.filter((candidate) => candidate.id !== item.id))} type="button">移出清单</button>
                ) : null}
                {item.serverId && (item.state === "UNCONFIRMED" || item.state === "UPLOADING") ? (
                  <button className={styles.webLawyerSecondaryAction} disabled={uploading} onClick={() => void checkStatus(item)} type="button">核验接收状态</button>
                ) : null}
              </article>
            ))}
          </div>
        )}
      </section>

      {showEvidenceReview ? <section className={styles.webCaseInputCard} aria-label="下一步办案工作">
        <div><h2>材料已入卷</h2><p>接下来回到案件概览，交代本案要完成的工作。系统会先读卷，再把风险、待补材料和需要你决定的事项集中呈现。</p></div>
        <a className={styles.webLawyerPrimaryAction} href={`/?case=${encodeURIComponent(caseItem.caseId)}`}>回案件概览开始办案</a>
      </section> : null}
      {showEvidenceReview ? (
        <WebEvidenceReview
          caseId={caseItem.caseId}
          focusPageIds={focusedEvidencePageIds}
          onClearFocus={() => setFocusedEvidencePageIds(null)}
          onSessionExpired={onSessionExpired}
          onVersionAdvanced={onCaseVersionAdvanced}
        />
      ) : null}
    </section>
  );
}

function ReceiptDetails({ receipt }: { receipt: WebMaterialReceipt | WebMaterialArchiveReceipt | WebCommonMaterialAdmissionReceipt }) {
  if ("processingStatus" in receipt) {
    return (
      <dl className={styles.webLawyerReceipt}>
        <div><dt>材料名称</dt><dd>{receipt.displayName}</dd></div>
        <div><dt>包内 PDF</dt><dd>{receipt.entryCount} 份</dd></div>
        <div><dt>材料大小</dt><dd>{formatBytes(receipt.expandedByteSize)}</dd></div>
        <div><dt>当前状态</dt><dd>已安全接收，等待整理</dd></div>
      </dl>
    );
  }
  if ("agentStatus" in receipt) {
    const visualEvidence = isVisualEvidenceReceipt(receipt);
    return (
      <dl className={styles.webLawyerReceipt}>
        <div><dt>材料名称</dt><dd>{receipt.displayName}</dd></div>
        <div><dt>格式</dt><dd>{receipt.admittedFormat}</dd></div>
        <div><dt>大小</dt><dd>{formatBytes(receipt.byteSize)}</dd></div>
        <div><dt>材料状态</dt><dd>已入卷，等待整理与审阅</dd></div>
        <div><dt>审阅状态</dt><dd>尚未形成正式事实或结论</dd></div>
        {visualEvidence ? <div><dt>页面审阅</dt><dd>可以进入页面审阅</dd></div> : null}
      </dl>
    );
  }
  return (
    <dl className={styles.webLawyerReceipt}>
      <div><dt>材料名称</dt><dd>{receipt.displayName}</dd></div>
      <div><dt>页数</dt><dd>{receipt.pageCount}</dd></div>
      <div><dt>材料状态</dt><dd>{receipt.scanStatus === "CLEAN" ? "已安全接收" : "正在核对"}</dd></div>
      {receipt.receivedAt ? <div><dt>接收时间</dt><dd>{formatTime(receipt.receivedAt)}</dd></div> : null}
    </dl>
  );
}

function isVisualEvidenceReceipt(receipt: UploadItem["receipt"] | WebCommonMaterialAdmissionReceipt): boolean {
  return receipt !== null
    && "agentStatus" in receipt
    && receipt.agentStatus === "AGENT_READY"
    && (receipt.admittedFormat === "JPEG" || receipt.admittedFormat === "PNG")
    && typeof receipt.agentSourceRef === "string"
    && receipt.agentSourceRef.startsWith("evidence-page:");
}

function commonMaterialReceiptMessage(receipt: WebCommonMaterialAdmissionReceipt): string {
  if (receipt.agentStatus === "INGESTED_PENDING_ADAPTER") {
    return "材料已安全入卷，等待后续整理；尚未形成事实或结论。";
  }
  if (isVisualEvidenceReceipt(receipt)) {
    return "材料已入卷，可以进入页面审阅；尚未形成事实或证据结论。";
  }
  return "材料已入卷，等待整理与审阅；尚未形成正式事实、交易、法律结论或可提交材料。";
}

function updateUploadItem(items: UploadItem[], id: string, update: Partial<UploadItem>): UploadItem[] {
  return items.map((item) => item.id === id ? { ...item, ...update } : item);
}

function uploadStateLabel(state: UploadItemState): string {
  switch (state) {
    case "READY": return "待接收";
    case "CREATING_SLOT": return "确认中";
    case "UPLOADING": return "接收中";
    case "RECEIVED": return "已出具回执";
    case "ARCHIVE_STORED": return "ZIP 已安全接收";
    case "REJECTED": return "未被接收";
    case "UNCONFIRMED": return "结果待核验";
  }
}

function uploadStateClass(state: UploadItemState): string {
  switch (state) {
    case "RECEIVED": return styles.webLawyerUploadReceived;
    case "ARCHIVE_STORED": return styles.webLawyerUploadReceived;
    case "REJECTED": return styles.webLawyerUploadRejected;
    case "UNCONFIRMED": return styles.webLawyerUploadUnconfirmed;
    case "CREATING_SLOT":
    case "UPLOADING": return styles.webLawyerUploadProgress;
    case "READY": return styles.webLawyerUploadReady;
  }
}

function canRemoveUploadItem(item: UploadItem): boolean {
  return item.state === "READY" || item.state === "REJECTED";
}

function caseMeta(item: WebLawyerCase): string {
  const time = item.updatedAt ? `更新于 ${formatTime(item.updatedAt)}` : "未提供更新时间";
  return `${time} · ${item.materialCount} 份材料`;
}

function roleSummary(roles: readonly string[]): string {
  const labels = roles.map((role) => {
    if (role === "LEAD_LAWYER") return "主办律师";
    if (role === "COLLABORATING_LAWYER") return "协办律师";
    if (role === "PARALEGAL") return "法务助理";
    return "受管成员";
  });
  return [...new Set(labels)].join(" / ");
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "大小未提供";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function makeQueueId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function isAbortError(reason: unknown): boolean {
  return reason instanceof DOMException && reason.name === "AbortError";
}

function operationalIssue(reason: unknown, fallback: string): { message: string; requestId: string | null } {
  if (reason instanceof WebLawyerApiError) {
    return { message: reason.message, requestId: reason.requestId };
  }
  return { message: fallback, requestId: null };
}

function localMessage(reason: unknown, fallback: string): string {
  if (reason instanceof Error && reason.message.length <= 180) return reason.message;
  return fallback;
}
