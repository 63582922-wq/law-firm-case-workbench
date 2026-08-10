"use client";

import { useEffect, useState } from "react";
import {
  activateDesktopEnrollment,
  disableLocalEnrollment,
  importSignedEnrollmentPackage,
  initializeDesktopInstallation,
  readDesktopEnrollmentVaultStatus,
  renewDesktopEnrollment,
  resolvePendingDesktopEnrollment,
  revokeDesktopEnrollment,
} from "@/lib/desktop-bridge";
import type { DesktopEnrollmentVaultStatus, DesktopRuntimeStatus } from "@/lib/desktop-bridge";
import { AgentCapabilities } from "@/components/agent-capabilities";
import styles from "./case-workbench.module.css";

export function IdentitySecurityWorkbench({
  desktopRuntime,
}: {
  desktopRuntime: DesktopRuntimeStatus | null;
}) {
  const [vaultStatus, setVaultStatus] = useState<DesktopEnrollmentVaultStatus | null>(null);
  const [vaultBusy, setVaultBusy] = useState<"initialize" | "activate" | "import" | "renew" | "revoke" | "resolve" | "disable" | null>(null);
  const [vaultMessage, setVaultMessage] = useState<string | null>(null);
  const [disableArmed, setDisableArmed] = useState(false);
  const [revokeArmed, setRevokeArmed] = useState(false);
  const processReady = desktopRuntime?.phase === "READY";
  const trustReady = desktopRuntime?.enrollmentTrustPhase === "READY";
  const identityEnrolled = desktopRuntime?.identityPhase === "ENROLLED";
  const sessionReady = desktopRuntime?.sessionPhase === "READY";
  const signedCredentialSaved = vaultStatus?.phase === "CREDENTIAL_SAVED_VERIFIED";
  const persistenceConfigured = desktopRuntime?.persistencePhase === "CONFIGURED";
  const caseAccessReady = processReady && trustReady && identityEnrolled && sessionReady && persistenceConfigured;

  useEffect(() => {
    let cancelled = false;
    async function refreshVault() {
      try {
        const status = await readDesktopEnrollmentVaultStatus();
        if (!cancelled) setVaultStatus(status);
      } catch {
        if (!cancelled) {
          setVaultStatus({
            phase: "UNAVAILABLE",
            message: "无法读取 macOS Keychain 状态；案件访问保持禁用。",
            installationInitialized: false,
            enrollmentEnvelopePresent: false,
          });
        }
      }
    }
    void refreshVault();
    return () => {
      cancelled = true;
    };
  }, []);

  async function initializeVault() {
    setVaultBusy("initialize");
    setVaultMessage(null);
    try {
      const status = await initializeDesktopInstallation();
      setVaultStatus(status);
      setVaultMessage("本机安装秘密已写入 macOS Keychain；尚未取得律所签名登记。没有上传任何案件材料。");
    } catch (error) {
      setVaultMessage(readableError(error, "本机安全存储初始化失败，未启用身份。"));
    } finally {
      setVaultBusy(null);
    }
  }

  async function clearLocalEnrollment() {
    if (!disableArmed) {
      setDisableArmed(true);
      setVaultMessage("再次点击将只删除本机登记凭证；不会删除安装秘密，也不会代表律所服务端已撤销。 ");
      return;
    }
    setVaultBusy("disable");
    try {
      const status = await disableLocalEnrollment();
      setVaultStatus(status);
      setVaultMessage(`${status.message} 请重启桌面应用，由当前生产信任目录重新验签并建立短时会话。`);
      setDisableArmed(false);
    } catch (error) {
      setVaultMessage(readableError(error, "本机登记未能清除；远程撤销状态没有改变。"));
    } finally {
      setVaultBusy(null);
    }
  }

  async function importEnrollment() {
    setVaultBusy("import");
    setVaultMessage(null);
    try {
      const status = await importSignedEnrollmentPackage();
      setVaultStatus(status);
      setVaultMessage(status.message);
    } catch (error) {
      setVaultMessage(readableError(error, "律所登记包未能通过验签；未写入 Keychain。"));
    } finally {
      setVaultBusy(null);
    }
  }

  async function activateEnrollment() {
    setVaultBusy("activate");
    setVaultMessage(null);
    try {
      const status = await activateDesktopEnrollment();
      setVaultStatus(status);
      setVaultMessage(status.message);
    } catch (error) {
      setVaultMessage(readableError(error, "律所登记未能激活；激活码和签名凭证均未写入页面状态。"));
    } finally {
      setVaultBusy(null);
    }
  }

  async function renewEnrollment() {
    setVaultBusy("renew");
    setVaultMessage(null);
    try {
      const status = await renewDesktopEnrollment();
      setVaultStatus(status);
      setVaultMessage(status.message);
    } catch (error) {
      setVaultMessage(readableError(error, "律所签名登记未能续期；当前凭证没有被覆盖。"));
    } finally {
      setVaultBusy(null);
    }
  }

  async function revokeEnrollment() {
    if (!revokeArmed) {
      setRevokeArmed(true);
      setDisableArmed(false);
      setVaultMessage("再次点击将联系律所服务端撤销当前登记；只有收到匹配回执后才删除本机凭证并停止会话。");
      return;
    }
    setVaultBusy("revoke");
    setVaultMessage(null);
    try {
      const status = await revokeDesktopEnrollment();
      setVaultStatus(status);
      setVaultMessage(`${status.message} 请重新启动桌面应用。`);
      setRevokeArmed(false);
    } catch (error) {
      setVaultMessage(readableError(error, "远程撤销未取得有效回执；本机凭证没有被当作已撤销。"));
    } finally {
      setVaultBusy(null);
    }
  }

  async function resolvePendingEnrollment() {
    setVaultBusy("resolve");
    setVaultMessage(null);
    try {
      const status = await resolvePendingDesktopEnrollment();
      setVaultStatus(status);
      setVaultMessage(status.message);
    } catch (error) {
      setVaultMessage(readableError(error, "尚未取得确定的远程结果；操作号仍安全保留，请稍后再次查询。"));
    } finally {
      setVaultBusy(null);
    }
  }

  return (
    <section className={styles.securityArea} aria-label="身份与安全">
      <header className={styles.securityHeading}>
        <div>
          <p className={styles.eyebrow}>本机身份边界</p>
          <h2>先证明律师身份，再核验本案权限</h2>
          <p>系统不会根据电脑用户名、文件夹名称或页面自选角色授予权限。</p>
        </div>
        <span className={caseAccessReady ? styles.statusPill : styles.securityBlockedPill}>{caseAccessReady ? "受控案件会话已就绪" : "真实案件未启用"}</span>
      </header>

      <div className={styles.securityStatusGrid}>
        <StatusCell
          label="桌面受控进程"
          value={processReady ? "已就绪" : desktopRuntime?.phase === "STARTING" ? "核验中" : "不可用"}
          note={processReady ? "动态数字 loopback；退出应用即终止" : desktopRuntime?.message ?? "请从桌面应用检查本机服务"}
          state={processReady ? "ready" : "blocked"}
        />
        <StatusCell
          label="生产信任目录"
          value={trustReady ? "核验通过" : desktopRuntime?.enrollmentTrustPhase === "BLOCKED" ? "核验失败" : "未配置"}
          note={trustReady ? "律所签发公钥、有效期与回滚链已核验" : "安装包不内置测试密钥，也不接受页面自填公钥"}
          state={trustReady ? "ready" : "blocked"}
        />
        <StatusCell
          label="律所签名登记"
          value={identityEnrolled ? "已登记" : signedCredentialSaved ? "已验签保存" : "未登记"}
          note={identityEnrolled ? sessionReady ? "已重新验签并建立最长 30 分钟本机会话" : desktopRuntime?.sessionPhase === "EXPIRED" ? "短时会话已到期；重启后重新核验" : "登记已验签，但本机会话尚未就绪" : signedCredentialSaved ? "已保存；重启后将按当前信任目录重新验签" : desktopRuntime?.identityPhase === "NOT_ENROLLED" ? "本机服务已确认没有可用律师登记" : "尚未取得受信签发状态"}
          state={identityEnrolled && sessionReady ? "ready" : "blocked"}
        />
        <StatusCell
          label="案件数据库"
          value={persistenceConfigured ? "已连接" : "未配置"}
          note={desktopRuntime?.persistencePhase === "NOT_CONFIGURED" ? "本机服务已确认未装配专用数据库" : "尚未取得持久化状态"}
          state={persistenceConfigured ? "ready" : "blocked"}
        />
        <StatusCell
          label="案件访问"
          value={caseAccessReady ? "已放行" : "保持禁用"}
          note={caseAccessReady ? "WebView 只在内存中取得短时令牌；每个案件仍由数据库逐案授权" : "任一前置门未通过时，不显示真实案卷，也不回退合成结果"}
          state={caseAccessReady ? "ready" : "blocked"}
        />
      </div>

      <div className={styles.securityColumns}>
        <section className={styles.securityPanel} aria-labelledby="enrollment-flow-title">
          <div className={styles.securityPanelHeading}>
            <div>
              <p className={styles.eyebrow}>登记流程</p>
              <h3 id="enrollment-flow-title">五层信任必须按顺序成立</h3>
            </div>
            <span>{!trustReady ? "当前停在第 2 层" : !sessionReady ? "当前停在第 3 层" : "当前停在第 5 层"}</span>
          </div>
          <ol className={styles.securityFlow}>
            <FlowStep index="01" title="受监护桌面进程" state={processReady ? "已完成" : "未通过"}>
              Tauri 启动随应用分发的本机服务并核验随机挑战、PID 与动态端口。
            </FlowStep>
            <FlowStep index="02" title="生产信任目录" state={trustReady ? "已通过" : "未配置"}>
              安装包只固定离线根公钥；门限签名目录提供当前签发公钥、服务地址、证书固定值、有效期和撤销状态。
            </FlowStep>
            <FlowStep index="03" title="律所核验并签发" state={identityEnrolled ? "已重新验签" : "等待真实服务"}>
              律所管理员核验律师后签发短期凭证；用户不能在页面选择律所、人员或案件角色。
            </FlowStep>
            <FlowStep index="04" title="本机 Keychain 与短时会话" state={sessionReady ? "已完成" : "未开始"}>
              凭证与 32 字节安装秘密分开保存；复制凭证到另一台电脑不能登录。当前：{vaultStatus?.message ?? "请从桌面版读取 Keychain 状态。"}
            </FlowStep>
            <FlowStep index="05" title="数据库逐案授权" state="未开始">
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

      <AgentCapabilities />

      <section className={styles.securityActions} aria-labelledby="security-actions-title">
        <div>
          <p className={styles.eyebrow}>本机操作</p>
          <h3 id="security-actions-title">先准备安全存储，再由律所安全签发</h3>
          <p>初始化只在本机 Keychain 生成安装秘密；一次性激活码只进入 macOS 原生密码框，并由受信律所服务决定身份和角色。</p>
        </div>
        <div className={styles.securityActionButtons}>
          {vaultStatus?.phase === "REMOTE_OPERATION_PENDING" ? (
            <button
              disabled={!trustReady || vaultBusy !== null}
              onClick={() => void resolvePendingEnrollment()}
              type="button"
            >
              {vaultBusy === "resolve" ? "正在核对远程结果…" : "查询待决远程操作"}
            </button>
          ) : null}
          <button
            disabled={vaultBusy !== null || vaultStatus === null || vaultStatus.installationInitialized || vaultStatus.phase === "REMOTE_OPERATION_PENDING"}
            onClick={() => void initializeVault()}
            type="button"
          >
            {vaultBusy === "initialize" ? "正在写入并复核…" : vaultStatus?.installationInitialized ? "本机安全存储已就绪" : "初始化本机安全存储"}
          </button>
          {vaultStatus?.installationInitialized && !vaultStatus.enrollmentEnvelopePresent && vaultStatus.phase !== "REMOTE_OPERATION_PENDING" ? (
            <button
              disabled={!trustReady || vaultBusy !== null}
              onClick={() => void activateEnrollment()}
              type="button"
              title={trustReady ? "激活码只进入 macOS 原生安全输入框" : "需要生产信任目录和律所签发服务"}
            >
              {vaultBusy === "activate" ? "正在等待原生安全输入…" : "使用一次性激活码登记"}
            </button>
          ) : null}
          <button
            disabled={!trustReady || !vaultStatus?.installationInitialized || vaultBusy !== null || vaultStatus.phase === "REMOTE_OPERATION_PENDING"}
            onClick={() => void importEnrollment()}
            type="button"
            title={trustReady ? "只从原生文件选择器读取 .lawenroll 登记包" : "需要生产信任目录和律所签发服务"}
          >
            {vaultBusy === "import" ? "正在受信验签…" : "导入律所签名登记包"}
          </button>
          {vaultStatus?.enrollmentEnvelopePresent && vaultStatus.phase !== "REMOTE_OPERATION_PENDING" ? (
            <button
              disabled={!trustReady || vaultBusy !== null}
              onClick={() => void renewEnrollment()}
              type="button"
            >
              {vaultBusy === "renew" ? "正在联系律所续期…" : "续期律所签名登记"}
            </button>
          ) : null}
          {vaultStatus?.enrollmentEnvelopePresent && vaultStatus.phase !== "REMOTE_OPERATION_PENDING" ? (
            <button
              className={styles.securityDangerButton}
              disabled={!trustReady || vaultBusy !== null}
              onClick={() => void revokeEnrollment()}
              type="button"
            >
              {vaultBusy === "revoke" ? "正在确认远程撤销…" : revokeArmed ? "确认远程撤销登记" : "远程撤销登记"}
            </button>
          ) : null}
          {vaultStatus?.enrollmentEnvelopePresent && vaultStatus.phase !== "REMOTE_OPERATION_PENDING" ? (
            <button
              className={styles.securityDangerButton}
              disabled={vaultBusy !== null}
              onClick={() => {
                setRevokeArmed(false);
                void clearLocalEnrollment();
              }}
              type="button"
            >
              {vaultBusy === "disable" ? "正在清除…" : disableArmed ? "确认只停用本机" : "只停用本机登记"}
            </button>
          ) : null}
        </div>
        {vaultMessage ? <p className={styles.securityActionMessage} role="status">{vaultMessage}</p> : null}
      </section>

      <div className={styles.securityNotice} role="status">
        <strong>不需要你提供模型 API Key</strong>
        <span>当前缺的是律所运营方的生产信任根和签发服务，不是普通用户的 OpenAI API Key。在线激活码只进入 macOS 原生密码框；离线登记包只由系统文件选择器读取。两条路径都必须先受信验签，再按当前 Keychain 状态原子写入。</span>
      </div>
    </section>
  );
}

function readableError(error: unknown, fallback: string): string {
  if (typeof error === "string" && error.trim()) return error;
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
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
