"use client";

import { useEffect, useState, type FormEvent } from "react";
import { CalculationWorkbench } from "@/components/calculation-workbench";
import { EvidenceWorkbench as EvidenceManifestWorkbench } from "@/components/evidence-workbench";
import { FactsWorkbench } from "@/components/facts-workbench";
import { LegalWorkbench } from "@/components/legal-workbench";
import { IdentitySecurityWorkbench } from "@/components/identity-security-workbench";
import { SubmissionWorkbench } from "@/components/submission-workbench";
import { CaseAssistantPlan } from "@/components/case-assistant-plan";
import {
  LocalStandaloneFeatureUnavailable,
  LocalStandaloneOnboarding,
  LocalStandaloneCaseHome,
} from "@/components/local-standalone-onboarding";
import {
  activatePersistentMatter,
  caseDataSourceConfig,
  clearActivePersistentMatter,
  createPersistentMatter,
  enableReadyDesktopPersistentWorkspace,
  getPersistentWorkspaceTarget,
  isDesktopCaseWorkspaceShell,
  isReadyDesktopPersistentWorkspace,
  loadCaseReview,
  loadPersistentMatterList,
  restoreActivePersistentMatter,
  type CaseDataSourceConfig,
  type CaseReviewView,
  type PersistentMatterListItem,
} from "@/lib/case-data-source";
import { readDesktopRuntimeStatus } from "@/lib/desktop-bridge";
import type { DesktopRuntimeStatus, LocalCaseSummary } from "@/lib/desktop-bridge";
import {
  activateLocalStandaloneCase,
  clearActiveLocalStandaloneCase,
  isReadyLocalStandaloneWorkspace,
  restoreActiveLocalStandaloneCase,
} from "@/lib/local-standalone-case-source";
import { syntheticMatter } from "@/lib/synthetic-matter";
import { WebLawyerWorkbench } from "@/components/web-lawyer-workbench";
import styles from "./case-workbench.module.css";

type View = "overview" | "evidence" | "facts" | "legal" | "calculation" | "bundle" | "security";
type DesktopWorkspaceState = "not-applicable" | "checking" | "ready" | "blocked";
type LocalStandaloneState = "not-applicable" | "checking" | "ready";

const navItems: ReadonlyArray<{ id: View | "facts" | "bundle"; label: string; href?: string }> = [
  { id: "overview", label: "办案首页", href: "/" },
  { id: "evidence", label: "收集材料", href: "/evidence" },
  { id: "facts", label: "核对案情", href: "/facts" },
  { id: "legal", label: "法律依据", href: "/legal" },
  { id: "calculation", label: "还款与利息", href: "/calculation" },
  { id: "bundle", label: "应诉材料", href: "/bundle" },
  { id: "security", label: "工作台设置", href: "/security" },
];

const lawyerProgress = ["收集材料", "核对案情", "确定法律与利息口径", "形成应诉材料"] as const;

export function CaseWorkbench({ initialView = "overview" }: { initialView?: View }) {
  const [view, setView] = useState<View>(initialView);
  const [sourceConfig, setSourceConfig] = useState<CaseDataSourceConfig>(caseDataSourceConfig);
  const [workspaceRestored, setWorkspaceRestored] = useState(() => getPersistentWorkspaceTarget() === null);
  const [desktopRuntime, setDesktopRuntime] = useState<DesktopRuntimeStatus | null>(null);
  const [desktopRuntimeRefreshKey, setDesktopRuntimeRefreshKey] = useState(0);
  const [desktopWorkspaceState, setDesktopWorkspaceState] = useState<DesktopWorkspaceState>(() => (
    isDesktopCaseWorkspaceShell() ? "checking" : "not-applicable"
  ));
  const [desktopWorkspaceMessage, setDesktopWorkspaceMessage] = useState<string | null>(null);
  const [localStandaloneState, setLocalStandaloneState] = useState<LocalStandaloneState>(() => (
    isDesktopCaseWorkspaceShell() ? "checking" : "not-applicable"
  ));
  const [localStandaloneCase, setLocalStandaloneCase] = useState<LocalCaseSummary | null>(null);
  const [localStandaloneRestoreMessage, setLocalStandaloneRestoreMessage] = useState<string | null>(null);
  const unresolvedCount = syntheticMatter.evidence.filter((item) => item.confidence !== "已核验").length;
  const currentStageIndex = view === "bundle" ? 3 : view === "legal" || view === "calculation" ? 2 : view === "facts" ? 1 : 0;
  const syntheticSource = sourceConfig.kind === "synthetic-alpha";
  const displaySyntheticSource = syntheticSource && desktopWorkspaceState === "not-applicable";
  const localStandaloneReady = isReadyLocalStandaloneWorkspace(desktopRuntime);
  const localStandaloneRestoring = localStandaloneReady && localStandaloneState === "checking";
  const localStandaloneAwaitingCase = localStandaloneReady && localStandaloneState === "ready" && localStandaloneCase === null;
  const localStandaloneCaseOpen = localStandaloneReady && localStandaloneCase !== null;
  const workspaceAwaitingCase = localStandaloneAwaitingCase || (sourceConfig.kind === "persistent-disabled" && getPersistentWorkspaceTarget() !== null);
  const restoringPersistentWorkspace = getPersistentWorkspaceTarget() !== null && !workspaceRestored;
  const desktopRuntimeNeedsSetup = isDesktopCaseWorkspaceShell()
    && desktopRuntime !== null
    && desktopRuntime.phase !== "STARTING"
    && !localStandaloneReady
    && !isReadyDesktopPersistentWorkspace(desktopRuntime);
  const desktopWorkspaceChecking = isDesktopCaseWorkspaceShell()
    && (desktopRuntime === null
      || desktopRuntime.phase === "STARTING"
      || (desktopWorkspaceState === "checking" && isReadyDesktopPersistentWorkspace(desktopRuntime)));
  const desktopWorkspaceBlocked = desktopWorkspaceState === "blocked" || desktopRuntimeNeedsSetup;
  const resolvedDesktopWorkspaceMessage = desktopWorkspaceState === "blocked"
    ? desktopWorkspaceMessage
    : desktopRuntimeNeedsSetup
      ? desktopRuntime?.message ?? "本机案件工作区尚未完成身份、会话和资料库核验。"
      : desktopWorkspaceMessage;
  const localFeatureLabel = navItems.find((item) => item.id === view)?.label ?? "该功能";
  // The browser product never falls back to a synthetic case or the legacy
  // desktop workspace. Browser users enter the same-origin, server-backed
  // lawyer flow below; Tauri keeps its existing isolated behavior.
  const webDeploymentRequired = !isDesktopCaseWorkspaceShell();

  useEffect(() => {
    if (!isDesktopCaseWorkspaceShell()) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let unavailableBridgeAttempts = 0;
    const refresh = async () => {
      try {
        const status = await readDesktopRuntimeStatus();
        if (cancelled) return;
        if (status === null) {
          unavailableBridgeAttempts += 1;
          if (unavailableBridgeAttempts < 4) {
            timer = setTimeout(refresh, 180);
            return;
          }
          setDesktopRuntime({
            phase: "BLOCKED",
            message: "未能连接桌面工作台接口；请重新启动应用后再试。",
            apiBase: null,
            processId: null,
            identityPhase: "UNAVAILABLE",
            enrollmentTrustPhase: "UNAVAILABLE",
            sessionPhase: "UNAVAILABLE",
            sessionExpiresAt: null,
            persistencePhase: "UNAVAILABLE",
            evidenceIntakeWorkerPhase: "UNAVAILABLE",
            officialSourceCaptureWorkerPhase: "UNAVAILABLE",
          });
          return;
        }
        setDesktopRuntime(status);
        if (status.phase === "STARTING") timer = setTimeout(refresh, 350);
      } catch {
        if (!cancelled) {
          setDesktopRuntime({
            phase: "BLOCKED",
            message: "无法核验本机受控服务，案件访问保持禁用。",
            apiBase: null,
              processId: null,
              identityPhase: "UNAVAILABLE",
              enrollmentTrustPhase: "UNAVAILABLE",
              sessionPhase: "UNAVAILABLE",
              sessionExpiresAt: null,
              persistencePhase: "UNAVAILABLE",
              evidenceIntakeWorkerPhase: "UNAVAILABLE",
              officialSourceCaptureWorkerPhase: "UNAVAILABLE",
          });
        }
      }
    };
    void refresh();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [desktopRuntimeRefreshKey]);

  useEffect(() => {
    if (getPersistentWorkspaceTarget() === null) return;
    const timer = window.setTimeout(() => {
      const restored = restoreActivePersistentMatter();
      setSourceConfig((current) => current === restored ? current : restored);
      setWorkspaceRestored(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!isDesktopCaseWorkspaceShell()) return;
    let active = true;
    void Promise.resolve().then(async () => {
      if (!active) return;
      if (!localStandaloneReady) {
        if (desktopRuntime !== null && desktopRuntime.workspaceMode !== "LOCAL_STANDALONE") {
          setLocalStandaloneState("not-applicable");
          setLocalStandaloneCase(null);
          setLocalStandaloneRestoreMessage(null);
        }
        return;
      }
      setLocalStandaloneState("checking");
      try {
        const { caseSummary, message } = await restoreActiveLocalStandaloneCase();
        if (!active) return;
        setLocalStandaloneCase(caseSummary);
        setLocalStandaloneRestoreMessage(message);
        setLocalStandaloneState("ready");
      } catch {
        if (!active) return;
        setLocalStandaloneCase(null);
        setLocalStandaloneRestoreMessage("无法读取上次打开的本机案件；你可以从案件目录重新打开。 ");
        setLocalStandaloneState("ready");
      }
    });
    return () => { active = false; };
  }, [desktopRuntime, localStandaloneReady]);

  useEffect(() => {
    if (!isDesktopCaseWorkspaceShell()) return;
    if (desktopRuntime === null || desktopRuntime.phase === "STARTING" || !isReadyDesktopPersistentWorkspace(desktopRuntime)) return;

    let active = true;
    void enableReadyDesktopPersistentWorkspace(desktopRuntime)
      .then((nextSourceConfig) => {
        if (!active) return;
        setSourceConfig(nextSourceConfig);
        setWorkspaceRestored(true);
        setDesktopWorkspaceState("ready");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setDesktopWorkspaceState("blocked");
        setDesktopWorkspaceMessage(reason instanceof Error ? reason.message : "无法取得本机案件会话；没有打开或创建案件。");
      });
    return () => {
      active = false;
    };
  }, [desktopRuntime]);

  if (webDeploymentRequired) {
    return <WebLawyerWorkbench initialView={view} />;
  }

  return (
    <main className={styles.shell}>
      <header className={styles.topbar}>
        <div className={styles.brand} aria-label="律师办案工作台">
          <span className={styles.brandMark}>案</span>
          <span>律师办案工作台</span>
          <small>{displaySyntheticSource ? "演示案件" : desktopWorkspaceChecking ? "正在核验本机工作区" : desktopWorkspaceBlocked ? "本机工作台未就绪" : localStandaloneRestoring ? "正在打开本机案件" : localStandaloneAwaitingCase ? "本机建案" : localStandaloneCaseOpen ? "本机案件" : workspaceAwaitingCase ? "新建案件" : sourceConfig.label}</small>
        </div>
        <div className={styles.topbarMeta}>
          <span>{displaySyntheticSource ? "主办律师视图" : desktopWorkspaceChecking ? "正在确认本机办案资格" : desktopWorkspaceBlocked ? "请先完成本机工作台设置" : localStandaloneRestoring ? "正在恢复本机案件" : localStandaloneAwaitingCase ? "先选择资料文件夹" : localStandaloneCaseOpen ? "个人本机办案" : workspaceAwaitingCase ? "先建立案件，再导入资料" : "本案工作区"}</span>
          <span className={styles.dot} aria-hidden="true" />
          <span>{displaySyntheticSource ? "仅供流程演示" : desktopWorkspaceChecking ? "尚未读取或创建案件" : desktopWorkspaceBlocked ? "没有读取演示案情或本案材料" : localStandaloneRestoring ? "尚未读取材料" : localStandaloneAwaitingCase ? "等待资料根关联" : localStandaloneCaseOpen ? localStandaloneCase?.inventory ? "本机材料已盘点" : "资料尚未盘点" : workspaceAwaitingCase ? "等待新建案件" : sourceConfig.kind === "persistent-disabled" ? "案件服务未连接" : "材料留在本机受控范围"}</span>
          {desktopRuntime ? (
            <>
              <span className={styles.dot} aria-hidden="true" />
              <span className={desktopRuntime.phase === "BLOCKED" || desktopRuntime.phase === "STOPPED" ? styles.runtimeBlocked : styles.runtimeState}>
                {desktopRuntime.phase === "READY" ? "办案服务已就绪" : desktopRuntime.message}
              </span>
            </>
          ) : null}
        </div>
      </header>

      <section className={styles.caseHeader} aria-labelledby="case-title">
        <div>
          <p className={styles.eyebrow}>{displaySyntheticSource ? `案件编号 / ${syntheticMatter.matterNo}` : localStandaloneCaseOpen ? "本机案件工作区" : "案件工作区"}</p>
          <h1 id="case-title">{displaySyntheticSource ? syntheticMatter.title : desktopWorkspaceChecking ? "正在核验本机案件工作区" : desktopWorkspaceBlocked ? "本机案件工作区暂不可用" : localStandaloneRestoring ? "正在恢复本机案件" : localStandaloneAwaitingCase ? "从本地资料建立案件" : localStandaloneCaseOpen ? localStandaloneCase?.title : workspaceAwaitingCase ? "建立案件工作区" : sourceConfig.kind === "persistent-disabled" ? "案件资料库尚未启用" : "案件标题将在事实台账中核验"}</h1>
          <p className={styles.caseSubline}>{displaySyntheticSource ? `${syntheticMatter.client} · ${syntheticMatter.court} · 对方：${syntheticMatter.opponent}` : desktopWorkspaceChecking ? "正在确认桌面身份、会话和资料库状态；尚未读取或创建案件。" : desktopWorkspaceBlocked ? resolvedDesktopWorkspaceMessage ?? "请先检查工作台设置。" : localStandaloneRestoring ? "正在读取本机案件目录；尚未读取资料文件夹。" : localStandaloneAwaitingCase ? "先选择本案资料文件夹，再建立一个仅保存在本机的案件。" : localStandaloneCaseOpen ? localStandaloneCase?.inventory ? `资料根：${localStandaloneCase.materialRoot.displayName} · 已只读盘点 ${localStandaloneCase.inventory.totalFiles} 个文件` : `资料根：${localStandaloneCase?.materialRoot.displayName ?? "未关联"} · 尚未开始材料盘点` : workspaceAwaitingCase ? "先建立一个案件，再选择本地资料文件夹。" : sourceConfig.kind === "persistent-disabled" ? sourceConfig.reason : "从已核验材料汇总案件进展"}</p>
        </div>
        <div className={styles.deadline}>
          <span>{displaySyntheticSource ? "下一项期限" : desktopWorkspaceChecking || desktopWorkspaceBlocked ? "工作台状态" : localStandaloneReady ? "本机办案状态" : "期限提醒"}</span>
          <strong>{displaySyntheticSource ? syntheticMatter.deadline : desktopWorkspaceChecking ? "正在核验" : desktopWorkspaceBlocked ? "暂不打开案件" : localStandaloneRestoring ? "正在恢复" : localStandaloneAwaitingCase ? "等待建案" : localStandaloneCaseOpen ? localStandaloneCase?.inventory ? "材料已盘点" : "等待材料盘点" : "尚未接入持久化期限台账"}</strong>
          <em>{displaySyntheticSource ? "演示时间，不代表真实法律期限" : desktopWorkspaceChecking || desktopWorkspaceBlocked ? "不会以演示案件代替真实案件" : localStandaloneReady ? localStandaloneCase?.inventory ? "尚未形成正式办案结论" : "资料内容尚未读取" : "由律师确认后纳入提醒"}</em>
        </div>
        {localStandaloneCaseOpen ? (
          <button className={styles.caseSwitch} onClick={() => {
            clearActiveLocalStandaloneCase();
            setLocalStandaloneCase(null);
            setLocalStandaloneRestoreMessage(null);
            setLocalStandaloneState("ready");
            setView("overview");
            window.history.replaceState(null, "", "/");
          }} type="button">切换案件</button>
        ) : !displaySyntheticSource && !desktopWorkspaceChecking && !desktopWorkspaceBlocked && !workspaceAwaitingCase ? (
          <button className={styles.caseSwitch} onClick={() => {
            clearActivePersistentMatter();
            setSourceConfig(caseDataSourceConfig);
            setView("overview");
            window.history.replaceState(null, "", "/");
          }} type="button">切换案件</button>
        ) : null}
      </section>

      <div className={styles.workspace}>
        <aside className={styles.sidebar} aria-label="案件导航">
          <p className={styles.sideLabel}>本案工作</p>
          <nav>
            {navItems.map((item) => {
              const active = item.id === view;
              const protectedItem = item.id !== "overview" && item.id !== "security";
              const lockedUntilCaseCreated = (workspaceAwaitingCase || desktopWorkspaceChecking || desktopWorkspaceBlocked) && protectedItem;
              const lockedUntilLocalCapability = localStandaloneCaseOpen && protectedItem;
              if (lockedUntilCaseCreated || lockedUntilLocalCapability) {
                return (
                  <button className={styles.navItem} key={item.id} disabled type="button">
                    <span>{item.label}</span><small>{lockedUntilLocalCapability ? localStandaloneCase?.inventory ? "待正式能力" : "待材料盘点" : desktopWorkspaceBlocked ? "先完成设置" : desktopWorkspaceChecking ? "正在核验" : "先新建案件"}</small>
                  </button>
                );
              }
              if (item.href) {
                return (
                  <a
                    aria-current={active ? "page" : undefined}
                    className={`${styles.navItem} ${active ? styles.navActive : ""}`}
                    href={item.href}
                    key={item.id}
                  >
                    <span>{item.label}</span>
                  </a>
                );
              }
              return (
                <button
                  className={`${styles.navItem} ${active ? styles.navActive : ""}`}
                  key={item.id}
                  disabled
                  type="button"
                >
                  <span>{item.label}</span>
                  <small>开发中</small>
                </button>
              );
            })}
          </nav>
          <div className={styles.sidebarRule} />
          <p className={styles.sideLabel}>本案进度</p>
          <ol className={styles.stageList}>
            {lawyerProgress.map((label, index) => (
              <li className={index === currentStageIndex ? styles.stageCurrent : index < currentStageIndex ? styles.stageDone : ""} key={label}>
                <span>{String(index + 1).padStart(2, "0")}</span>{label}
              </li>
            ))}
          </ol>
        </aside>

        {view === "security" ? (
          <IdentitySecurityWorkbench desktopRuntime={desktopRuntime} />
        ) : desktopWorkspaceChecking ? (
          <section className={styles.content} aria-label="正在核验本机案件工作区">
            <div className={styles.evidenceLoading}>正在确认本机身份、会话和案件资料库…</div>
          </section>
        ) : desktopWorkspaceBlocked ? (
          <DesktopWorkspaceBlocked
            message={resolvedDesktopWorkspaceMessage}
            onRetry={() => {
              setDesktopWorkspaceState("checking");
              setDesktopWorkspaceMessage(null);
              setDesktopRuntime(null);
              setDesktopRuntimeRefreshKey((current) => current + 1);
            }}
          />
        ) : restoringPersistentWorkspace ? (
          <section className={styles.content} aria-label="正在打开案件工作区">
            <div className={styles.evidenceLoading}>正在恢复本机案件工作区…</div>
          </section>
        ) : localStandaloneRestoring ? (
          <section className={styles.content} aria-label="正在恢复本机案件">
            <div className={styles.evidenceLoading}>正在读取本机案件目录；尚未读取资料文件夹…</div>
          </section>
        ) : localStandaloneAwaitingCase ? (
          <LocalStandaloneOnboarding initialNotice={localStandaloneRestoreMessage} onCaseOpened={(caseSummary) => {
            activateLocalStandaloneCase(caseSummary);
            setLocalStandaloneCase(caseSummary);
            setLocalStandaloneRestoreMessage(null);
            setLocalStandaloneState("ready");
            setView("overview");
            window.history.replaceState(null, "", "/");
          }} />
        ) : localStandaloneCaseOpen && localStandaloneCase !== null ? (
          view === "overview" ? (
            <LocalStandaloneCaseHome
              caseSummary={localStandaloneCase}
              onCaseUpdated={(caseSummary) => {
                activateLocalStandaloneCase(caseSummary);
                setLocalStandaloneCase(caseSummary);
              }}
              onShowCaseList={() => {
                clearActiveLocalStandaloneCase();
                setLocalStandaloneCase(null);
                setLocalStandaloneRestoreMessage(null);
                setLocalStandaloneState("ready");
                setView("overview");
                window.history.replaceState(null, "", "/");
              }}
            />
          ) : (
            <LocalStandaloneFeatureUnavailable
              caseSummary={localStandaloneCase}
              featureLabel={localFeatureLabel}
              onReturnHome={() => {
                setView("overview");
                window.history.replaceState(null, "", "/");
              }}
            />
          )
        ) : view === "overview" && workspaceAwaitingCase ? (
          <PersistentWorkspaceSetup onMatterCreated={async (matterId) => {
            const nextSourceConfig = activatePersistentMatter(matterId);
            setSourceConfig(nextSourceConfig);
            setView("evidence");
            window.history.pushState(null, "", "/evidence");
          }} />
        ) : view === "overview" ? (
          <Overview unresolvedCount={unresolvedCount} sourceConfig={sourceConfig} syntheticSource={displaySyntheticSource} />
        ) : view === "evidence" ? (
          <EvidenceManifestWorkbench />
        ) : view === "facts" ? <FactsWorkbench /> : view === "legal" ? <LegalWorkbench /> : view === "calculation" ? (
          <CalculationWorkbench />
        ) : <SubmissionWorkbench />}
      </div>

      <footer className={styles.footer}>
        <span>{displaySyntheticSource ? "演示案件：用于确认办案流程和界面，不代表真实案情或法律意见" : desktopWorkspaceChecking || desktopWorkspaceBlocked ? "尚未读取、创建或变更任何案件材料" : localStandaloneReady ? localStandaloneCase?.inventory ? "本机清单来自只读盘点；尚未形成事实、法律或提交结论" : "资料根仅在本机关联；未取得盘点回执前不显示任何材料结果" : "每一项结论均可追溯至材料并由律师确认"}</span>
        <span>原始材料不被改写；对外发送前须逐次取得授权</span>
      </footer>
    </main>
  );
}

function DesktopWorkspaceBlocked({ message, onRetry }: { message: string | null; onRetry: () => void }) {
  return (
    <section className={styles.content} aria-label="本机案件工作区未就绪">
      <div className={styles.contentTopline}><span>办案首页</span><span className={styles.statusPill}>尚未打开案件</span></div>
      <article className={styles.nextDecision}>
        <div>
          <p className={styles.eyebrow}>本机工作台未就绪</p>
          <h2>暂不能新建、打开或读取案件</h2>
          <p>{message ?? "请先完成本机身份、会话和案件资料库核验。"}</p>
          <p>系统没有以演示案情代替真实案件，也没有读取、创建或修改任何案件材料。</p>
        </div>
        <div className={styles.caseSetupActions}>
          <button onClick={onRetry} type="button">重新核验</button>
          <a className={styles.primaryAction} href="/security">查看工作台设置</a>
        </div>
      </article>
    </section>
  );
}

function PersistentWorkspaceSetup({ onMatterCreated }: { onMatterCreated: (matterId: string) => Promise<void> }) {
  const [title, setTitle] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [matters, setMatters] = useState<PersistentMatterListItem[]>([]);
  const [listState, setListState] = useState<"loading" | "ready" | "blocked">("loading");
  const [listReloadKey, setListReloadKey] = useState(0);
  const [openingMatterId, setOpeningMatterId] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void loadPersistentMatterList()
      .then((items) => {
        if (!active) return;
        setMatters(items);
        setListState("ready");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setListState("blocked");
        setNotice(reason instanceof Error ? reason.message : "案件列表读取失败。");
      });
    return () => { active = false; };
  }, [listReloadKey]);

  async function createMatter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
    setBusy(true);
    try {
      const receipt = await createPersistentMatter(title);
      try {
        await onMatterCreated(receipt.matterId);
      } catch (reason: unknown) {
        const message = reason instanceof Error ? reason.message : "请刷新页面后从“已有案件”继续。";
        setNotice(`案件已经建立，但未能打开收集材料页面：${message}`);
      }
    } catch (reason: unknown) {
      setNotice(reason instanceof Error ? reason.message : "案件未创建；请保留当前页面后重试。");
    } finally {
      setBusy(false);
    }
  }

  async function openMatter(matterId: string) {
    setNotice(null);
    setOpeningMatterId(matterId);
    try {
      await onMatterCreated(matterId);
    } catch (reason: unknown) {
      const message = reason instanceof Error ? reason.message : "请稍后再次尝试。";
      setNotice(`未能打开该案件：${message}`);
    } finally {
      setOpeningMatterId(null);
    }
  }

  function retryMatterList() {
    setNotice(null);
    setListState("loading");
    setListReloadKey((current) => current + 1);
  }

  return (
    <section className={styles.content} aria-label="新建或打开案件">
      <div className={styles.contentTopline}><span>新建或打开案件</span><span className={styles.statusPill}>尚未读取任何材料</span></div>
      <article className={styles.nextDecision}>
        <div>
          <p className={styles.eyebrow}>第一步</p>
          <h2>先建一宗案件，再放入资料</h2>
          <p>先填写便于你识别的案件名称。下一步选择本案资料文件夹；双方、金额、期限和诉讼立场都由材料核对后再形成。</p>
        </div>
      </article>
      <form className={styles.caseSetupForm} onSubmit={(event) => void createMatter(event)}>
        <label>
          <span>案件名称</span>
          <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={160} placeholder="例如：周雅丽民间借贷纠纷" required />
        </label>
        <div className={styles.caseSetupActions}>
          <button type="submit" disabled={busy || openingMatterId !== null || title.trim().length < 2}>{busy ? "正在建立…" : "建立案件，下一步选择材料文件夹"}</button>
          <small>建立后，选择本案资料文件夹。系统先列出文件清单，由你确认后才开始整理。</small>
        </div>
        {notice ? <p className={styles.caseSetupNotice}>{notice}</p> : null}
      </form>
      <section className={styles.casePicker} aria-label="可访问案件">
        <div>
          <p className={styles.eyebrow}>已有案件</p>
          <h3>{listState === "loading" ? "正在读取案件…" : listState === "blocked" ? "暂时无法读取案件" : matters.length === 0 ? "还没有案件" : "继续办理已有案件"}</h3>
        </div>
        {listState === "ready" && matters.length > 0 ? (
          <div className={styles.casePickerList}>
            {matters.map((matter) => (
              <button disabled={busy || openingMatterId !== null} key={matter.matterId} onClick={() => void openMatter(matter.matterId)} type="button">
                <span><strong>{matter.title}</strong><small>{matter.stage} · 版本 {matter.version}</small></span>
                <em>{openingMatterId === matter.matterId ? "正在打开…" : "打开"}</em>
              </button>
            ))}
          </div>
        ) : listState === "blocked" ? (
          <div className={styles.caseSetupActions}>
            <button onClick={retryMatterList} type="button">重新读取案件</button>
            <small>如果本机服务刚启动或刚完成律所登记，可重新读取；不会建立新案件。</small>
          </div>
        ) : <small>这里只显示你当前有权限办理的案件。</small>}
      </section>
    </section>
  );
}

function Overview({
  unresolvedCount,
  sourceConfig,
  syntheticSource,
}: {
  unresolvedCount: number;
  sourceConfig: CaseDataSourceConfig;
  syntheticSource: boolean;
}) {
  if (!syntheticSource) {
    return <PersistentOverview sourceConfig={sourceConfig} />;
  }
  return <LawyerDashboard
    sourceConfig={sourceConfig}
    status="材料处理中"
    headline="先整理与对方之间的还款记录"
    description="工作台已经找到可能相关的交易页。先确认重复页和不相关页面，随后系统会把可用还款记录汇入案件台账。"
    actionLabel="开始整理材料"
    actionHref="/evidence"
    summary={[
      ["案件类型", "民间借贷纠纷"],
      ["对方", syntheticMatter.opponent],
      ["本金", syntheticMatter.principal],
      ["下一项期限", syntheticMatter.deadline],
    ]}
    tasks={[
      { state: "现在处理", title: "筛选与对方有关的微信交易页", detail: `有 ${unresolvedCount} 页需要你确认是否保留`, href: "/evidence", action: "去整理" },
      { state: "接下来", title: "核对每笔还款的时间、金额和币种", detail: "不先判断它是否属于本金或利息", href: "/facts", action: "去核对" },
      { state: "待材料确认", title: "确定法律依据与利息口径", detail: "先确认适用规则和时间边界，不让系统自行选择利率", href: "/legal", action: "看依据" },
      { state: "接着处理", title: "核算已支付利息与可抵扣本金", detail: "按已确认交易和律师确定的规则计算", href: "/calculation", action: "去核算" },
      { state: "最后形成", title: "整理应诉材料", detail: "答辩初稿、收付款核对表与材料核对清单均保留来源，须经律师确认后导出", href: "/bundle", action: "查看材料" },
    ]}
  />;
}

type DashboardTask = { state: string; title: string; detail: string; href: string; action: string };

function LawyerDashboard({
  sourceConfig, status, headline, description, actionLabel, actionHref, summary, tasks,
}: {
  sourceConfig: CaseDataSourceConfig;
  status: string;
  headline: string;
  description: string;
  actionLabel: string;
  actionHref: string;
  summary: ReadonlyArray<readonly [string, string]>;
  tasks: ReadonlyArray<DashboardTask>;
}) {
  return (
    <section className={`${styles.content} ${styles.lawyerDashboard}`} aria-label="办案首页">
      <div className={styles.contentTopline}>
        <span>办案首页</span>
        <span className={styles.statusPill}>{status}</span>
      </div>
      <article className={styles.dashboardHero}>
        <div>
          <p className={styles.eyebrow}>今天先办这一件</p>
          <h2>{headline}</h2>
          <p>{description}</p>
          <a className={styles.primaryAction} href={actionHref}>{actionLabel}</a>
        </div>
        <div className={styles.dashboardHeroNote}>
          <strong>AI 会做什么</strong>
          <span>整理材料、找出待核对项、生成可编辑初稿。</span>
          <strong>律师要做什么</strong>
          <span>确认事实、法律口径、对外发送和最终文书。</span>
        </div>
      </article>
      <CaseAssistantPlan sourceConfig={sourceConfig} />
      <section className={styles.caseSnapshot} aria-labelledby="case-snapshot-heading">
        <div className={styles.dashboardSectionHeading}>
          <div><p className={styles.eyebrow}>案件要点</p><h2 id="case-snapshot-heading">一眼掌握本案</h2></div>
          <span>来自已登记或已核验材料</span>
        </div>
        <dl>
          {summary.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
        </dl>
      </section>
      <section className={styles.caseTaskBoard} aria-labelledby="case-tasks-heading">
        <div className={styles.dashboardSectionHeading}>
          <div><p className={styles.eyebrow}>办理路线</p><h2 id="case-tasks-heading">从资料到应诉材料</h2></div>
          <span>按顺序办，不需要先研究系统</span>
        </div>
        <div className={styles.taskList}>
          {tasks.map((task, index) => (
            <article key={task.title}>
              <span className={styles.taskIndex}>{String(index + 1).padStart(2, "0")}</span>
              <div><small>{task.state}</small><h3>{task.title}</h3><p>{task.detail}</p></div>
              <a href={task.href}>{task.action}</a>
            </article>
          ))}
        </div>
      </section>
      <section className={styles.dashboardAssurance} aria-label="工作台原则">
        <div><strong>原件不改动</strong><span>所有筛选、标框和文书都作为独立工作成果保存。</span></div>
        <div><strong>每个结论可回看</strong><span>从结论可回到对应材料页、交易或法律依据。</span></div>
        <div><strong>对外发送有确认</strong><span>模型、网络检索和法院提交都需要明确授权。</span></div>
      </section>
    </section>
  );
}

function PersistentOverview({ sourceConfig }: { sourceConfig: CaseDataSourceConfig }) {
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; review: CaseReviewView }
    | { status: "blocked"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    let active = true;
    loadCaseReview(sourceConfig)
      .then((review) => {
        if (active) setState({ status: "ready", review });
      })
      .catch((reason: unknown) => {
        if (active) setState({
          status: "blocked",
          message: reason instanceof Error ? reason.message : "案件总览快照读取失败",
        });
      });
    return () => {
      active = false;
    };
  }, [sourceConfig]);

  if (state.status === "loading") {
    return <section className={styles.content} aria-label="案件总览"><div className={styles.calculationLoading}>正在读取版本化案件总览…</div></section>;
  }
  if (state.status === "blocked") {
    return (
      <section className={styles.content} aria-label="案件总览">
        <div className={styles.contentTopline}><span>案件总览</span><span className={styles.statusPill}>等待版本化材料</span></div>
        <article className={styles.nextDecision}>
          <div>
            <p className={styles.eyebrow}>案件尚未形成可展示的总览快照</p>
            <h2>先确认案卷范围并接收材料</h2>
            <p>请稍后重新打开本页，或先检查工作台设置。系统不会以示例案情、示例金额或示例期限填充真实案件。</p>
          </div>
          <a className={styles.primaryAction} href="/evidence">进入证据核验</a>
        </article>
      </section>
    );
  }
  const { review } = state;
  const awaitsMaterialIntake = review.factPage.totalCount === 0 && review.transactionPage.totalCount === 0 && review.claims.length === 0;
  return <LawyerDashboard
    sourceConfig={sourceConfig}
    status={awaitsMaterialIntake ? "等待导入资料" : `案件版本 ${review.matterVersion}`}
    headline={awaitsMaterialIntake ? "把本案资料放进同一个文件夹" : "核对案件事实与付款记录"}
    description={awaitsMaterialIntake ? "选择资料文件夹后，工作台会先给出材料清单。你确认范围后，才会开始读取、归类和整理。" : "工作台已汇总当前台账。先处理待确认事项，再进入利息口径与应诉材料。"}
    actionLabel={awaitsMaterialIntake ? "选择资料文件夹" : "继续核对案情"}
    actionHref={awaitsMaterialIntake ? "/evidence" : "/facts"}
    summary={[
      ["已登记事实", `${review.factPage.totalCount} 项`],
      ["已登记交易", `${review.transactionPage.totalCount} 笔`],
      ["待你确认", `${review.pendingFacts.length} 项`],
      ["争点", `${review.issues.length} 项`],
    ]}
    tasks={[
      { state: awaitsMaterialIntake ? "现在处理" : "已开始", title: "收集和整理本案材料", detail: awaitsMaterialIntake ? "选择资料文件夹，确认哪些文件纳入本案" : "材料范围已建立，可继续补充或查看", href: "/evidence", action: "查看材料" },
      { state: review.pendingFacts.length ? "需要确认" : "下一步", title: "核对案情与还款记录", detail: `${review.factPage.totalCount} 项事实、${review.transactionPage.totalCount} 笔交易在台账中`, href: "/facts", action: "去核对" },
      { state: "准备中", title: "确定法律依据与利息口径", detail: "先核对适用规则、时间边界和官方依据，再计算金额", href: "/legal", action: "查看依据" },
      { state: "接着处理", title: "核算还款与利息", detail: "只读取已确认的交易、分类和律师批准的规则", href: "/calculation", action: "去核算" },
      { state: "最后形成", title: "整理应诉材料", detail: "答辩初稿、收付款核对表与材料核对清单均可逐项核对和确认", href: "/bundle", action: "查看材料" },
    ]}
  />;
}
