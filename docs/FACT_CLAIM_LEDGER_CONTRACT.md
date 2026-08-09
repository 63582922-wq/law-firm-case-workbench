# 事实、诉请与争点台账契约

状态：`内部合成 Alpha 领域契约；待真实律师流程验证`

## 不可逾越的边界

- 原告起诉材料中的文字首先是“原告主张候选”，不是案件事实；系统抽取、助理录入和模型建议也同样不是事实。
- 只有承办律师确认并保留原始证据位置的事实，才能支撑诉请回应、争点和后续法律/计算/文书对象。
- “不予认可”“存在争议”“超出诉请范围”都是律师的回应立场，不等于系统认定事实为真或假。
- 任何金额均须保存数值、币种和证据位置；外币可记录但不能传给 v1 正式利息计算。
- 事实、诉请、回应、争点和时间线都版本化；上游事实改变必须使相关回应、争点、规则包、计算和文书失效。

## 最小对象

```text
EvidenceLink
  evidence_id / original_file_sha256 / page / region / original_label

FactAssertion
  assertion_id / original_text / origin / status / evidence_links
  status: CANDIDATE | CONFIRMED | DISPUTED | DENIED | INVALIDATED

ClaimItem
  claim_id / original_claim_text / claimed_amount / currency / evidence_links
  status: CANDIDATE | CONFIRMED_SCOPE | INVALIDATED

ClaimResponse
  claim_id / position / confirmed_fact_ids / partial_amount / currency
  approved_by / approval_hash

DisputeIssue
  issue_id / question / claim_ids / confirmed_fact_ids / status
```

## 正式快照前的阻断项

1. 诉请金额没有显式币种或来源；
2. 诉请回应引用了候选、争议、否定或已失效事实；
3. “部分认可”没有金额或币种，或金额超过对应诉请；
4. 争点没有已确认事实支撑；
5. 任何所选对象在律师批准后发生变更。

本契约不决定个案应当“承认”还是“争议”；系统仅验证该决定的来源、范围和版本边界。
