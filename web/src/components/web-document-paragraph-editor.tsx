"use client";

import { useRef, useState } from "react";
import {
  createWebCaseAgentIdempotencyKey, readWebDocumentContentProposal,
  saveWebDocumentContentProposal, type WebDocumentContentProposal,
  type WebDocumentParagraphChange, WebLawyerApiError,
  type WebDocumentGenerationStatus,
  authorizeWebDocumentContentGeneration,
  listWebDocumentContentProposals, resolveWebDocumentContentProposal, readWebLawyerSession, type WebDocumentContentProposalPage,
} from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";
import { documentRecoverySlot, readDocumentRecovery, rememberDocumentRecovery, forgetDocumentRecovery } from "@/lib/web-document-recovery";

const generationLabels: Record<WebDocumentGenerationStatus, string> = {
  UNAVAILABLE: "当前服务暂不提供生成进度，不能据此判断是否已生成。",
  NOT_AUTHORIZED: "修改已保存，尚无生成授权记录。",
  QUEUED: "已授权，等待生成。",
  GENERATING: "正在生成并核验新版文书。",
  RECOVERING: "任务正在等待恢复，无需重新提交修改。",
  UNKNOWN: "生成结果待核验，请勿重复提交或重新生成。",
  UNKNOWN_REGISTERED: "已找到与本次授权匹配的文件登记记录；文件完整性和成功回执仍待核验，不能据此下载或提交。请勿重新生成。",
  UNKNOWN_FILES_VERIFIED: "本次读取已核验候选、Word 和 PDF 文件及其登记摘要，但成功回执仍未恢复。尚不能下载新版或提交，请勿重新生成。",
  FAILED: "生成未完成，需核对失败记录；修改提案仍保留。",
  GENERATED_REVIEW_COPY: "已生成待律师复核的文书；不代表已经批准或可提交。",
};

export function WebDocumentModificationHistory({ caseId, runId, artifactId }: {
  caseId: string; runId: string; artifactId: string;
}) {
  const [page, setPage] = useState<WebDocumentContentProposalPage | null>(null);
  const [detail, setDetail] = useState<WebDocumentContentProposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  async function read(proposalId: string | null, after: string | null = null) {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null); setDetail(null);
    try {
      if (proposalId) setDetail(await readWebDocumentContentProposal(caseId, runId, artifactId, proposalId));
      else setPage(await listWebDocumentContentProposals(caseId, runId, artifactId, after));
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : "修改记录暂时无法读取。");
    } finally { inFlight.current = false; setBusy(false); }
  }
  return <section className={styles.caseAgentDocumentVersionGate} aria-label="已保存修改记录">
    <h5>已保存修改</h5>
    <p>查看修改前后内容及生成进度。历史修改不会覆盖原稿。</p>
    <button type="button" disabled={busy} onClick={() => void read(null)}>{busy ? "正在读取…" : page ? "重新读取首页" : "查看已保存修改"}</button>
    {error ? <p role="alert">{error}</p> : null}
    {page ? <><p role="status">本页 {page.items.length} 项修改记录，最近保存的在前。</p>
      <ul>{page.items.map((item) => <li key={item.proposalId}><button type="button" disabled={busy} onClick={() => void read(item.proposalId)}>查看 {new Date(item.createdAt).toLocaleString("zh-CN")} 的修改 · 基于第 {item.revisionNumber} 版</button></li>)}</ul>
      {page.nextAfter ? <button type="button" disabled={busy} onClick={() => void read(null, page.nextAfter)}>下一页</button> : null}
    </> : null}
    {detail ? <div><strong>{detail.basedOnCurrentVersion ? "待来源与律师复核" : "基于旧版本 · 不可直接用于当前文书"}</strong>
      <p role="status">{generationLabels[detail.generationStatus]}</p>
      <button type="button" disabled={busy} onClick={() => void read(detail.proposalId)}>刷新生成状态</button>
      {detail.changes.map((change, index) => <article key={index}><p>修改前：{change.before}</p><p>修改后：{change.after}</p><p>理由：{change.reason}</p></article>)}
      <WebDocumentGenerationAuthorization key={`${caseId}:${runId}:${artifactId}:${detail.proposalId}`}
        caseId={caseId} runId={runId} artifactId={artifactId} detail={detail}
        onUpdated={(value) => setDetail((current) => current?.proposalId === value.proposalId ? value : current)} />
      <p>本记录不是已批准或可提交的文书。</p>
    </div> : null}
  </section>;
}

function WebDocumentGenerationAuthorization({ caseId, runId, artifactId, detail, onUpdated }: {
  caseId: string; runId: string; artifactId: string; detail: WebDocumentContentProposal;
  onUpdated: (value: WebDocumentContentProposal) => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const eligible = detail.basedOnCurrentVersion && detail.generationStatus === "NOT_AUTHORIZED";

  async function act() {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null);
    let slot: string | null = null;
    let key: string | null = null;
    let writing = false;
    try {
      const session = await readWebLawyerSession();
      const namespace = session?.actor.recoveryNamespace;
      if (!namespace) throw new Error("登录恢复信息不可用，本次未发送授权。");
      slot = documentRecoverySlot(namespace, [caseId, runId, artifactId, detail.proposalId, "generation"]);
      const prior = readDocumentRecovery(window.sessionStorage, slot);
      if (prior) { key = prior.key; setPending(true); }
      else {
        if (!eligible) throw new Error("请刷新修改和文书状态，本次未发送授权。");
        if (!session.actor.roles.some((role) => role === "LEAD_LAWYER" || role === "REVIEWER")) throw new Error("请由本案主办律师或复核人授权生成。");
        if (!note.trim()) throw new Error("请填写本次修改的复核说明。");
        key = createWebCaseAgentIdempotencyKey();
        rememberDocumentRecovery(window.sessionStorage, slot, { key, revision: detail.revisionNumber, createdAt: Date.now() });
        setPending(true); writing = true;
        await authorizeWebDocumentContentGeneration(caseId, runId, artifactId, detail.proposalId,
          detail.revisionNumber, note, key, namespace);
        writing = false;
      }
      const updated = await readWebDocumentContentProposal(caseId, runId, artifactId, detail.proposalId);
      onUpdated(updated);
      if (updated.generationStatus === "NOT_AUTHORIZED" || updated.generationStatus === "UNAVAILABLE") {
        throw new Error("尚不能确认授权结果，请稍后核对；本次不会重复提交。");
      }
      forgetDocumentRecovery(window.sessionStorage, slot, key);
      setPending(false);
    } catch (cause: unknown) {
      if (writing && slot && key && cause instanceof WebLawyerApiError && cause.status !== null && [400, 413, 415, 422].includes(cause.status)) {
        try { forgetDocumentRecovery(window.sessionStorage, slot, key); setPending(false); }
        catch { /* Keep the recovery marker if browser storage cannot be updated. */ }
      }
      setError(cause instanceof Error ? cause.message : "授权结果暂时无法确认，请先核对结果。");
    } finally { inFlight.current = false; setBusy(false); }
  }

  return <div aria-label="生成修改后的文书">
    <p>主办律师或复核人核对上述修改与来源后，可授权生成 Word/PDF 待复核稿。此操作不是事实批准、终审或法院提交。</p>
    {eligible && !pending ? <label>复核说明<textarea value={note} maxLength={2000} disabled={busy}
      onChange={(event) => setNote(event.target.value)} /></label> : null}
    {eligible || pending ? <button type="button" disabled={busy} onClick={() => void act()}>
      {busy ? "正在核对…" : pending ? "核对授权结果（不重新提交）" : "授权生成待复核文书"}
    </button> : null}
    {error ? <p role="alert">{error}</p> : null}
  </div>;
}

export function WebDocumentParagraphEditor({ caseId, runId, artifactId, revisionNumber, sectionIndex, paragraphIndex, text, sourceRefs, disabled }: {
  caseId: string; runId: string; artifactId: string; revisionNumber: number;
  sectionIndex: number; paragraphIndex: number; text: string;
  sourceRefs: readonly string[]; disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [replacement, setReplacement] = useState(text);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [hasPending, setHasPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<WebDocumentContentProposal | null>(null);
  // Only memory: client matter text must not enter browser persistent storage.
  const pending = useRef<{ key: string; change?: WebDocumentParagraphChange; sent?: boolean; proposalId?: string } | null>(null);
  const recoveryScope = useRef<{ namespace: string; slot: string } | null>(null);
  const inFlight = useRef(false);

  async function start() {
    if (disabled || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      const session = await readWebLawyerSession();
      const namespace = session?.actor.recoveryNamespace;
      if (!namespace) throw new Error("登录状态或修改恢复服务尚未就绪，未发送修改。");
      const slot = documentRecoverySlot(namespace, [caseId, runId, artifactId, String(sectionIndex), String(paragraphIndex)]);
      const previous = readDocumentRecovery(window.sessionStorage, slot);
      recoveryScope.current = { namespace, slot };
      if (previous) { pending.current = { key: previous.key, sent: true }; setHasPending(true); }
      setOpen(true);
    } catch (cause: unknown) { setError(cause instanceof Error ? cause.message : "无法读取修改恢复记录。"); }
    finally { inFlight.current = false; setBusy(false); }
  }

  async function save() {
    if (disabled || inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(null);
    let writing = false;
    try {
      const scope = recoveryScope.current;
      if (!scope) throw new Error("修改恢复服务未就绪，未发送修改。");
      const session = await readWebLawyerSession();
      if (session?.actor.recoveryNamespace !== scope.namespace) throw new Error("登录身份已变化，请重新打开文书；本次未发送修改。");
      if (!pending.current) {
        if (!replacement.trim() || replacement === text || !reason.trim()) throw new Error("请修改正文并填写修改理由。");
        const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
        pending.current = {
          key: createWebCaseAgentIdempotencyKey(),
          change: { section_index: sectionIndex, paragraph_index: paragraphIndex,
            expected_text_hash: Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join(""),
            replacement_text: replacement, reason, source_refs: sourceRefs },
        };
        setHasPending(true);
      }
      const request = pending.current;
      if (!request.proposalId && request.sent) {
        const resolved = await resolveWebDocumentContentProposal(caseId, runId, artifactId, request.key);
        if (!resolved) throw new Error("尚未查到已提交的保存记录；原请求可能仍在处理，本次未重新提交。请稍后核对。");
        request.proposalId = resolved;
      }
      if (!request.proposalId) {
        if (!request.change) throw new Error("已恢复请求只能查询，不会重新发送正文。");
        rememberDocumentRecovery(window.sessionStorage, scope.slot, { key: request.key, revision: revisionNumber, createdAt: Date.now() });
        request.sent = true;
        writing = true;
        request.proposalId = await saveWebDocumentContentProposal(
          caseId, runId, artifactId, revisionNumber, [request.change], request.key, scope.namespace,
        );
        writing = false;
      }
      setSaved(await readWebDocumentContentProposal(caseId, runId, artifactId, request.proposalId));
      forgetDocumentRecovery(window.sessionStorage, scope.slot, request.key);
    } catch (cause: unknown) {
      if (writing && !pending.current?.proposalId && cause instanceof WebLawyerApiError && cause.status !== null && [400, 413, 415, 422].includes(cause.status)) {
        try {
          if (recoveryScope.current && pending.current) forgetDocumentRecovery(window.sessionStorage, recoveryScope.current.slot, pending.current.key);
        } catch { /* Retain a possibly unresolved marker if browser storage fails. */ }
        pending.current = null;
        setHasPending(false);
      }
      setError(cause instanceof Error ? cause.message : "修改结果暂时无法确认。");
    } finally { inFlight.current = false; setBusy(false); }
  }

  if (!open) return <div><button type="button" disabled={disabled || busy} onClick={() => void start()}>{busy ? "正在读取修改状态…" : "修改本段 / 核对待确认修改"}</button>{error ? <p role="alert">{error}</p> : null}</div>;
  return <div className={styles.caseAgentDocumentVersionGate}>
    <strong>修改本段 · 基于第 {revisionNumber} 版</strong>
    <p>沿用本段所列来源。修改只保存为待复核提案，原稿与下载文件不会改变；需要新增依据时请先补充案件资料。</p>
    {error ? <p role="alert">{error} {hasPending ? "已发出的修改将保持原请求核对，请勿另建相同修改。" : "请修正后保存。"}</p> : null}
    {saved ? <div role="status">
      <strong>修改已保存，待来源与律师复核</strong>
      {!saved.basedOnCurrentVersion ? <p>这项修改基于旧版本，不可直接用于当前文书。</p> : null}
      {saved.changes.map((change, index) => <div key={index}><p>修改后：{change.after}</p><p>理由：{change.reason}</p></div>)}
      <p>{generationLabels[saved.generationStatus]}</p>
      <WebDocumentGenerationAuthorization key={`${caseId}:${runId}:${artifactId}:${saved.proposalId}`}
        caseId={caseId} runId={runId} artifactId={artifactId} detail={saved}
        onUpdated={(value) => setSaved((current) => current?.proposalId === value.proposalId ? value : current)} />
      <button type="button" disabled={busy || disabled} onClick={() => void save()}>刷新修改状态</button>
    </div> : <>
      <label className={styles.caseAgentDecisionNote}><span>修改后正文</span><textarea rows={5} maxLength={8000} value={replacement} disabled={busy || disabled || hasPending} onChange={(event) => setReplacement(event.target.value)} /></label>
      <label className={styles.caseAgentDecisionNote}><span>修改理由（必填）</span><textarea rows={2} maxLength={1000} value={reason} disabled={busy || disabled || hasPending} onChange={(event) => setReason(event.target.value)} /></label>
      <button type="button" disabled={busy || disabled || (!hasPending && (!replacement.trim() || replacement === text || !reason.trim()))} onClick={() => void save()}>{busy ? "正在保存并核对…" : hasPending ? "核对这次保存结果" : "保存为待复核修改"}</button>
      {!hasPending ? <button type="button" disabled={busy} onClick={() => setOpen(false)}>收起，保留草稿</button> : null}
    </>}
  </div>;
}
