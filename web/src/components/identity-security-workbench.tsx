import type { DesktopRuntimeStatus } from "@/lib/desktop-bridge";
import styles from "./case-workbench.module.css";

export function IdentitySecurityWorkbench({
  desktopRuntime,
}: {
  desktopRuntime: DesktopRuntimeStatus | null;
}) {
  const processReady = desktopRuntime?.phase === "READY";
  const identityEnrolled = false;
  const persistenceConfigured = false;

  return (
    <section className={styles.securityArea} aria-label="身份与安全">
      <header className={styles.securityHeading}>
        <div>
          <p className={styles.eyebrow}>本机身份边界</p>
          <h2>先证明律师身份，再核验本案权限</h2>
          <p>系统不会根据电脑用户名、文件夹名称或页面自选角色授予权限。</p>
        </div>
        <span className={styles.securityBlockedPill}>真实案件未启用</span>
      </header>

      <div className={styles.securityStatusGrid}>
        <StatusCell
          label="桌面受控进程"
          value={processReady ? "已就绪" : desktopRuntime?.phase === "STARTING" ? "核验中" : "不可用"}
          note={processReady ? "动态数字 loopback；退出应用即终止" : desktopRuntime?.message ?? "请从桌面应用检查本机服务"}
          state={processReady ? "ready" : "blocked"}
        />
        <StatusCell
          label="律所签名登记"
          value={identityEnrolled ? "已登记" : "未登记"}
          note={desktopRuntime?.identityPhase === "NOT_ENROLLED" ? "本机服务已确认没有可用律师登记" : "尚未取得受信签发状态"}
          state="blocked"
        />
        <StatusCell
          label="案件数据库"
          value={persistenceConfigured ? "已连接" : "未配置"}
          note={desktopRuntime?.persistencePhase === "NOT_CONFIGURED" ? "本机服务已确认未装配专用数据库" : "尚未取得持久化状态"}
          state="blocked"
        />
        <StatusCell
          label="案件访问"
          value="保持禁用"
          note="任一前置门未通过时，不显示真实案卷，也不回退合成结果"
          state="blocked"
        />
      </div>

      <div className={styles.securityColumns}>
        <section className={styles.securityPanel} aria-labelledby="enrollment-flow-title">
          <div className={styles.securityPanelHeading}>
            <div>
              <p className={styles.eyebrow}>登记流程</p>
              <h3 id="enrollment-flow-title">四层信任必须按顺序成立</h3>
            </div>
            <span>当前停在第 2 层</span>
          </div>
          <ol className={styles.securityFlow}>
            <FlowStep index="01" title="受监护桌面进程" state={processReady ? "已完成" : "未通过"}>
              Tauri 启动随应用分发的本机服务并核验随机挑战、PID 与动态端口。
            </FlowStep>
            <FlowStep index="02" title="律所核验并签发" state="等待真实服务">
              律所管理员或统一身份服务核验律师后，以受信 Ed25519 私钥签发短期凭证。
            </FlowStep>
            <FlowStep index="03" title="本机 Keychain 绑定" state="未开始">
              凭证与 32 字节安装秘密分开保存；复制凭证到另一台电脑不能登录。
            </FlowStep>
            <FlowStep index="04" title="数据库逐案授权" state="未开始">
              每次读写重新检查在职状态、未撤销的本案角色和律所隔离策略。
            </FlowStep>
          </ol>
        </section>

        <aside className={styles.securityPanel} aria-labelledby="security-boundary-title">
          <div className={styles.securityPanelHeading}>
            <div>
              <p className={styles.eyebrow}>禁止绕过</p>
              <h3 id="security-boundary-title">这些信息不能授予权限</h3>
            </div>
          </div>
          <ul className={styles.securityBoundaryList}>
            <li><strong>页面填写的姓名或角色</strong><span>只能作为普通文本，不能成为身份</span></li>
            <li><strong>macOS 用户名或设备名</strong><span>只说明本机环境，不证明执业身份</span></li>
            <li><strong>案卷文件夹名称</strong><span>只定义材料范围，不证明案件委托</span></li>
            <li><strong>AI 推断的律师关系</strong><span>永远不能创建、提升或撤销权限</span></li>
          </ul>
        </aside>
      </div>

      <div className={styles.securityNotice} role="status">
        <strong>为什么现在没有“立即登记”按钮</strong>
        <span>真实律所签发服务和生产公钥尚未部署。此时开放导入或自助选角色会制造假登录，因此界面只展示可验证状态，不提供无效操作。</span>
      </div>
    </section>
  );
}

function StatusCell({
  label,
  value,
  note,
  state,
}: {
  label: string;
  value: string;
  note: string;
  state: "ready" | "blocked";
}) {
  return (
    <div className={state === "ready" ? styles.securityStatusReady : styles.securityStatusBlocked}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
    </div>
  );
}

function FlowStep({
  index,
  title,
  state,
  children,
}: {
  index: string;
  title: string;
  state: string;
  children: React.ReactNode;
}) {
  return (
    <li>
      <span>{index}</span>
      <div><strong>{title}</strong><p>{children}</p></div>
      <em>{state}</em>
    </li>
  );
}
