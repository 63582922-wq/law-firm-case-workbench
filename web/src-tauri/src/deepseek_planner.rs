//! Fixed-shape native transport helpers for the case-planning model.
//!
//! This module deliberately knows nothing about folders, original files, a
//! browser prompt, or a general-purpose Tool API.  A later native command may
//! pass it only a lawyer-authorised minimal case projection and a closed list
//! of registered Skills.  The response is a plan candidate, never a fact,
//! payment classification, legal rule, or court document.

use crate::model_provider_vault::DeepSeekPlannerCredentials;
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use zeroize::Zeroizing;

const DEEPSEEK_CHAT_ENDPOINT: &str = "https://api.deepseek.com/chat/completions";
const DEEPSEEK_CASE_PLANNER_MODEL: &str = "deepseek-v4-pro";
const MAX_PROJECTION_BYTES: usize = 24 * 1024;
const MAX_TASK_LABEL_BYTES: usize = 240;
const MAX_PLAN_PROPOSALS: usize = 12;

pub(crate) struct PreparedCasePlanRequest {
    pub(crate) endpoint: &'static str,
    pub(crate) authorization: Zeroizing<String>,
    pub(crate) body: Vec<u8>,
    pub(crate) request_hash: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DeepSeekPlanProposal {
    pub(crate) skill_id: String,
    pub(crate) tool_id: String,
    pub(crate) rationale: String,
}

pub(crate) fn prepare_case_plan_request(
    credentials: &DeepSeekPlannerCredentials,
    task_label: &str,
    authorised_case_projection: &str,
    registered_skills: &[(&str, &str)],
) -> Result<PreparedCasePlanRequest, String> {
    let task_label = task_label.trim();
    if task_label.is_empty() || task_label.len() > MAX_TASK_LABEL_BYTES {
        return Err("案件任务名称无效；未向 DeepSeek 发送任何内容。".to_string());
    }
    if authorised_case_projection.is_empty()
        || authorised_case_projection.len() > MAX_PROJECTION_BYTES
        || !authorised_case_projection.is_char_boundary(authorised_case_projection.len())
    {
        return Err("案件最小快照无效或超出授权上限；未向 DeepSeek 发送任何内容。".to_string());
    }
    if registered_skills.is_empty() || registered_skills.len() > 32 {
        return Err("受控 Skill 清单无效；未向 DeepSeek 发送任何内容。".to_string());
    }
    if registered_skills
        .iter()
        .any(|(skill_id, tool_id)| !valid_capability_id(skill_id) || !valid_capability_id(tool_id))
    {
        return Err("受控 Skill 清单字段无效；未向 DeepSeek 发送任何内容。".to_string());
    }
    let skills: Vec<Value> = registered_skills
        .iter()
        .map(|(skill_id, tool_id)| json!({ "skill_id": skill_id, "tool_id": tool_id }))
        .collect();
    let body = serde_json::to_vec(&json!({
        "model": DEEPSEEK_CASE_PLANNER_MODEL,
        "temperature": 0,
        "max_tokens": 1200,
        "response_format": { "type": "json_object" },
        "messages": [
            {
                "role": "system",
                "content": "你是律师案件工作台的受控计划器。只从允许的 skill_id/tool_id 组合中提出最少步骤。不得输出事实结论、付款性质、利率、法律意见、法院文书或未列出的工具。只输出 JSON 对象：{\\\"proposals\\\":[{\\\"skill_id\\\":string,\\\"tool_id\\\":string,\\\"rationale\\\":string}]}。"
            },
            {
                "role": "user",
                "content": {
                    "task": task_label,
                    "authorised_case_projection": authorised_case_projection,
                    "allowed_skill_tools": skills
                }
            }
        ]
    }))
    .map_err(|_| "无法编码受控案件计划请求；未向 DeepSeek 发送任何内容。".to_string())?;
    let request_hash = sha256_hex(&body);
    Ok(PreparedCasePlanRequest {
        endpoint: DEEPSEEK_CHAT_ENDPOINT,
        authorization: Zeroizing::new(format!("Bearer {}", credentials.api_key.as_str())),
        body,
        request_hash,
    })
}

pub(crate) fn parse_case_plan_response(body: &[u8]) -> Result<Vec<DeepSeekPlanProposal>, String> {
    let response: DeepSeekChatResponse = serde_json::from_slice(body)
        .map_err(|_| "DeepSeek 未返回可复核的计划 JSON。".to_string())?;
    let content = response
        .choices
        .first()
        .and_then(|choice| choice.message.content.as_deref())
        .ok_or_else(|| "DeepSeek 响应缺少计划内容。".to_string())?;
    let plan: DeepSeekPlanEnvelope = serde_json::from_str(content)
        .map_err(|_| "DeepSeek 计划不是受限 JSON 结构。".to_string())?;
    if plan.proposals.is_empty() || plan.proposals.len() > MAX_PLAN_PROPOSALS {
        return Err("DeepSeek 计划步骤数量超出受控范围。".to_string());
    }
    let proposals: Vec<DeepSeekPlanProposal> = plan
        .proposals
        .into_iter()
        .map(|item| {
            let skill_id = item.skill_id.trim().to_string();
            let tool_id = item.tool_id.trim().to_string();
            let rationale = item.rationale.trim().to_string();
            if !valid_capability_id(&skill_id)
                || !valid_capability_id(&tool_id)
                || rationale.is_empty()
                || rationale.len() > 1_000
            {
                return Err("DeepSeek 计划字段超出受控范围。".to_string());
            }
            Ok(DeepSeekPlanProposal {
                skill_id,
                tool_id,
                rationale,
            })
        })
        .collect::<Result<_, _>>()?;
    let mut pairs = std::collections::BTreeSet::new();
    if proposals
        .iter()
        .any(|item| !pairs.insert((item.skill_id.as_str(), item.tool_id.as_str())))
    {
        return Err("DeepSeek 计划包含重复的 Skill 步骤。".to_string());
    }
    Ok(proposals)
}

#[derive(Deserialize)]
struct DeepSeekChatResponse {
    choices: Vec<DeepSeekChoice>,
}

#[derive(Deserialize)]
struct DeepSeekChoice {
    message: DeepSeekMessage,
}

#[derive(Deserialize)]
struct DeepSeekMessage {
    content: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeepSeekPlanEnvelope {
    proposals: Vec<DeepSeekPlanProposalWire>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeepSeekPlanProposalWire {
    skill_id: String,
    tool_id: String,
    rationale: String,
}

fn sha256_hex(value: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(value);
    format!("{:x}", hasher.finalize())
}

fn valid_capability_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=120).contains(&bytes.len())
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

#[cfg(test)]
mod tests {
    use super::{parse_case_plan_response, prepare_case_plan_request};
    use crate::model_provider_vault::DeepSeekPlannerCredentials;
    use zeroize::Zeroizing;

    fn credentials() -> DeepSeekPlannerCredentials {
        DeepSeekPlannerCredentials {
            api_key: Zeroizing::new("k".repeat(24)),
        }
    }

    #[test]
    fn parses_only_a_bounded_structured_plan() {
        let response = r#"{"choices":[{"message":{"content":"{\"proposals\":[{\"skill_id\":\"office_reading\",\"tool_id\":\"parse_office_document\",\"rationale\":\"Read the approved document scope first.\"}]}"}}]}"#;
        let proposals = parse_case_plan_response(response.as_bytes()).expect("structured plan");
        assert_eq!(proposals.len(), 1);
        assert_eq!(proposals[0].tool_id, "parse_office_document");
    }

    #[test]
    fn fixed_request_cannot_choose_model_or_raw_tool_shape() {
        let request = prepare_case_plan_request(
            &credentials(),
            "核对案件材料",
            r#"{"projection_version":"case-plan-minimal-v1"}"#,
            &[("office_reading", "parse_office_document")],
        )
        .expect("fixed request");
        let body = String::from_utf8(request.body).expect("json body");
        assert_eq!(
            request.endpoint,
            "https://api.deepseek.com/chat/completions"
        );
        assert!(body.contains("deepseek-v4-pro"));
        assert!(body.contains("allowed_skill_tools"));
        assert!(!body.contains("https://example.invalid"));
        assert_eq!(request.request_hash.len(), 64);
    }

    #[test]
    fn response_rejects_unknown_plan_fields_and_duplicate_steps() {
        let unknown = r#"{"choices":[{"message":{"content":"{\"proposals\":[{\"skill_id\":\"office_reading\",\"tool_id\":\"parse_office_document\",\"rationale\":\"Read.\",\"extra\":true}]}"}}]}"#;
        assert!(parse_case_plan_response(unknown.as_bytes()).is_err());
        let duplicate = r#"{"choices":[{"message":{"content":"{\"proposals\":[{\"skill_id\":\"office_reading\",\"tool_id\":\"parse_office_document\",\"rationale\":\"Read.\"},{\"skill_id\":\"office_reading\",\"tool_id\":\"parse_office_document\",\"rationale\":\"Again.\"}]}"}}]}"#;
        assert!(parse_case_plan_response(duplicate.as_bytes()).is_err());
    }
}
