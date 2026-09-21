"""应诉文书内容：每份文书独立成文，正文里不出现任何系统说明。

按律师视角的规则：

1. **一份文书一个文件**——答辩状、证据目录、质证意见、代理词、授权委托书、
   送达地址确认书、当事人陈述、证据来源说明、调解意见确认、按需申请书，
   各自独立，单独可用于提交或交给当事人签字，不再全部塞进一个 Word；
2. **正文不含系统话术**："由系统生成""律师工作稿""待核清单""生成时间"等一律
   不进提交件；律师需要看的提示统一放在「内部文件（不提交）」里；
3. **待填位置统一下划线**（`＿＿＿＿＿＿`），律师一眼看到该填哪里；
4. 事实、立场、法条一律留空或只引用律师已登记的内容——系统不替律师写事实、
   不替当事人作陈述。

排版交给 `case_api/legal_docx.py`（字体、字号、行距、页边距统一）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Mapping, Sequence

from case_kernel.matter_deliverables import MatterParties

BLANK = "＿＿＿＿＿＿"
SHORT_BLANK = "＿＿＿＿"

# 答辩状正文里可以保留的小节（其余系统区块一律剔除）
_KEEP_SECTION = "## 答辩请求"
_REASON_HEADING = "### "


@dataclass
class LegalDocSpec:
    """一份待排版的文书：相对路径、标题、内容块。"""

    path: str                      # 例如 "01-民事答辩状.docx"
    title: str                     # 文档内标题（"民事答辩状"）
    blocks: list[object] = field(default_factory=list)
    needs_client_signature: bool = False


def _or(value: str, blank: str = BLANK) -> str:
    return value.strip() if value and value.strip() else blank


# 门禁把模型自算的数字改成了这些标记：提交法院的文书里不能出现它们，
# 统一改成手写空白，并在内部文件里登记位置，由律师按计算表填写。
_GATE_MARKERS = ("[见计算表]", "［见计算表］", "（见计算表）", "(见计算表)")
# 系统内部的占位语同样不能出现在提交件里：改成"由律师填写法条"的规范空白
_AUTHORITY_MARKERS = ("[依据待律师登记]", "［依据待律师登记］", "【依据待律师登记】")


def _strip_gate_markers(text: str) -> str:
    cleaned = str(text)
    for marker in _GATE_MARKERS:
        cleaned = cleaned.replace(marker, SHORT_BLANK)
    for marker in _AUTHORITY_MARKERS:
        cleaned = cleaned.replace(marker, "《" + SHORT_BLANK + "》")
    # 连续下划线收敛为固定宽度，读起来像表单而不是一堆横线
    cleaned = re.sub(r"_{6,}", SHORT_BLANK, cleaned)
    cleaned = re.sub(r"＿{6,}", SHORT_BLANK, cleaned)
    return cleaned


def gate_marker_count(markdown: str) -> int:
    """统计草稿里被门禁留空的数字位置（供内部文件提示律师）。"""
    return sum(str(markdown).count(marker) for marker in _GATE_MARKERS)


def _date_line() -> str:
    return "＿＿＿＿年＿＿月＿＿日"


def _party_line(prefix: str, name: str, parties: MatterParties) -> str:
    parts = [f"{prefix}：{_or(name)}"]
    if prefix.startswith("答辩人") or "被告" in prefix:
        if parties.respondent_id.strip():
            parts.append(f"身份证号：{parties.respondent_id.strip()}")
        if parties.respondent_address.strip():
            parts.append(f"住址：{parties.respondent_address.strip()}")
        if parties.respondent_phone.strip():
            parts.append(f"联系电话：{parties.respondent_phone.strip()}")
    return "，".join(parts) + "。"


def parse_answer_markdown(markdown: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """从答辩状草稿里取出「答辩请求」与「事实与理由」两部分的干净内容。

    丢弃一切系统区块（警示语、依据与数字来源、待核清单、签名提示、生成时间），
    这些内容不得出现在提交给法院的文书里。
    """
    requests: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_paragraphs: list[str] = []
    mode = ""

    def flush() -> None:
        nonlocal current_title, current_paragraphs
        if current_title is not None and current_paragraphs:
            sections.append((current_title, current_paragraphs))
        current_title, current_paragraphs = None, []

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("## 答辩请求"):
            flush()
            mode = "requests"
            continue
        if stripped.startswith("## 事实与理由"):
            flush()
            mode = "reasons"
            continue
        if stripped.startswith("## ") and mode:
            flush()
            mode = ""
            continue
        if stripped.startswith("> ") or stripped.startswith("- [ ]") or stripped.startswith("---"):
            continue
        if not stripped:
            continue
        if mode == "requests":
            match = re.match(r"^\d+[.、]\s*(.+)$", stripped)
            if match:
                requests.append(match.group(1).strip())
            continue
        if mode == "reasons":
            if stripped.startswith(_REASON_HEADING):
                flush()
                current_title = stripped[len(_REASON_HEADING):].strip()
                continue
            if current_title is None:
                continue
            current_paragraphs.append(stripped)
    flush()
    return requests, sections


def _answer_requests(requests: Sequence[str], engine_numbers: Mapping[str, str],
                     parties: MatterParties) -> list[str]:
    if requests:
        return list(requests)
    # 没有草稿时按引擎数字给出请求骨架，数字仍来自计算表
    out: list[str] = []
    unpaid = engine_numbers.get("未付货款本金")
    loss = engine_numbers.get("逾期付款损失（净额）")
    if unpaid:
        out.append(f"请求依法认定答辩人应付货款本金为 {unpaid} 元，驳回被答辩人超出该数额的请求。")
    if loss is not None:
        out.append(f"请求依法认定逾期付款损失为 {loss} 元，驳回被答辩人超出该数额的请求。")
    if not out:
        out.append("请求依法驳回被答辩人对答辩人的全部诉讼请求。")
    out.append("本案诉讼费用由被答辩人负担。")
    return out


def build_answer_document(parties: MatterParties, *, answer_markdown: str = "",
                          engine_numbers: Mapping[str, str] | None = None) -> LegalDocSpec:
    """民事答辩状：可提交的正式文书（不含任何系统说明）。"""
    from case_api.legal_docx import Paragraph, Signature

    numbers = dict(engine_numbers or {})
    requests, sections = parse_answer_markdown(answer_markdown)
    requests = [_strip_gate_markers(item) for item in _answer_requests(requests, numbers, parties)]
    sections = [(title, [_strip_gate_markers(text) for text in paragraphs])
                for title, paragraphs in sections]

    blocks: list[object] = [
        Paragraph(_party_line("答辩人（被告）", parties.respondent, parties), indent=False),
        Paragraph(f"被答辩人（原告）：{_or(parties.claimant)}。", indent=False),
        Paragraph(
            f"答辩人因与被答辩人{_or(parties.cause, '纠纷')}一案"
            f"（案号：{_or(parties.case_number)}），现提出答辩如下：", indent=True),
        Paragraph("一、答辩请求", indent=True, bold=True),
    ]
    for index, item in enumerate(requests, start=1):
        blocks.append(Paragraph(f"{index}. {item}", indent=True))
    blocks.append(Paragraph("二、事实与理由", indent=True, bold=True))
    if sections:
        for index, (title, paragraphs) in enumerate(sections, start=1):
            label = "一二三四五六七八九十"[index - 1] if index <= 10 else str(index)
            blocks.append(Paragraph(f"（{label}）{title}", indent=True, bold=True))
            for paragraph in paragraphs:
                blocks.append(Paragraph(paragraph, indent=True))
    else:
        blocks.append(Paragraph(f"（一）{BLANK}", indent=True, bold=True))
        blocks.append(Paragraph(BLANK, indent=True))
    blocks += [
        Paragraph("此致", indent=False),
        Paragraph(f"{_or(parties.court)}", indent=False),
        Signature(["答辩人：" + SHORT_BLANK + "（签名或捺印）", _date_line()]),
    ]
    return LegalDocSpec(path="01-民事答辩状.docx", title="民事答辩状",
                        blocks=blocks, needs_client_signature=True)


_HASH_NAME = re.compile(r"^[0-9a-f]{16,}(?:\.[A-Za-z]+)?$")


def _evidence_name(material: Mapping) -> str:
    """证据名称：去掉扩展名；哈希式文件名不写进正式文书，留空由律师填写。

    系统内部用哈希命名上传件（例如 12207299bbcf….jpg），那不是证据名称；
    正式文书里出现它只会让法官看不懂。
    """
    name = str(material.get("display_name") or "")
    if _HASH_NAME.match(name):
        return SHORT_BLANK
    for suffix in (".pdf", ".PDF", ".jpg", ".jpeg", ".png"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def build_evidence_list_document(parties: MatterParties,
                                 materials: Sequence[Mapping]) -> LegalDocSpec:
    """证据目录：只列**我方要提交的证据**（由律师勾选），证明内容留空。"""
    from case_api.legal_docx import Paragraph, Signature, Table

    rows = []
    for index, material in enumerate(materials, start=1):
        rows.append([
            str(index),
            _evidence_name(material),
            str(material.get("page_count") or ""),
            "",
        ])
    if not rows:
        rows.append(["", "", "", ""])
    blocks: list[object] = [
        Paragraph(f"案号：{_or(parties.case_number)}", indent=False),
        Paragraph(f"提交人（答辩人）：{_or(parties.respondent)}", indent=False),
        Paragraph("现将本案证据提交如下：", indent=True),
        Table(header=["序号", "证据名称", "页数", "证明内容"], rows=rows,
              widths=[1.3, 6.0, 1.4, 6.3]),
        Paragraph("以上证据材料共 " + SHORT_BLANK + " 份，页数合计 " + SHORT_BLANK + " 页。", indent=True),
        Paragraph("此致", indent=False),
        Paragraph(f"{_or(parties.court)}", indent=False),
        Signature(["提交人（签名）：" + SHORT_BLANK, _date_line()]),
    ]
    return LegalDocSpec(path="02-证据目录.docx", title="证据目录", blocks=blocks)


def build_cross_examination_document(parties: MatterParties,
                                     materials: Sequence[Mapping]) -> LegalDocSpec:
    """质证意见：逐份证据一行的表格，三性意见与理由留空。"""
    from case_api.legal_docx import Paragraph, Signature, Table

    rows = []
    for index, material in enumerate(materials, start=1):
        rows.append([str(index), _evidence_name(material), "", "", "", ""])
    if not rows:
        rows.append(["", "", "", "", "", ""])
    blocks: list[object] = [
        Paragraph(f"案号：{_or(parties.case_number)}", indent=False),
        Paragraph(f"质证人（答辩人）：{_or(parties.respondent)}", indent=False),
        Paragraph("对被答辩人提交的证据，发表如下质证意见：", indent=True),
        Table(header=["序号", "证据名称", "真实性", "合法性", "关联性", "质证理由"],
              rows=rows, widths=[1.1, 4.6, 1.6, 1.6, 1.6, 4.5]),
        Paragraph("此致", indent=False),
        Paragraph(f"{_or(parties.court)}", indent=False),
        Signature(["质证人（签名）：" + SHORT_BLANK, _date_line()]),
    ]
    return LegalDocSpec(path="03-质证意见.docx", title="质证意见", blocks=blocks)


def build_argument_document(parties: MatterParties,
                            issues: Sequence[str] | None = None) -> LegalDocSpec:
    """代理词：五段体例骨架。"""
    from case_api.legal_docx import Paragraph, Signature

    blocks: list[object] = [
        Paragraph(f"案号：{_or(parties.case_number)}", indent=False),
        Paragraph("尊敬的审判长、审判员：", indent=False),
        Paragraph(
            f"本人受答辩人{_or(parties.respondent)}委托，担任其与被答辩人"
            f"{_or(parties.claimant)}、{_or(parties.cause, '纠纷')}一案的诉讼代理人，"
            "现发表如下代理意见：", indent=True),
        Paragraph("一、案件基本情况", indent=True, bold=True),
        Paragraph(BLANK, indent=True),
        Paragraph("二、争议焦点", indent=True, bold=True),
    ]
    if issues:
        for index, issue in enumerate(issues, start=1):
            blocks.append(Paragraph(f"{index}. {issue}", indent=True))
    else:
        blocks.append(Paragraph("1. " + BLANK, indent=True))
    blocks += [
        Paragraph("三、代理意见", indent=True, bold=True),
        Paragraph("（一）" + BLANK, indent=True),
        Paragraph("（二）" + BLANK, indent=True),
        Paragraph("四、法律依据", indent=True, bold=True),
        Paragraph(BLANK, indent=True),
        Paragraph("五、结论", indent=True, bold=True),
        Paragraph("综上，请求法院" + BLANK + "。", indent=True),
        Signature(["代理人（签名）：" + SHORT_BLANK, _date_line()]),
    ]
    return LegalDocSpec(path="04-代理词.docx", title="代理词", blocks=blocks)


def build_authorisation_document(parties: MatterParties) -> LegalDocSpec:
    from case_api.legal_docx import Paragraph, Signature

    blocks = [
        Paragraph(_party_line("委托人（被告）", parties.respondent, parties), indent=False),
        Paragraph(f"受托人：{_or(parties.lawyer)}"
                  + (f"（{parties.law_firm}）" if parties.law_firm.strip() else "。"),
                  indent=False),
        Paragraph(f"现委托上述受托人作为委托人与{_or(parties.claimant, '对方当事人')}"
                  f"{_or(parties.cause, '纠纷')}一案（案号：{_or(parties.case_number)}）"
                  "的诉讼代理人。", indent=True),
        Paragraph("代理权限：", indent=True),
        Paragraph("1. 一般代理：代为陈述事实、提交与接收证据材料、参加庭审、发表辩论意见。", indent=True),
        Paragraph("2. 特别授权：代为承认、放弃、变更诉讼请求，进行和解、调解，"
                  "提起反诉或上诉，代为签收法律文书（选择本项的，请逐项写明：" + SHORT_BLANK + "）。", indent=True),
        Paragraph("委托期限：自签署之日起至本案" + SHORT_BLANK + "审程序终结（含调解、和解）之日止。", indent=True),
        Signature(["委托人（签名）：" + SHORT_BLANK, _date_line()]),
    ]
    return LegalDocSpec(path="05-授权委托书（当事人签字）.docx", title="授权委托书",
                        blocks=blocks, needs_client_signature=True)


def build_service_address_document(parties: MatterParties) -> LegalDocSpec:
    from case_api.legal_docx import Paragraph, Signature

    blocks = [
        Paragraph("本人确认下列地址与方式用于接收本案诉讼文书：", indent=True),
        Paragraph(f"当事人：{_or(parties.respondent)}", indent=True),
        Paragraph(f"送达地址：{_or(parties.respondent_address)}", indent=True),
        Paragraph(f"联系电话：{_or(parties.respondent_phone)}", indent=True),
        Paragraph("电子邮箱：" + SHORT_BLANK, indent=True),
        Paragraph("微信或其他电子送达方式：" + SHORT_BLANK, indent=True),
        Paragraph("本人已知悉：", indent=True),
        Paragraph("一、因本人提供的地址不准确、拒不提供送达地址、地址变更未及时告知，"
                  "导致诉讼文书未能实际接收的，文书退回之日视为送达之日；", indent=True),
        Paragraph("二、同意人民法院采用电话、短信、电子邮件、微信等电子方式送达的，"
                  "以送达平台记录的时间为送达时间；", indent=True),
        Paragraph("三、如需变更送达地址，本人将以书面方式及时告知受诉法院。", indent=True),
        Signature(["确认人（签名）：" + SHORT_BLANK, _date_line()]),
    ]
    return LegalDocSpec(path="06-送达地址确认书（当事人签字）.docx", title="送达地址确认书",
                        blocks=blocks, needs_client_signature=True)


def build_statement_document(parties: MatterParties) -> LegalDocSpec:
    """当事人陈述：提问式骨架，事实一律留空由当事人自己写。"""
    from case_api.legal_docx import Paragraph, Signature

    blocks: list[object] = [
        Paragraph(f"陈述人：{_or(parties.respondent)}", indent=False),
        Paragraph("一、主体与身份", indent=True, bold=True),
        Paragraph("1. 陈述人与被答辩人之间是否存在直接交易关系：" + SHORT_BLANK, indent=True),
        Paragraph("2. 实际下单、对账、收货的主体：" + SHORT_BLANK, indent=True),
        Paragraph("3. 陈述人在交易中的身份与职责：" + SHORT_BLANK, indent=True),
        Paragraph("二、交易经过", indent=True, bold=True),
        Paragraph("1. 交易起止时间与方式（微信、电话、书面合同）：" + SHORT_BLANK, indent=True),
        Paragraph("2. 下单、送货、对账的流程与参与人员：" + SHORT_BLANK, indent=True),
        Paragraph("3. 是否有书面合同、送货单签收、对账确认：" + SHORT_BLANK, indent=True),
        Paragraph("三、款项支付情况", indent=True, bold=True),
        Paragraph("1. 已付款项的时间、金额、付款账户与附言：" + SHORT_BLANK, indent=True),
        Paragraph("2. 由他人代付或代收的原因与约定：" + SHORT_BLANK, indent=True),
        Paragraph("3. 目前是否仍有未结款项及金额依据：" + SHORT_BLANK, indent=True),
        Paragraph("四、争议事实", indent=True, bold=True),
        Paragraph("1. 对被答辩人主张金额的异议与理由：" + SHORT_BLANK, indent=True),
        Paragraph("2. 是否存在质量异议、退换货、运费承担争议：" + SHORT_BLANK, indent=True),
        Paragraph("3. 其他需要法院了解的情况：" + SHORT_BLANK, indent=True),
        Paragraph("以上陈述属实。如有虚假，本人愿承担相应法律责任。", indent=True),
        Signature(["陈述人（签名）：" + SHORT_BLANK, _date_line()]),
    ]
    return LegalDocSpec(path="07-当事人陈述（当事人签字）.docx", title="当事人陈述",
                        blocks=blocks, needs_client_signature=True)


def build_evidence_source_document(parties: MatterParties,
                                   materials: Sequence[Mapping]) -> LegalDocSpec:
    from case_api.legal_docx import Paragraph, Signature, Table

    rows = [[str(index), _evidence_name(material), "", "", "", ""]
            for index, material in enumerate(materials, start=1)]
    if not rows:
        rows.append(["", "", "", "", "", ""])
    blocks: list[object] = [
        Paragraph(f"案号：{_or(parties.case_number)}", indent=False),
        Paragraph("本方提交证据的来源、原始载体与提取方式说明如下：", indent=True),
        Table(header=["序号", "证据名称", "来源", "原始载体", "提取方式", "保管情况"],
              rows=rows, widths=[1.0, 4.4, 2.0, 2.4, 2.4, 2.4]),
        Paragraph("电子数据（微信、短信、网页截图等）专项说明：", indent=True, bold=True),
        Paragraph("1. 原始载体（手机或电脑、账号、保管人）：" + SHORT_BLANK, indent=True),
        Paragraph("2. 能否当庭出示原始载体并演示：" + SHORT_BLANK, indent=True),
        Paragraph("3. 记录是否连续、有无删除或拼接：" + SHORT_BLANK, indent=True),
        Paragraph("4. 如无法提供原始载体，理由与替代方式：" + SHORT_BLANK, indent=True),
        Paragraph("本方保证上述证据来源真实合法，未作剪辑、拼接或篡改。", indent=True),
        Signature(["提供人（签名）：" + SHORT_BLANK, _date_line()]),
    ]
    return LegalDocSpec(path="08-证据来源说明（当事人签字）.docx", title="证据来源说明",
                        blocks=blocks, needs_client_signature=True)


def build_mediation_document(parties: MatterParties) -> LegalDocSpec:
    from case_api.legal_docx import Paragraph, Signature

    blocks = [
        Paragraph(f"当事人：{_or(parties.respondent)}", indent=False),
        Paragraph(f"案号：{_or(parties.case_number)}", indent=False),
        Paragraph("一、是否同意调解", indent=True, bold=True),
        Paragraph("同意调解，并授权代理人参与调解。", indent=True),
        Paragraph("同意调解，但需本人到场确认。", indent=True),
        Paragraph("不同意调解。", indent=True),
        Paragraph("（请在所选项后签名确认）", indent=True),
        Paragraph("二、授权范围与底线", indent=True, bold=True),
        Paragraph("1. 可接受的最高付款总额：" + SHORT_BLANK, indent=True),
        Paragraph("2. 付款方式与期限：" + SHORT_BLANK, indent=True),
        Paragraph("3. 是否可以分期、是否要求出具结清凭证：" + SHORT_BLANK, indent=True),
        Paragraph("4. 其他条件：" + SHORT_BLANK, indent=True),
        Paragraph("本人知悉：代理人不得超越上述范围作出让步；超出部分须另行书面确认。", indent=True),
        Signature(["当事人（签名）：" + SHORT_BLANK, _date_line()]),
    ]
    return LegalDocSpec(path="09-调解意见确认（当事人签字）.docx", title="调解意见确认",
                        blocks=blocks, needs_client_signature=True)


def build_application_documents(parties: MatterParties) -> list[LegalDocSpec]:
    """四类申请书各自独立成文（按需选用）。"""
    from case_api.legal_docx import Paragraph, Signature

    def skeleton(title: str, items: list[str]) -> LegalDocSpec:
        blocks: list[object] = [
            Paragraph(f"申请人：{_or(parties.respondent)}", indent=False),
            Paragraph(f"案号：{_or(parties.case_number)}", indent=False),
            Paragraph("申请事项", indent=True, bold=True),
            Paragraph(items[0], indent=True),
            Paragraph("事实与理由", indent=True, bold=True),
            Paragraph(items[1], indent=True),
            Paragraph("此致", indent=False),
            Paragraph(f"{_or(parties.court)}", indent=False),
            Signature(["申请人（签名）：" + SHORT_BLANK, _date_line()]),
        ]
        # 文件名带序号，文书内标题保持规范（"申请书"），编号不进正文
        return LegalDocSpec(path=f"申请书（按需选用）/{title}.docx",
                            title="申请书", blocks=blocks)

    return [
        skeleton("10-申请书（追加当事人）", [
            f"请求追加{SHORT_BLANK}（名称、住所、统一社会信用代码或身份信息）为本案当事人。",
            f"{SHORT_BLANK}（写明该主体与本案交易的关系及应承担责任的理由）。",
        ]),
        skeleton("11-申请书（调查取证）", [
            f"请求依法调取{SHORT_BLANK}（证据名称、持有单位或个人）。",
            f"待证事实：{SHORT_BLANK}；无法自行收集的理由：{SHORT_BLANK}。",
        ]),
        skeleton("12-申请书（鉴定）", [
            f"请求对{SHORT_BLANK}（鉴定事项）进行司法鉴定。",
            f"事实与理由：{SHORT_BLANK}；检材与样本：{SHORT_BLANK}。",
        ]),
        skeleton("13-申请书（延期举证）", [
            f"请求准予延期举证至{SHORT_BLANK}。",
            f"事实与理由：{SHORT_BLANK}（客观障碍，例如证据由第三方持有、需调取原件）。",
        ]),
    ]


def build_submission_documents(
    *,
    parties: MatterParties,
    materials: Sequence[Mapping] | None = None,
    our_materials: Sequence[Mapping] | None = None,
    plaintiff_materials: Sequence[Mapping] | None = None,
    engine_numbers: Mapping[str, str] | None = None,
    answer_markdown: str = "",
    issues: Sequence[str] | None = None,
) -> list[LegalDocSpec]:
    """提交件与签字件全集（不含内部文件）。

    ``our_materials``：我方要提交的证据（证据目录、证据来源说明用它）；
    ``plaintiff_materials``：原告提交的证据（质证意见用它）。
    两者都由律师在页面上勾选，系统不替律师决定某份材料算谁的证据。
    """
    ours = list(our_materials if our_materials is not None else (materials or []))
    plaintiff = list(plaintiff_materials if plaintiff_materials is not None else [])
    documents = [
        build_answer_document(parties, answer_markdown=answer_markdown,
                              engine_numbers=engine_numbers),
        build_evidence_list_document(parties, ours),
        build_cross_examination_document(parties, plaintiff),
        build_argument_document(parties, issues),
        build_authorisation_document(parties),
        build_service_address_document(parties),
        build_statement_document(parties),
        build_evidence_source_document(parties, ours),
        build_mediation_document(parties),
        *build_application_documents(parties),
    ]
    return documents


def build_internal_checklist_document(
    *,
    parties: MatterParties,
    states: Mapping[str, str],
    materials: Sequence[Mapping],
    engine_numbers: Mapping[str, str] | None = None,
    review_items: Sequence[str] | None = None,
    contract_summary: str = "",
    blanked_amount_count: int = 0,
) -> LegalDocSpec:
    """内部文件（不提交）：交付清单、填写指引、数字来源与待核事项。

    律师需要看的提示集中在这里，提交件里一句都不出现。
    """
    from case_api.legal_docx import Paragraph, Table

    numbers = dict(engine_numbers or {})
    rows = [
        ["01", "民事答辩状", "法院", "答辩人（当事人签名）", "是", states.get("answer", "未开始")],
        ["02", "证据目录", "法院", "提交人（律师）", "否", states.get("evidence_list", "未开始")],
        ["03", "质证意见", "法院", "质证人（律师）", "否", states.get("cross_examination", "未开始")],
        ["04", "代理词", "法院", "代理人（律师）", "否", states.get("argument", "未开始")],
        ["05", "授权委托书", "法院", "委托人（当事人）", "是", states.get("authorisation", "未开始")],
        ["06", "送达地址确认书", "法院", "当事人", "是", states.get("service_address", "未开始")],
        ["07", "当事人陈述", "法院", "当事人", "是", states.get("statement", "未开始")],
        ["08", "证据来源说明", "法院", "当事人", "是", states.get("evidence_source", "未开始")],
        ["09", "调解意见确认", "法院", "当事人", "是", states.get("mediation", "未开始")],
        ["10–13", "申请书（按需选用）", "法院", "律师", "否", states.get("applications", "未开始")],
    ]
    blocks: list[object] = [
        Paragraph("本文为内部工作文件，不提交法院、不交当事人。", indent=False, bold=True),
        Paragraph(f"案号：{_or(parties.case_number)}　案由：{_or(parties.cause)}", indent=False),
        Paragraph(f"被告（答辩人）：{_or(parties.respondent)}　承办律师：{_or(parties.lawyer)}", indent=False),
        Paragraph("一、交付清单与签字要求", indent=True, bold=True),
        Table(header=["编号", "文书", "去向", "署名人", "需当事人签字", "状态"],
              rows=rows, widths=[1.2, 3.6, 1.6, 3.4, 2.4, 2.0]),
        Paragraph("二、各文书需要填写的位置", indent=True, bold=True),
        Paragraph("所有下划线空白（＿＿＿＿）均为需要填写的位置：", indent=True),
        Paragraph("1. 民事答辩状：事实与理由的每一节、案号、答辩人身份信息、签名与日期。", indent=True),
        Paragraph("2. 证据目录：每份证据的「证明内容」。", indent=True),
        Paragraph("3. 质证意见：每份证据的真实性、合法性、关联性与理由。", indent=True),
        Paragraph("4. 代理词：案件基本情况、争议焦点、代理意见、法律依据、结论。", indent=True),
        Paragraph("5. 授权委托书：代理权限（一般代理或特别授权）、委托期限、签名与日期。", indent=True),
        Paragraph("6. 送达地址确认书：电子邮箱、微信或其他电子送达方式、签名与日期。", indent=True),
        Paragraph("7. 当事人陈述：全部事实，必须由当事人本人填写并签名。", indent=True),
        Paragraph("8. 证据来源说明：原始载体、提取方式、保管情况、能否当庭演示。", indent=True),
        Paragraph("9. 调解意见确认：选择的调解方案、授权底线、签名与日期。", indent=True),
        Paragraph("10. 申请书：申请事项与事实理由，按需选用。", indent=True),
    ]
    if numbers:
        blocks += [
            Paragraph("三、本文书所引用的正式数字（来自计算表）", indent=True, bold=True),
        ]
        for key, value in numbers.items():
            blocks.append(Paragraph(f"{key}：{value}", indent=True))
    if contract_summary:
        blocks += [
            Paragraph("四、计算口径", indent=True, bold=True),
            Paragraph(contract_summary, indent=True),
        ]
    if blanked_amount_count:
        blocks += [
            Paragraph("四、答辩状中留空的数字", indent=True, bold=True),
            Paragraph(f"答辩状正文有 {blanked_amount_count} 处原为模型自算数字，"
                      "已留空为下划线。请按计算表核对后填写，不要沿用模型给出的数字。",
                      indent=True),
        ]
    if review_items:
        blocks += [Paragraph("五、提交前需要确认的事项", indent=True, bold=True)]
        for item in review_items:
            blocks.append(Paragraph("· " + str(item), indent=True))
    return LegalDocSpec(path="内部文件（不提交）/交付清单与填写指引.docx",
                        title="交付清单与填写指引", blocks=blocks)
