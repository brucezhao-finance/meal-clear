# MCP 接入说明

本文件描述麦麦明白点（meal-clear）对麦当劳 MCP 的实际使用方式。内容依据 `scripts/meal_clear/mcp.py`、`live.py`、`cli.py` 编写；与代码不一致时以代码为准。本项目为独立开发的参赛作品，非麦当劳官方产品。

## 1. Server

| 项目 | 内容 |
| --- | --- |
| 端点 | `https://mcp.mcd.cn`（`mcp.py` 中的常量 `ENDPOINT`，客户端没有任意端点入口） |
| 传输 | Streamable HTTP：`POST` JSON-RPC，`Accept: application/json, text/event-stream`，可解析 SSE 响应 |
| 协议版本 | 默认 `2024-11-05`，以 `initialize` 返回值为准 |
| 客户端标识 | `meal-clear`，版本 `0.1.0` |
| 重定向 | 禁止（`NoRedirect`），认证头不会随跳转发往其他地址 |

## 2. 认证与配置

- Token 先读环境变量 `MCD_MCP_TOKEN`；未设置时才读取用户用 `--token-file` 显式指定的本地文件。Token 只保存在内存，以 `Authorization: Bearer` 头发送，不输出、不入库、不写日志。
- [`mcp-config.example.json`](mcp-config.example.json) 只含占位符 `${MCD_MCP_TOKEN}`，供在通用 MCP 客户端中接入同一端点时参考。meal-clear 自带的客户端**不读取**该文件。
- 不要把真实 Token 写进任何提交到仓库的文件。

## 3. 白名单工具：12 个只读 + 2 个受控写
`mcp.py` 中的 `READ_ONLY` 共 12 个只读工具（含 `query-order`）。`McpClient.call()` 对白名单之外的名称直接抛出 `McpError`，不发起网络请求。
| 工具 | 代码中是否调用 | 调用位置 | 用途 | 隐私处理 |
| --- | --- | --- | --- | --- |
| `query-nearby-stores` | 是 | `cli.py` 的 `stores` 子命令（`--be-type 1/5`） | 按城市和关键词查询到店/得来速门店，由用户自行选店 | 仅展示门店公开信息 |
| `query-meals` | 是 | `live.py` `fetch_menu_offers` | 读取所选门店当前菜单 | 无 |
| `query-store-coupons` | 是 | `live.py` `fetch_menu_offers`、`MultiLivePlanner.coupon_preview` | 读取本店账户可用券（只读，不领取） | 只输出券名，券码不入输出 |
| `query-meal-detail` | 是 | `live.py` `fetch_menu_offers` | 读取候选套餐的详情与可选项 | 无 |
| `calculate-price` | 是 | `live.py` `LivePlanner.plan`、`MultiLivePlanner.plan_v2` | 对候选组合做官方核价 | 券码只存在于请求内存，不进输出 |
| `available-coupons` | 是 | `MultiLivePlanner.coupon_preview` | 列出可领取的券（只读预览，不领取） | 只输出券名与是否需领取 |
| `query-my-coupons` | 是 | `MultiLivePlanner.coupon_preview` | 列出已持有的券（分页，每页 200） | 只输出券名 |
| `delivery-query-addresses` | 是 | `MultiLivePlanner.list_addresses`（`addresses` 命令） | 列出配送地址 | 立即脱敏，只保留序号与标签，不保留地址、手机号、联系人 |
| `delivery-query-stores` | 是 | `MultiLivePlanner.list_delivery_stores`（`delivery-stores` 命令） | 查询某地址可配送门店与 beCode | 内部使用 addressId，输出不含地址内容 |
| `campaign-calendar` | 否 | 无 | 白名单允许，当前未调用 | — |
| `now-time-info` | 否 | 无 | 白名单允许，当前未调用 | — |
| `query-order` | 是 | `order-status` 命令 | 只读查询订单状态 | 订单号输出时只显示打码尾号 |
其余写类工具（取消订单、新建地址、抽奖、积分商城下单、派对/活动预约 `party-order-create`、买卡、积分兑换等）**永久拒绝**：不在任何白名单内，客户端在发出网络请求之前就抛错，相关测试断言模拟服务器上的禁止工具尝试为空。团餐没有独立的下单工具，走受控写工具 `create-order`（`beType=6`），其真实下单在官方端点上被代码拒绝。受控写工具见下一节。
另外，`inspect-tools` 子命令和 `live-plan` 会通过 `tools/list`（含分页，最多 10 页）读取服务端工具清单：前者只列出清单中属于白名单的工具名与总数，后者用于校验所需工具存在且必填字段未变化。

### 团餐专用只读工具（另 2 个，不计入上述 12 个只读）
`mcp.py` 的 `GROUP_READ_ONLY` 另有 2 个团餐专用只读工具（12 个只读 + 2 个团餐专用只读 = 全部可调用只读工具）：
| 工具 | 命令 | 用途 | 参数 |
| --- | --- | --- | --- |
| `query-promotions` | `group-info`、团餐 `live-plan` | 查询门店促销规则（满减、满折），只作提示 | 门店上下文 `storeCode`、`orderType`、`beType` |
| `query-meal-assistance` | `group-info`、团餐 `live-plan` | 查询助餐服务与 `gmServiceCode`；用户从中选定，程序不采用官方默认项 | `storeCode` |

团餐的 `create-order` 需带用户选定的 `gmServiceCode`，有预约时带 `reservationDate`（`yyyy-MM-dd HH:mm`）；官方端点上 `beType=6` 的 `create-order` 与团餐领券在传输层、命令层均被拒绝（退出码 4），只在本机回环模拟服务上可演练。
### 受控写工具（官方端点默认关闭，未经真实验证）
`mcp.py` 的 `CONTROLLED_WRITE` 只有 2 个工具，只能经 `gate.py` 的 `WriteGate` 调用（`McpClient.call_write`，需要门签发的授权，单次尝试，**永不自动重试**）：
| 工具 | 命令 | 用途 | 参数映射 |
| --- | --- | --- | --- |
| `auto-bind-coupons` | `order-claim --execute` | 一键领取全部可领券（无法逐张选择，不可撤销） | 无参数 |
| `create-order` | `order-place --execute` | 创建待支付订单（不付款、不取消） | 按摘要映射 `storeCode`、`orderType`、`beType`，必要时 `beCode`；到店/得来速带 `takeWayCode`，外送带 `addressId`；`items` 为摘要核价用的商品行，`needTableware=false`；不发送备注、预约时间等其他字段 |
**四道门**（缺一不可）：① 命令带 `--execute`；② `--confirm-digest` 为完整 64 位摘要编号，与账本记录匹配且摘要未过期；③ 写目标固定：严格回环地址（`127.0.0.1` 或 `[::1]`），或官方端点 `https://mcp.mcd.cn`（字符串严格相等，命令行无法指定其他地址）；④ 仅官方端点：真实写开关打开 + 紧急锁未锁 + stdin/stdout 均为终端并由用户亲手输入确认短语 + 金额与 24 小时次数上限（默认单笔 ¥100、下单 3 次、领券 1 次；硬上限 ¥500、10 次、3 次）。回环模拟不需要第 4 道门。
**官方写通道默认关闭，真实领券/下单未经真实验证**：开关未开时在读取 Token 和任何网络请求之前拒绝（退出码 4）；官方写使用单次尝试，3xx 一律按结果未知处理，结果未知会锁定该篮子 24 小时（退出码 8），由用户核实后用 `order-resolve` 人工标注；`write-lock` 可紧急锁定。写命令只接受 `MCD_MCP_TOKEN` 或固定凭证文件，不接受任意 `--token-file`。局限：终端检查只能证明有交互终端，无法区分真人与被程序驱动的伪终端，因此另有 Agent 禁令、开关由用户自己打开、金额与次数上限和紧急锁作为防线。启用步骤见 [真实写操作启用指南](docs/真实写操作启用指南.md)。
**写前账本**：发出写请求前先把“待执行”记入本地账本（`local-state/orders`，目录 700、文件 600，只存步骤状态、时间与数字错误码，不存参数、回包、券码、地址或支付链接）；重复同一摘要同一步骤被拒（退出码 5）。仅支持 POSIX。
**结果分类**：成功；官方明确业务失败（可核实原因，退出码 1）；结果未知（超时、结构异常、成功码但缺订单号，退出码 6，**禁止重试**，需 `order-status` 或在 App 订单列表核实）。领券后和下单前都会重新查券、重新核价，价格或内容变化则作废旧摘要并生成新摘要（退出码 3）。
## 4. 调用流程

`live-plan`（真实模式）：

1. `initialize` → `notifications/initialized`。
2. `tools/list`，确认首版所需的只读工具存在，且必填字段没有变化；否则停止并提示需要更新适配。
3. 以用户给定的门店上下文（`storeCode`、`orderType=1`、`beType=1`）调用 `query-meals` 与 `query-store-coupons`。
4. 对有限的候选套餐调用 `query-meal-detail`。
5. 对候选组合调用 `calculate-price` 取得官方整单应付金额（v1 最多 3 个组合；v2 最多 `limits.max_quotes` 个，默认 12、上限 30）。
6. 本地确定性校验：逐人硬约束、预算、门店与时间上下文一致、未知属性不当作满足。
7. 输出方案并标明报价时间；`live-plan` 本身不领券、不下单。

v2 多人流程（`live-plan` 带 `schema_version=2` 请求）按取餐方式使用不同参数，其余步骤相同：
| 取餐方式 | beType | orderType | beCode | 调用链 |
| --- | --- | --- | --- | --- |
| 到店自取 | 1 | 1 | 不传 | 菜单/详情/券/核价 |
| 得来速 | 5 | 1 | 必填 | 同上，带 beCode；门店来自 `stores --be-type 5` |
| 外送 | 2 | 2 | 必填 | `delivery-query-addresses` 取序号 → `delivery-query-stores` 校验所选门店与 beCode 在可配送列表内 → 菜单/详情/券/核价（核价含配送费、包装费） |
| 团餐 | 6 | 2 | 必填 | `delivery-query-addresses` → `delivery-query-stores`（`beType=6`）→ `query-meal-assistance` 取助餐服务、`query-promotions` 取促销规则（仅提示）→ 菜单/详情/券 → `calculate-price`（带用户选定的 `gmServiceCode`，有预约时带 `reservationDate`）；**真实团餐写被代码拒绝** |
券预览另调用 `available-coupons`、`query-my-coupons`，任一类失败只在说明里记录，不影响排名；预览不领取任何券。
确认式下单：`order-summary`（只读联网 + 写本地账本）→ `order-claim`（预演或 `--execute`）→ `order-place`（预演或 `--execute`）→ `order-status`（只读）。

其他入口：

- `stores`：只调用 `query-nearby-stores`，列出门店供用户明确选择，不自动选店。
- `addresses`、`delivery-stores`、`coupons`：分别只读列出地址序号、可配送门店、券预览。
- `update`：状态文件为真实模式时，修改后重新执行上述流程取数核价，不复用旧报价；离线状态需显式提供 `--catalog`。
- `demo`、`plan`、`break-even`：完全离线，不联网、不需要 Token。

## 5. 业务价值

- 1–50 个人点餐时，逐人检查硬性要求（如不辣、无糖饮品），在预算内比较有限的候选方案。
- 只改一个人的要求，其他人的要求和预算保持不变，重新选餐并说明价差。
- 价格取自官方核价；无法确认的属性（如菜单未标明辣度）保持“未知”，不当作满足。
- 仅比较已检查的候选，不承诺全网最低价，也不构成营养或医疗建议。

## 6. 安全边界

- 默认只读 + 受控写（官方端点默认关闭）：领券和下单只能经四道门，不支付、不买卡、不兑换、不取消、不抽奖；其余写类工具无法通过客户端调用。
- 限流：客户端内置滑动窗口，默认 60 秒最多 500 次调用，超出时先等待；v2 单次运行另有 150 次工具调用上限，达到上限即停止并在说明里标注已检查范围。
- 回环测试端点：`McpClient` 的 `endpoint` 参数只接受 `http://127.0.0.1:端口` 或 `http://localhost:端口`，供本地模拟 MCP 服务器做自动测试；命令行没有此入口，正式运行只访问固定端点。写目标另按解析后的 IP 严格校验：只认 `127.0.0.1` 与 `[::1]` 字面量，不做 DNS 猜测；`localhost` 仅供既有只读测试端点，且要求全部解析结果为回环。
- HTTP 401/403 立即停止；仅对 429、500、502、503、504 与网络超时做最多共 3 次尝试并退避。
- 响应上限 8 MB；业务失败只暴露状态码，不保存原始账户回包与券码。
- 保存的状态文件权限为 600；`.gitignore` 排除 `*token*`、`.env`、`local-state/`、`live-private/`。
- 真实模式只支持 `meal_time=now`，门店编码必须为数字。

## 7. 验证状态

- 离线行为回归可复现：`python3 -m unittest discover -s tests`，不需要网络或账户；其中包含本地模拟 MCP 服务器上的多人、取餐方式、券预览端到端用例。
- **验证分级（三档）：** ① 模拟已验证：多人（至 50 人）、取餐方式、排名、券预览、订单摘要、受控领券与下单、订单查询、重复与未知结果保护，均在本机回环模拟服务与合成数据上通过自动测试；② 真实只读待用户验证：v1 到店自取的真实只读有作者自述记录（2026-10-17/19，未经第三方复现）；v2 新增只读能力（多人、外送/得来速、团餐、券预览）在真实账户上的行为作者尚未用真实 Token 验证；③ 真实领券下单代码已具备但默认关闭、未经真实验证：官方端点写通道须用户自己打开开关并在终端亲手确认，官方真实返回结构与超时后的真实状态均未验证。团餐：模拟已验证、真实只读待用户验证；团餐真实领券/下单未开放，官方端点上被代码拒绝。
- 真实联调为作者自述记录，未经第三方复现：见 [verification/真实联调记录.md](https://github.com/brucezhao-finance/meal-clear/blob/main/verification/真实联调记录.md) 与 [verification/2026-10-19-acceptance/测试报告.md](https://github.com/brucezhao-finance/meal-clear/blob/main/verification/2026-10-19-acceptance/测试报告.md)。报价仅对应当时的时间、门店与账户，不代表当前价格。
