#!/usr/bin/env python3
"""Preflight enabled provider capabilities without logging credentials or content.

The standard modes deliberately perform bounded live provider probes.  The
fixed local defendant-response acceptance uses ``controlled-defence`` instead:
its one allowed model call is the later, durable lawyer-analysis task, so this
startup check validates only server configuration inside a no-network
namespace.

The materials-configuration mode extends that offline check to planning and
ledger extraction. It proves configuration only, not balance or inference;
bounded durable task execution must establish those before acceptance.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
import re


DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_BALANCE_ENDPOINT = "https://api.deepseek.com/user/balance"
QWEN_MODEL = "qwen3.5-ocr"
QWEN_HOST_SUFFIX = ".cn-beijing.maas.aliyuncs.com"
OCR_PROBE_CODE = "7391"


class DeepSeekBalanceUnavailable(RuntimeError):
    """The configured account is valid but cannot currently fund inference."""


class DeepSeekBalanceCheckRejected(RuntimeError):
    """The pinned balance preflight could not establish account availability."""


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise RuntimeError(f"missing:{name}")
    return value


def _deepseek_balance_available(api_key: str) -> dict[str, object]:
    """Fail before a billed model request without exposing account amounts."""

    from time import monotonic
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        DEEPSEEK_BALANCE_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )
    started = monotonic()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=20) as response:
            if int(response.status) != 200:
                raise DeepSeekBalanceCheckRejected("DeepSeek balance status differs")
            body = response.read(64 * 1024 + 1)
    except urllib.error.HTTPError as error:
        raise DeepSeekBalanceCheckRejected(
            f"DeepSeek balance request was rejected with HTTP {error.code}"
        ) from error
    except urllib.error.URLError as error:
        raise DeepSeekBalanceCheckRejected(
            "DeepSeek balance availability could not be established"
        ) from error
    if len(body) > 64 * 1024:
        raise DeepSeekBalanceCheckRejected("DeepSeek balance response is oversized")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeepSeekBalanceCheckRejected(
            "DeepSeek balance response is not JSON"
        ) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("is_available"), bool):
        raise DeepSeekBalanceCheckRejected("DeepSeek balance contract differs")
    if payload["is_available"] is not True:
        raise DeepSeekBalanceUnavailable("DeepSeek account balance is unavailable")
    return {
        "provider": "deepseek",
        "capability": "account_balance",
        "available": True,
        "elapsed_ms": round((monotonic() - started) * 1000),
    }


def _parse_chat_response(
    body: bytes, *, expected_model: str, allow_exact_json_fence: bool = False
) -> tuple[str, object]:
    value = json.loads(body)
    if not isinstance(value, dict) or value.get("model") != expected_model:
        raise RuntimeError("provider model differs")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise RuntimeError("provider choice count differs")
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    if finish_reason != "stop":
        raise RuntimeError("provider response did not stop")
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        if allow_exact_json_fence:
            stripped = content.strip()
            lines = stripped.splitlines()
            if (
                len(lines) >= 3
                and lines[0] == "```json"
                and lines[-1] == "```"
                and "```" not in "\n".join(lines[1:-1])
            ):
                content = "\n".join(lines[1:-1])
        content = json.loads(content)
    if not isinstance(content, dict):
        raise RuntimeError("provider content is not structured JSON")
    return finish_reason, content


def _deepseek_document(api_key: str) -> dict[str, object]:
    from time import monotonic

    from case_kernel.case_agent_document_exchange_postgres import (
        DeepSeekDocumentRawHttpsTransport,
    )
    from case_kernel.deepseek_document_drafting import (
        DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
        DeepSeekDocumentDraftConfig,
        DeepSeekDocumentDraftCredentials,
        PreparedDeepSeekDocumentRequest,
    )

    body = _json_bytes(
        {
            "model": DEEPSEEK_MODEL,
            "temperature": 0,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "Only return the requested JSON object. Do not use Markdown.",
                },
                {
                    "role": "user",
                    "content": (
                        'Return exactly one JSON object with schema_version '
                        '"lawcase-provider-preflight-v1" and document_ready true.'
                    ),
                },
            ],
        }
    )
    config = DeepSeekDocumentDraftConfig(
        endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
        model=DEEPSEEK_MODEL,
        allowed_models=(DEEPSEEK_MODEL,),
        timeout_seconds=120,
        max_output_tokens=1024,
    )
    transport = DeepSeekDocumentRawHttpsTransport(
        credentials=DeepSeekDocumentDraftCredentials(api_key=api_key),
        config=config,
    )
    started = monotonic()
    response = transport.send_raw(
        prepared=PreparedDeepSeekDocumentRequest(
            endpoint=DEEPSEEK_OFFICIAL_CHAT_COMPLETIONS_ENDPOINT,
            model=DEEPSEEK_MODEL,
            body=body,
            request_hash=sha256(body).hexdigest(),
        )
    )
    finish_reason, content = _parse_chat_response(response, expected_model=DEEPSEEK_MODEL)
    if content.get("schema_version") != "lawcase-provider-preflight-v1" or content.get("document_ready") is not True:
        raise RuntimeError("document JSON contract differs")
    return {
        "provider": "deepseek",
        "capability": "document",
        "model": DEEPSEEK_MODEL,
        "finish_reason": finish_reason,
        "json_valid": True,
        "elapsed_ms": round((monotonic() - started) * 1000),
    }


def _deepseek_ledger(api_key: str) -> dict[str, object]:
    from time import monotonic

    from case_kernel.case_agent_ledger_extraction_adapters import (
        DEEPSEEK_LEDGER_EXTRACTION_MAX_TOKENS,
        DEEPSEEK_LEDGER_EXTRACTION_SYSTEM_PROMPT,
        PreparedLedgerExtractionRequest,
    )
    from case_kernel.case_agent_ledger_extraction_exchange_postgres import (
        DeepSeekLedgerExtractionCredentials,
        DeepSeekLedgerExtractionRawHttpsTransport,
    )

    body = _json_bytes(
        {
            "model": DEEPSEEK_MODEL,
            "temperature": 0,
            "max_tokens": DEEPSEEK_LEDGER_EXTRACTION_MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": DEEPSEEK_LEDGER_EXTRACTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "pages": [
                                {
                                    "evidence_page_id": "55555555-5555-4555-8555-555555555555",
                                    "page_number": 1,
                                    "source_mode": "NATIVE_TEXT",
                                    "text": "测试付款凭证：2026年8月15日付款人甲向收款人乙转账人民币100元。",
                                }
                            ]
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
        }
    )
    transport = DeepSeekLedgerExtractionRawHttpsTransport(
        credentials=DeepSeekLedgerExtractionCredentials(api_key=api_key),
        timeout_seconds=120,
    )
    started = monotonic()
    response = transport.send_raw(
        request=PreparedLedgerExtractionRequest(
            external_request_id="66666666-6666-4666-8666-666666666666",
            request_hash=sha256(body).hexdigest(),
            body=body,
        )
    )
    finish_reason, content = _parse_chat_response(response, expected_model=DEEPSEEK_MODEL)
    if set(content) != {"candidates"} or not isinstance(content["candidates"], list):
        raise RuntimeError("ledger JSON contract differs")
    return {
        "provider": "deepseek",
        "capability": "ledger_extraction",
        "model": DEEPSEEK_MODEL,
        "finish_reason": finish_reason,
        "json_valid": True,
        "elapsed_ms": round((monotonic() - started) * 1000),
    }


def _ocr_png() -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1024, 320), color="white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        size=64,
    )
    draw.text((48, 112), f"LAWCASE OCR CHECK {OCR_PROBE_CODE}", fill="black", font=font)
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _qwen_ocr(api_key: str, workspace_id: str) -> dict[str, object]:
    from base64 import b64encode
    from time import monotonic

    from case_kernel.qwen_visual_ocr_transport import (
        PinnedQwenVisualOcrHttpsBroker,
        QwenVisualOcrTransportRequest,
    )

    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,62}", workspace_id) is None:
        raise RuntimeError("Qwen workspace id is invalid")
    host = f"{workspace_id}{QWEN_HOST_SUFFIX}"
    image = _ocr_png()
    body = _json_bytes(
        {
            "model": QWEN_MODEL,
            "stream": False,
            "max_tokens": 512,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + b64encode(image).decode("ascii")
                            },
                            "min_pixels": 3072,
                            "max_pixels": 30_720_000,
                        },
                        {
                            "type": "text",
                            "text": (
                                "Read the visible page. Return only one JSON object with exactly one "
                                "key named text. Its value must contain every visible character in "
                                "reading order. Do not use Markdown."
                            ),
                        },
                    ],
                }
            ],
        }
    )
    request_hash = sha256(body).hexdigest()
    broker = PinnedQwenVisualOcrHttpsBroker()
    started = monotonic()
    result = broker.send(
        request=QwenVisualOcrTransportRequest(
            external_request_id="77777777-7777-4777-8777-777777777777",
            endpoint=f"https://{host}/compatible-mode/v1/chat/completions",
            endpoint_host=host,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            body=body,
            request_hash=request_hash,
            projection_hash=sha256(b"lawcase-provider-preflight-projection-v1").hexdigest(),
            rendered_page_sha256=sha256(image).hexdigest(),
            timeout_seconds=120,
            max_response_bytes=2 * 1024 * 1024,
        )
    )
    finish_reason, content = _parse_chat_response(
        result.response_body,
        expected_model=QWEN_MODEL,
        allow_exact_json_fence=True,
    )
    if (
        set(content) != {"text"}
        or not isinstance(content["text"], str)
        or "LAWCASE" not in content["text"].upper()
        or OCR_PROBE_CODE not in content["text"]
    ):
        raise RuntimeError("Qwen did not read the image probe code")
    return {
        "provider": "qwen",
        "capability": "visual_ocr",
        "model": QWEN_MODEL,
        "finish_reason": finish_reason,
        "json_valid": True,
        "image_probe_recognized": True,
        "elapsed_ms": round((monotonic() - started) * 1000),
    }


def _controlled_defence_configuration() -> dict[str, object]:
    """Validate the one enabled provider without performing a probe call.

    A successful result means only that the server-side credentials satisfy
    the strict local shape contract.  It is intentionally not evidence that
    the provider is reachable or that the model can return a valid result;
    ADR-0058 reserves that proof for the single durable lawyer-analysis task.
    """

    from case_kernel.case_agent_lawyer_analysis_transport import (
        QwenLawyerAnalysisCredentials,
    )

    credentials = QwenLawyerAnalysisCredentials(
        api_key=_required("LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_API_KEY"),
        workspace_id=_required("LAWCASE_AGENT_WORKER_LAWYER_ANALYSIS_WORKSPACE_ID"),
    )
    return {
        "provider": "qwen",
        "capability": "lawyer_analysis",
        "configuration_valid": True,
        "network_calls": 0,
        "endpoint_host_hash": sha256(
            credentials.endpoint_host.encode("ascii")
        ).hexdigest(),
    }


def _materials_configuration() -> list[dict[str, object]]:
    """Check wired credentials only; durable tasks still own live-call proof."""
    from case_kernel.deepseek_case_agent_planner import DeepSeekPlannerCredentials
    from case_kernel.case_agent_ledger_extraction_exchange_postgres import DeepSeekLedgerExtractionCredentials

    DeepSeekPlannerCredentials(api_key=_required("LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY"))
    DeepSeekLedgerExtractionCredentials(api_key=_required("LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY"))
    if _required("LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_MODEL") != DEEPSEEK_MODEL:
        raise RuntimeError("ledger extraction model differs")
    return [
        {"provider": "deepseek", "capability": capability, "configuration_valid": True,
         "network_calls": 0, "model": DEEPSEEK_MODEL}
        for capability in ("planning", "ledger_extraction")
    ] + [_controlled_defence_configuration()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("all", "deepseek", "qwen", "controlled-defence", "materials-configuration"),
        default="all",
    )
    args = parser.parse_args()
    stage = "configuration"
    checks: list[dict[str, object]] = []
    try:
        if args.mode == "materials-configuration":
            stage = "materials_configuration"
            checks.extend(_materials_configuration())
        if args.mode == "controlled-defence":
            stage = "lawyer_analysis_configuration"
            checks.append(_controlled_defence_configuration())
        if args.mode in {"all", "deepseek"}:
            document_key = _required("LAWCASE_AGENT_WORKER_DEEPSEEK_API_KEY")
            ledger_key = _required("LAWCASE_AGENT_WORKER_LEDGER_EXTRACTION_API_KEY")
            stage = "deepseek_balance"
            checks.append(_deepseek_balance_available(document_key))
            if ledger_key != document_key:
                checks.append(_deepseek_balance_available(ledger_key))
            stage = "deepseek_document"
            checks.append(_deepseek_document(document_key))
            stage = "deepseek_ledger_extraction"
            checks.append(_deepseek_ledger(ledger_key))
        if args.mode in {"all", "qwen"}:
            qwen_key = _required("LAWCASE_AGENT_WORKER_QWEN_API_KEY")
            workspace_id = _required("LAWCASE_AGENT_WORKER_QWEN_WORKSPACE_ID")
            stage = "qwen_visual_ocr"
            checks.append(_qwen_ocr(qwen_key, workspace_id))
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema_version": "lawcase-provider-preflight-v1",
                    "status": "BLOCKED",
                    "mode": args.mode,
                    "stage": stage,
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "schema_version": "lawcase-provider-preflight-v1",
                "status": "PASS",
                "mode": args.mode,
                "checks": checks,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
