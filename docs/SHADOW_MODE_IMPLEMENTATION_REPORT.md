# 影子测试模式实现报告（S1–S7）

- 日期：2026-08-19
- 规格：`docs/SHADOW_MODE_ACCEPTANCE.md`（SHA-256 `90351aa03446f283445b5515b27f5b4e6737bb53ca82231854de0a33eeeaed6f`，已在 `case_kernel/shadow_mode.py` 中锁定为 `SHADOW_SPEC_SHA256`）
- 结论：**七道门全部 PASS（S1–S6 实测，S7 由回归与复现验证）；对抗用例全部按预期阻断；未达标的既有项（A2/A3/A4 的合成实验遗留）不属于本次交付范围，见文末说明。**

## 交付物

1. 代码：
   - `backend/case_kernel/shadow_engine.py` — 通用确定性引擎（冻结规则 R0–R9 的任意行实现）+ §4.2 引擎同一性证明；
   - `backend/case_kernel/shadow_mode.py` — S1–S6 门、导入清单、引用解析、金额消毒、preflight/账本、S6 对账、S2 隔离、编排器、影子报告渲染；
   - `backend/scripts/run_shadow_case.py` + `scripts/run_shadow_case` — CLI（`--materials` / `--expected` / `--case-config` / `--proposal-file` / `--confirm-data-path` / `--budget-cny` / `--open` / 隐藏的 `--attempt-lock` 对抗钩子）；
   - `backend/tests/test_shadow_mode.py` — 21 项验收测试。
2. S1–S7 判定步骤与对抗用例实测：见下表与测试文件（`python -m unittest backend.tests.test_shadow_mode`，21/21 OK）。
3. 本报告（≤2 页）。

## 七道门实测

| 门 | 判定 | 实测数字 | 对抗用例结果 |
|---|---|---|---|
| S1 脱敏完整性 | PASS | 扫描 2 文件/2 页，0 检出 | 植入有效身份证（GB11643 校验位）/手机号/银行卡（Luhn）→ 整次阻断；掩码版本通过 |
| S2 无正式输出隔离 | PASS | 正式产物 0 | `--attempt-lock` → exit 2 + BLOCKED；植入 `current_submission.json`/`locked_submission/` → 检出失败 |
| S3 引用硬拦截 | PASS | 正常 2/2 解析，可见坏引用 0 | 植入 3 条虚构引用（不存在文件/越界页码/错误哈希）→ 3/3 拦截，exit 2，阻断日志留痕 |
| S4 金额纪律 | PASS | 引擎渲染派生金额 5 个，Agent 自算 0 个入可见输出 | 注入自算总额 280,000.00 → 拦截并计入阻断日志 |
| S5 数据路径 | PASS（干跑）/ PASS（假传输） | 干跑调用 0、preflight 0；live 分支 preflight 1 + 账本 2 行 | preflight 缺字段/未确认 → 阻断；未授权页面 0 次发送 |
| S6 自带答案对账 | PASS | 正常行全部 match | 故意改错 2 行（金额、分类）→ 精确检出 mismatch/missing |
| S7 既有资产不变 | PASS | 规格 `932e7a…`、复算器 `a868d4…` 逐字节不变；原 20 项回归 OK；合成切片一键复现 exit 0、341 字段差异 0 | 无 |

## 引擎同一性证明（§4.2）

通用引擎对权威合成 47 行输入，四情景（S-A-1/2、S-B-1/2）共 **91 个 oracle 实际输出的字段**（各情景 L1/L2 本金与未付息、合计、S-A-1 全迹线逐笔结余）与 `docs/golden-case/golden_calc.py` 黑盒输出**逐分一致，0 差异**；oracle 哈希钉死 `a868d4…`。非 oracle 输出字段（如非默认情景的付息簿记）不参与比对，属口径而非缺陷。

## 边界与未达项

- 真实 Qwen 调用路径（`--confirm-data-path` + live 传输层）已按 preflight/账本契约实现并通过假传输测试，但**未使用真实密钥执行**（不读取密钥文件、不产生真实费用）；真实调用由用户在本机以 `--confirm-data-path` 执行，账本会记录每次载荷哈希与用量。
- 合成 Agent 实验的既有未达项（A2 情景数字、A3 坏引用、A4 自检误报）不属于本次交付；其结构性修复（金额由引擎注入、引用硬拦截、自检收窄）已在影子模式中落地为 S3/S4/S6 硬门，回归合成实验重测需用户另行安排。
- S1 只承诺机器可检模式（身份证/手机号/银行卡）；姓名、地址级脱敏由用户导入前自行完成。
