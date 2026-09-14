"""Combined issue discovery and analysis request using the existing governed provider.

The adapter selects this contract for unconfirmed extraction sources. Historical
confirmed-ledger runs retain their legacy reader. No discovery-only call is required.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Mapping

from .case_agent_issue_discovery import issue_discovery_schema
from .case_agent_lawyer_analysis import (LAWYER_ANALYSIS_MAX_INPUT_ESTIMATE,
    LawyerAnalysisBlocked, _source_hash, _numeric_fact_source_index, _prompt_numeric_fact_labels,
    _prompt_source, _json_bytes, _system_prompt, _prepare_lawyer_analysis_request_for_contract)


@dataclass(frozen=True)
class DiscoveredAnalysisContract:
    run_id: str
    task_input_hash: str
    source_hash: str
    source_ids: tuple[str, ...]
    authority_ids: tuple[str, ...]
    numeric_fact_sources: Mapping[str, tuple[str, ...]]
    schema: Mapping[str, object]
    system_prompt: str
    user_prompt: str
    estimated_input_tokens: int
    schema_name: str = "case_agent_discovered_analysis_v1"

    def validate(self):
        from uuid import UUID
        UUID(self.run_id)
        for value in (self.task_input_hash, self.source_hash):
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise LawyerAnalysisBlocked("discovered analysis source binding differs")
        if (not 1 <= len(self.source_ids) <= 500 or len(set(self.source_ids)) != len(self.source_ids)
                or not set(self.authority_ids).issubset(self.source_ids)
                or not 1 <= self.estimated_input_tokens <= LAWYER_ANALYSIS_MAX_INPUT_ESTIMATE):
            raise LawyerAnalysisBlocked("discovered analysis source scope or input limit differs")
        if self.estimated_input_tokens != len(self.system_prompt) + len(self.user_prompt):
            raise LawyerAnalysisBlocked("discovered analysis estimate differs")


def build_discovered_analysis_contract(projection):
    projection.validate()
    source_ids = tuple(source.input_ref for source in projection.sources)
    authorities = tuple(source.input_ref for source in projection.sources
        if source.source_type.value == "VERIFIED_LEGAL_SOURCE")
    numerics = _numeric_fact_source_index({source.input_ref: (source.primary_text, source.secondary_text)
        for source in projection.sources})
    labels = _prompt_numeric_fact_labels(numerics)
    schema = issue_discovery_schema(source_ids)
    issue = schema["properties"]["issues"]["items"]
    def text():
        return {"type": "string", "minLength": 1, "maxLength": 400}
    def texts():
        return {"type": "array", "items": text(), "minItems": 0, "maxItems": 5}
    analysis_fields = {"strengths": texts(), "weaknesses": texts(), "missing_evidence": texts(),
        "rebuttal_route": text(), "residual_risk": text(), "next_action": text(),
        "authority_refs": {"type": "array", "items": {"type": "string", "enum": list(authorities) or ["NO_AUTHORITY_AVAILABLE"]},
                           "minItems": 0, "maxItems": 4 if authorities else 0}}
    issue["properties"].update(analysis_fields)
    issue["required"].extend(analysis_fields)
    # Reuse legal/data/quantity boundaries, but remove obsolete forced-anchor instructions.
    base = _system_prompt()
    start = base.index("每个issues的supporting_source_refs")
    end = base.index("所有自由文本必须是定性表达", start)
    system = base[:start] + base[end:]
    system += (
        "本次任务是在同一响应中发现实质问题并完成初步分析，不是逐来源造风险。"
        "从多份材料之间的支持、冲突和不确定性提出问题，用自拟标题和question描述。"
        "our_position必须站在代理档案所确定的我方，opponent_position是对方已主张或明确标为可能的反驳。"
        "未知立场写尚待确认，不替任何一方承认事实。各问题source_refs包含分析实际依赖的原始来源。"
        "source_dispositions逐条覆盖全部输入：背景信息不必形成问题，关联性不明的材料明确标为待核。"
        "不要把案号、日期或复核流程本身写成实体争点；不要为凑数量编造风险。"
        "missing_evidence只列已核对现有材料后仍缺少的具体证据，不索取已持有的同一文件或让当事人做法律研究。"
        "没有已核验法源时，仍应完成材料矛盾分析及具体下一步，不得把整个任务退回律师。"
        "issue key仅用于本响应内关联，既不是正式争点编号，也不表示审批。"
    )
    user = _json_bytes({"run_id": projection.run_id,
        "authority_mode": "VERIFIED" if authorities else "RESEARCH_HYPOTHESES_ONLY",
        "verified_authority_refs": authorities,
        "sources": [_prompt_source(source, numeric_fact_labels=labels) for source in projection.sources],
        "output_policy": "propose_issues_and_analyze_together_account_for_all_sources_no_formal_approval"}).decode()
    contract = DiscoveredAnalysisContract(projection.run_id, projection.task_input_hash,
        _source_hash(projection), source_ids, authorities, numerics, schema, system, user, len(system) + len(user))
    contract.validate()
    return contract


def prepare_discovered_analysis_request(*, projection, task_id, attempt_id, endpoint_host):
    contract = build_discovered_analysis_contract(projection)
    return _prepare_lawyer_analysis_request_for_contract(projection=projection, contract=contract,
        task_id=task_id, attempt_id=attempt_id, endpoint_host=endpoint_host)


def validate_discovered_analysis_core(core, *, contract):
    from .case_agent_issue_discovery import parse_issue_discovery, IssueDiscoveryBlocked
    from .case_agent_lawyer_analysis import _model_text, _ref_list
    contract.validate()
    if not isinstance(core, dict) or set(core) != {"issues", "source_dispositions"}:
        raise LawyerAnalysisBlocked("discovered analysis fields differ")
    if not isinstance(core["issues"], list):
        raise LawyerAnalysisBlocked("discovered analysis issues are invalid")
    discovery_fields = {"key", "title", "question", "our_position", "opponent_position",
                        "source_refs", "needs_lawyer_decision"}
    text_fields = {"rebuttal_route", "residual_risk", "next_action"}
    list_fields = {"strengths", "weaknesses", "missing_evidence"}
    stripped = []
    for row in core["issues"]:
        if not isinstance(row, dict) or set(row) != discovery_fields | text_fields | list_fields | {"authority_refs"}:
            raise LawyerAnalysisBlocked("discovered issue fields differ")
        refs = _ref_list(row["source_refs"], contract.source_ids, "discovered issue", 1, 20)
        _ref_list(row["authority_refs"], contract.authority_ids, "discovered authorities", 0, 4)
        for field in text_fields | {"title", "question", "our_position", "opponent_position"}:
            _model_text(row[field], field, 400, numeric_fact_sources=contract.numeric_fact_sources,
                allowed_numeric_source_refs=refs)
        for field in list_fields:
            values = row[field]
            if not isinstance(values, list) or len(values) > 5:
                raise LawyerAnalysisBlocked("discovered issue text list differs")
            for value in values:
                _model_text(value, field, 400, numeric_fact_sources=contract.numeric_fact_sources,
                    allowed_numeric_source_refs=refs)
        stripped.append({key: row[key] for key in discovery_fields})
    # The model explains every source's role, but it does not own the
    # bidirectional source-to-issue index.  Rebuild that index from the
    # validated issue source_refs so a harmless ordering or duplication error
    # in source_dispositions cannot make a real, source-bound analysis fail.
    # A disposition can only be issue-relevant when the validated issue rows
    # actually cite it; the server derives its exact local keys.
    links_by_source = {source_id: [] for source_id in contract.source_ids}
    for issue in stripped:
        for source_id in issue["source_refs"]:
            links_by_source[source_id].append(issue["key"])
    normalized_dispositions = []
    for disposition in core["source_dispositions"]:
        if not isinstance(disposition, dict):
            raise LawyerAnalysisBlocked("discovered source disposition differs")
        source_ref = disposition.get("source_ref")
        linked_keys = links_by_source.get(source_ref)
        if linked_keys is None:
            raise LawyerAnalysisBlocked("discovered source disposition differs")
        model_disposition = disposition.get("disposition")
        if model_disposition not in {
            "ISSUE_RELEVANT", "BACKGROUND", "UNRESOLVED_RELEVANCE"
        }:
            raise LawyerAnalysisBlocked("discovered source disposition differs")
        derived_disposition = (
            "ISSUE_RELEVANT" if linked_keys
            else ("UNRESOLVED_RELEVANCE"
                  if model_disposition == "UNRESOLVED_RELEVANCE" else "BACKGROUND")
        )
        reason = disposition.get("reason")
        if model_disposition == "ISSUE_RELEVANT" and not linked_keys:
            # The source is still preserved and shown to the lawyer, but it
            # cannot be called a direct issue basis unless an accepted issue
            # row actually cites it.
            reason = "该来源未列为已发现问题的直接依据，保留供律师复核。"
        normalized_dispositions.append({
            "source_ref": source_ref,
            "disposition": derived_disposition,
            "issue_keys": linked_keys,
            "reason": reason,
        })
    try:
        discovery = parse_issue_discovery({"issues": stripped, "source_dispositions": normalized_dispositions},
            source_hash=contract.source_hash, sources=dict.fromkeys(contract.source_ids))
    except (IssueDiscoveryBlocked, TypeError, KeyError) as exc:
        raise LawyerAnalysisBlocked("discovered analysis source coverage differs") from exc
    for disposition in discovery.source_dispositions:
        _model_text(disposition.reason, "source disposition reason", 400,
            numeric_fact_sources=contract.numeric_fact_sources, allowed_numeric_source_refs=(disposition.source_ref,))
    return discovery


def parse_discovered_analysis_provider_response(response, *, contract):
    from .case_agent_lawyer_analysis import (_parse_lawyer_analysis_provider_outer,
        _parse_lawyer_analysis_provider_usage, _reject_duplicate_object, _reject_json_constant,
        ParsedLawyerAnalysisResponse)
    outer, response_id = _parse_lawyer_analysis_provider_outer(response)
    choices = outer.get("choices")
    if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
            or choices[0].get("finish_reason") != "stop"):
        raise LawyerAnalysisBlocked("discovered analysis did not finish normally")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise LawyerAnalysisBlocked("discovered analysis content is empty")
    try:
        core = json.loads(content, object_pairs_hook=_reject_duplicate_object, parse_constant=_reject_json_constant)
    except (ValueError, TypeError) as exc:
        raise LawyerAnalysisBlocked("discovered analysis JSON is invalid") from exc
    validate_discovered_analysis_core(core, contract=contract)
    prompt, completion, total, cost = _parse_lawyer_analysis_provider_usage(outer)
    return ParsedLawyerAnalysisResponse(core, (), response_id, sha256(response).hexdigest(),
        prompt, completion, total, cost)
