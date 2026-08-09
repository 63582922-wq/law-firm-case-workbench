"use client";

import { useMemo, useState } from "react";
import { CalculationWorkbench } from "@/components/calculation-workbench";
import { stageLabels, syntheticMatter, type EvidencePage } from "@/lib/synthetic-matter";
import styles from "./case-workbench.module.css";

type View = "overview" | "evidence" | "calculation";
type DuplicateDecision = "pending" | "exclude" | "keep";

const navItems: ReadonlyArray<{ id: View | "facts" | "bundle"; label: string; href?: string }> = [
  { id: "overview", label: "案件总览", href: "/" },
  { id: "evidence", label: "证据核验", href: "/evidence" },
  { id: "facts", label: "事实与争点" },
  { id: "calculation", label: "利息测算", href: "/calculation" },
  { id: "bundle", label: "提交材料" },
];

function confidenceClass(confidence: EvidencePage["confidence"]) {
  if (confidence === "已核验") return styles.verified;
  if (confidence === "待人工决定") return styles.pending;
  return styles.needsMaterial;
}

export function CaseWorkbench({ initialView = "overview" }: { initialView?: View }) {
  const [view] = useState<View>(initialView);
  const [selectedPage, setSelectedPage] = useState(17);
  const [duplicateDecision, setDuplicateDecision] = useState<DuplicateDecision>("pending");
  const [auditNotice, setAuditNotice] = useState("尚未记录新的模拟审计决定。");

  const selected = useMemo(
    () => syntheticMatter.evidence.find((item) => item.page === selectedPage) ?? syntheticMatter.evidence[0],
    [selectedPage],
  );
  const unresolvedCount = syntheticMatter.evidence.filter((item) => item.confidence !== "已核验").length;
  const currentStageIndex = view === "calculation" ? 2 : 1;

  function recordDecision() {
    if (duplicateDecision === "pending") {
      setAuditNotice("请先选择律师决定；系统不会替代律师作出取舍。");
      return;
    }
    const action = duplicateDecision === "exclude" ? "排除第17页的衍生提交引用" : "保留第17页的衍生提交引用";
    setAuditNotice(`已记录合成审计：${action}。原始影像仍保留且不改写。`);
  }

  return (
    <main className={styles.shell}>
      <header className={styles.topbar}>
        <div className={styles.brand} aria-label="律所案件 AI 工作台">
          <span className={styles.brandMark}>案</span>
          <span>律所案件 AI 工作台</span>
          <small>内部合成 Alpha</small>
        </div>
        <div className={styles.topbarMeta}>
          <span>当前角色：主办律师（合成）</span>
          <span className={styles.dot} aria-hidden="true" />
          <span>不连接真实案件材料</span>
        </div>
      </header>

      <section className={styles.caseHeader} aria-labelledby="case-title">
        <div>
          <p className={styles.eyebrow}>案件卷宗 / {syntheticMatter.matterNo}</p>
          <h1 id="case-title">{syntheticMatter.title}</h1>
          <p className={styles.caseSubline}>{syntheticMatter.client} · {syntheticMatter.court} · 争议对方：{syntheticMatter.opponent}</p>
        </div>
        <div className={styles.deadline}>
          <span>最近期限</span>
          <strong>{syntheticMatter.deadline}</strong>
          <em>合成演示时间，不代表真实法律期限</em>
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
          <Overview unresolvedCount={unresolvedCount} />
        ) : view === "evidence" ? (
          <EvidenceWorkbench
            auditNotice={auditNotice}
            duplicateDecision={duplicateDecision}
            onDecisionChange={setDuplicateDecision}
            onRecordDecision={recordDecision}
            onSelectPage={setSelectedPage}
            selected={selected}
            selectedPage={selectedPage}
            unresolvedCount={unresolvedCount}
          />
        ) : (
          <CalculationWorkbench />
        )}
      </div>

      <footer className={styles.footer}>
        <span>内部合成 Alpha · 不接收真实案件材料 · 不生成可提交法院的文件</span>
        <span>所有结论、取舍与锁定均须由具备权限的人员在后续流程确认</span>
      </footer>
    </main>
  );
}

function Overview({ unresolvedCount }: { unresolvedCount: number }) {
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

type EvidenceProps = {
  selected: EvidencePage;
  selectedPage: number;
  unresolvedCount: number;
  duplicateDecision: DuplicateDecision;
  auditNotice: string;
  onSelectPage: (page: number) => void;
  onDecisionChange: (value: DuplicateDecision) => void;
  onRecordDecision: () => void;
};

function EvidenceWorkbench({ selected, selectedPage, unresolvedCount, duplicateDecision, auditNotice, onSelectPage, onDecisionChange, onRecordDecision }: EvidenceProps) {
  const related = syntheticMatter.evidence;
  return (
    <section className={styles.evidenceArea} aria-label="证据核验台">
      <header className={styles.evidenceHeading}>
        <div>
          <p className={styles.eyebrow}>证据工作台</p>
          <h2>交易记录页核验</h2>
        </div>
        <p><strong>{related.length}</strong> 页相关记录 · <strong>{unresolvedCount}</strong> 页待人工处理</p>
      </header>

      <div className={styles.evidenceColumns}>
        <aside className={styles.pageList}>
          <div className={styles.listHeading}><span>相关页</span><small>仅合成数据</small></div>
          {related.map((item) => (
            <button
              className={`${styles.pageItem} ${item.page === selectedPage ? styles.pageSelected : ""}`}
              key={item.page}
              onClick={() => onSelectPage(item.page)}
              type="button"
            >
              <span className={styles.pageNumber}>第 {item.page} 页</span>
              <strong>{item.amount}</strong>
              <small>{item.date}</small>
              <em className={confidenceClass(item.confidence)}>{item.confidence}</em>
            </button>
          ))}
        </aside>

        <article className={styles.documentStage}>
          <div className={styles.documentToolbar}>
            <span>原始影像预览 · 第 {selected.page} 页</span>
            <span>缩放 100%</span>
          </div>
          <div className={styles.documentPaper} aria-label={`合成交易记录第 ${selected.page} 页`}>
            <div className={styles.documentBrand}>微信支付 <small>合成示例</small></div>
            <div className={styles.documentTitle}>交易明细证明</div>
            <div className={styles.documentMeta}><span>交易时间</span><strong>{selected.date} 10:16</strong></div>
            <div className={`${styles.transactionRow} ${styles.redBox}`}>
              <div><span>转账给</span><strong>{selected.counterpart}</strong></div>
              <b>{selected.amount}</b>
            </div>
            <div className={styles.documentMeta}><span>交易单号</span><strong>ALPHA-TRX-{String(selected.page).padStart(4, "0")}</strong></div>
            <div className={styles.documentMeta}><span>资金性质</span><strong>转账（合成标签）</strong></div>
            <p className={styles.documentFootnote}>红框仅为证据定位坐标，未对原始内容作修改或法律定性。</p>
          </div>
          <p className={styles.sourceNote}>来源层：原始影像只读保存；后续提交材料应使用已核验的衍生版本，并保留来源关系。</p>
        </article>

        <aside className={styles.inspector}>
          <p className={styles.eyebrow}>核验说明</p>
          <h3>第 {selected.page} 页</h3>
          <dl className={styles.inspectorFacts}>
            <div><dt>识别对象</dt><dd>{selected.counterpart}</dd></div>
            <div><dt>金额与币种</dt><dd>{selected.amount} · CNY</dd></div>
            <div><dt>当前状态</dt><dd className={confidenceClass(selected.confidence)}>{selected.confidence}</dd></div>
          </dl>
          <p className={styles.inspectorNote}>{selected.note}</p>

          {(selected.page === 17 || selected.page === 18) && (
            <div className={styles.decisionPanel}>
              <label htmlFor="duplicate-decision">律师决定（合成演示）</label>
              <select id="duplicate-decision" value={duplicateDecision} onChange={(event) => onDecisionChange(event.target.value as DuplicateDecision)}>
                <option value="pending">尚未决定</option>
                <option value="exclude">排除重复提交引用</option>
                <option value="keep">保留为独立页</option>
              </select>
              <button type="button" onClick={onRecordDecision}>记录模拟审计决定</button>
            </div>
          )}

          <div className={styles.auditNotice} role="status">{auditNotice}</div>
          <button className={styles.disabledAction} disabled type="button">生成提交材料（待核验完成）</button>
        </aside>
      </div>
    </section>
  );
}
