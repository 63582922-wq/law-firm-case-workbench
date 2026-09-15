"""应诉材料包：律师交付清单 + 当事人签字文件 + 证据目录。

为什么需要它：律师真正要交出去的不是一份"分析报告"，而是一整套东西——
哪些递法院、哪些给当事人签字、哪些只是内部工作件。本模块把这些固定下来：

- **交付清单**：每项交付物的去向（法院/当事人/内部）、署名人、是否需要当事人签字；
- **签字文件**：授权委托书、送达地址确认书、当事人陈述、证据来源说明、调解意见确认——
  这些不签字的文件对法院无效，因此单独成文、留签名栏，事实部分留【待填】由律师填；
- **证据目录**：按本案已入卷材料自动生成行，证明内容由律师填（系统不替律师证明什么）。

系统只做结构化与排版：不替律师写事实、不替当事人作陈述、不判断该不该提交。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Mapping, Sequence

PACKAGE_WARNING = (
    "本材料包由系统按案件信息与已入卷材料生成，**属律师工作稿**；"
    "标注【待填】的内容必须由律师或当事人补全，签字文件必须由当事人本人签署后才可使用。"
)

PLACEHOLDER = "【待填】"

# 交付状态（律师在界面上逐项维护）
DELIVERABLE_STATES: tuple[str, ...] = (
    "未开始", "起草中", "待当事人签字", "已签字", "已提交", "不适用",
)


@dataclass(frozen=True)
class Deliverable:
    item_id: str
    name: str
    destination: str        # 法院 / 当事人 / 内部
    signer: str             # 署名人
    needs_client_signature: bool
    note: str
    source: str             # template / brief / analysis / materials / manual


CATALOGUE: tuple[Deliverable, ...] = (
    Deliverable("authorisation", "授权委托书", "法院", "委托人（当事人）", True,
                "委托律师代理诉讼，载明代理权限与期限", "template"),
    Deliverable("service_address", "送达地址确认书", "法院", "当事人", True,
                "确认收件地址、电话与电子送达方式，避免缺席风险", "template"),
    Deliverable("statement", "当事人陈述（事实经过）", "法院", "当事人", True,
                "交易主体、经办与代付经过、付款情况；事实必须由当事人自己确认", "template"),
    Deliverable("evidence_source", "证据来源说明", "法院", "当事人", True,
                "说明微信/截图等证据的原始载体、提取方式与保管情况", "template"),
    Deliverable("mediation", "调解意见确认", "法院", "当事人", True,
                "确认是否同意调解与授权底线", "template"),
    Deliverable("answer", "民事答辩状", "法院", "答辩人（当事人签名）", True,
                "答辩请求与事实理由；本机可生成草稿", "brief"),
    Deliverable("evidence_list", "证据目录", "法院", "律师/律所", False,
                "证据名称、页数、证明内容、来源；由本案已入卷材料自动列出", "materials"),
    Deliverable("evidence_copies", "证据材料（副本）", "法院", "律师/律所", False,
                "按目录顺序装订，原件自行保管", "manual"),
    Deliverable("cross_examination", "质证意见", "法院", "律师", False,
                "对原告证据的真实性、合法性、关联性意见", "manual"),
    Deliverable("applications", "申请书（追加当事人/调查取证/鉴定/延期举证）", "法院", "律师", False,
                "按需提交，注意举证期限", "manual"),
    Deliverable("argument", "代理词", "法院", "律师", False,
                "庭审后提交的辩论意见", "manual"),
    Deliverable("appeal", "上诉状", "法院", "上诉人（当事人签名）", True,
                "仅在判决不利时使用；上诉期 15 日", "manual"),
    Deliverable("internal_analysis", "案件分析决策包", "内部", "律师", False,
                "争点、对抗、策略与待核清单；工作稿", "analysis"),
    Deliverable("internal_balance", "收付款与余额核对表", "内部", "律师", False,
                "数据源：逐笔核对金额与性质", "payments"),
    Deliverable("internal_deadline", "期限与开庭提示", "内部", "律师", False,
                "举证期限、开庭时间、上诉期", "manual"),
)

CATALOGUE_BY_ID: dict[str, Deliverable] = {item.item_id: item for item in CATALOGUE}

TEMPLATE_IDS: tuple[str, ...] = tuple(
    item.item_id for item in CATALOGUE if item.source == "template"
)


@dataclass
class MatterParties:
    """案件主体信息（律师填写；签字文件与答辩状共用同一份）。"""

    respondent: str = ""        # 被告/答辩人
    respondent_id: str = ""     # 身份证号（可空）
    respondent_address: str = ""
    respondent_phone: str = ""
    claimant: str = ""          # 原告/被答辩人
    court: str = ""
    case_number: str = ""
    cause: str = ""             # 案由
    lawyer: str = ""            # 承办律师
    law_firm: str = ""          # 律所
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "respondent": self.respondent, "respondent_id": self.respondent_id,
            "respondent_address": self.respondent_address,
            "respondent_phone": self.respondent_phone, "claimant": self.claimant,
            "court": self.court, "case_number": self.case_number, "cause": self.cause,
            "lawyer": self.lawyer, "law_firm": self.law_firm, "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, value: Mapping | None) -> "MatterParties":
        if not isinstance(value, Mapping):
            return cls()
        return cls(**{
            key: str(value.get(key) or "")[:200]
            for key in cls().to_dict()
        })


def catalogue_payload() -> list[dict]:
    return [
        {
            "item_id": item.item_id,
            "name": item.name,
            "destination": item.destination,
            "signer": item.signer,
            "needs_client_signature": item.needs_client_signature,
            "note": item.note,
            "source": item.source,
        }
        for item in CATALOGUE
    ]


def _or(value: str, fallback: str = PLACEHOLDER) -> str:
    return value.strip() if value and value.strip() else fallback


def _header(parties: MatterParties, title: str) -> list[str]:
    return [
        f"# {title}",
        "",
        f"- 案号：{_or(parties.case_number)}",
        f"- 受理法院：{_or(parties.court)}",
        f"- 案由：{_or(parties.cause)}",
        f"- 被告（答辩人）：{_or(parties.respondent)}",
        f"- 原告（被答辩人）：{_or(parties.claimant)}",
        "",
    ]


def _signature_block(person: str, *, date_placeholder: str = "____年__月__日") -> list[str]:
    return [
        "",
        f"{person}（签名）：________________",
        "",
        f"日期：{date_placeholder}",
        "",
    ]


def render_authorisation(parties: MatterParties) -> str:
    """授权委托书：权限与期限由律师确认后勾选，系统不预设。"""
    lines = _header(parties, "授权委托书")
    lines += [
        f"委托人：{_or(parties.respondent)}"
        + (f"，身份证号：{parties.respondent_id}" if parties.respondent_id.strip() else "")
        + (f"，住址：{parties.respondent_address}" if parties.respondent_address.strip() else "")
        + (f"，联系电话：{parties.respondent_phone}" if parties.respondent_phone.strip() else ""),
        "",
        f"受托人：{_or(parties.lawyer)}"
        + (f"（{parties.law_firm}）" if parties.law_firm.strip() else ""),
        "",
        f"现委托上述受托人作为委托人与{_or(parties.claimant, '对方当事人')}"
        f"{_or(parties.cause, '本案')}一案的诉讼代理人。",
        "",
        "代理权限（由委托人确认后勾选，未勾选的不授予）：",
        "",
        "- [ ] 一般代理：代为陈述事实、提交与接收证据材料、参加庭审、发表辩论意见。",
        "- [ ] 特别授权：代为承认、放弃、变更诉讼请求，进行和解、调解，提起反诉或上诉，"
        "代为签收法律文书（选择特别授权的，须逐项写明：________________）。",
        "",
        "委托期限：自签署之日起至本案____审程序终结（含调解、和解）之日止。",
        "",
        "委托人确认：以上代理权限系本人真实意思表示；未经本人书面同意，"
        "受托人不得处分本人的实体权利。",
    ]
    lines += _signature_block("委托人")
    lines += ["", "受托人（签名）：________________　　日期：____年__月__日", ""]
    return "\n".join(lines)


def render_service_address(parties: MatterParties) -> str:
    lines = _header(parties, "送达地址确认书")
    lines += [
        "本人/本单位确认如下送达地址与方式，用于接收本案（含后续程序）的诉讼文书：",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        f"| 当事人 | {_or(parties.respondent)} |",
        f"| 送达地址 | {_or(parties.respondent_address)} |",
        f"| 联系电话 | {_or(parties.respondent_phone)} |",
        f"| 电子邮箱 | {PLACEHOLDER} |",
        f"| 微信/其他电子送达 | {PLACEHOLDER} |",
        "",
        "本人已知悉：",
        "",
        "1. 因本人提供的地址不准确、拒不提供送达地址、地址变更未及时告知，"
        "导致诉讼文书未能实际接收的，文书退回之日视为送达之日；",
        "2. 同意人民法院采用电话、短信、电子邮件、微信等电子方式送达的，"
        "以送达平台记录的时间为送达时间；",
        "3. 如需变更送达地址，本人将以书面方式及时告知受诉法院。",
    ]
    lines += _signature_block("确认人")
    return "\n".join(lines)


def render_statement(parties: MatterParties) -> str:
    lines = _header(parties, "当事人陈述（事实经过）")
    lines += [
        f"陈述人：{_or(parties.respondent)}",
        "",
        "> 说明：以下各项必须由陈述人本人核对后填写；不清楚的写「不清楚」，不要推测。"
        "本陈述将作为当事人陈述提交法院，虚假陈述可能承担法律责任。",
        "",
        "一、主体与身份",
        f"1. 本人与{_or(parties.claimant, '对方')}之间是否存在直接交易关系：{PLACEHOLDER}",
        f"2. 实际下单、对账、收货的主体是谁（个人/个体户/公司名称）：{PLACEHOLDER}",
        f"3. 本人在交易中的身份与职责（经办人/负责人/代付款人/买受人）：{PLACEHOLDER}",
        "",
        "二、交易经过",
        f"1. 交易起止时间与方式（微信/电话/书面合同）：{PLACEHOLDER}",
        f"2. 下单、送货、对账的流程与参与人员：{PLACEHOLDER}",
        f"3. 是否存在书面合同、送货单签收、对账确认：{PLACEHOLDER}",
        "",
        "三、付款情况",
        f"1. 已付款项的时间、金额、付款人账户、附言：{PLACEHOLDER}",
        f"2. 代付的原因与约定：{PLACEHOLDER}",
        f"3. 目前是否仍有未结款项，金额与依据：{PLACEHOLDER}",
        "",
        "四、争议事实",
        f"1. 对原告主张金额的异议与理由：{PLACEHOLDER}",
        f"2. 是否存在质量异议、退换货、运费承担争议：{PLACEHOLDER}",
        f"3. 其他需要法院了解的情况：{PLACEHOLDER}",
        "",
        "以上陈述属实，如有虚假，本人愿承担相应法律责任。",
    ]
    lines += _signature_block("陈述人")
    return "\n".join(lines)


def render_evidence_source(parties: MatterParties, materials: Sequence[Mapping]) -> str:
    lines = _header(parties, "证据来源说明")
    lines += [
        "本方提交的证据来源、原始载体与提取方式说明如下：",
        "",
        "| 序号 | 证据名称 | 来源 | 原始载体 | 提取方式 | 保管情况 |",
        "|---|---|---|---|---|---|",
    ]
    if not materials:
        lines.append(f"| 1 | {PLACEHOLDER} | {PLACEHOLDER} | {PLACEHOLDER} | {PLACEHOLDER} | {PLACEHOLDER} |")
    for index, material in enumerate(materials, start=1):
        name = str(material.get("display_name") or "").replace("|", "／")
        lines.append(
            f"| {index} | {name} | {PLACEHOLDER} | {PLACEHOLDER} | {PLACEHOLDER} | {PLACEHOLDER} |")
    lines += [
        "",
        "特别说明（微信、短信、网页截图等电子数据）：",
        "",
        f"1. 原始载体（手机/电脑型号、账号、保管人）：{PLACEHOLDER}",
        f"2. 是否可当庭出示原始载体并演示：{PLACEHOLDER}",
        f"3. 聊天记录是否连续、有无删除或拼接：{PLACEHOLDER}",
        f"4. 如无法提供原始载体，说明理由与替代方式：{PLACEHOLDER}",
        "",
        "本方保证上述证据来源真实合法，未作剪辑、拼接或篡改。",
    ]
    lines += _signature_block("提供人")
    return "\n".join(lines)


def render_mediation(parties: MatterParties) -> str:
    lines = _header(parties, "调解意见确认")
    lines += [
        f"当事人：{_or(parties.respondent)}",
        "",
        "一、是否同意调解：",
        "",
        "- [ ] 同意调解，并授权代理人参与调解；",
        "- [ ] 同意调解，但需本人到场确认；",
        "- [ ] 不同意调解。",
        "",
        "二、授权范围与底线（由当事人填写）：",
        "",
        f"1. 可接受的最高付款总额：{PLACEHOLDER}",
        f"2. 付款方式与期限：{PLACEHOLDER}",
        f"3. 是否可以分期、是否要求对方出具结清凭证：{PLACEHOLDER}",
        f"4. 其他条件（撤诉、保密、不再追究等）：{PLACEHOLDER}",
        "",
        "三、本人知悉：代理人不得超越上述范围作出让步；超出部分须另行书面确认。",
    ]
    lines += _signature_block("当事人")
    return "\n".join(lines)


def render_evidence_list(parties: MatterParties, materials: Sequence[Mapping]) -> str:
    lines = _header(parties, "证据目录")
    lines += [
        "| 序号 | 证据名称 | 页数 | 证明内容 | 来源 |",
        "|---|---|---|---|---|",
    ]
    if not materials:
        lines.append(f"| 1 | {PLACEHOLDER} | {PLACEHOLDER} | {PLACEHOLDER} | {PLACEHOLDER} |")
    for index, material in enumerate(materials, start=1):
        name = str(material.get("display_name") or "").replace("|", "／")
        pages = material.get("page_count") or ""
        lines.append(f"| {index} | {name} | {pages} | {PLACEHOLDER} | {PLACEHOLDER} |")
    lines += [
        "",
        "说明：证据材料按上表顺序装订提交，原件由本方保管并在庭审时出示核对；"
        "「证明内容」一项由律师逐项填写，系统不代填。",
        "",
    ]
    return "\n".join(lines)


def render_template(
    item_id: str,
    parties: MatterParties,
    materials: Sequence[Mapping] | None = None,
) -> str | None:
    """渲染单个签字文件/证据目录；未知 id 返回 None（调用方给出明确错误）。"""
    if item_id == "authorisation":
        return render_authorisation(parties)
    if item_id == "service_address":
        return render_service_address(parties)
    if item_id == "statement":
        return render_statement(parties)
    if item_id == "evidence_source":
        return render_evidence_source(parties, materials or [])
    if item_id == "mediation":
        return render_mediation(parties)
    if item_id == "evidence_list":
        return render_evidence_list(parties, materials or [])
    return None


def render_checklist(
    parties: MatterParties,
    states: Mapping[str, str],
) -> str:
    """交付清单：去向、署名人、是否需要当事人签字、当前状态。"""
    lines = [
        "# 交付清单（应诉）",
        "",
        f"> {PACKAGE_WARNING}",
        "",
        f"- 案号：{_or(parties.case_number)}　受理法院：{_or(parties.court)}　案由：{_or(parties.cause)}",
        f"- 被告（答辩人）：{_or(parties.respondent)}　承办律师：{_or(parties.lawyer)}",
        "",
        "## 一、需要当事人签字的文件（未签字不得提交）",
        "",
        "| 交付物 | 去向 | 署名人 | 状态 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for item in CATALOGUE:
        if not item.needs_client_signature:
            continue
        lines.append(
            f"| {item.name} | {item.destination} | {item.signer} | "
            f"{states.get(item.item_id, '未开始')} | {item.note} |")
    lines += [
        "",
        "## 二、由律师/律所署名的文件",
        "",
        "| 交付物 | 去向 | 署名人 | 状态 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for item in CATALOGUE:
        if item.needs_client_signature or item.destination == "内部":
            continue
        lines.append(
            f"| {item.name} | {item.destination} | {item.signer} | "
            f"{states.get(item.item_id, '未开始')} | {item.note} |")
    lines += [
        "",
        "## 三、内部工作件（不对外提交）",
        "",
        "| 交付物 | 状态 | 说明 |",
        "|---|---|---|",
    ]
    for item in CATALOGUE:
        if item.destination != "内部":
            continue
        lines.append(f"| {item.name} | {states.get(item.item_id, '未开始')} | {item.note} |")
    lines += [
        "",
        "> 提示：上表状态由律师逐项维护；签字文件必须由当事人本人签署，"
        "律师不得代签，也不得代当事人作事实陈述。",
        "",
    ]
    return "\n".join(lines)


def render_package(
    *,
    parties: MatterParties,
    states: Mapping[str, str],
    materials: Sequence[Mapping],
    answer_draft: str = "",
    generated_at: str = "",
) -> str:
    """应诉材料包：清单 + 签字文件 + 证据目录 + 已有答辩状草稿。"""
    lines = [
        "# 应诉材料包（律师工作稿）",
        "",
        f"> {PACKAGE_WARNING}",
        "",
        f"- 案号：{_or(parties.case_number)}　受理法院：{_or(parties.court)}　案由：{_or(parties.cause)}",
        f"- 被告（答辩人）：{_or(parties.respondent)}　承办律师：{_or(parties.lawyer)}",
    ]
    if generated_at:
        lines.append(f"- 生成时间：{generated_at}")
    lines += ["", "---", "", render_checklist(parties, states), "", "---", ""]
    for item_id in ("authorisation", "service_address", "statement", "mediation",
                    "evidence_list", "evidence_source"):
        rendered = render_template(item_id, parties, materials)
        if rendered:
            lines += [rendered, "", "---", ""]
    if answer_draft.strip():
        lines += ["# 附：民事答辩状（草稿）", "", answer_draft.strip(), "", "---", ""]
    else:
        lines += [
            "# 附：民事答辩状",
            "",
            "> 尚未生成答辩状草稿。请先在「答辩状」页面勾选主张、填写当事人信息并生成草稿，"
            "再回到本页导出材料包。",
            "",
            "---",
            "",
        ]
    lines += [
        "# 提交前检查清单",
        "",
        "- [ ] 所有【待填】内容已补全，没有留下空项；",
        "- [ ] 需要当事人签字的文件已由当事人本人签署（授权委托书、送达地址确认书、"
        "当事人陈述、证据来源说明、调解意见确认、答辩状）；",
        "- [ ] 证据目录的「证明内容」已逐项填写，证据副本按目录顺序装订；",
        "- [ ] 举证期限、开庭时间已核对，份数满足法院要求；",
        "- [ ] 提交前确认材料中不含需要脱敏的无关个人信息。",
        "",
        f"*本材料包由系统生成，仅供律师复核与整理；不构成法律意见，也不代表已提交法院。*",
        "",
    ]
    return "\n".join(lines)


def sanitize_filename(name: str, *, fallback: str = "应诉材料包") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "")).strip("_")
    return cleaned[:60] or fallback
