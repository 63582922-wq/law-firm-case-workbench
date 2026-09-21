"use client";

import { useEffect, useState } from "react";
import {
  confirmWebCasePosture,
  createWebCaseIdempotencyKey,
  isWebLoginRequired,
  readWebCasePosture,
  type WebCasePosture,
  type WebCasePostureOptions,
} from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";

type Draft = {
  partyKind: string;
  representedParty: string;
  forumType: string;
  caseType: string;
  procedureStage: string;
  position: string;
  authorityScope: string;
  engagementState: string;
};

type PendingConfirmation = { signature: string; idempotencyKey: string };

const EMPTY_DRAFT: Draft = {
  partyKind: "",
  representedParty: "",
  forumType: "",
  caseType: "",
  procedureStage: "",
  position: "",
  authorityScope: "",
  engagementState: "",
};

/**
 * This is deliberately a small, accountable intake for the context in which
 * the firm acts. It is not a plaintiff/defendant template selector: once it
 * is current, the Agent still derives its plan from this context, admitted
 * material and verified law.
 */
export function WebCasePosture({
  canConfirm: canConfirmCasePosture,
  canReview,
  caseId,
  caseVersion,
  onCurrentChanged,
  onSessionExpired,
  onVersionAdvanced,
}: {
  canConfirm: boolean;
  canReview: boolean;
  caseId: string;
  caseVersion: number;
  onCurrentChanged: (current: boolean) => void;
  onSessionExpired: () => void;
  onVersionAdvanced: (version: number) => void;
}) {
  const [posture, setPosture] = useState<WebCasePosture | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [matterVersion, setMatterVersion] = useState(caseVersion);
  const [submitting, setSubmitting] = useState(false);
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const [editingCurrent, setEditingCurrent] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setPosture(null);
      setMatterVersion(caseVersion);
      setPendingConfirmation(null);
      setEditingCurrent(false);
      setMessage(null);
      onCurrentChanged(false);
      if (!canReview) return;
      void readWebCasePosture(caseId, controller.signal)
        .then((next) => {
          if (controller.signal.aborted) return;
          setPosture(next);
          setDraft(draftFor(next));
          onCurrentChanged(next.status === "CURRENT");
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          if (isWebLoginRequired(reason)) {
            onSessionExpired();
            return;
          }
          setMessage(readableError(reason, "暂不能读取本案代理情境。"));
        });
    }, 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [canReview, caseId, caseVersion, onCurrentChanged, onSessionExpired]);

  const options = posture?.options;
  const canConfirm = canConfirmCasePosture && posture?.canConfirm === true;
  const isCurrent = posture?.status === "CURRENT";
  const isStale = posture?.status === "STALE";

  function updateDraft<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function acceptReceipt(version: number, advanceCase = false) {
    setMatterVersion(version);
    if (advanceCase) onVersionAdvanced(version);
  }

  async function submitCompletePosture() {
    if (!hasCompleteDraft(draft)) {
      setMessage("请完成被代理当事人、程序、当事人地位和委托范围后再确认。");
      return;
    }
    const signature = confirmationSignature(draft);
    const confirmation = pendingConfirmation?.signature === signature
      ? pendingConfirmation
      : { signature, idempotencyKey: createWebCaseIdempotencyKey() };
    setPendingConfirmation(confirmation);
    setSubmitting(true);
    setMessage(null);
    try {
      const receipt = await confirmWebCasePosture({
        caseId,
        expectedVersion: matterVersion,
        idempotencyKey: confirmation.idempotencyKey,
        partyKind: draft.partyKind,
        displayLabel: draft.representedParty,
        forumType: draft.forumType,
        caseTypeCode: draft.caseType,
        procedureStage: draft.procedureStage,
        positionCode: draft.position,
        authorityScopeCode: draft.authorityScope,
        engagementState: draft.engagementState,
      });
      acceptReceipt(receipt.matterVersion, true);
      const refreshed = await readWebCasePosture(caseId);
      if (refreshed.status !== "CURRENT" || refreshed.profile === null) throw new Error("服务端尚未确认当前代理情境，请勿将前述记录当作完成。");
      setPosture(refreshed);
      setPendingConfirmation(null);
      setEditingCurrent(false);
      onCurrentChanged(true);
      setMessage("本案代理情境已确认。Agent 会据此、现有材料和法源动态拟定后续工作。");
    } catch (reason: unknown) {
      handleError(reason, "确认本案代理情境未完成。请勿改变填写内容；可用原请求再次提交以继续核验。");
    } finally {
      setSubmitting(false);
    }
  }

  function handleError(reason: unknown, fallback: string) {
    if (isWebLoginRequired(reason)) {
      onSessionExpired();
      return;
    }
    setMessage(readableError(reason, fallback));
  }

  if (!canReview) {
    return <section className={styles.casePosturePanel}><p className={styles.eyebrow}>本案代理情境</p><strong>暂不能确认代理情境</strong><span>请联系案件负责人恢复本案权限后再继续。系统不会以默认信息代替本案确认。</span></section>;
  }
  if (message && posture === null) {
    return <section className={styles.casePosturePanel} role="alert"><p className={styles.eyebrow}>本案代理情境</p><strong>暂不能读取</strong><span>{message}</span></section>;
  }
  if (posture === null || options === undefined) {
    return <section className={styles.casePosturePanel} aria-busy="true"><p className={styles.eyebrow}>本案代理情境</p><strong>正在读取本所代理身份与程序信息…</strong></section>;
  }

  if (isCurrent && posture.profile && !editingCurrent) {
    return <CurrentPosture canConfirm={canConfirm} onUpdate={() => setEditingCurrent(true)} options={options} profile={posture.profile} />;
  }

  return (
    <section className={styles.casePosturePanel} aria-labelledby="case-posture-title">
      <header>
        <div>
          <p className={styles.eyebrow}>办案前提</p>
          <h2 id="case-posture-title">先确认本所代理的实际情境</h2>
          <p>{editingCurrent ? "更新后，已有分析和文件需要按新情况重新核对。" : "请确认本所代理谁、案件处于什么阶段及委托范围。确认后，办案助手会据此整理本案需要优先处理的事项。"}</p>
        </div>
        <span className={isStale || editingCurrent ? styles.casePostureStale : styles.casePosturePending}>{isStale ? "需要重新确认" : editingCurrent ? "正在更新" : "尚未确认"}</span>
      </header>

      {isStale || editingCurrent ? <div className={styles.casePostureWarning}><strong>{editingCurrent ? "更新后需要重新核对" : "原确认已过期"}</strong><span>{editingCurrent ? "提交后，相关分析和成果会按新的代理情境重新核对。" : "案件信息已经变化；请由主办律师重新确认后继续办案。"}</span></div> : null}
      {!canConfirm ? <div className={styles.casePostureReadonly}><strong>当前为协办只读</strong><span>你可以查看确认状态；本案代理情境只能由已完成多因素登录的主办律师确认。</span></div> : null}
      {message ? <p className={styles.casePostureNotice} role="status">{message}</p> : null}

      <div className={styles.casePostureForm}>
        <fieldset disabled={!canConfirm || submitting}>
          <legend>本所代理谁</legend>
          <label><span>当事人名称</span><input onChange={(event) => updateDraft("representedParty", event.target.value)} placeholder="例如：周雅丽" value={draft.representedParty} /></label>
          <OptionSelect disabled={!canConfirm || submitting} label="主体类型" onChange={(value) => updateDraft("partyKind", value)} options={options.partyKinds} value={draft.partyKind} />
        </fieldset>
        <fieldset disabled={!canConfirm || submitting}>
          <legend>当前程序</legend>
          <OptionSelect disabled={!canConfirm || submitting} label="受理机构" onChange={(value) => updateDraft("forumType", value)} options={options.forumTypes} value={draft.forumType} />
          <OptionSelect disabled={!canConfirm || submitting} label="案件类型" onChange={(value) => updateDraft("caseType", value)} options={options.caseTypes} value={draft.caseType} />
          <OptionSelect disabled={!canConfirm || submitting} label="程序阶段" onChange={(value) => updateDraft("procedureStage", value)} options={options.procedureStages} value={draft.procedureStage} />
        </fieldset>
        <fieldset disabled={!canConfirm || submitting}>
          <legend>程序地位与委托</legend>
          <OptionSelect disabled={!canConfirm || submitting} label="当事人地位" onChange={(value) => updateDraft("position", value)} options={options.partyPositions} value={draft.position} />
          <OptionSelect disabled={!canConfirm || submitting} label="代理权限" onChange={(value) => updateDraft("authorityScope", value)} options={options.authorityScopes} value={draft.authorityScope} />
          <OptionSelect disabled={!canConfirm || submitting} label="委托状态" onChange={(value) => updateDraft("engagementState", value)} options={options.engagementStates} value={draft.engagementState} />
        </fieldset>
        <div className={styles.casePostureSubmitLine}>
          <p>确认后，后续分析和文件都会以这份案件情况为准。网络结果不明时，请保持填写内容不变后再次提交。</p>
          <button className={styles.webLawyerPrimaryAction} disabled={!canConfirm || submitting} onClick={submitCompletePosture} type="button">{submitting ? "正在确认…" : "确认案件情况并开始处理"}</button>
        </div>
      </div>
    </section>
  );
}

function CurrentPosture({ canConfirm, onUpdate, profile, options }: { canConfirm: boolean; onUpdate: () => void; profile: NonNullable<WebCasePosture["profile"]>; options: WebCasePostureOptions }) {
  return (
    <details className={`${styles.casePosturePanel} ${styles.webCaseDisclosure}`}>
      <summary>
        <strong>代理 {profile.representedPartyDisplayLabel}</strong>
        <span> · {optionLabel(profile.representedPosition, options.partyPositions)} · {optionLabel(profile.procedureStage, options.procedureStages)}</span>
        <span className={styles.casePostureCurrent}>已确认</span>
        <span> · 查看与更新</span>
      </summary>
      <dl className={styles.casePostureSummary}>
        <div><dt>本所代理</dt><dd>{profile.representedPartyDisplayLabel}</dd></div>
        <div><dt>主体类型</dt><dd>{optionLabel(profile.representedPartyKind, options.partyKinds)}</dd></div>
        <div><dt>受理机构</dt><dd>{optionLabel(profile.forumType, options.forumTypes)}</dd></div>
        <div><dt>案件类型</dt><dd>{optionLabel(profile.caseTypeCode, options.caseTypes)}</dd></div>
        <div><dt>程序阶段</dt><dd>{optionLabel(profile.procedureStage, options.procedureStages)}</dd></div>
        <div><dt>当事人地位</dt><dd>{optionLabel(profile.representedPosition, options.partyPositions)}</dd></div>
        <div><dt>代理权限</dt><dd>{optionLabel(profile.authorityScopeCode, options.authorityScopes)}</dd></div>
        <div><dt>委托状态</dt><dd>{optionLabel(profile.engagementState, options.engagementStates)}</dd></div>
      </dl>
      <div className={styles.casePostureFootnote}>{canConfirm ? <><span>如需更新，系统会形成新的案件版本，并让依赖该情境的计划和成果重新核验。</span><button onClick={onUpdate} type="button">更新代理情境</button></> : <span>当前为协办只读；如需更正，请由主办律师更新并重新确认。</span>}</div>
    </details>
  );
}

function OptionSelect({ disabled, label, onChange, options, value }: { disabled: boolean; label: string; onChange: (value: string) => void; options: readonly string[]; value: string }) {
  return <label><span>{label}</span><select disabled={disabled} onChange={(event) => onChange(event.target.value)} value={value}><option value="">请选择</option>{options.map((option) => <option key={option} value={option}>{optionLabel(option, options)}</option>)}</select></label>;
}

function draftFor(posture: WebCasePosture): Draft {
  const profile = posture.profile;
  const first = (values: readonly string[]) => values[0] ?? "";
  return {
    partyKind: profile?.representedPartyKind ?? first(posture.options.partyKinds),
    representedParty: profile?.representedPartyDisplayLabel ?? "",
    forumType: profile?.forumType ?? first(posture.options.forumTypes),
    caseType: profile?.caseTypeCode ?? first(posture.options.caseTypes),
    procedureStage: profile?.procedureStage ?? first(posture.options.procedureStages),
    position: profile?.representedPosition ?? first(posture.options.partyPositions),
    authorityScope: profile?.authorityScopeCode ?? first(posture.options.authorityScopes),
    engagementState: profile?.engagementState ?? first(posture.options.engagementStates),
  };
}

function hasCompleteDraft(draft: Draft): boolean {
  return Boolean(
    draft.partyKind
    && draft.representedParty.trim()
    && draft.forumType
    && draft.caseType
    && draft.procedureStage
    && draft.position
    && draft.authorityScope
    && draft.engagementState,
  );
}

function confirmationSignature(draft: Draft): string {
  return JSON.stringify({
    partyKind: draft.partyKind,
    representedParty: draft.representedParty.trim().replace(/\s+/g, " "),
    forumType: draft.forumType,
    caseType: draft.caseType,
    procedureStage: draft.procedureStage,
    position: draft.position,
    authorityScope: draft.authorityScope,
    engagementState: draft.engagementState,
  });
}

function optionLabel(code: string, available: readonly string[]): string {
  void available;
  return POSTURE_LABELS[code] ?? code.replaceAll("_", " ");
}

function readableError(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message.trim() ? reason.message : fallback;
}

const POSTURE_LABELS: Record<string, string> = {
  NATURAL_PERSON: "自然人",
  LEGAL_PERSON: "法人",
  UNINCORPORATED_ORGANIZATION: "非法人组织",
  STATE_OR_PUBLIC_BODY: "国家机关或公共机构",
  OTHER_LEGAL_SUBJECT: "其他法律主体",
  PEOPLE_COURT: "人民法院",
  ARBITRATION_COMMISSION: "仲裁委员会",
  LABOR_ARBITRATION_COMMISSION: "劳动人事争议仲裁委员会",
  ADMINISTRATIVE_AUTHORITY: "行政机关",
  PEOPLE_PROCURATORATE: "人民检察院",
  PUBLIC_SECURITY_OR_SUPERVISORY_AUTHORITY: "公安或监察机关",
  OTHER_STATUTORY_FORUM: "其他法定机构",
  "CIVIL.GENERAL": "民事案件（一般）",
  "CIVIL.PRIVATE_LENDING": "民间借贷",
  "CIVIL.CONTRACT": "合同纠纷",
  "CIVIL.TORT": "侵权纠纷",
  "CIVIL.MARRIAGE_FAMILY": "婚姻家事",
  "CIVIL.LABOR": "劳动争议",
  "COMMERCIAL.GENERAL": "商事案件（一般）",
  "FINANCIAL.GENERAL": "金融案件（一般）",
  "ADMINISTRATIVE.GENERAL": "行政案件（一般）",
  "CRIMINAL.DEFENSE": "刑事辩护",
  "CRIMINAL.INCIDENTAL_CIVIL": "刑事附带民事",
  "ENFORCEMENT.GENERAL": "执行案件（一般）",
  "ARBITRATION.COMMERCIAL": "商事仲裁",
  "ARBITRATION.LABOR": "劳动仲裁",
  "OTHER.STATUTORY_PROCEEDING": "其他法定程序",
  PRE_ACTION: "诉前",
  PRE_ARBITRATION: "仲裁前",
  PRESERVATION: "保全程序",
  FIRST_INSTANCE: "一审",
  SECOND_INSTANCE: "二审",
  RETRIAL_REVIEW: "再审审查",
  RETRIAL: "再审",
  ENFORCEMENT: "执行",
  ENFORCEMENT_OBJECTION: "执行异议",
  ARBITRATION: "仲裁",
  LABOR_ARBITRATION: "劳动仲裁",
  ADMINISTRATIVE_RECONSIDERATION: "行政复议",
  CRIMINAL_INVESTIGATION: "刑事侦查",
  CRIMINAL_PROSECUTION_REVIEW: "审查起诉",
  CRIMINAL_FIRST_INSTANCE: "刑事一审",
  CRIMINAL_SECOND_INSTANCE: "刑事二审",
  CLOSED: "已结案",
  PLAINTIFF: "原告",
  DEFENDANT: "被告",
  APPELLANT: "上诉人",
  APPELLEE: "被上诉人",
  RETRIAL_APPLICANT: "再审申请人",
  RETRIAL_RESPONDENT: "再审被申请人",
  APPLICANT: "申请人",
  RESPONDENT: "被申请人",
  EXECUTION_APPLICANT: "申请执行人",
  EXECUTION_RESPONDENT: "被执行人",
  ARBITRATION_CLAIMANT: "仲裁申请人",
  ARBITRATION_RESPONDENT: "仲裁被申请人",
  LABOR_ARBITRATION_CLAIMANT: "劳动仲裁申请人",
  LABOR_ARBITRATION_RESPONDENT: "劳动仲裁被申请人",
  ADMINISTRATIVE_APPLICANT: "行政复议申请人",
  ADMINISTRATIVE_RESPONDENT: "行政复议被申请人",
  CRIMINAL_SUSPECT: "犯罪嫌疑人",
  CRIMINAL_DEFENDANT: "被告人",
  VICTIM: "被害人",
  PRIVATE_PROSECUTOR: "自诉人",
  THIRD_PARTY: "第三人",
  INTERESTED_PARTY: "利害关系人",
  OTHER_PARTICIPANT: "其他诉讼参与人",
  GENERAL_AUTHORITY: "一般代理权限",
  SPECIAL_AUTHORITY: "特别代理权限",
  LIMITED_AUTHORITY: "有限代理权限",
  LEGAL_AID: "法律援助",
  COURT_APPOINTED_DEFENSE: "指定辩护",
  ACTIVE: "有效",
  PAUSED: "暂停",
  TERMINATED: "终止",
  WITHDRAWN: "已撤回",
};
