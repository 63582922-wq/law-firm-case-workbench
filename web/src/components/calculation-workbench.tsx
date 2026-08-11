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
          message: error instanceof Error ? error.message : "暂时无法取得本案利息测算结果",
        });
      }
    }
    void load();
    void loadLegalReview()
      .then((review) => { if (active) setLegalReview(review); })
      .catch((reason: unknown) => { if (active) setScenarioNotice(reason instanceof Error ? reason.message : "暂时无法读取本案适用口径"); });
    return () => {
      active = false;
    };
  }, []);

  const persistent = caseDataSourceConfig.kind !== "synthetic-alpha";
  const review = state.status === "ready" ? state.review : null;

  async function createScenario() {
    if (!legalReview?.currentBundle || legalReview.matterVersion === null) return;
    if (!scenario.approved) {
      setScenarioNotice("请先确认：对应借款项目、测算期间、人民币币种和还款抵扣顺序均已核对。");
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
      setScenarioNotice(`本次利息测算及复核已完成；案件版本更新为 ${receipt.matterVersion}。`);
    } catch (reason: unknown) {
      setScenarioNotice(reason instanceof Error ? reason.message : "利息测算未完成");
    } finally {
      setScenarioBusy(false);
    }
  }

  return (
    <section className={styles.calculationArea} aria-label="利息计算">
      <header className={styles.calculationHeading}>
        <div>
          <p className={styles.eyebrow}>{persistent ? "利息测算" : "演示测算"}</p>
          <h2>利息核算与还款抵扣</h2>
          <p>{persistent ? "本页仅根据已核对的案件资料和已确认的适用口径生成测算；不自行认定事实或法律结论。" : "本页展示演示资料，不代表真实案件或法律结论。"}</p>
        </div>
        <div className={styles.calculationStatus}>
          <span>计算币种</span>
          <strong>人民币 / CNY</strong>
          <small>本案测算仅支持人民币金额</small>
        </div>
      </header>

      <div className={styles.calculationGrid}>
        <div className={styles.calculationMain}>
          {state.status === "loading" && (
            <section className={styles.calculationLoading} aria-live="polite">
              正在读取{persistent ? "已确认资料并生成利息测算" : "演示测算结果"}；完成前不显示金额结论。
            </section>
          )}

          {state.status === "blocked" && (
            <section className={styles.calculationBlocked} role="alert">
              <p className={styles.eyebrow}>暂不能出具测算</p>
              <h3>目前没有可核对的利息结果</h3>
              <p>请先检查案件资料、适用依据和本机工作台是否已就绪，然后重新打开本页。</p>
              <small>资料未就绪时，系统不会以示例金额或临时计算代替本案结果。</small>
              <button className={styles.candidateAction} onClick={() => window.location.reload()} type="button">重新载入本页</button>
            </section>
          )}

          {review?.status === "empty" && (
            <section className={styles.calculationBlocked} role="status">
              <p className={styles.eyebrow}>尚未开始测算</p>
              <h3>适用口径或对应借款项目尚未就绪</h3>
              <p>{review.emptyReason}</p>
              <small>请先核对收付款、款项用途、同日先后和适用期间，再由主办律师确认。</small>
            </section>
          )}

          {persistent && legalReview?.status === "reviewable" && (
            <form className={styles.formalCalculationForm} onSubmit={(event) => { event.preventDefault(); void createScenario(); }}>
              <div className={styles.formalCalculationHeading}><div><p className={styles.eyebrow}>开始利息测算</p><h3>确认本次测算范围</h3><small>利率、收付款金额、款项用途和人民币币种均来自已确认的案件资料；本页不能直接改写这些资料。</small></div><span>{legalReview.currentBundle ? `适用口径 v${legalReview.currentBundle.version}` : "尚无已确认适用口径"}</span></div>
              <div className={styles.formalCalculationFields}>
                <label><span>对应借款项目</span><input required value={scenario.obligationId} onChange={(event) => setScenario((prior) => ({ ...prior, obligationId: event.target.value }))} placeholder="填写已确认收付款对应的借款项目" /></label>
                <label><span>测算起日</span><input required type="date" value={scenario.startDate} onChange={(event) => setScenario((prior) => ({ ...prior, startDate: event.target.value }))} /></label>
                <label><span>测算止日</span><input required type="date" value={scenario.endDate} onChange={(event) => setScenario((prior) => ({ ...prior, endDate: event.target.value }))} /></label>
                <label><span>还款抵扣顺序</span><select value={scenario.allocationPolicy} onChange={(event) => setScenario((prior) => ({ ...prior, allocationPolicy: event.target.value as typeof prior.allocationPolicy }))}><option value="INTEREST_THEN_PRINCIPAL">先息后本</option><option value="PRINCIPAL_THEN_INTEREST">先本后息</option></select></label>
              </div>
              <label className={styles.formalCalculationCheck}><input checked={scenario.approved} onChange={(event) => setScenario((prior) => ({ ...prior, approved: event.target.checked }))} type="checkbox" /><span>我确认该借款项目已有本案已确认、已确定用途的人民币收付款；测算期间已由当前适用口径连续覆盖，抵扣顺序已由律师确认。</span></label>
              <div className={styles.formalCalculationActions}><button disabled={scenarioBusy || !legalReview.currentBundle} type="submit">{scenarioBusy ? "正在核算并复核…" : "生成利息测算"}</button><small>收付款、适用口径、币种、同日先后或复核条件不符合时，系统不会生成可提交的测算结果。</small></div>
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
          <p className={styles.eyebrow}>办案提示</p>
          <h3>利息口径须由律师确认</h3>
          <ul>
            <li>系统不自行认定借款、付款或利息的法律性质。</li>
            <li>系统不自行选择利率上限、过渡规则或测算起止日。</li>
            <li>案件事实、证据或适用口径变化后，本次测算需重新核对。</li>
          </ul>
          <div className={styles.calculationTrace}>
            <span>案件资料来源</span>
            <code>{review?.sourceLabel ?? caseDataSourceConfig.label}</code>
            <small>{review?.snapshotHash ? `已保存核对记录 ${review.snapshotHash.slice(0, 16)}…` : "尚未取得可核对的案件资料"}</small>
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
          <p className={styles.eyebrow}>已确认资料</p>
          <h3 id="calculation-assumptions">本次测算口径</h3>
        </div>
        <span>{review.sourceKind === "synthetic-alpha" ? "演示资料" : `案件版本 ${review.matterVersion}`}</span>
      </div>
      <div className={styles.assumptionGrid}>
        <dl>
          <div><dt>测算期间</dt><dd>{day(review.startDate)} — {day(review.endDate)}</dd></div>
          <div><dt>期间边界</dt><dd>[起算日，截止日)</dd></div>
        </dl>
        <dl>
          <div><dt>日计数</dt><dd>实际日数 / 365 固定分母</dd></div>
          <div><dt>冲抵顺序</dt><dd>{allocationPolicy(review.allocationPolicy)}</dd></div>
        </dl>
        <dl>
          <div><dt>借款项目</dt><dd>{review.obligationId}</dd></div>
          <div><dt>核算版本</dt><dd>{review.engineVersion}</dd></div>
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
          <p className={styles.eyebrow}>适用依据与期间</p>
          <h3 id="rule-segments">本次测算采用的利率期间</h3>
        </div>
        <span>{review.sourceKind === "synthetic-alpha" ? "不构成法律结论" : "已对应本案适用口径"}</span>
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
    <section className={styles.calculationResult} aria-label="利息测算结果">
      <div className={styles.resultHeader}>
        <div>
          <p className={styles.eyebrow}>{review.sourceKind === "synthetic-alpha" ? "演示测算结果" : "本案利息测算结果"}</p>
          <h3>分段明细与逐笔还款抵扣</h3>
        </div>
        <span className={review.independentCheckMatch ? styles.checkPassed : styles.checkFailed}>
          {review.independentCheckMatch ? "复核结果一致" : "复核未通过"}
        </span>
      </div>

      <div className={styles.resultNumbers}>
        <div><span>期末本金余额</span><strong>{cny(review.remainingPrincipal)}</strong></div>
        <div><span>累计计提利息</span><strong>{cny(review.totalInterestAccrued)}</strong></div>
        <div><span>已冲抵利息</span><strong>{cny(review.totalInterestPaid)}</strong></div>
        <div><span>未付利息</span><strong>{cny(review.remainingUnpaidInterest)}</strong></div>
      </div>

      <div className={styles.calculationTable} role="table" aria-label="利息测算分段明细">
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
          <p className={styles.eyebrow}>逐笔还款抵扣</p>
          <h4>每笔还款抵扣后，剩余本金进入下一期间</h4>
        </div>
        {review.paymentAllocations.length === 0 ? (
          <p>本次测算期间内没有可纳入的已确认还款。</p>
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
        本次测算已与案件资料、适用口径和复核结果对应保存；币种固定显示为人民币 / CNY。
      </p>
    </section>
  );
}
