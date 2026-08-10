//! Strict native request/response envelope for a single authorised evidence
//! page.  This module does not perform network I/O; the transport is kept
//! separate so tests cannot accidentally spend money or send case material.

use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};

use crate::model_provider_vault::QwenOcrCredentials;

pub(crate) const QWEN_OCR_MODEL: &str = "qwen3.5-ocr";
pub(crate) const MAX_QWEN_OCR_PAGE_BYTES: usize = 20 * 1024 * 1024;
pub(crate) const MAX_QWEN_OCR_OUTPUT_CHARS: usize = 32_768;

pub(crate) struct PreparedQwenOcrRequest {
    pub(crate) endpoint: String,
    pub(crate) authorization: String,
    pub(crate) body: Vec<u8>,
    pub(crate) request_hash: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct QwenOcrResult {
    pub(crate) text: String,
    pub(crate) output_hash: String,
    pub(crate) provider_request_ref_hash: String,
}

/// Construct the only supported outbound OCR request shape.  The caller does
/// not choose a URL, model id, prompt, or an arbitrary content type.
pub(crate) fn prepare_single_page_ocr(
    credentials: &QwenOcrCredentials,
    png_page: &[u8],
) -> Result<PreparedQwenOcrRequest, String> {
    if png_page.is_empty() || png_page.len() > MAX_QWEN_OCR_PAGE_BYTES {
        return Err("待识别证据页为空或超过单页受控上传上限。".to_string());
    }
    if !is_png(png_page) {
        return Err("待识别内容不是已验证的 PNG 证据页。".to_string());
    }
    let host = qwen_workspace_host(&credentials.workspace_id, &credentials.region_id)?;
    let image_url = format!("data:image/png;base64,{}", STANDARD.encode(png_page));
    // Deliberately request text only.  OCR output remains a review candidate;
    // it never becomes a fact, payment record or filing document by itself.
    let body = serde_json::to_vec(&json!({
        "model": QWEN_OCR_MODEL,
        "stream": false,
        "temperature": 0.01,
        "max_tokens": MAX_QWEN_OCR_OUTPUT_CHARS,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                    "min_pixels": 3072,
                    "max_pixels": 8388608
                },
                {
                    "type": "text",
                    "text": "请逐字提取本页可辨识文字。不要总结、推断、补全或解释；无法辨识处用 ? 标示。仅输出原文文本。"
                }
            ]
        }]
    }))
    .map_err(|_| "无法构造受控 OCR 请求。".to_string())?;
    let request_hash = sha256_hex(&body);
    Ok(PreparedQwenOcrRequest {
        endpoint: format!("https://{host}/compatible-mode/v1/chat/completions"),
        authorization: format!("Bearer {}", credentials.api_key.as_str()),
        body,
        request_hash,
    })
}

pub(crate) fn parse_single_page_ocr_response(
    response_body: &[u8],
    provider_request_ref: &str,
) -> Result<QwenOcrResult, String> {
    if response_body.is_empty() || response_body.len() > 2 * 1024 * 1024 {
        return Err("OCR 服务回执为空或超过受控上限。".to_string());
    }
    if provider_request_ref.is_empty() || provider_request_ref.len() > 512 {
        return Err("OCR 服务回执标识无效。".to_string());
    }
    let parsed: QwenResponse = serde_json::from_slice(response_body)
        .map_err(|_| "OCR 服务回执格式无效。".to_string())?;
    let text = parsed
        .choices
        .first()
        .and_then(|choice| choice.message.content.as_ref())
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty() && value.chars().count() <= MAX_QWEN_OCR_OUTPUT_CHARS)
        .ok_or_else(|| "OCR 服务未返回可复核的文本结果。".to_string())?;
    Ok(QwenOcrResult {
        output_hash: sha256_hex(text.as_bytes()),
        provider_request_ref_hash: sha256_hex(provider_request_ref.as_bytes()),
        text,
    })
}

pub(crate) fn qwen_workspace_host(workspace_id: &str, region_id: &str) -> Result<String, String> {
    if !valid_workspace_id(workspace_id) {
        return Err("百炼业务空间 ID 无效。".to_string());
    }
    let suffix = match region_id {
        "cn-beijing" => "cn-beijing.maas.aliyuncs.com",
        "ap-southeast-1" => "ap-southeast-1.maas.aliyuncs.com",
        _ => return Err("百炼地域不在受控白名单中。".to_string()),
    };
    Ok(format!("{workspace_id}.{suffix}"))
}

fn valid_workspace_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    (3..=120).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes.iter().all(|byte| byte.is_ascii_alphanumeric() || *byte == b'-')
}

fn is_png(value: &[u8]) -> bool {
    value.starts_with(b"\x89PNG\r\n\x1a\n")
}

fn sha256_hex(value: &[u8]) -> String {
    let mut digest = Sha256::new();
    digest.update(value);
    format!("{:x}", digest.finalize())
}

#[derive(Deserialize)]
struct QwenResponse {
    choices: Vec<QwenChoice>,
}

#[derive(Deserialize)]
struct QwenChoice {
    message: QwenMessage,
}

#[derive(Deserialize)]
struct QwenMessage {
    content: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::{
        parse_single_page_ocr_response, prepare_single_page_ocr, qwen_workspace_host,
        MAX_QWEN_OCR_PAGE_BYTES,
    };
    use crate::model_provider_vault::QwenOcrCredentials;
    use zeroize::Zeroizing;

    fn credentials() -> QwenOcrCredentials {
        QwenOcrCredentials {
            api_key: Zeroizing::new("k".repeat(24)),
            region_id: "cn-beijing".to_string(),
            workspace_id: "workspace-prod-01".to_string(),
        }
    }

    #[test]
    fn only_allowlisted_workspace_hosts_are_constructed() {
        assert_eq!(
            qwen_workspace_host("workspace-prod-01", "cn-beijing").unwrap(),
            "workspace-prod-01.cn-beijing.maas.aliyuncs.com"
        );
        assert!(qwen_workspace_host("workspace.prod", "cn-beijing").is_err());
        assert!(qwen_workspace_host("workspace-prod", "https://example.invalid").is_err());
    }

    #[test]
    fn request_is_one_png_page_with_fixed_ocr_contract() {
        let request = prepare_single_page_ocr(&credentials(), b"\x89PNG\r\n\x1a\nminimal").unwrap();
        let body = String::from_utf8(request.body).unwrap();
        assert!(request.endpoint.starts_with("https://workspace-prod-01.cn-beijing.maas.aliyuncs.com/"));
        assert!(body.contains("qwen3.5-ocr"));
        assert!(body.contains("data:image/png;base64,"));
        assert!(!request.authorization.contains("minimal"));
        assert_eq!(request.request_hash.len(), 64);
    }

    #[test]
    fn rejects_non_png_or_oversized_pages_before_any_transport() {
        assert!(prepare_single_page_ocr(&credentials(), b"not-a-page").is_err());
        let mut oversized = b"\x89PNG\r\n\x1a\n".to_vec();
        oversized.resize(MAX_QWEN_OCR_PAGE_BYTES + 1, 0);
        assert!(prepare_single_page_ocr(&credentials(), &oversized).is_err());
    }

    #[test]
    fn response_is_text_only_and_hashes_are_audit_safe() {
        let result = parse_single_page_ocr_response(
            "{\"choices\":[{\"message\":{\"content\":\"  还款 100 元  \"}}]}".as_bytes(),
            "provider-request-123",
        )
        .unwrap();
        assert_eq!(result.text, "还款 100 元");
        assert_eq!(result.output_hash.len(), 64);
        assert_eq!(result.provider_request_ref_hash.len(), 64);
    }
}
