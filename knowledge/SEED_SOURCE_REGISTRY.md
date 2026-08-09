# 民间借贷研究来源注册表（初始种子）

状态：`仅登记来源元数据；未将任何网页或裁判全文并入产品知识库`

登记日期：`2026-08-09`

本表是受控研究网关的初始白名单输入，不是个案法律意见。每次研究任务必须重新取得来源、保存内容哈希与抓取时间，并由律师确认条文版本、适用时间和个案事实。任何付费数据库、全文下载或批量案例接口，均须先确认许可和律所账号授权。

| ID | 层级 | 来源与用途 | 网址 | 运行时核验要求 |
|---|---|---|---|---|
| CN-CIVIL-CODE-680 | 一级法源 | 国家法律法规数据库《民法典》文本；用于借款合同成立、利息约定和高利放贷等基础规则定位 | [《民法典》官方 PDF](https://wb.flk.npc.gov.cn/flfg/PDF/bd53dd912c1048f2aecbaa229238334b.pdf) | 记录条款号、文本发布日期、抓取哈希并人工核对第 679—680 条 |
| SPC-PRIVATE-LENDING-2020-SECOND-REVISION | 一级司法解释 | 最高人民法院公布的民间借贷司法解释（2020 年第二次修正）；用于利率保护上限、预扣利息、本息结算、逾期利息和过渡条款 | [最高人民法院](https://www.court.gov.cn/zixun/xiangqing/282621.html) | 保存正文版本、公布/施行时间、条款定位；规则引擎须记录合同成立、借贷行为、起诉/受理、付款和返还日期 |
| SPC-PRIVATE-LENDING-2020-FIRST-REVISION | 一级司法解释历史版本 | 最高人民法院公布的 2020 年第一次修正历史文本；用于版本变更与过渡路径对照 | [最高人民法院](https://www.court.gov.cn/zixun/xiangqing/249031.html) | 不将历史版本误标为现行文本；解析器必须保留版本和有效时点 |
| SPC-PRIVATE-LENDING-2015-ORIGINAL | 一级司法解释历史版本 | 法释〔2015〕18号原始全文；用于区分 24% 司法保护、24%—36% 已自愿履行和超过 36% 已付利息返还候选路径 | [最高人民法院公报](https://gongbao.court.gov.cn/Details/48786dea74c9545c2f4fb27254ca08.html) | 必须分别提取第 26、31 条并保留付款是否已经履行；发布说明不能替代条文全文 |
| CFETS-LPR-HISTORY | 一级利率数据 | 全国银行间同业拆借中心/中国货币网 LPR 历史数据页；用于取得每个相关日期的一年期 LPR 原始记录 | [中国货币网 LPR 历史数据](https://www.chinamoney.com.cn/r/cms/chinese/chinamoney/html/currency/lpr-shibor-history-download.html) | 保存原始响应或导出文件、发布日、期限、值、来源哈希与下载时间；禁止由模型记忆填入利率 |
| CASE-CASH-DISBURSEMENT-2023 | 一级公开裁判研究 | 最高人民法院第一巡回法庭公开的民间借贷大额现金交付裁判研究；用于提示“本金交付、预扣利息、证据链与证明标准”需要单列事实/证据问题 | [最高人民法院公开文章](https://www.court.gov.cn/xunhui1/xiangqing/385751.html) | 标记为“研究参考，不是规则”；保存法院、发布日期、公开链接、问题标签和与本案的事实差异 |
| CASE-INTEREST-CAP-HISTORY | 一级公开裁判研究 | 最高人民法院公报公开的历史民间借贷裁判；用于验证旧时期“同期同类贷款利率四倍”与分段计算属于历史规则，不能替代现行/过渡规则 | [最高人民法院公报](https://gongbao.court.gov.cn/Details/8e5a855a1d656c6b26158702e72098.html) | 标记历史适用区间；不得从单案提炼出普适当前规则 |

## 运行时输出契约

每个研究结论至少包含：

```text
source_id
source_url
source_tier
publisher
publication_date / effective_date / repeal_or_amendment_date
retrieved_at
content_sha256
pinpoint (条款、页码或段落)
legal_issue
conditions_and_temporal_anchors
quotation_or_paraphrase_scope
status: CANDIDATE | LAWYER_APPROVED | SUPERSEDED | UNAVAILABLE
```

裁判研究额外包含：公开性、法院层级、程序阶段、案号（如公开页确有）、事实标签、相似点、差异点和“不可作为法源”的固定标签。

## 明确禁止

- 将搜索摘要、新闻、律所文章、模型记忆或无版本的网页剪贴直接写入答辩状；
- 因一个公开案例的结果就推断本案的利率、本金冲抵或已付利息结论；
- 把本案当事人资料自动带入搜索词，或把案卷文件上传到这些站点；
- 允许规则卡在来源失效、哈希变化、无法打开或律师未批准时参与正式计算/文书。
