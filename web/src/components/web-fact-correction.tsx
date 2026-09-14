"use client";

import { useState } from "react";
import { isWebLoginRequired, readWebFactCorrection, recoverWebFactCorrection, saveWebFactCorrection, WebLawyerApiError,
  readWebFactCorrectionSubmission, recoverWebFactCorrectionSubmission, submitWebFactCorrection,
  type WebFactCorrectionSubmission, type WebFactCorrectionDraft } from "@/lib/web-lawyer-api";
import styles from "./case-workbench.module.css";

export function WebFactCorrection({caseId,candidateId,version,originalText,onSessionExpired,expectedFactId,onSourceContextLoaded}:{
  caseId:string;candidateId:string;version:number;originalText:string;onSessionExpired:()=>void;
  expectedFactId?:string;onSourceContextLoaded?:(version:number|null)=>void;
}) {
  const [open,setOpen]=useState(false);
  const [loaded,setLoaded]=useState(false);
  const [reviewVersion,setReviewVersion]=useState(version);
  const [busy,setBusy]=useState(false);
  const [draft,setDraft]=useState<WebFactCorrectionDraft|null>(null);
  const [text,setText]=useState(originalText);
  const [reason,setReason]=useState("");
  const [pending,setPending]=useState<string|null>(null);
  const [pendingSubmission,setPendingSubmission]=useState<string|null>(null);
  const [submission,setSubmission]=useState<WebFactCorrectionSubmission|null>(null);
  const [message,setMessage]=useState("");
  const storageKey=`lawcase:fact-correction:${caseId}:${candidateId}`;
  const submissionKey=`lawcase:fact-correction-submit:${caseId}:${candidateId}`;
  const locked=busy||!loaded||!!pending||!!pendingSubmission||!!submission;
  const unchanged=!!draft&&text.trim()===draft.revisedText.trim()&&reason.trim()===draft.reason.trim();
  function fail(error:unknown) {
    if(isWebLoginRequired(error)) onSessionExpired();
    setMessage(error instanceof Error?error.message:"暂不能处理修改稿，请保留文字并查询保存结果。");
  }
  async function refresh() {
    const [context,state]=await Promise.all([readWebFactCorrection(caseId,candidateId),readWebFactCorrectionSubmission(caseId,candidateId)]);
    if(context.currentMatterVersion!==state.currentMatterVersion)throw new Error("读取期间案件发生变化，请重新读取后再操作。");
    if(state.submission&&state.submission.proposalId!==context.draft?.proposalId)throw new Error("最新保存稿与已送审稿不是同一修订，暂不能在此覆盖或再次送审，请到事实审批核对关联记录。");
    if(expectedFactId&&state.submission?.factId!==expectedFactId)throw new Error("送审记录与本事实不一致，不能据此审批。");
    const saved=context.draft;setReviewVersion(context.currentMatterVersion);
    setDraft(saved);setSubmission(state.submission);
    setText(saved?.revisedText??originalText);setReason(saved?.reason??"");setLoaded(true);
    onSourceContextLoaded?.(context.currentMatterVersion);
    return state.submission;
  }
  async function load() {
    setOpen(true);setBusy(true);setLoaded(false);
    onSourceContextLoaded?.(null);
    try {
      // Persist only an opaque request key, never legal text or client material.
      const sentKey=sessionStorage.getItem(submissionKey);
      setPendingSubmission(sentKey);
      if(sentKey) {
        const receipt=await recoverWebFactCorrectionSubmission(caseId,sentKey);
        if(!receipt) {setMessage("该次送审仍未查到回执。请只查询原请求，不重复送审。");return;}
        sessionStorage.removeItem(submissionKey);setPendingSubmission(null);
      }
      const key=sessionStorage.getItem(storageKey);
      setPending(key);
      if(key) {
        const receipt=await recoverWebFactCorrection(caseId,key);
        if(!receipt) {setMessage("该次保存仍未查到回执。请保留当前文字，只查询结果，不重复提交。");return;}
        sessionStorage.removeItem(storageKey);setPending(null);
      }
      const sent=await refresh();
      setMessage(sent?"已读取关联事实的当前状态；送审不等于批准。":"已读取服务器稿；保存不会确认事实，送审需要单独操作。");
    } catch(error) {fail(error);} finally {setBusy(false);}
  }
  async function save() {
    if(locked)return;
    setBusy(true);
    const key=`fact-correction-${crypto.randomUUID()}`;
    try {
      sessionStorage.setItem(storageKey,key);setPending(key);
      await saveWebFactCorrection(caseId,candidateId,reviewVersion,draft?.revision??0,text.trim(),reason.trim(),key);
      sessionStorage.removeItem(storageKey);setPending(null);
      setLoaded(false);
      await refresh();setMessage("修改稿已保存，未批准事实，也未改变案件立场。");
    } catch(error) {
      // A definite rejection is not an unknown submission. Preserve text,
      // release the unused request key, and require a fresh server read.
      if(error instanceof WebLawyerApiError && error.status !== null && [400,401,403,404,409,422].includes(error.status)) {
        sessionStorage.removeItem(storageKey);setPending(null);setLoaded(false);
      }
      fail(error);
    } finally {setBusy(false);}
  }
  async function submit() {
    if(locked||!draft||draft.stale||!unchanged)return;
    setBusy(true);
    try {
      const key=`fact-correction-submit-${crypto.randomUUID()}`;
      sessionStorage.setItem(submissionKey,key);setPendingSubmission(key);
      await submitWebFactCorrection(caseId,candidateId,draft.proposalId,reviewVersion,key);
      sessionStorage.removeItem(submissionKey);setPendingSubmission(null);setLoaded(false);
      await refresh();setMessage("已送入事实审批。尚未确认事实，也未关闭待研判事项。");
    } catch(error) {
      if(error instanceof WebLawyerApiError&&error.status!==null&&[400,401,403,404,409,422].includes(error.status)) {
        sessionStorage.removeItem(submissionKey);setPendingSubmission(null);setLoaded(false);
      }
      fail(error);
    } finally {setBusy(false);}
  }
  if(!open)return <button type="button" onClick={()=>void load()}>{expectedFactId?"核对原候选、修改理由和摘录":"查看／纠正事实表述"}</button>;
  return <section className={styles.ledgerExceptionDecisionForm} aria-label="事实修改稿">
    <strong>事实修改稿 · 保存不等于批准</strong>
    <small>未保存文字只保留在当前页面；刷新前请先保存。保存结果不明时，请查询原请求。</small>
    <p>原候选：{draft?.originalText??originalText}</p>
    {draft?.excerpts.map(excerpt=><blockquote key={excerpt.pageId}><strong>原始证据摘录</strong><p>{excerpt.text}</p></blockquote>)}
    {submission?<p>关联事实：{{CANDIDATE:"待律师审批",CONFIRMED:"律师已确认",DISPUTED:"已标为争议事实",DENIED:"已否认",INVALIDATED:"已失效"}[submission.status]}。本处仅供对照，不可覆盖已送审记录，也不代表文书可提交法院。</p>:null}
    {draft?.stale&&!submission?<p role="alert">案件已变化。请重新核对证据后保存新修订；旧稿不会自动生效。</p>:null}
    <label><span>修改后表述</span><textarea rows={4} maxLength={4000} value={text} disabled={locked} onChange={e=>setText(e.target.value)}/></label>
    <label><span>修改理由（必填）</span><textarea rows={2} maxLength={2000} value={reason} disabled={locked} onChange={e=>setReason(e.target.value)}/></label>
    <p role="status">{message}</p>
    {!submission?<><button type="button" disabled={locked||!text.trim()||!reason.trim()||text.trim()===originalText.trim()||(!draft?.stale&&unchanged)} onClick={()=>void save()}>保存待审修改稿</button>
      <button type="button" disabled={locked||!draft||draft.stale||!unchanged} onClick={()=>void submit()}>将已保存稿送入事实审批</button>
      <small>送审仅使用已保存且来源有效的修改稿。未保存的改动不会随送审提交。</small></>:null}
    <button type="button" disabled={busy} onClick={()=>void load()}>{busy?"正在核对…":pendingSubmission?"查询本次送审结果":pending?"查询本次保存结果":"重新读取服务器稿（替换未保存文字）"}</button>
    {draft?<small>修改稿修订 {draft.revision} · 不可直接提交法院</small>:null}
  </section>;
}
