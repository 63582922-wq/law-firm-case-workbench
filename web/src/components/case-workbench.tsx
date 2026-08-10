"use client";

import { useEffect, useState } from "react";
import { CalculationWorkbench } from "@/components/calculation-workbench";
import { EvidenceWorkbench as EvidenceManifestWorkbench } from "@/components/evidence-workbench";
import { FactsWorkbench } from "@/components/facts-workbench";
import { LegalWorkbench } from "@/components/legal-workbench";
import { IdentitySecurityWorkbench } from "@/components/identity-security-workbench";
import { SubmissionWorkbench } from "@/components/submission-workbench";
import { caseDataSourceConfig } from "@/lib/case-data-source";
import { readDesktopRuntimeStatus } from "@/lib/desktop-bridge";
import type { DesktopRuntimeStatus } from "@/lib/desktop-bridge";
import { stageLabels, syntheticMatter } from "@/lib/synthetic-matter";
import styles from "./case-workbench.module.css";

type View = "overview" | "evidence" | "facts" | "legal" | "calculation" | "bundle" | "security";

const navItems: ReadonlyArray<{ id: View | "facts" | "bundle"; label: string; href?: string }> = [
  { id: "overview", label: "案件总览", href: "/" },
  { id: "evidence", label: "证据核验", href: "/evidence" },
  { id: "facts", label: "事实与争点", href: "/facts" },
  { id: "legal", label: "法律规则", href: "/legal" },
  { id: "calculation", label: "利息测算", href: "/calculation" },
  { id: "bundle", label: "提交材料", href: "/bundle" },
  { id: "security", label: "身份与安全", href: "/security" },
];

export function CaseWorkbench({ initialView = "overview" }: { initialView?: View }) {
  const [view] = useState<View>(initialView);
  const [desktopRuntime, setDesktopRuntime] = useState<DesktopRuntimeStatus | null>(null);
  const unresolvedCount = syntheticMatter.evidence.filter((item) => item.confidence !== "已核验").length;
  const currentStageIndex = view === "bundle" ? 4 : view === "legal" || view === "calculation" ? 2 : 1;
  const syntheticSource = caseDataSourceConfig.kind === "synthetic-alpha";

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      try {
        const status = await readDesktopRuntimeStatus();
        if (cancelled || status === null) return;
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
          });
        }
      }
    };
    void refresh();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  return (
    <main className={styles.shell}>
      <header className={styles.topbar}>
        <div className={styles.brand} aria-label="律所案件 AI 工作台">
          <span className={styles.brandMark}>案</span>
          <span>律所案件 AI 工作台</span>
          <small>{syntheticSource ? "内部合成 Alpha" : caseDataSourceConfig.label}</small>
        </div>
        <div className={styles.topbarMeta}>
          <span>{syntheticSource ? "当前角色：主办律师（合成）" : "身份来源：服务端会话与数据库案件角色"}</span>
          <span className={styles.dot} aria-hidden="true" />
          <span>{syntheticSource ? "不连接真实案件材料" : caseDataSourceConfig.kind === "persistent-disabled" ? "持久化数据源未启用" : "持久化内部预览"}</span>
          {desktopRuntime ? (
            <>
              <span className={styles.dot} aria-hidden="true" />
              <span className={desktopRuntime.phase === "BLOCKED" || desktopRuntime.phase === "STOPPED" ? styles.runtimeBlocked : styles.runtimeState}>
                {desktopRuntime.phase === "READY" ? "本机服务已就绪（案件仍禁用）" : desktopRuntime.message}
              </span>
            </>
          ) : null}
        </div>
      </header>

      <section className={styles.caseHeader} aria-labelledby="case-title">
        <div>
          <p className={styles.eyebrow}>{syntheticSource ? `案件卷宗 / ${syntheticMatter.matterNo}` : "持久化案件 / 由版本化快照读取"}</p>
          <h1 id="case-title">{syntheticSource ? syntheticMatter.title : caseDataSourceConfig.kind === "persistent-disabled" ? "持久化案件尚未启用" : "案件标题将在事实台账中核验"}</h1>
          <p className={styles.caseSubline}>{syntheticSource ? `${syntheticMatter.client} · ${syntheticMatter.court} · 争议对方：${syntheticMatter.opponent}` : caseDataSourceConfig.kind === "persistent-disabled" ? caseDataSourceConfig.reason : "不会以合成案件内容回退或覆盖持久化案件状态"}</p>
        </div>
        <div className={styles.deadline}>
          <span>{syntheticSource ? "最近期限" : "期限状态"}</span>
          <strong>{syntheticSource ? syntheticMatter.deadline : "尚未接入持久化期限台账"}</strong>
          <em>{syntheticSource ? "合成演示时间，不代表真实法律期限" : "系统不会沿用合成期限"}</em>
        </div>
      </section>

      <div className={styles.workspace}>
        <aside className={styles.sidebar} aria-label="案件导航">
          <p className={styles.sideLabel}>工作区</p>
          <nav>
            {navItems.map((item) => {
              const active = item.id === view;
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
          <p className={styles.sideLabel}>流程位置</p>
          <ol className={styles.stageList}>
            {stageLabels.map((label, index) => (
              <li className={index === currentStageIndex ? styles.stageCurrent : index < currentStageIndex ? styles.stageDone : ""} key={label}>
                <span>{String(index + 1).padStart(2, "0")}</span>{label}
              </li>
            ))}
          </ol>
        </aside>

        {view === "overview" ? (
          <Overview unresolvedCount={unresolvedCount} syntheticSource={syntheticSource} />
        ) : view === "evidence" ? (
          <EvidenceManifestWorkbench />
        ) : view === "facts" ? <FactsWorkbench /> : view === "legal" ? <LegalWorkbench /> : view === "calculation" ? (
          <CalculationWorkbench />
        ) : view === "bundle" ? <SubmissionWorkbench /> : (
          <IdentitySecurityWorkbench desktopRuntime={desktopRuntime} />
        )}
      </div>

      <footer className={styles.footer}>
        <span>{syntheticSource ? "内部合成 Alpha · 不接收真实案件材料 · 不生成可提交法院的文件" : "持久化内部预览 · 仅在依赖、审批、本机编译与导出核验全部通过后形成法院 ZIP"}</span>
        <span>所有结论、取舍与锁定均须由具备权限的人员在后续流程确认</span>
      </footer>
    </main>
  );
}

function Overview({
  unresolvedCount,
  syntheticSource,
}: {
  unresolvedCount: number;
  syntheticSource: boolean;
}) {
  if (!syntheticSource) {
    return (
      <section className={styles.content} aria-label="案件总览">
        <div className={styles.contentTopline}>
          <span>案件总览</span>
          <span className={styles.statusPill}>等待版本化材料</span>
        </div>
        <article className={styles.nextDecision}>
          <div>
            <p className={styles.eyebrow}>案件尚未形成可展示的总览快照</p>
            <h2>先确认案卷范围并接收材料</h2>
            <p>系统不会以示例案情、示例金额、示例期限或模拟审计记录填充真实案件。完成材料盘点后，证据、事实、法律和计算页面会分别展示各自的版本化快照。</p>
          </div>
          <a className={styles.primaryAction} href="/evidence">进入证据核验</a>
        </article>
        <section className={styles.overviewGrid} aria-label="案件建立顺序">
          <article className={styles.paperCard}>
            <p className={styles.cardKicker}>第一步</p>
            <h3>本机案卷范围</h3>
            <p className={styles.cardNote}>由律师选择案件文件夹；系统只登记相对路径、受检哈希和材料状态，不改写原件。</p>
          </article>
          <article className={styles.paperCard}>
            <p className={styles.cardKicker}>第二步</p>
            <h3>事实与规则</h3>
            <p className={styles.cardNote}>证据、付款性质、法源和利率期间均须分别确认；没有正式快照时不显示金额或法律结论。</p>
          </article>
        </section>
      </section>
    );
  }
  return (
    <section className={styles.content} aria-label="案件总览">
      <div className={styles.contentTopline}>
        <span>案件总览</span>
        <span className={styles.statusPill}>材料核验中</span>
      </div>
      <article className={styles.nextDecision}>
        <div>
          <p className={styles.eyebrow}>下一项律师决定</p>
          <h2>确认第 17 与 18 页是否作为重复页处理</h2>
          <p>系统只标出相同的视觉线索，不删除原始证据，也不推断其法律证明力。</p>
        </div>
        <a className={styles.primaryAction} href="/evidence">进入证据核验</a>
      </article>

      <section className={styles.overviewGrid}>
        <article className={styles.paperCard}>
          <p className={styles.cardKicker}>收件与范围</p>
          <h3>原始微信交易记录</h3>
          <dl>
            <div><dt>已发现页数</dt><dd>6 页（合成）</dd></div>
            <div><dt>与对方相关</dt><dd>6 页（合成）</dd></div>
            <div><dt>待人工处理</dt><dd className={styles.warnText}>{unresolvedCount} 页</dd></div>
          </dl>
          <p className={styles.cardNote}>导出材料只能引用经核验、人工取舍并锁定的衍生页；原件永远单独保存。</p>
        </article>
        <article className={styles.paperCard}>
          <p className={styles.cardKicker}>金额快照</p>
          <h3>{syntheticMatter.currency}</h3>
          <dl>
            <div><dt>借款本金（合成）</dt><dd>{syntheticMatter.principal}</dd></div>
            <div><dt>已标记利息（合成）</dt><dd>{syntheticMatter.reviewedInterest}</dd></div>
            <div><dt>币种显示</dt><dd>人民币 / CNY</dd></div>
          </dl>
          <p className={styles.cardNote}>金额是界面合成示例，不能作为利息口径或诉讼策略建议。</p>
        </article>
      </section>

      <section className={styles.auditBlock} aria-labelledby="audit-heading">
        <div className={styles.sectionHeading}>
          <div>
            <p className={styles.eyebrow}>可追溯性</p>
            <h2 id="audit-heading">模拟审计记录</h2>
          </div>
          <span>只读</span>
        </div>
        <div className={styles.auditTable} role="table" aria-label="模拟审计记录">
          <div className={styles.auditRow} role="row"><span>合成操作员</span><span>创建合成案件</span><span>2026年08月09日 09:30</span></div>
          <div className={styles.auditRow} role="row"><span>系统</span><span>生成原始页摘要</span><span>2026年08月09日 09:31</span></div>
          <div className={styles.auditRow} role="row"><span>系统</span><span>标记疑似重复页（17 / 18）</span><span>2026年08月09日 09:32</span></div>
        </div>
      </section>
    </section>
  );
}
