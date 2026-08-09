"use client";

import { useEffect, useState } from "react";
import {
  alphaCalculationApiBase,
  alphaCalculationPreviewRequest,
  type CalculationPreview,
} from "@/lib/synthetic-calculation";
import styles from "./case-workbench.module.css";

type PreviewState =
  | { status: "loading" }
  | { status: "ready"; preview: CalculationPreview }
  | { status: "blocked"; message: string };

const moneyFormatter = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function cny(value: string) {
  return moneyFormatter.format(Number(value));
}

function day(value: string) {
  return value.replaceAll("-", "年").replace(/年(\d{2})$/, "月$1日");
}

function rate(value: string) {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

export function CalculationWorkbench() {
  const [state, setState] = useState<PreviewState>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();

    async function loadPreview() {
      try {
        const response = await fetch(`${alphaCalculationApiBase}/v1/calculation-previews`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Alpha-Actor": "alpha_lead_lawyer",
          },
          body: JSON.stringify(alphaCalculationPreviewRequest),
          signal: controller.signal,
        });
        const payload = (await response.json()) as CalculationPreview | { detail?: string };
        if (!response.ok || !("independent_check_match" in payload)) {
          throw new Error("detail" in payload && payload.detail ? payload.detail : `服务返回 ${response.status}`);
        }
        setState({ status: "ready", preview: payload });
      } catch (error) {
        if (controller.signal.aborted) return;
        setState({
          status: "blocked",
          message: error instanceof Error ? error.message : "未取得计算服务响应",
        });
      }
    }

    void loadPreview();
    return () => controller.abort();
  }, []);

  return (
    <section className={styles.calculationArea} aria-label="利息测算">
      <header className={styles.calculationHeading}>
        <div>
          <p className={styles.eyebrow}>确定性计算 / 合成预览</p>
          <h2>利息与冲抵测算</h2>
          <p>页面不在浏览器计算金额；结果仅来自本机合成计算服务的已批准参数。</p>
        </div>
        <div className={styles.calculationStatus}>
          <span>币种</span>
          <strong>人民币 / CNY</strong>
          <small>正式计算仅允许 CNY</small>
        </div>
      </header>

      <div className={styles.calculationGrid}>
        <div className={styles.calculationMain}>
          <section className={styles.assumptionBlock} aria-labelledby="calculation-assumptions">
            <div className={styles.sectionHeading}>
              <div>
                <p className={styles.eyebrow}>已批准参数</p>
                <h3 id="calculation-assumptions">计算前提</h3>
              </div>
              <span>合成数据</span>
            </div>
            <div className={styles.assumptionGrid}>
              <dl>
                <div><dt>计算区间</dt><dd>2020年08月20日 — 2020年09月20日</dd></div>
                <div><dt>内部期间</dt><dd>[起算日，截止日)</dd></div>
              </dl>
              <dl>
                <div><dt>日计数</dt><dd>实际日数 / 365 固定分母</dd></div>
                <div><dt>冲抵顺序</dt><dd>先息后本（合成审批）</dd></div>
              </dl>
              <dl>
                <div><dt>金额舍入</dt><dd>每期间分币四舍五入</dd></div>
                <div><dt>付款事件</dt><dd>2020年09月04日 · ¥1,000.00</dd></div>
              </dl>
            </div>
          </section>

          <section className={styles.ruleBlock} aria-labelledby="rule-segments">
            <div className={styles.sectionHeading}>
              <div>
                <p className={styles.eyebrow}>参数来源</p>
                <h3 id="rule-segments">已批准的合成规则期间</h3>
              </div>
              <span>尚非法律结论</span>
            </div>
            <div className={styles.ruleRows}>
              {alphaCalculationPreviewRequest.rule_segments.map((segment, index) => (
                <div className={styles.ruleRow} key={segment.segment_id}>
                  <span className={styles.ruleOrdinal}>{String(index + 1).padStart(2, "0")}</span>
                  <div><strong>{segment.start_date} 至 {segment.end_date}</strong><small>合成规则版本：{segment.source_rule_version}</small></div>
                  <span>{rate(segment.annual_rate)}</span>
                  <em>合成适用锚点</em>
                </div>
              ))}
            </div>
          </section>

          {state.status === "loading" && (
            <section className={styles.calculationLoading} aria-live="polite">
              正在向本机合成计算服务请求预览；在服务返回前不显示金额结论。
            </section>
          )}

          {state.status === "blocked" && (
            <section className={styles.calculationBlocked} role="alert">
              <p className={styles.eyebrow}>测算已阻断</p>
              <h3>本机计算服务未返回可核验结果</h3>
              <p>{state.message}</p>
              <small>没有服务响应时，系统不得把静态示例作为计算结论显示。</small>
            </section>
          )}

          {state.status === "ready" && <CalculationResult preview={state.preview} />}
        </div>

        <aside className={styles.calculationInspector}>
          <p className={styles.eyebrow}>边界说明</p>
          <h3>本页不作法律选择</h3>
          <ul>
            <li>不推断借款、付款或利息的法律性质。</li>
            <li>不选择利率规则、法源版本或过渡路径。</li>
            <li>不替代律师批准，也不能进入正式文书。</li>
          </ul>
          <div className={styles.calculationTrace}>
            <span>服务地址</span>
            <code>{alphaCalculationApiBase}</code>
            <small>只允许本机合成 Alpha 开发环境使用；这不是生产身份认证。</small>
          </div>
        </aside>
      </div>
    </section>
  );
}

function CalculationResult({ preview }: { preview: CalculationPreview }) {
  return (
    <section className={styles.calculationResult} aria-label="本机计算结果">
      <div className={styles.resultHeader}>
        <div>
          <p className={styles.eyebrow}>本机计算服务结果</p>
          <h3>期间明细与冲抵结果</h3>
        </div>
        <span className={preview.independent_check_match ? styles.checkPassed : styles.checkFailed}>
          {preview.independent_check_match ? "独立复算一致" : "独立复算不一致"}
        </span>
      </div>

      <div className={styles.resultNumbers}>
        <div><span>期末本金余额</span><strong>{cny(preview.remaining_principal)}</strong></div>
        <div><span>累计计提利息</span><strong>{cny(preview.total_interest_accrued)}</strong></div>
        <div><span>已付利息</span><strong>{cny(preview.total_interest_paid)}</strong></div>
        <div><span>未付利息</span><strong>{cny(preview.remaining_unpaid_interest)}</strong></div>
      </div>

      <div className={styles.calculationTable} role="table" aria-label="计算期间明细">
        <div className={`${styles.calculationTableRow} ${styles.calculationTableHead}`} role="row">
          <span>期间</span><span>期初本金</span><span>年利率</span><span>日数</span><span>本期利息</span><span>规则来源</span>
        </div>
        {preview.line_items.map((item) => (
          <div className={styles.calculationTableRow} role="row" key={`${item.period_start}-${item.rule_segment_id}`}>
            <span>{day(item.period_start)} — {day(item.period_end)}</span>
            <span>{cny(item.opening_principal)}</span>
            <span>{rate(item.annual_rate)}</span>
            <span>{item.day_count}</span>
            <strong>{cny(item.accrued_interest)}</strong>
            <span>{item.source_rule_version}</span>
          </div>
        ))}
      </div>

      <div className={styles.paymentResult}>
        <div>
          <p className={styles.eyebrow}>付款冲抵</p>
          <h4>付款与证据回链</h4>
        </div>
        {preview.payment_allocations.map((allocation) => (
          <dl key={allocation.payment_event_id}>
            <div><dt>付款</dt><dd>{allocation.effective_date} · {cny(allocation.payment_amount)}</dd></div>
            <div><dt>冲抵利息</dt><dd>{cny(allocation.allocated_interest)}</dd></div>
            <div><dt>冲抵本金</dt><dd>{cny(allocation.allocated_principal)}</dd></div>
            <div><dt>证据</dt><dd>{allocation.evidence_ids.join("、")}</dd></div>
          </dl>
        ))}
      </div>

      <p className={styles.resultHash}>本次合成预览输入与输出均已哈希绑定。正式环境还须持久化、律师审批、规则来源核验及下游失效追踪。</p>
    </section>
  );
}
