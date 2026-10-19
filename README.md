# 麦麦明白点 meal-clear

**麦麦点餐，改一句就明白。**

> **“麦麦” = 大家对麦当劳（McDonald's）的昵称。** 1–6 个人一起点、要求各不同、预算有限，这个 Skill 帮你逐人核对，在到店自取、得来速、外送中给出带理由的多方案排名；改一句也只重算该改的部分。当前只读：不下单、不领券。

![麦麦明白点总流程：你说一句、逐人检查、比较方案、给出报价、改一句只重算B，价格从模拟38元变为43元，全程只读不下单](docs/images/01-overview.svg)

体验（零依赖、免 Token、不联网）：`python3 scripts/meal_clear.py demo`（两人改一句）· `python3 scripts/meal_clear.py demo-multi`（3 人多方案排名）

> 图中商品与价格均为**模拟数据**。**非麦当劳官方产品，参赛作品。**

## 3 步用起来

| 步骤 | 你做什么 |
| --- | --- |
| 1 | 下载项目，运行 `python3 scripts/meal_clear.py demo` |
| 2 | 让支持本地 Skill 的 Agent 读取 [SKILL.md](SKILL.md) |
| 3 | 说出每人的要求和预算，看结果：谁吃什么、总价、已核实／冲突／未知 |

![对话与程序输出示意：用户说两人预算50元，程序输出模拟报价38.00元；改一句后输出43.00元，delta_cents为500](docs/images/02-terminal-demo.svg)

<details>
<summary>文字版流程（Mermaid）</summary>

```mermaid
flowchart LR
  A[你说一句] --> B[逐人检查]
  B --> C[比较方案]
  C --> D[给出报价 38.00]
  D --> E[改一句：B想吃牛肉]
  E --> F[只重算B 43.00 +5]
```

</details>

可以这样对 Agent 说：

> 读取这个项目的 SKILL.md，使用“麦麦明白点”。先用内置模拟数据演示两人选餐，再把 B 的主食要求改成牛肉，其他要求不变，解释差价。

## 多方案排名
![多方案排名：模拟数据，3人预算70元，第1名52.50元依赖券，第2名53.00元不依赖券，第3名53.50元偏好最多；排序规则与检查范围](docs/images/05-ranking.svg)
`demo-multi` 的输出节选（模拟数据，非真实菜单与价格）：
```text
名次 | 总价 | 比第1名 | 偏好 | 券依赖
1 | ¥52.50 | — | 1/3 | 1
2 | ¥53.00 | +¥0.50 | 0/3 | 0
3 | ¥53.50 | +¥1.00 | 2/3 | 1
共检查 105 个整单候选（搜索空间 392 种），结果是已检查方案中的排名，不是全局最优。
```
排序规则：先过门槛（每人硬要求全部已核实且预算内），再按总价升序、偏好数降序、新购卡升序、券依赖升序、方案编号排列；每个名次附理由。未领取的券不计入报价。请求格式见 [数据契约](references/data-contract.md)，外送与得来速写法见 [examples/request-delivery.json](examples/request-delivery.json)、[examples/request-drive-thru.json](examples/request-drive-thru.json)。

## 改一句，不重来

![改一句前后对比：A保持模拟鸡肉组合19元，B从鸡肉组合19元换成牛肉组合24元，合计从38.00元变为43.00元，增加5元](docs/images/03-change-one-line.svg)

只改 B，其他条件（不辣、无糖、预算）保留；旧报价不会被当作新结果。

## 它不会做什么

![安全边界：只读，会逐人核对和算差价；不会下单、领券、支付、买卡、取消订单](docs/images/04-safety-boundary.svg)

不知道，就明确说不知道。仅比较已检查的有限候选。

## 安装与依赖

- Python ≥ 3.9，**零运行依赖**。
- 把整个 `meal-clear` 目录放进 Agent 的 Skill 位置，或使用发布包 `dist/meal-clear.skill`（由 `python3 scripts/build_skill.py` 生成）。
- 真实菜单模式需自行配置凭证，详见 [使用与接入](docs/使用与接入.md)。

## 目录

| 路径 | 说明 |
| --- | --- |
| `SKILL.md` | Skill 入口 |
| `scripts/` | 命令行程序与打包脚本 |
| `references/` · `examples/` | 数据格式、验收场景、模拟数据 |
| `docs/` | 使用指南、[真实验证与能力边界](docs/真实验证与能力边界.md)、配图 |
| `tests/` | 离线回归测试 |

## 合规与文档

[参赛声明](CONTEST_DECLARATION.md) · [MCP 接入说明](MCP_INTEGRATION.md) · [MCP 配置示例](mcp-config.example.json) · [LICENSE](LICENSE) · [真实验证与能力边界](docs/真实验证与能力边界.md)

**同日全量验收（作者本地报告记录，未经第三方复核）：** 自动测试方法与数据子用例、测试点和函数覆盖、两店真实只读验证、跨早餐至正餐重算、真实 401 停止和非白名单工具阻断检查的具体数字与口径见[全量报告与逐点覆盖矩阵](https://github.com/brucezhao-finance/meal-clear/blob/main/verification/2026-10-19-full/全量测试报告.md)。

[真实联调记录](https://github.com/brucezhao-finance/meal-clear/blob/main/verification/真实联调记录.md) · [结构化验证摘要](https://github.com/brucezhao-finance/meal-clear/blob/main/verification/live-validation.json)

## 能力与边界

| 项目 | 当前状态 |
| --- | --- |
| 逐人约束、预算比较、修改后差异计算 | 已实现；同门店、同时间，1–6 人；到店自取、得来速、外送 |
| 多方案排名与理由、券预览（可领/已领/本店可用） | 已实现，全部只读；**仅经本地模拟服务器验证，真实账户只读验证未做** |
| 真实菜单、无糖饮品替换、官方核价 | 已接入；只处理接口明确支持的套餐与选项 |
| 门店券 | 能查询并有限尝试券商品；已验证单张当前券参与官方算价，未验证核销或多券 |
| 会员卡回本 | 支持给定卡费与每次节省额的计算；尚未接入实时卡权益 |
| 下单、领券、支付、积分兑换、取消 | 客户端拒绝调用相关写工具 |

仅比较已检查的有限候选，**不承诺全网最低价**。领券、下单、团餐为后续阶段，**当前未实现**；也不支持任意去冰去酱或自动长期口味记忆；无法保证过敏安全。验证分级：模拟已验证 / 真实只读未验证（本阶段新能力）。真实菜单缺少的属性保持未知。[完整限制与常见问题](docs/使用与接入.md#常见问题)

## 验证与维护

```sh
python3 -m unittest discover -s tests -v
```

**154 项离线测试**（含本地回环模拟 MCP 服务器）覆盖金额精度、卡费、未知约束、多人排名、取餐方式、隐私脱敏、限流、券预览、换店报价失效、官方费用矛盾、协议解析、文件安全与写工具拒绝。测试不需要网络和真实账户。另有本次验收的 [27 项命令行场景与 5 项故障测试](https://github.com/brucezhao-finance/meal-clear/blob/main/verification/2026-10-19-acceptance/完整测试过程.md)。

全量复现入口是 `python3 verification/2026-10-19-full/run_full.py`，其中可能包含联网真实只读步骤，运行前请先阅读脚本；全量清单不等于所有账户、门店、模型或未来输入永不失败，外部验证边界逐项列在[报告](https://github.com/brucezhao-finance/meal-clear/blob/main/verification/2026-10-19-full/全量测试报告.md)中。

[开发与接入指南](docs/使用与接入.md) · [验收场景](references/evaluation-cases.md) · [项目规则](AGENTS.md)

## 免责声明

麦麦明白点是使用麦当劳 MCP 的独立项目，**非麦当劳官方产品**，为参赛作品。示例价格均为模拟数据，真实价格以官方当次核价为准；不构成医疗营养建议。
