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

## 3. 白名单只读工具

`mcp.py` 中的 `READ_ONLY` 共 7 个工具。`McpClient.call()` 对白名单之外的名称直接抛出 `McpError`，不发起网络请求。

| 工具 | 代码中是否调用 | 调用位置 | 用途 |
| --- | --- | --- | --- |
| `query-nearby-stores` | 是 | `cli.py` 的 `stores` 子命令 | 按城市和关键词查询门店，由用户自行选店 |
| `query-meals` | 是 | `live.py` `LivePlanner.catalog` | 读取所选门店当前菜单 |
| `query-store-coupons` | 是 | `live.py` `LivePlanner.catalog` | 读取该门店下账户可用券（只读，不领取） |
| `query-meal-detail` | 是 | `live.py` `LivePlanner.catalog` | 读取候选套餐的详情与可选项 |
| `calculate-price` | 是 | `live.py` `LivePlanner.plan` | 对候选组合做官方核价，最多尝试 3 个组合 |
| `campaign-calendar` | 否 | 无 | 白名单允许，当前未调用 |
| `now-time-info` | 否 | 无 | 白名单允许，当前未调用 |

另外，`inspect-tools` 子命令和 `live-plan` 会通过 `tools/list`（含分页，最多 10 页）读取服务端工具清单：前者只列出清单中属于白名单的工具名与总数，后者用于校验所需工具存在且必填字段未变化。

## 4. 调用流程

`live-plan`（真实模式）：

1. `initialize` → `notifications/initialized`。
2. `tools/list`，确认首版所需的只读工具存在，且必填字段没有变化；否则停止并提示需要更新适配。
3. 以用户给定的门店上下文（`storeCode`、`orderType=1`、`beType=1`）调用 `query-meals` 与 `query-store-coupons`。
4. 对有限的候选套餐调用 `query-meal-detail`。
5. 对候选组合调用 `calculate-price` 取得官方整单应付金额。
6. 本地确定性校验：逐人硬约束、预算、门店与时间上下文一致、未知属性不当作满足。
7. 输出方案并标明报价时间；不下单。

其他入口：

- `stores`：只调用 `query-nearby-stores`，列出门店供用户明确选择，不自动选店。
- `update`：状态文件为真实模式时，修改后重新执行上述流程取数核价，不复用旧报价；离线状态需显式提供 `--catalog`。
- `demo`、`plan`、`break-even`：完全离线，不联网、不需要 Token。

## 5. 业务价值

- 两个人点餐时，逐人检查硬性要求（如不辣、无糖饮品），在预算内比较有限的候选方案。
- 只改一个人的要求，其他人的要求和预算保持不变，重新选餐并说明价差。
- 价格取自官方核价；无法确认的属性（如菜单未标明辣度）保持“未知”，不当作满足。
- 仅比较已检查的候选，不承诺全网最低价，也不构成营养或医疗建议。

## 6. 安全边界

- 只读：不下单、不领券、不支付、不买卡、不兑换、不取消；写类工具无法通过客户端调用。
- HTTP 401/403 立即停止；仅对 429、500、502、503、504 与网络超时做最多共 3 次尝试并退避。
- 响应上限 8 MB；业务失败只暴露状态码，不保存原始账户回包与券码。
- 保存的状态文件权限为 600；`.gitignore` 排除 `*token*`、`.env`、`local-state/`、`live-private/`。
- 真实模式只支持 `meal_time=now`，门店编码必须为数字。

## 7. 验证状态

- 离线行为回归可复现：`python3 -m unittest discover -s tests`，不需要网络或账户。
- 真实联调为作者自述记录，未经第三方复现：见 [verification/真实联调记录.md](https://github.com/brucezhao-finance/meal-clear/blob/main/verification/真实联调记录.md) 与 [verification/2026-10-19-acceptance/测试报告.md](https://github.com/brucezhao-finance/meal-clear/blob/main/verification/2026-10-19-acceptance/测试报告.md)。报价仅对应当时的时间、门店与账户，不代表当前价格。
