# 麦麦明白点 · meal-clear

**帮我选好并买明白这顿饭。** 一份可运行的 Skill v0.1，支持到店自取选餐、逐人约束检查、官方核价，以及修改要求后重新比较。

## 已实现

- Skill把自然语言任务整理成请求，Python负责金额、逐人硬约束、有限组合与差异计算。
- “改一句”：保留其他人要求和预算，修改口味后重新生成结果；真实模式重新取数和官方报价。
- “要求对账单”：已核实/冲突/未知分开，未知不能当作满足不辣或无糖。
- 比较便宜与偏好匹配程度；不为凑两套推荐更贵、还多购卡、偏好却不更好的方案。
- 菜单明确支持的无糖饮品替换、当前门店券查询与有限券商品尝试。
- 固定条件的会员回本计算；新卡费计入本次支出，不能用未来可能节省抵扣当前预算。
- 模拟、历史、用户提供与实时数据分开；只读MCP白名单、有限重试、隐私状态文件。

## 直接体验（无需联网/Token/依赖安装）

在此目录运行：

```sh
python3 scripts/meal_clear.py demo
```

演示使用**完全模拟**的单人组合、无糖和非辣证据，展示两人方案从38元改为43元，B换牛肉，A的要求和50元预算保留。这里的商品与价格不是当前麦当劳菜单，不可据此购买。

这些是Skill配套命令，不是另外一个大模型服务；无需购买模型API。让当前Agent读取`SKILL.md`理解中文需求，再调用程序。当前项目没有单独的网页或聊天后端。

## 在Agent中使用

入口是本目录的 `SKILL.md`，可明确指定本地文件使用，例如：“读取这个项目的SKILL.md，用麦麦明白点帮我比较这顿饭”。也可以把**完整meal-clear目录**放入目标Agent支持的Skill目录，保留scripts、references和examples相对位置。本次未修改你的全局Skill配置。

请求格式见 [输入与接口约定](references/data-contract.md)。不能通过去掉不支持的约束来制造成功推荐。

## 真实只读联调

环境变量 `MCD_MCP_TOKEN` 由用户在安全环境中设置。也支持`--token-file`读取已有本地文件，命令中只传路径，不传明文Token。没有凭证时仍可离线演示。

```sh
# 发现当前工具
python3 scripts/meal_clear.py inspect-tools

# 让用户确认实际门店，不能直接替用户选择技术测试店
python3 scripts/meal_clear.py stores --city 上海 --keyword 静安寺

# 编辑请求中的门店、预算、人数与要求后再运行
python3 scripts/meal_clear.py live-plan --request examples/live-request.json --save local-state/meal.json
```

`examples/live-request.json`使用上海静安寺工程测试门店1450002、单人无糖饮品、预算100元。它不是你的默认位置；使用前应改为自己确认的门店和要求。实时推荐通常共查询一次菜单、一次券、至多8个详情、至多3次报价，限于单人套餐结构。

修改后重新核价：

```sh
python3 scripts/meal_clear.py update --state local-state/meal.json --patch examples/patch-live-beef.json --save local-state/updated.json
```

该修改示例将A主食要求改为牛肉，并保留无糖、原预算和门店。没有新的可行方案时会明确失败，不使用旧报价冒充成功。

## 离线文件和会话

```sh
python3 scripts/meal_clear.py plan --request examples/request.json --catalog examples/demo-catalog.json --save local-state/demo.json
python3 scripts/meal_clear.py update --state local-state/demo.json --patch examples/patch-beef.json --catalog examples/demo-catalog.json
python3 scripts/meal_clear.py break-even --fee 1400 --saving 200
```

金额参数为整数分。最后一个命令只计算固定每次省2元、卡费14元的回本次数7次，不表示今天存在该卡或该优惠。

CLI退出码：0有推荐/查询或计算完成；2无可确认推荐（未知、超预算、核价失败等业务状态）；1输入、认证或协议错误。用`--json`获得结构化结果。

状态文件保存请求、脱敏结果和工具耗时，权限为600，不保存Token或券码；仍可能包含你的口味偏好，不应上传公开仓库。

## 验证

```sh
python3 -m unittest discover -s tests -v
# 环境已有pytest时也可运行
python3 -m pytest tests
```

27项离线回归已通过，覆盖金额单位/精度、卡费、约束未知、双人修改、换店报价失效、多券边界、官方费用矛盾、SSE解析和写工具拒绝。结构校验`quick_validate.py`通过；Ruff检查通过，Black格式化完成。

本次开发环境直接使用已有标准库和本机已有校验工具，没有全局pip安装。`pyproject.toml`与`uv.lock`保留零运行依赖配置，也可用已有uv管理环境。

2026-10-17真实测试：官方35个工具发现成功；上海静安寺测试门店返回126个菜单条目；门店券查询成功但该账户当时返回0张，**有券核销/算价分支未在真实账户验证**。单人无糖套餐核价22.90元；加牛肉要求重新取数、核价得到38.00元，差15.10元。日期与范围见 [真实联调记录](verification/真实联调记录.md)。这些是当次报价，不保证今天其他时间、其他门店/账户同价。

## 限制与常见问题

- **说不辣却没有结果？** 当前菜单/详情通常不完整声明辣度；不能凭“没有辣字”确认不辣。程序拒绝强行推荐，模拟菜单仅用来验证这条逻辑。过敏安全不在能力范围。
- **为什么有官方总价，运费拆分却未知？** 到店自取实测返回整单`price`与`productPrice`，部分费用字段为空。采用官方整单应付，拆分保持未知，不擅自填0。
- **为什么不叫全网最便宜？** 首版只搜索有限单人套餐和少量券商品，没有覆盖全菜单、所有拆分与跨渠道活动。
- **能自动下单吗？** 不能。首版客户端主动拒绝创建订单、领券、取消和积分消费等工具。
- **套餐没覆盖？** 目前仅支持每个必选轮次选一件、有完整默认项、含主食和饮品的单人套餐。不会把单个薯条当作一餐。
- **能自动去冰去酱吗？** 暂不支持任意特制修改；只保留返回的默认特制和明确支持的无糖饮料替换。不能用备注伪造能力。
- **报401/403或网络失败？** 检查授权环境；认证错误不重试。暂时性只读失败最多额外重试2次，失败后不会伪造结果。
- **会记住我的口味吗？** 本地会话可保留并修改，尚无自动长期偏好记忆。

## 目录

- `SKILL.md`、`agents/openai.yaml`：Agent入口和界面元数据。
- `scripts/meal_clear/`：约束组合、只读MCP、实时菜单与报价适配、CLI。
- `references/`：请求契约和验收场景。
- `examples/`：模拟菜单、请求与修改示例。
- `tests/`：无网络行为回归。
- `verification/`：当次公开技术验证摘要，不含原始账户回包。
- `dist/meal-clear.skill`：可分发的完整Skill压缩包；不包含local-state、凭证或缓存。
