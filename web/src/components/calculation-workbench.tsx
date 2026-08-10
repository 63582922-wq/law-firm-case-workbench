"use client";

import { useEffect, useState } from "react";
import {
  caseDataSourceConfig,
  createFormalCalculation,
  loadCalculationReview,
  loadLegalReview,
  type CalculationReviewView,
  type LegalReviewView,
} from "@/lib/case-data-source";
import styles from "./case-workbench.module.css";

type CalculationState =
  | { status: "loading" }
  | { status: "ready"; review: CalculationReviewView }
  | { status: "blocked"; message: string };

const moneyFormatter = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function cny(value: string | null) {
  return value === null ? "—" : moneyFormatter.format(Number(value));
}

function day(value: string | null) {
  if (!value) return "—";
  const [year, month, date] = value.split("-");
  return `${year}年${month}月${date}日`;
}

function rate(value: string) {
  return `${(Number(value) * 100).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}%`;
}

function allocationPolicy(value: string | null) {
  if (value === "INTEREST_THEN_PRINCIPAL") return "先息后本";
  if (value === "PRINCIPAL_THEN_INTEREST") return "先本后息";
  return "—";
}

export function CalculationWorkbench() {
  const [state, setState] = useState<CalculationState>({ status: "loading" });
  const [legalReview, setLegalReview] = useState<LegalReviewView | null>(null);
  const [scenario, setScenario] = useState({
    obligationId: "",
    startDate: "",
    endDate: "",
    allocationPolicy: "INTEREST_THEN_PRINCIPAL" as const,
    approved: false,
  });
  const [scenarioBusy, setScenarioBusy] = useState(false);
  const [scenarioNotice, setScenarioNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const review = await loadCalculationReview();
        if (active) setState({ status: "ready", review });
      } catch (error) {
        if (!active) return;
        setState({
          status: "blocked",
          message: error instanceof Error ? error.message : "未取得正式计算服务响应",
        });
      }
    }
    void load();
    void loadLegalReview()
      .then((review) => { if (active) setLegalReview(review); })
      .catch((reason: unknown) => { if (active) setScenarioNotice(reason instanceof Error ? reason.message : "法律规则包快照读取失败"); });
    return () => {
      active = false;
    };
  }, []);

  const persistent = caseDataSourceConfig.kind !== "synthetic-alpha";
  const review = state.status === "ready" ? state.review : null;

  async function createScenario() {
    if (!legalReview?.currentBundle || legalReview.matterVersion === null) return;
    if (!scenario.approved) {
      setScenarioNotice("请先确认：债务单元、计算区间、人民币币种与已批准冲抵顺序已经核对。");
      return;
    }
    setScenarioBusy(true);
    setScenarioNotice(null);
    try {
      const receipt = await createFormalCalculation({
        expectedVersion: legalReview.matterVersion,
        obligationId: scenario.obligationId,
        startDate: scenario.startDate,
        endDate: scenario.endDate,
        legalBundleId: legalReview.currentBundle.bundleId,
        legalBundleHash: legalReview.currentBundle.bundleHash,
        allocationPolicy: scenario.allocationPolicy,
      });
      const [calculation, refreshedLegal] = await Promise.all([
        loadCalculationReview(undefined, scenario.obligationId.trim()),
        loadLegalReview(),
      ]);
      setState({ status: "ready", review: calculation });
      setLegalReview(refreshedLegal);
      setScenario((prior) => ({ ...prior, approved: false }));
      setScenarioNotice(`正式计算与独立复算已完成；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setScenarioNotice(reason instanceof Error ? reason.message : "正式计算未完成");
    } finally {
      setScenarioBusy(false);
    }
  }

  return (
    <section className={styles.calculationArea} aria-label="利息计算">
      <header className={styles.calculationHeading}>
        <div>
          <p className={styles.eyebrow}>{persistent ? "正式计算 / 持久化快照" : "确定性计算 / 合成预览"}</p>
          <h2>利息与还款冲抵</h2>
          <p>{persistent ? "金额只读取经律师批准、独立复算并持久化的结果。" : "本页仅展示本机合成数据，不代表真实案件或法律结论。"}</p>
        </div>
        <div className={styles.calculationStatus}>
          <span>币种</span>
          <strong>人民币 / CNY</strong>
          <small>正式计算仅允许人民币分金额</small>
        </div>
      </header>

      <div className={styles.calculationGrid}>
        <div className={styles.calculationMain}>
          {state.status === "loading" && (
            <section className={styles.calculationLoading} aria-live="polite">
              正在读取{persistent ? "正式计算快照" : "本机合成计算结果"}；返回前不显示金额结论。
            </section>
          )}

          {state.status === "blocked" && (
            <section className={styles.calculationBlocked} role="alert">
              <p className={styles.eyebrow}>计算读取已阻断</p>
              <h3>没有可核验的计算结果</h3>
              <p>{state.message}</p>
              <small>系统没有使用静态示例或浏览器计算作为替代。</small>
            </section>
          )}

          {review?.status === "empty" && (
            <section className={styles.calculationBlocked} role="status">
              <p className={styles.eyebrow}>尚无正式计算</p>
              <h3>法律规则包或债务单元尚未就绪</h3>
              <p>{review.emptyReason}</p>
              <small>需要先核验交易、付款性质、同日顺序和规则适用期间，再由主办律师批准。</small>
            </section>
          )}

          {persistent && legalReview?.status === "reviewable" && (
            <form className={styles.formalCalculationForm} onSubmit={(event) => { event.preventDefault(); void createScenario(); }}>
              <div className={styles.formalCalculationHeading}><div><p className={styles.eyebrow}>正式情景审批</p><h3>按当前规则包运行独立复算</h3><small>利率、交易金额、付款性质和人民币币种均从已批准台账读取；本表不提供这些字段的编辑入口。</small></div><span>{legalReview.currentBundle ? `规则包 v${legalReview.currentBundle.version}` : "尚无已批准规则包"}</span></div>
              <div className={styles.formalCalculationFields}>
                <label><span>债务单元标识</span><input required value={scenario.obligationId} onChange={(event) => setScenario((prior) => ({ ...prior, obligationId: event.target.value }))} placeholder="选择已批准付款分配使用的 obligation_id" /></label>
                <label><span>计算起日</span><input required type="date" value={scenario.startDate} onChange={(event) => setScenario((prior) => ({ ...prior, startDate: event.target.value }))} /></label>
                <label><span>计算止日</span><input required type="date" value={scenario.endDate} onChange={(event) => setScenario((prior) => ({ ...prior, endDate: event.target.value }))} /></label>
                <label><span>冲抵顺序</span><select value={scenario.allocationPolicy} onChange={(event) => setScenario((prior) => ({ ...prior, allocationPolicy: event.target.value as typeof prior.allocationPolicy }))}><option value="INTEREST_THEN_PRINCIPAL">先息后本</option><option value="PRINCIPAL_THEN_INTEREST">先本后息</option></select></label>
              </div>
              <label className={styles.formalCalculationCheck}><input checked={scenario.approved} onChange={(event) => setScenario((prior) => ({ ...prior, approved: event.target.checked }))} type="checkbox" /><span>我确认该债务单元已有同案、已确认且已分类的人民币交易；计算区间受当前规则包连续覆盖，冲抵顺序已由律师批准。</span></label>
              <div className={styles.formalCalculationActions}><button disabled={scenarioBusy || !legalReview.currentBundle} type="submit">{scenarioBusy ? "正在独立复算…" : "建立正式计算"}</button><small>任何交易、规则包、币种、同日顺序、重复组或独立复算不符合条件，服务端都会拒绝创建结果。</small></div>
              {scenarioNotice && <p className={styles.formalCalculationNotice} role="status">{scenarioNotice}</p>}
            </form>
          )}

          {review?.status === "ready" && (
            <>
              <CalculationAssumptions review={review} />
              <RuleTrace review={review} />
              <CalculationResult review={review} />
            </>
          )}
        </div>

        <aside className={styles.calculationInspector}>
          <p className={styles.eyebrow}>控制边界</p>
          <h3>法律选择由律师批准</h3>
          <ul>
            <li>系统不自行认定借款、付款或利息的法律性质。</li>
            <li>系统不自行选择利率上限、过渡规则或起止日期。</li>
            <li>任何上游事实、证据或规则变化都会使正式结果失效。</li>
          </ul>
          <div className={styles.calculationTrace}>
            <span>数据来源</span>
            <code>{review?.sourceLabel ?? caseDataSourceConfig.label}</code>
            <small>{review?.snapshotHash ? `快照 ${review.snapshotHash.slice(0, 16)}…` : "未取得可核验快照"}</small>
          </div>
        </aside>
      </div>
    </section>
  );
}

function CalculationAssumptions({ review }: { review: CalculationReviewView }) {
  return (
    <section className={styles.assumptionBlock} aria-labelledby="calculation-assumptions">
      <div className={styles.sectionHeading}>
        <div>
          <p className={styles.eyebrow}>已批准参数</p>
          <h3 id="calculation-assumptions">计算前提</h3>
        </div>
        <span>{review.sourceKind === "synthetic-alpha" ? "合成数据" : `案件版本 ${review.matterVersion}`}</span>
      </div>
      <div className={styles.assumptionGrid}>
        <dl>
          <div><dt>计算区间</dt><dd>{day(review.startDate)} — {day(review.endDate)}</dd></div>
          <div><dt>期间边界</dt><dd>[起算日，截止日)</dd></div>
        </dl>
        <dl>
          <div><dt>日计数</dt><dd>实际日数 / 365 固定分母</dd></div>
          <div><dt>冲抵顺序</dt><dd>{allocationPolicy(review.allocationPolicy)}</dd></div>
        </dl>
        <dl>
          <div><dt>债务单元</dt><dd>{review.obligationId}</dd></div>
          <div><dt>计算引擎</dt><dd>{review.engineVersion}</dd></div>
        </dl>
      </div>
    </section>
  );
}

function RuleTrace({ review }: { review: CalculationReviewView }) {
  return (
    <section className={styles.ruleBlock} aria-labelledby="rule-segments">
      <div className={styles.sectionHeading}>
        <div>
          <p className={styles.eyebrow}>规则与期间回链</p>
          <h3 id="rule-segments">进入本次计算的规则期间</h3>
        </div>
        <span>{review.sourceKind === "synthetic-alpha" ? "尚非法律结论" : "已绑定法律规则包"}</span>
      </div>
      <div className={styles.ruleRows}>
        {review.lineItems.map((item) => (
          <div className={styles.ruleRow} key={item.lineSequence}>
            <span className={styles.ruleOrdinal}>{String(item.lineSequence).padStart(2, "0")}</span>
            <div><strong>{item.periodStart} 至 {item.periodEnd}</strong><small>{item.sourceRuleVersion}</small></div>
            <span>{rate(item.annualRate)}</span>
            <em>{item.dayCount} 日</em>
          </div>
        ))}
      </div>
    </section>
  );
}

function CalculationResult({ review }: { review: CalculationReviewView }) {
  return (
    <section className={styles.calculationResult} aria-label="计算结果">
      <div className={styles.resultHeader}>
        <div>
          <p className={styles.eyebrow}>{review.sourceKind === "synthetic-alpha" ? "本机合成计算结果" : "正式持久化计算结果"}</p>
          <h3>期间明细与逐笔冲抵</h3>
        </div>
        <span className={review.independentCheckMatch ? styles.checkPassed : styles.checkFailed}>
          {review.independentCheckMatch ? "独立复算一致" : "独立复算未通过"}
        </span>
      </div>

      <div className={styles.resultNumbers}>
        <div><span>期末本金余额</span><strong>{cny(review.remainingPrincipal)}</strong></div>
        <div><span>累计计提利息</span><strong>{cny(review.totalInterestAccrued)}</strong></div>
        <div><span>已冲抵利息</span><strong>{cny(review.totalInterestPaid)}</strong></div>
        <div><span>未付利息</span><strong>{cny(review.remainingUnpaidInterest)}</strong></div>
      </div>

      <div className={styles.calculationTable} role="table" aria-label="计算期间明细">
        <div className={`${styles.calculationTableRow} ${styles.calculationTableHead}`} role="row">
          <span>期间</span><span>期初本金</span><span>年利率</span><span>日数</span><span>本期利息</span><span>规则来源</span>
        </div>
        {review.lineItems.map((item) => (
          <div className={styles.calculationTableRow} role="row" key={item.lineSequence}>
            <span>{day(item.periodStart)} — {day(item.periodEnd)}</span>
            <span>{cny(item.openingPrincipal)}</span>
            <span>{rate(item.annualRate)}</span>
            <span>{item.dayCount}</span>
            <strong>{cny(item.accruedInterest)}</strong>
            <span>{item.sourceRuleVersion}</span>
          </div>
        ))}
      </div>

      <div className={styles.paymentResult}>
        <div>
          <p className={styles.eyebrow}>逐笔还款</p>
          <h4>每次冲抵后本金重新进入下一期间</h4>
        </div>
        {review.paymentAllocations.length === 0 ? (
          <p>本计算区间内没有进入正式计算的已批准还款。</p>
        ) : review.paymentAllocations.map((allocation) => (
          <dl key={allocation.allocationSequence}>
            <div><dt>付款</dt><dd>{allocation.effectiveDate} · {cny(allocation.paymentAmount)}</dd></div>
            <div><dt>冲抵利息</dt><dd>{cny(allocation.allocatedInterest)}</dd></div>
            <div><dt>冲抵本金</dt><dd>{cny(allocation.allocatedPrincipal)}</dd></div>
            <div><dt>剩余未分配</dt><dd>{cny(allocation.unappliedAmount)}</dd></div>
            <div><dt>证据</dt><dd>{allocation.evidenceIds.join("、")}</dd></div>
          </dl>
        ))}
      </div>

      <p className={styles.resultHash}>
        法律规则包 {review.legalBundleId}、案件输入与计算输出均已哈希绑定；币种固定显示为人民币 / CNY。
      </p>
    </section>
  );
}
