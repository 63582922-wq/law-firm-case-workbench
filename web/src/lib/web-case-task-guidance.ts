import type { WebCaseAgentRun } from "./web-lawyer-api.ts";

/** Explain an empty decision list without implying the task is progressing. */
export function emptyCaseTaskDecisionMessage(status: WebCaseAgentRun["status"]): string {
  switch (status) {
    case "WAITING_INPUT":
      return "任务已停下，但当前没有可处理的决定项。请查看任务原因；这不代表你漏填了材料，也不代表系统仍在继续分析。";
    case "FAILED":
      return "本次任务未完成，当前没有需要你批准的事项。已有材料和成果记录保留；技术失败不能通过律师批准解决。";
    case "RECONCILIATION_REQUIRED":
      return "系统尚未确认上一次执行结果，需由运维核对。请勿重复交办或重新上传；当前没有可处理的律师决定项。";
    case "PAUSED":
      return "任务已暂停，当前没有待处理决定。需要继续时，使用任务提供的继续操作。";
    case "CANCELLED":
      return "本次任务已取消，不会继续执行。历史成果不等于已完成交付。";
    case "STALE":
      return "案件输入已变化，本轮成果不能代替当前案情分析。请核对变化，再按当前案情重新交办。";
    case "READY_FOR_REVIEW":
      return "本轮成果等待审阅。请查看分析依据、文书与下载状态；没有流程待办不代表内容已经获批。";
    case "COMPLETED":
      return "本轮任务已完成。请查看具体成果及其适用版本；任务完成不代表材料已经提交法院。";
    default:
      return "当前没有需要你处理的流程决定。执行情况以任务状态为准；成果中的法律判断仍须律师审阅。";
  }
}
