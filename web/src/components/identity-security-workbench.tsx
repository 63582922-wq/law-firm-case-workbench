"use client";

import { useEffect, useState } from "react";
import {
  activateDesktopEnrollment,
  configureDesktopQwenConnection,
  disableLocalEnrollment,
  configureDesktopModelProviderKey,
  importSignedEnrollmentPackage,
  initializeDesktopInstallation,
  readDesktopModelProviderStatuses,
  readDesktopEnrollmentVaultStatus,
  removeDesktopModelProviderKey,
  renewDesktopEnrollment,
  resolvePendingDesktopEnrollment,
  revokeDesktopEnrollment,
} from "@/lib/desktop-bridge";
import type { DesktopEnrollmentVaultStatus, DesktopModelProviderStatus, DesktopRuntimeStatus } from "@/lib/desktop-bridge";
import { AgentCapabilities } from "@/components/agent-capabilities";
import { AgentExecutionAudit } from "@/components/agent-execution-audit";
import { ExternalRequestAudit } from "@/components/external-request-audit";
import styles from "./case-workbench.module.css";

export function IdentitySecurityWorkbench({
  desktopRuntime,
}: {
  desktopRuntime: DesktopRuntimeStatus | null;
}) {
  const [vaultStatus, setVaultStatus] = useState<DesktopEnrollmentVaultStatus | null>(null);
  const [modelProviderStatuses, setModelProviderStatuses] = useState<DesktopModelProviderStatus[] | null>(null);
  const [vaultBusy, setVaultBusy] = useState<"initialize" | "activate" | "import" | "renew" | "revoke" | "resolve" | "disable" | null>(null);
  const [modelProviderBusy, setModelProviderBusy] = useState<DesktopModelProviderStatus["providerId"] | null>(null);
  const [modelProviderMessage, setModelProviderMessage] = useState<string | null>(null);
  const [qwenWorkspaceId, setQwenWorkspaceId] = useState("");
  const [qwenRegionId, setQwenRegionId] = useState<"cn-beijing" | "ap-southeast-1">("cn-beijing");
  const [qwenConnectionBusy, setQwenConnectionBusy] = useState(false);
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
  const localStandaloneReady = processReady
    && desktopRuntime?.workspaceMode === "LOCAL_STANDALONE"
    && desktopRuntime?.localWorkspacePhase === "READY";

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
            message: "无法读取本机配置记录；案件访问保持禁用。",
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

  useEffect(() => {
    let cancelled = false;
    async function refreshModelProviders() {
      try {
        const statuses = await readDesktopModelProviderStatuses();
        if (!cancelled) setModelProviderStatuses(statuses);
      } catch {
        if (!cancelled) setModelProviderStatuses([]);
      }
    }
    void refreshModelProviders();
    return () => {
      cancelled = true;
    };
  }, []);

  async function configureModelProvider(providerId: DesktopModelProviderStatus["providerId"]) {
    setModelProviderBusy(providerId);
    setModelProviderMessage(null);
    try {
      const status = await configureDesktopModelProviderKey(providerId);
      setModelProviderStatuses((current) => replaceModelProviderStatus(current, status));
      setModelProviderMessage(`${status.displayName} 的服务密钥已保存到本机系统钥匙串。密钥不会显示在页面、案件资料或处理记录中。`);
    } catch (error) {
      setModelProviderMessage(readableError(error, "服务密钥未保存。"));
    } finally {
      setModelProviderBusy(null);
    }
  }

  async function removeModelProvider(providerId: DesktopModelProviderStatus["providerId"]) {
    setModelProviderBusy(providerId);
    setModelProviderMessage(null);
    try {
      const status = await removeDesktopModelProviderKey(providerId);
      setModelProviderStatuses((current) => replaceModelProviderStatus(current, status));
      setModelProviderMessage(`${status.displayName} 的服务密钥已从本机系统钥匙串移除。`);
    } catch (error) {
      setModelProviderMessage(readableError(error, "服务密钥未移除。"));
    } finally {
      setModelProviderBusy(null);
    }
  }

  async function configureQwenConnection() {
    setQwenConnectionBusy(true);
    setModelProviderMessage(null);
    try {
      const status = await configureDesktopQwenConnection(qwenRegionId, qwenWorkspaceId);
      setModelProviderStatuses((current) => replaceModelProviderStatus(current, status));
      setQwenWorkspaceId("");
      setModelProviderMessage(`通义千问百炼已固定到${status.connectionLabel}。业务空间标识不会显示在页面、案件资料或处理记录中。`);
    } catch (error) {
      setModelProviderMessage(readableError(error, "百炼业务空间配置未保存。"));
    } finally {
      setQwenConnectionBusy(false);
    }
  }

  async function initializeVault() {
    setVaultBusy("initialize");
    setVaultMessage(null);
    try {
      const status = await initializeDesktopInstallation();
      setVaultStatus(status);
      setVaultMessage("本机安全信息已保存到系统钥匙串；尚未完成律所登记。没有上传任何案件材料。");
    } catch (error) {
      setVaultMessage(readableError(error, "本机安全存储初始化失败，未启用身份。"));
    } finally {
      setVaultBusy(null);
    }
  }

  async function clearLocalEnrollment() {
    if (!disableArmed) {
      setDisableArmed(true);
      setVaultMessage("再次点击将只停用这台电脑的律所登记；不会删除本机安全信息，也不会通知律所撤销登记。 ");
      return;
    }
    setVaultBusy("disable");
    try {
      const status = await disableLocalEnrollment();
      setVaultStatus(status);
      setVaultMessage(`${status.message} 请重启桌面应用，以当前律所授权来源重新确认并建立短时会话。`);
      setDisableArmed(false);
    } catch (error) {
      setVaultMessage(readableError(error, "本机登记未能停用；律所端的登记状态没有改变。"));
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
      setVaultMessage(readableError(error, "律所登记包未能确认；没有写入本机系统钥匙串。"));
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
      setVaultMessage(readableError(error, "律所登记未能启用；激活码和登记信息均没有保存在页面中。"));
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
      setVaultMessage(readableError(error, "律所登记未能更新；当前登记没有被覆盖。"));
    } finally {
      setVaultBusy(null);
    }
  }

  async function revokeEnrollment() {
    if (!revokeArmed) {
      setRevokeArmed(true);
      setDisableArmed(false);
      setVaultMessage("再次点击将向律所申请撤销当前登记；只有收到确认结果后才会删除本机登记并停止会话。");
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
      setVaultMessage(readableError(error, "律所撤销未取得有效确认；本机登记仍保持原状。"));
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
      setVaultMessage(readableError(error, "尚未取得确定的律所结果；本次操作会安全保留，请稍后再次查询。"));
    } finally {
      setVaultBusy(null);
    }
  }

  return (
    <section className={styles.securityArea} aria-label="工作台设置">
      <header className={styles.securityHeading}>
        <div>
          <p className={styles.eyebrow}>工作台设置</p>
          <h2>本机工作台与律所访问设置</h2>
          <p>{localStandaloneReady ? "本机办案已启用：可建立案件、关联资料根并进行已确认的只读盘点。律所协作、受管资料库和模型服务均为可选后续设置。打开本页只显示本机配置记录，不会读取系统钥匙串。" : "在这里完成本机启用、律所登记和可选模型服务设置。打开本页只显示本机配置记录，不会读取系统钥匙串；真实登记状态只在你明确执行登记、更新、撤销或进入受管工作区时核验。"}</p>
        </div>
        <span className={caseAccessReady || localStandaloneReady ? styles.statusPill : styles.securityBlockedPill}>{caseAccessReady ? "可以处理受管案件" : localStandaloneReady ? "本机办案已启用" : "尚未完成工作台启用"}</span>
      </header>

      <div className={styles.securityStatusGrid}>
        <StatusCell
          label={localStandaloneReady ? "本机办案" : "本机工作台"}
          value={processReady ? "已就绪" : desktopRuntime?.phase === "STARTING" ? "正在启动" : "暂不可用"}
          note={localStandaloneReady ? "可建立和打开本机案件；关闭应用后本机服务自动停止" : processReady ? "本机工作台仅随当前桌面应用运行；关闭应用后自动停止" : desktopRuntime?.message ?? "请从桌面应用检查本机工作台"}
          state={processReady ? "ready" : "blocked"}
        />
        <StatusCell
          label={localStandaloneReady ? "律所协作" : "律所授权来源"}
          value={trustReady ? "已核对" : localStandaloneReady ? "可选" : desktopRuntime?.enrollmentTrustPhase === "BLOCKED" ? "核对失败" : "待管理员配置"}
          note={trustReady ? "律所提供的授权来源、有效期和撤销状态已核对" : localStandaloneReady ? "不影响当前本机办案；需要团队协作时再由律所管理员配置" : "只能由律所管理员配置，不能在此页面自行填写"}
          state={trustReady || localStandaloneReady ? "ready" : "blocked"}
        />
        <StatusCell
          label={localStandaloneReady ? "本机案件台账" : "律师登记"}
          value={identityEnrolled ? "已登记" : localStandaloneReady ? "已启用" : signedCredentialSaved ? "已保存，待启用" : "未登记"}
          note={identityEnrolled ? sessionReady ? "当前律师登记已确认，可以建立本机短时会话" : desktopRuntime?.sessionPhase === "EXPIRED" ? "本机会话已到期；重启后会重新确认" : "律师登记已保存，但本机尚未完成启用" : localStandaloneReady ? "仅保存本机案件名称、资料根标识和盘点回执；不保存绝对路径" : signedCredentialSaved ? "登记已保存；重启后会按当前律所授权来源重新确认" : desktopRuntime?.identityPhase === "NOT_ENROLLED" ? "本机尚没有可用的律师登记" : "尚未取得律所确认"}
          state={(identityEnrolled && sessionReady) || localStandaloneReady ? "ready" : "blocked"}
        />
        <StatusCell
          label={localStandaloneReady ? "受管资料库" : "案件资料库"}
          value={persistenceConfigured ? "已连接" : localStandaloneReady ? "可选" : "未配置"}
          note={persistenceConfigured ? "已连接律所专用案件资料库" : localStandaloneReady ? "本机模式不需要数据库；接入律所受管案件时再配置" : desktopRuntime?.persistencePhase === "NOT_CONFIGURED" ? "本机尚未连接专用案件资料库" : "尚未取得案件资料库状态"}
          state={persistenceConfigured || localStandaloneReady ? "ready" : "blocked"}
        />
        <StatusCell
          label={localStandaloneReady ? "本机案件" : "受管案件"}
          value={caseAccessReady ? "可以打开" : localStandaloneReady ? "可以打开" : "暂不可打开"}
          note={caseAccessReady ? "每个案件仍会单独检查当前律师是否有权访问" : localStandaloneReady ? "当前可以建立、打开和盘点本机案件；正式受管工作流另行启用" : "任一准备步骤未完成时，系统不会打开受管案卷"}
          state={caseAccessReady || localStandaloneReady ? "ready" : "blocked"}
        />
      </div>

      <details aria-label="高级 / 管理员设置" className={styles.securityPanel}>
        <summary className={styles.securityPanelHeading}>
          <div>
            <p className={styles.eyebrow}>高级 / 管理员设置</p>
            <h3>律所管理员工具与故障诊断</h3>
          </div>
          <span>需要时展开查看</span>
        </summary>
        <div className={styles.securityColumns}>
          <section className={styles.securityPanel} aria-labelledby="enrollment-flow-title">
          <div className={styles.securityPanelHeading}>
            <div>
              <p className={styles.eyebrow}>管理员说明</p>
              <h3 id="enrollment-flow-title">工作台访问的五项确认</h3>
            </div>
            <span>{!trustReady ? "需先完成律所授权来源" : !sessionReady ? "需完成律师登记" : "可检查案件权限"}</span>
          </div>
          <ol className={styles.securityFlow}>
            <FlowStep index="01" title="本机工作台" state={processReady ? "已完成" : "未通过"}>
              桌面应用会启动并确认本机辅助服务仅属于当前工作台。
            </FlowStep>
            <FlowStep index="02" title="律所授权来源" state={trustReady ? "已确认" : "未配置"}>
              律所统一提供当前授权信息、有效期和撤销状态；用户不能在页面自行替换。
            </FlowStep>
            <FlowStep index="03" title="律师登记" state={identityEnrolled ? "已确认" : "等待律所确认"}>
              律所管理员确认律师后提供短期登记；页面不能自行选择律所、人员或案件角色。
            </FlowStep>
            <FlowStep index="04" title="本机安全存储" state={sessionReady ? "已完成" : "未开始"}>
              律所登记与本机安全信息分开保存，复制登记到另一台电脑不能使用。当前：{vaultStatus?.message ?? "请从桌面版读取本机安全存储状态。"}
            </FlowStep>
            <FlowStep index="05" title="逐案权限" state="未开始">
              每次打开或修改案卷时，系统都会重新检查当前律师的本案权限和律所隔离规则。
            </FlowStep>
          </ol>
          </section>

          <aside className={styles.securityPanel} aria-labelledby="security-boundary-title">
          <div className={styles.securityPanelHeading}>
            <div>
              <p className={styles.eyebrow}>高级说明</p>
              <h3 id="security-boundary-title">以下信息不能代替律所授权</h3>
            </div>
          </div>
          <ul className={styles.securityBoundaryList}>
            <li><strong>页面填写的姓名或角色</strong><span>只能作为普通文字，不能成为律师身份</span></li>
            <li><strong>macOS 用户名或设备名</strong><span>只说明当前电脑，不证明执业身份</span></li>
            <li><strong>材料文件夹名称</strong><span>只定义材料范围，不代表案件委托</span></li>
            <li><strong>AI 推断的律师关系</strong><span>不能创建、提高或撤销任何权限</span></li>
          </ul>
          </aside>
        </div>

        <AgentCapabilities />
        <AgentExecutionAudit />
        <ExternalRequestAudit />
      </details>
      <section className={styles.modelProviderSettings} aria-labelledby="model-provider-settings-title">
        <div className={styles.modelProviderSettingsHeading}>
          <div>
            <p className={styles.eyebrow}>{localStandaloneReady ? "外部模型服务" : "可选模型服务"}</p>
            <h3 id="model-provider-settings-title">{localStandaloneReady ? "本机办案模式不使用外部模型" : "按需配置模型服务"}</h3>
            <p>{localStandaloneReady ? "当前本机案件不会调用 DeepSeek、Qwen 或任何外部网络服务，因此这里不显示密钥配置操作。需要受控模型协助时，应先由律所管理员完成受管工作区部署与逐案授权。" : "打开本页不会读取或核验系统钥匙串中的服务密钥。这里仅显示本机的非敏感配置记录；在你确认一次模型调用后，系统才会读取并核验对应密钥。配置密钥不代表允许发送案件材料。"}</p>
          </div>
          <span>{localStandaloneReady ? "当前未启用" : modelProviderStatuses === null ? "正在读取配置记录" : "不读取系统钥匙串"}</span>
        </div>
        {localStandaloneReady ? (
          <div className={styles.securityNotice} role="status">
            <strong>外部模型保持关闭</strong>
            <span>本机文件夹、盘点清单和案件台账不会被发送到外部服务；也不会因为打开设置页而访问系统钥匙串。</span>
          </div>
        ) : (
          <>
        <div className={styles.modelProviderList}>
          <ModelProviderRow
            provider={modelProviderStatuses?.find((item) => item.providerId === "deepseek")}
            fallbackName="DeepSeek（法规、分析与文书）"
            note="用于法规检索、事实梳理、争点分析和文书初稿；不会自动发送案卷。"
            busy={modelProviderBusy === "deepseek"}
            onConfigure={() => void configureModelProvider("deepseek")}
            onRemove={() => void removeModelProvider("deepseek")}
          />
          <ModelProviderRow
            provider={modelProviderStatuses?.find((item) => item.providerId === "qwen")}
            fallbackName="通义千问百炼 Qwen3.5-OCR（图片与扫描件识别）"
            note="固定使用 qwen3.5-ocr：用于扫描件、图片、表格和 PDF 单页识别；不会自动发送案卷。"
            busy={modelProviderBusy === "qwen"}
            onConfigure={() => void configureModelProvider("qwen")}
            onRemove={() => void removeModelProvider("qwen")}
          />
        </div>
        {modelProviderStatuses?.find((item) => item.providerId === "qwen")?.configured && (
          <div className={styles.qwenConnectionSettings}>
            <div>
              <strong>通义千问连接设置（高级）</strong>
              <small>Qwen 的密钥按地域和业务空间生效。这里只接受官方地域与业务空间 ID，不接受自定义接口地址。</small>
            </div>
            <label>
              <span>地域</span>
              <select value={qwenRegionId} onChange={(event) => setQwenRegionId(event.target.value as "cn-beijing" | "ap-southeast-1")}>
                <option value="cn-beijing">华北2（北京）</option>
                <option value="ap-southeast-1">新加坡</option>
              </select>
            </label>
            <label>
              <span>业务空间 ID（管理员提供）</span>
              <input autoComplete="off" maxLength={120} onChange={(event) => setQwenWorkspaceId(event.target.value)} placeholder="从百炼控制台复制" value={qwenWorkspaceId} />
            </label>
            <button disabled={qwenConnectionBusy || qwenWorkspaceId.trim().length === 0} onClick={() => void configureQwenConnection()} type="button">
              {qwenConnectionBusy ? "正在保存…" : "保存连接设置"}
            </button>
          </div>
        )}
        {modelProviderMessage ? <p className={styles.modelProviderMessage} role="status">{modelProviderMessage}</p> : null}
          </>
        )}
      </section>
      <section className={styles.securityActions} aria-labelledby="security-actions-title">
        <div>
          <p className={styles.eyebrow}>{localStandaloneReady ? "律所受管模式（可选）" : "本机与律所登记"}</p>
          <h3 id="security-actions-title">{localStandaloneReady ? "当前本机办案无需额外启用" : "启用这台电脑的案件工作台"}</h3>
          <p>{localStandaloneReady ? "你已经可以在本机建立案件并盘点资料。律所协作、受管资料库和受控模型调用需要由管理员另行部署，不应在当前本机案件中临时开启。" : "首次使用时，先准备本机安全存储；再使用律所管理员提供的一次性激活码或登记包完成登记。"}</p>
        </div>
        {localStandaloneReady ? (
          <details className={styles.localFirmOption}>
            <summary>了解律所受管模式的接入条件</summary>
            <p>接入前需要律所管理员完成授权来源、律师登记、专用案件资料库和受管部署配置。为避免在本机办案时意外请求系统权限，本页不会显示或执行这些登记操作。</p>
          </details>
        ) : (
        <div className={styles.securityActionButtons}>
          {vaultStatus?.phase === "REMOTE_OPERATION_PENDING" ? (
            <button
              disabled={!trustReady || vaultBusy !== null}
              onClick={() => void resolvePendingEnrollment()}
              type="button"
            >
              {vaultBusy === "resolve" ? "正在核对律所结果…" : "查询待确认的律所操作"}
            </button>
          ) : null}
          <button
            disabled={vaultBusy !== null || vaultStatus === null || vaultStatus.installationInitialized || vaultStatus.phase === "REMOTE_OPERATION_PENDING"}
            onClick={() => void initializeVault()}
            type="button"
          >
            {vaultBusy === "initialize" ? "正在准备…" : vaultStatus?.installationInitialized ? "本机安全存储已就绪" : "准备本机安全存储"}
          </button>
          {vaultStatus?.installationInitialized && !vaultStatus.enrollmentEnvelopePresent && vaultStatus.phase !== "REMOTE_OPERATION_PENDING" ? (
            <button
              disabled={!trustReady || vaultBusy !== null}
              onClick={() => void activateEnrollment()}
              type="button"
              title={trustReady ? "激活码只进入 macOS 原生安全输入框" : "需要律所管理员配置授权来源并提供登记服务"}
            >
              {vaultBusy === "activate" ? "正在等待原生安全输入…" : "使用一次性激活码登记"}
            </button>
          ) : null}
          <button
            disabled={!trustReady || !vaultStatus?.installationInitialized || vaultBusy !== null || vaultStatus.phase === "REMOTE_OPERATION_PENDING"}
            onClick={() => void importEnrollment()}
            type="button"
            title={trustReady ? "只从原生文件选择器读取 .lawenroll 登记包" : "需要律所管理员配置授权来源并提供登记服务"}
          >
            {vaultBusy === "import" ? "正在确认登记包…" : "导入律所登记包"}
          </button>
          {vaultStatus?.enrollmentEnvelopePresent && vaultStatus.phase !== "REMOTE_OPERATION_PENDING" ? (
            <button
              disabled={!trustReady || vaultBusy !== null}
              onClick={() => void renewEnrollment()}
              type="button"
            >
              {vaultBusy === "renew" ? "正在联系律所更新…" : "更新律所登记"}
            </button>
          ) : null}
          {vaultStatus?.enrollmentEnvelopePresent && vaultStatus.phase !== "REMOTE_OPERATION_PENDING" ? (
            <button
              className={styles.securityDangerButton}
              disabled={!trustReady || vaultBusy !== null}
              onClick={() => void revokeEnrollment()}
              type="button"
            >
              {vaultBusy === "revoke" ? "正在确认撤销…" : revokeArmed ? "确认向律所撤销登记" : "向律所申请撤销登记"}
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
              {vaultBusy === "disable" ? "正在停用…" : disableArmed ? "确认停用本机工作台" : "只停用本机工作台"}
            </button>
          ) : null}
        </div>
        )}
        {vaultMessage ? <p className={styles.securityActionMessage} role="status">{vaultMessage}</p> : null}
      </section>

      <div className={styles.securityNotice} role="status">
        <strong>模型服务与律所权限分开管理</strong>
        <span>模型服务密钥只决定能否使用相应模型，不能取得律师身份、案件权限或绕开材料外发确认。律所登记仍由律所授权与本机安全存储单独控制。</span>
      </div>
    </section>
  );
}

function replaceModelProviderStatus(
  current: DesktopModelProviderStatus[] | null,
  next: DesktopModelProviderStatus,
): DesktopModelProviderStatus[] {
  const known = current ?? [];
  const withoutNext = known.filter((item) => item.providerId !== next.providerId);
  return [...withoutNext, next];
}

function ModelProviderRow({
  provider,
  fallbackName,
  note,
  busy,
  onConfigure,
  onRemove,
}: {
  provider: DesktopModelProviderStatus | undefined;
  fallbackName: string;
  note: string;
  busy: boolean;
  onConfigure: () => void;
  onRemove: () => void;
}) {
  const configured = provider?.configured === true;
  const configuration = providerConfigurationCopy(provider?.configurationState);
  return (
    <article>
      <div>
        <strong>{provider?.displayName ?? fallbackName}</strong>
        <small>{provider ? `${provider.modelId} · ${configuration.detail} · ${provider.connectionLabel} · ${note}` : note}</small>
      </div>
      <em className={configured ? styles.modelProviderReady : styles.modelProviderMissing}>{configuration.label}</em>
      <div className={styles.modelProviderActions}>
        <button disabled={busy} onClick={onConfigure} type="button">
          {busy ? "正在打开安全输入框…" : configured ? "更换服务密钥" : "配置服务密钥"}
        </button>
        {configured ? <button className={styles.modelProviderRemove} disabled={busy} onClick={onRemove} type="button">移除服务密钥</button> : null}
      </div>
    </article>
  );
}

function providerConfigurationCopy(
  state: DesktopModelProviderStatus["configurationState"] | undefined,
): { label: string; detail: string } {
  switch (state) {
    case "CONFIGURATION_RECORDED":
      return { label: "已记录配置", detail: "已记录配置，尚未读取密钥" };
    case "VALIDATED_FOR_CURRENT_SESSION":
      return { label: "本次调用已验证", detail: "仅本次调用已验证密钥" };
    case "NOT_CONFIGURED":
      return { label: "未配置", detail: "尚未记录服务密钥" };
    default:
      return { label: "未读取密钥", detail: "未读取系统钥匙串中的密钥" };
  }
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
