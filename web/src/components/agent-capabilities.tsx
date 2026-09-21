import manifest from "@/lib/case-skill-manifest.json";
import styles from "./case-workbench.module.css";

type CapabilityManifest = {
  schema_version: string;
  skills: {
    skill_id: string;
    version: string;
    title: string;
    maturity: "IMPLEMENTED" | "GATED" | "PLANNED";
    approval_gate: "NONE" | "MATERIAL_SCOPE" | "LAWYER_REVIEW" | "RELEASE_LOCK";
    required_scopes: string[];
    allowed_tools: string[];
    output_kind: string;
    prohibited_actions: string[];
  }[];
};

const capabilities = manifest as CapabilityManifest;

export function AgentCapabilities() {
  const implemented = capabilities.skills.filter((item) => item.maturity === "IMPLEMENTED").length;
  const gated = capabilities.skills.filter((item) => item.maturity === "GATED").length;
  return (
    <section className={styles.agentCapabilities} aria-label="Agent 受控技能">
      <header className={styles.agentCapabilitiesHeading}>
        <div>
          <p className={styles.eyebrow}>Agent 受控技能</p>
          <h3>像助手一样工作，但不获得电脑管理员权限</h3>
          <p>能力清单直接由本机策略注册表生成。模型只能提出动作；代码负责范围、审批、哈希和审计。</p>
        </div>
        <dl>
          <div><dt>已启用</dt><dd>{implemented}</dd></div>
          <div><dt>受门禁保护</dt><dd>{gated}</dd></div>
        </dl>
      </header>
      <div className={styles.agentCapabilityList}>
        {capabilities.skills.map((skill) => (
          <article key={skill.skill_id}>
            <div>
              <strong>{skill.title}</strong>
              <small>{skill.output_kind} · v{skill.version}</small>
            </div>
            <span className={skill.maturity === "IMPLEMENTED" ? styles.agentSkillReady : styles.agentSkillGated}>
              {skill.maturity === "IMPLEMENTED" ? "已启用" : skill.maturity === "GATED" ? "安全门禁" : "待开发"}
            </span>
            <span>{approvalLabel(skill.approval_gate)}</span>
            <code>{scopeLabel(skill.required_scopes)}</code>
          </article>
        ))}
      </div>
      <p className={styles.agentCapabilitiesFootnote}>无论状态如何，Agent 都不能直接读任意路径、执行 Shell、删除原件、上传案卷或替代律师作法律确认。</p>
    </section>
  );
}

function approvalLabel(value: CapabilityManifest["skills"][number]["approval_gate"]) {
  const labels = {
    NONE: "无需额外确认",
    MATERIAL_SCOPE: "材料范围已批准",
    LAWYER_REVIEW: "须律师复核",
    RELEASE_LOCK: "须锁定提交版",
  } as const;
  return labels[value];
}

function scopeLabel(scopes: string[]) {
  const labels: Record<string, string> = {
    CASE_READ: "本案只读",
    MANAGED_DERIVATIVE_WRITE: "受管派生件",
    PUBLIC_RESEARCH_READ: "公开研究",
    FORMAL_CALCULATION: "确定性计算",
    COURT_RELEASE: "法院交付",
  };
  return scopes.map((scope) => labels[scope] ?? scope).join(" · ");
}
