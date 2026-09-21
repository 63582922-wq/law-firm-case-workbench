"""Server-owned goal contract for ledger-exception control recovery.

The browser supplies only the outer command idempotency key.  Recovery goal
identity and semantics are fixed here and are independently recomputed by the
PostgreSQL 0052 contract before a run may be resumed, transferred or claimed.
"""

from __future__ import annotations

from case_kernel.case_agent_supervisor import AgentGoal
from case_kernel.models import Actor


RECOVERY_OBJECTIVE = "恢复本案异常材料后续工作并基于当前权威台账继续研判"
RECOVERY_SUCCESS_CRITERIA = (
    "接管全部待完成异常分流工作",
    "重新提取任务覆盖原异常组完整受管来源并通过独立校验",
    "全部后续工作完成后基于当前案件版本重新规划",
)
RECOVERY_CONSTRAINTS = (
    "不得自动确认正式事实、法律口径或对外提交",
    "不得读取其他案件或使用浏览器提供的运行、图谱或对象定位",
)


def build_web_case_agent_recovery_goal(
    *, actor: Actor, replacement_run_id: str
) -> AgentGoal:
    """Build the one canonical goal allowed to own a recovery run.

    New 0052 recovery runs deliberately use the replacement run UUID as their
    goal UUID.  PostgreSQL derives the same binding before ``RUN_CREATED``;
    therefore a generic Web create-run call (which derives a different goal
    UUID) cannot occupy the prepared run identity with arbitrary semantics.
    """

    return AgentGoal.build(
        goal_id=replacement_run_id,
        objective=RECOVERY_OBJECTIVE,
        success_criteria=RECOVERY_SUCCESS_CRITERIA,
        constraints=RECOVERY_CONSTRAINTS,
        requested_by=actor.actor_id,
    )


__all__ = (
    "RECOVERY_CONSTRAINTS",
    "RECOVERY_OBJECTIVE",
    "RECOVERY_SUCCESS_CRITERIA",
    "build_web_case_agent_recovery_goal",
)
