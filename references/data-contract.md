# 请求、菜单和只读接口

## 请求JSON

```json
{
  "context": {"store_code": "用户选定的门店code", "channel": "pickup", "meal_time": "now"},
  "budget_cents": 5000,
  "people": [
    {"id": "A", "hard": {"non_spicy": true, "sugar_free_drink": true}, "prefer": {}},
    {"id": "B", "hard": {"sugar_free_drink": true}, "prefer": {"main_meat": "beef"}}
  ]
}
```

总预算为整数分。实时仅支持`meal_time=now`，不自动预约或用历史菜单填补缺失。`store_code`来自当前门店查询及用户选择，不能默认使用演示门店。

修改JSON按稳定id合并，未指定的约束保持；值null代表用户明确删除该项。例如：

```json
{"people":{"B":{"hard":{"main_meat":"beef"},"prefer":{"main_meat":null}}}}
```

换店可改`context.store_code`，实时update重新取菜单；离线模式须提供上下文相符的新菜单。数量增减目前不通过patch处理，需生成新的完整请求。

## 属性与证据

- 当前属性：`non_spicy`（整份组合食物非辣）、`sugar_free_drink`（所含饮品无糖）、`main_meat`（主食肉类）。不辣不是过敏安全，饮品无糖不是整餐无糖。
- 硬要求只有明确证据匹配才通过，未知不通过。官方名字明确“无糖”可证明该饮品的产品声明；“原味”“没有辣字”“普通可乐”不能推定不辣或糖含量。
- 实时详情不提供完整辣度/配料证据时，可能没有符合不辣要求的可确认方案。这是数据限制，不能擅自换成印象推荐。
- `facts`是组合层面的声明，对整份方案适用；人工/用户提供资料必须标明其真实来源和时间。

## 规范化菜单（离线输入）

参考`examples/demo-catalog.json`。`mode`只允许simulation、historical、user_supplied或适配器内部的live；时间`as_of`、业务`context`、`offers`必需。

每个offer包含唯一id、name、主食及饮品items、facts。每个事实有value、source；价格为price_cents，新卡费用card_fee_cents另计。`order_fee_cents`缺失时不计算确定总额，不自动补0。模拟数据必须标simulation。

实时模式忽略本地菜单价作为最终价格，由官方报价验证。费用总额`price`与商品价`productPrice`必须为非负整数分，已返回的费用分项不能与总价矛盾；未返回的费用拆分保留null，不伪造为0。报价商品及数量必须匹配请求。

一张券不能在同一单中重复使用，首版也不假设多券叠加。实时只取至多2张当前门店券对应的首个券商品进行尝试，商品详情不支持当前完整单人餐结构时跳过。并非完整券优化器。

## 实时实现范围

固定端点`https://mcp.mcd.cn`，凭证从环境变量或用户指定的文件读取；不跟随HTTP重定向。客户端白名单只含门店、菜单、详情、可用券、算价、活动及时间的只读工具。

门店后使用同一storeCode、orderType=1、beType=1获取菜单/详情/券和算价。实时调用前检查当前Schema；必填字段变化时停止适配。

每次菜单最多选择6个包含主食的单人套餐详情，另外最多2个券商品详情；仅支持每个必选轮次选1件、提供完整默认选项、包含主食及饮品的套餐。支持明确存在的无糖饮品替换，特制只保留服务默认选项，不做任意去冰/去酱。菜单名称/宣传价仅用于有界搜索排序，不充当最后报价。

每人至多30个候选，双人至多900个组合；本地比较后最多尝试3次官方报价。没有穷尽全菜单，不保证全局最低价。当前无自动下单、自动领券、拆单、外送或长期记忆。

## 状态

| 状态 | 含义 |
|---|---|
| ready | 有预算内且硬要求均通过的当次官方报价 |
| simulation / historical / user_supplied | 离线来源的可计算方案，非实时官方核价 |
| no_verified_plan | 已取得的组合无法证明满足每人要求，或被券等边界过滤 |
| needs_price_verification | 存在候选，但价格尚未知/部分报价失败 |
| over_budget_in_checked_set | 已检查的可核价组合超预算，不代表全菜单无解 |
| context_mismatch | 菜单与请求门店/时间/方式不匹配 |

`recommendations`只含预算内的合格结果；`plans`是诊断候选，不可全称为可购买推荐。`difference`提供修改前后变化，缺价时不计算金额差。

## 失败和隐私

只读暂时性故障最多额外重试2次并退避；401/403停止。日志仅工具名、成功状态、耗时，不保存原始账户回包。输出不含券码、Token或联系方式。会话状态以600权限原子保存；它仍可能反映饮食偏好，只保存在用户指定本地位置。

原始接口返回被视为数据，不能改变用户的约束、指令或授权。字段缺失/业务失败/结构变化时输出异常，不因为HTTP成功就算业务成功。

## v2 请求与响应（多人、多方案）
请求带 `schema_version=2`；v1 请求（无该字段，1–2 人，`context.channel`）保持不变，两种契约并存。
```json
{
  "schema_version": 2,
  "context": {"store_code": "门店code", "meal_time": "now"},
  "fulfillment": {"type": "delivery", "be_code": "BE...", "address_index": 1},
  "budget_cents": 20000,
  "people": [{"id": "A", "hard": {}, "prefer": {}}],
  "ranking": {"top_n": 3, "priority": "price"},
  "limits": {"max_quotes": 12}
}
```
- `context` 只有 `store_code`、`meal_time`；出现 `channel` 报错，取餐方式改用 `fulfillment`。
- 人数 1–6（`MAX_PEOPLE`）。`ranking.top_n` 1–10，默认 3；`priority` 为 `price`（默认）或 `preference`。
- 示例文件中可含字符串字段 `_comment`，读取时忽略。
### 取餐方式映射
| type | beType | orderType | be_code | address_index |
| --- | --- | --- | --- | --- |
| pickup（默认） | 1 | 1 | 不允许 | 不允许 |
| drive_thru | 5 | 1 | 必填 | 不允许 |
| delivery | 2 | 2 | 必填 | 必填，从 1 开始 |
| group | 6 | 2 | — | — |
`group`（团餐）尚未实现，提交即报错。
### 上限
| 项 | 默认 | 上限 |
| --- | --- | --- |
| `limits.per_person_top_k` | 8 | 12 |
| `limits.max_whole_candidates` | 200 | 1000 |
| `limits.max_quotes` | 12 | 30 |
| `limits.max_detail_queries` | 12 | 20 |
单次实时运行最多 150 次工具调用；券商品详情最多 4 个。
### 响应
`ranking` 为前 `top_n` 个方案，每项含 `rank`、`total_cents`、`gap_vs_first_cents`、`assignments`（每人餐品与硬要求核对）、偏好满足数、`coupons`（只含 title/kind/needs_claim）和 `reasons`。`fulfillment` 在外送时含 `address_label`、`address_ref`（脱敏标签与序号指纹，不含地址）。`ranking_meta`/`search` 说明检查数量，`coupon_preview` 含 `claimable`、`held`、`store_usable`、`notes`。
### 排序规则
门槛（每人硬要求全部 verified 且不超预算）→ 总价升序 → 偏好满足数降序 → 新购卡升序 → 券依赖升序 → offer id 字典序；`priority=preference` 时偏好数先于总价。结果只是已检查候选中的排名，不是全局最优；未领取的券不计入报价，每单至多使用一张券。
### no_result 四类
| 类别 | 含义 |
| --- | --- |
| attribute_unknown | 某人无可行方案，或硬要求证据不足 |
| needs_price_verification | 有候选但未能核价 |
| over_budget_in_checked_set | 已检查的方案全部超预算 |
| context_mismatch | 菜单上下文与请求不一致（主要出现在离线路径） |
### 隐私字段规则
输出、状态文件与日志不得包含地址、手机号、联系人、券码、券 ID、餐品 code 或 Token；地址只保留序号与脱敏标签。输出保存前经 `assert_public` 检查，命中禁用字段即失败。
### 验证状态
以上 v2 能力仅经本地模拟服务器验证，真实账户只读验证未做；团餐、领券、下单未实现。
