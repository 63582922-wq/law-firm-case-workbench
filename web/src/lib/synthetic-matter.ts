export type EvidencePage = {
  page: number;
  date: string;
  counterpart: string;
  amount: string;
  confidence: "已核验" | "待人工决定" | "需补材料";
  note: string;
};

/**
 * Alpha 阶段只允许这份合成数据进入界面。真实案件材料、联系人和金额
 * 必须在后续经权限、留痕和试点验收后，才可以接入持久化服务。
 */
export const syntheticMatter = {
  matterNo: "演示-2026-008",
  title: "民间借贷纠纷 · 还款材料核对（演示）",
  client: "演示当事人",
  court: "演示法院",
  opponent: "演示对方",
  currency: "CNY（人民币）",
  deadline: "2026年08月21日 17:00",
  principal: "¥ 80,000.00",
  reviewedInterest: "¥ 6,400.00",
  caseStage: "材料核验中",
  evidence: [
    { page: 15, date: "2019年06月17日", counterpart: "演示对方", amount: "¥ 800.00", confidence: "已核验", note: "付款方、收款方和金额均来自该页原始影像。" },
    { page: 16, date: "2019年07月17日", counterpart: "演示对方", amount: "¥ 800.00", confidence: "已核验", note: "与当页交易要素一致。" },
    { page: 17, date: "2019年08月17日", counterpart: "演示对方", amount: "¥ 800.00", confidence: "待人工决定", note: "检测到与第18页视觉相同；只能由律师确认保留或排除。" },
    { page: 18, date: "2019年08月17日", counterpart: "演示对方", amount: "¥ 800.00", confidence: "待人工决定", note: "检测到与第17页视觉相同；原件保留，不自动删除。" },
    { page: 19, date: "2019年09月17日", counterpart: "演示对方", amount: "¥ 800.00", confidence: "需补材料", note: "姓名相近但未满足自动归类条件，等待人工补证。" },
    { page: 20, date: "2019年10月19日", counterpart: "演示对方", amount: "¥ 800.00", confidence: "已核验", note: "与当页交易要素一致。" },
  ] satisfies EvidencePage[],
};

export const stageLabels = ["收件", "事实核验", "利息口径复核", "律师审批", "材料锁定", "导出前核验"];
