/** Business briefs only: these never authorize tools, facts or submission. */
export const CASE_TASK_PRESETS = [
  {
    id: "risk", label: "分析案件风险",
    objective: "审阅本案现有材料，分析主要争点、证据强弱、对方可能提出的意见和可选应对方案，明确仍需律师决定的事项。",
    criteria: "每项关键判断列明来源并区分事实与假设\n列出主要风险、反证和分析限制\n给出补证问题、应对选项和下一步工作清单",
  },
  {
    id: "evidence", label: "核查证据与缺口",
    objective: "围绕本案诉请核查现有证据，找出材料之间的矛盾、不能证明的事项和需要补充的证据，并说明补证用途。",
    criteria: "逐项说明材料能够和不能证明什么，并列明来源\n区分缺少材料、事实冲突与需要律师判断的事项\n形成按重要性排序的补证清单，不自行确认争议事实",
  },
  {
    id: "response", label: "准备应诉方案",
    objective: "结合本案已确认代理情境和现有材料，逐项分析对方诉请，提出应诉理由、证据组织建议和文书准备清单，标明待核事实与法律依据。",
    criteria: "逐项回应诉请，列明支持来源、反证和风险\n形成应诉方案、补证清单与需要律师决定的事项\n说明文书起草所需条件；不把未核实内容写成正式立场",
  },
] as const;

export const DEFAULT_CASE_TASK = CASE_TASK_PRESETS[0];
