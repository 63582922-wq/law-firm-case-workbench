#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金标准合成案件独立复算器
案件：合成民间借贷案（2025）京0105民初17532号（全部为合成/脱敏数据）

用法：python3 golden_calc.py

本工具是规格文档 docs/GOLDEN_CASE_SYNTHETIC.md 的配套确定性复算器，
用于验证利息与冲抵计算的工程实现。仅用于工程验证，不构成法律意见。

冻结规则（详见规格 §2，本文件只实现、不解释）：
  R1 计息：日利率 = 约定月利率 ÷ 30，按实际日历天数计息，算头不算尾，北京时间。
      区间内本金不变；每段利息独立四舍五入到分（ROUND_HALF_UP）。
  R2 上限：2020-08-20 起（含当日），受保护年利率上限 = 起诉时一年期 LPR(3.00%) × 4
      = 年 12% = 月 1%；此前受保护上限 = 年 24% = 月 2%。
  R3 冲抵顺序：费用 → 利息 → 本金（本案费用为 0，无约定时按《民法典》第 561 条）。
  R4 老区间（<2020-08-20）已付利息：年 24% 内受保护；24%–36% 为自然债务（已付不退
      不冲）；超过年 36%（月 3%，按付款时未偿本金计）部分冲抵本金。
  R5 新区间（≥2020-08-20）已付利息超过月 1% 部分，冲抵本金。
  R6 逾期利息：按借期内约定利率继续计（受上限约束）；L2 未定期限，随时可要求返还。
  R7 借款人单方注明“还本金”不改变法定冲抵顺序（除当事人另有约定外）。
  R8 多笔债务未指定清偿顺序时，按《民法典》第 560 条：先履行已到期债务（L1 先于 L2）。
  R9 所有金额以“分”为最小单位；每笔支付应用时对利息与冲抵额四舍五入到分。
"""
from decimal import Decimal, ROUND_HALF_UP
from datetime import date

CENT = Decimal('0.01')

def q(x):
    """四舍五入到分（ROUND_HALF_UP）。"""
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)

BOUNDARY = date(2020, 8, 20)   # 新区间起点（含当日）
FINAL    = date(2025, 6, 14)   # 利息暂计截止日（算头不算尾：至 2025-06-14 24:00）
R_OLD    = Decimal('0.02')     # 老区间受保护月利率（年24%）
R_NEW    = Decimal('0.01')     # 新区间受保护月利率（年12% = 起诉时 LPR3.00%×4）
R36      = Decimal('0.03')     # 老区间年36%线（月3%）
DIV      = Decimal('30')       # 日利率 = 月利率 ÷ 30

# ---------------------------------------------------------------------------
# 案件输入（全部合成数据）
# ---------------------------------------------------------------------------
LOANS = {
    'L1': dict(label='借款1（借条1）', principal=q(300000), disbursed=date(2019, 6, 3),
               agreed=Decimal('0.02'), due=date(2020, 6, 2)),
    'L2': dict(label='借款2（借条2）', principal=q(200000), disbursed=date(2019, 11, 15),
               agreed=Decimal('0.035'), due=None),
}

# 事件：(日期, 说明, 债务, 金额, 类型)
# 类型 interest=付息（触发老区间 36% 带逻辑）; repay=还款（先息后本）
EVENTS_BASE = [
    # ---- L1 付息（约定月息2分 = 6,000/月）----
    (date(2019, 7, 3),  'P1  微信转账 6,000 “6月利息”',           'L1', q(6000), 'interest'),
    (date(2019, 8, 3),  'P2  微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    (date(2019, 9, 3),  'P3  微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    (date(2019, 10, 3), 'P4  银行转账 6,000 摘要空',               'L1', q(6000), 'interest'),
    (date(2019, 11, 3), 'P5  微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    (date(2019, 12, 3), 'P6  微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    (date(2020, 1, 3),  'P7  微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    (date(2020, 2, 3),  'P8  微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    (date(2020, 3, 3),  'P9  微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    (date(2020, 4, 3),  'P10 微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    (date(2020, 5, 3),  'P11 微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    (date(2020, 6, 3),  'P12 微信转账 6,000 “利息”',             'L1', q(6000), 'interest'),
    # ---- L2 付息（约定月息3.5分 = 7,000/月，触发 36% 带冲抵）----
    (date(2019, 12, 15), 'Q1  银行转账 7,000 “12月息”',          'L2', q(7000), 'interest'),
    (date(2020, 1, 15),  'Q2  银行转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    (date(2020, 2, 15),  'Q3  微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    (date(2020, 3, 15),  'Q4  微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    (date(2020, 4, 15),  'Q5  微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    (date(2020, 5, 15),  'Q6  微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    (date(2020, 6, 15),  'Q7  微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    (date(2020, 7, 15),  'Q8  微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    (date(2020, 8, 15),  'Q9  微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    # ---- L1 本金/还款事件 ----
    (date(2020, 6, 10),  'R1  银行转账 50,000 “还本金”',          'L1', q(50000), 'repay'),
    (date(2020, 7, 3),   'P13 微信转账 6,000 “利息”',            'L1', q(6000), 'interest'),
    (date(2020, 8, 3),   'P14 微信转账 6,000 “利息”',            'L1', q(6000), 'interest'),
    # ---- L2 新区间付息（超过月1%部分冲本金）----
    (date(2020, 9, 15),  'Q10 微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    (date(2020, 10, 15), 'Q11 微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    (date(2020, 11, 15), 'Q12 微信转账 7,000 “利息”',            'L2', q(7000), 'interest'),
    # ---- 本金还款 ----
    (date(2021, 3, 1),   'R2  银行转账 100,000 “还王强借款”（未指定，法定顺序→L1）', 'L1', q(100000), 'repay'),
    (date(2021, 3, 1),   'R3  微信转账 10,000 “还第二笔”（指定→L2）', 'L2', q(10000), 'repay'),
    (date(2021, 5, 20),  'LM  李梅银行转账 10,000 摘要空（代付，确认→L1）', 'L1', q(10000), 'repay'),
    (date(2023, 8, 5),   'R4  银行转账 5,000 “还借款”（时效中断事件）', 'L1', q(5000), 'repay'),
    (date(2024, 11, 20), 'R5  微信转账 8,000 摘要空（确认→L1）',   'L1', q(8000), 'repay'),
]

# 情景开关事件
EVT_CASH = (date(2020, 12, 20), 'C1  现金 30,000（被告主张，无凭证，情景开关）', 'L1', q(30000), 'repay')
EVT_U2    = (date(2022, 9, 10), 'U2  微信转账 50,000 “周转款”（性质争议，情景A计入L1）', 'L1', q(50000), 'repay')

SCENARIOS = [
    ('S-A-1', dict(u2=True,  cash=False), 'U2计入L1本金；现金不认定'),
    ('S-A-2', dict(u2=True,  cash=True),  'U2计入L1本金；现金认定'),
    ('S-B-1', dict(u2=False, cash=False), 'U2系案外往来不计入；现金不认定'),
    ('S-B-2', dict(u2=False, cash=True),  'U2系案外往来不计入；现金认定'),
]

# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
def days_between(a, b):
    return (b - a).days

def accrual(principal, a, b):
    """区间 [a, b) 的受保护利息，跨 2020-08-20 分段，每段四舍五入到分。"""
    assert b >= a and principal >= 0
    if a >= BOUNDARY:
        return q(principal * R_NEW * days_between(a, b) / DIV)
    if b <= BOUNDARY:
        return q(principal * R_OLD * days_between(a, b) / DIV)
    return (q(principal * R_OLD * days_between(a, BOUNDARY) / DIV)
            + q(principal * R_NEW * days_between(BOUNDARY, b) / DIV))

def run_scenario(name, opts):
    loans = {k: dict(principal=v['principal'], arrears=Decimal('0'),
                     last=v['disbursed'], int_paid=Decimal('0'),
                     offset=Decimal('0'), prin_paid=Decimal('0'))
             for k, v in LOANS.items()}
    events = list(EVENTS_BASE)
    if opts['cash']:
        events.append(EVT_CASH)
    if opts['u2']:
        events.append(EVT_U2)
    events.sort(key=lambda e: (e[0], e[1]))
    trace = []
    for (d, desc, ln, amt, kind) in events:
        st = loans[ln]
        acc = accrual(st['principal'], st['last'], d)
        st['arrears'] += acc
        st['last'] = d
        pay = amt
        if kind == 'interest' and ln == 'L2' and d < BOUNDARY:
            int_paid = min(pay, st['arrears']); pay -= int_paid; st['arrears'] -= int_paid
            natural = min(pay, q(st['principal'] * (R36 - R_OLD)))
            pay -= natural
            offset = pay if pay > 0 else Decimal('0')
            st['principal'] -= offset; st['offset'] += offset
        else:
            int_paid = min(pay, st['arrears']); pay -= int_paid; st['arrears'] -= int_paid
            if pay > 0:
                st['principal'] -= pay; st['prin_paid'] += pay
            offset = Decimal('0')
        st['int_paid'] += int_paid
        trace.append((d, desc, ln, amt, acc, int_paid, offset,
                      loans[ln]['principal'], loans[ln]['arrears']))
    # 结至 FINAL
    final = {}
    for k, st in loans.items():
        acc = accrual(st['principal'], st['last'], FINAL)
        st['arrears'] += acc
        st['last'] = FINAL
        final[k] = dict(principal=st['principal'], arrears=st['arrears'],
                        int_paid=st['int_paid'], offset=st['offset'],
                        prin_paid=st['prin_paid'])
    return trace, final

def fmt(x):
    return f'{x:,.2f}'

def print_trace(trace):
    print('日期        事件                                                  债务  支付        本期计息      已付利息      超额冲本      本金余额        利息挂账')
    for (d, desc, ln, amt, acc, intp, off, prin, arr) in trace:
        print(f'{d.isoformat()}  {desc[:50]:<50}  {ln}  {fmt(amt):>12} {fmt(acc):>12} {fmt(intp):>12} {fmt(off):>12} {fmt(prin):>14} {fmt(arr):>12}')

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    print('=' * 130)
    print('金标准合成案件复算器  （2025）京0105民初17532号 · 全部合成数据 · 仅工程验证')
    print('冻结规则：月利率/30 × 实际天数，算头不算尾；2020-08-20 前上限月2%，之后月1%；')
    print('          老区间超年36%部分冲本金；新区间超月1%部分冲本金；先息后本。')
    print('=' * 130)

    default = SCENARIOS[0]
    trace, final = run_scenario(default[0], default[1])
    print(f'\n【默认情景 {default[0]}：{default[2]}】完整事件追踪：')
    print_trace(trace)

    print('\n\n【全情景汇总】（金额单位：元；利息暂计至 2025-06-14，算头不算尾）')
    hdr = f'{"情景":<8}{"说明":<28}{"L1本金":>14}{"L1未付息":>12}{"L2本金":>14}{"L2未付息":>12}{"合计本金":>14}{"合计未付息":>12}'
    print(hdr); print('-' * len(hdr))
    for name, opts, desc in SCENARIOS:
        _, f = run_scenario(name, opts)
        tp = f['L1']['principal'] + f['L2']['principal']
        ti = f['L1']['arrears'] + f['L2']['arrears']
        print(f'{name:<8}{desc:<28}{fmt(f["L1"]["principal"]):>14}{fmt(f["L1"]["arrears"]):>12}'
              f'{fmt(f["L2"]["principal"]):>14}{fmt(f["L2"]["arrears"]):>12}{fmt(tp):>14}{fmt(ti):>12}')

    print('\n【被告已履行情况】（默认情景）')
    for k in ('L1', 'L2'):
        f = final[k]
        print(f'  {LOANS[k]["label"]}：已付利息 {fmt(f["int_paid"])}，超额冲本 {fmt(f["offset"])}，'
              f'本金偿付 {fmt(f["prin_paid"])}，未偿本金 {fmt(f["principal"])}，未付利息挂账 {fmt(f["arrears"])}')

    # 原告主张参考值（其算法：本金未还、按约定利率全额计息、扣除其自认已收利息）
    print('\n【原告诉请参考值】（原告算法，非法院支持范围）')
    l1_days = days_between(date(2019, 6, 3), FINAL)
    l2_days = days_between(date(2019, 11, 15), FINAL)
    l1_int = q(Decimal(300000) * LOANS['L1']['agreed'] * l1_days / DIV) - q(84000)
    l2_int = q(Decimal(200000) * LOANS['L2']['agreed'] * l2_days / DIV) - q(63000)
    print(f'  本金合计 500,000.00；利息主张 L1 {fmt(l1_int)} + L2 {fmt(l2_int)} = {fmt(l1_int + l2_int)}；')
    print(f'  诉请合计 {fmt(Decimal(500000) + l1_int + l2_int)}（原告自认已收利息 147,000.00 已扣除）')

    print('\n【时效核查】（需律师确认催收/还款证据真实性后作为参数）')
    print('  L1：到期 2020-06-02，时效 3 年；R2(2021-03-01)、R4(2023-08-05) 部分还款构成中断，')
    print('      重新起算至 2026-08-05；起诉 2025-06-15 → 未过时效。')
    print('  L2：未定期限；催收 2022-12-05 + 合理宽限期 → 时效至 2026 年初；起诉 → 未过时效。')
