---
name: meal-clear
description: 按人数、预算和个人口味规划一顿麦当劳到店自取餐，使用配套程序检查约束、核对官方报价与优惠条件，并在用户修改要求后重新比较。
---

# 麦麦明白点

帮用户选好并买明白这顿饭。一份Skill组织任务，Python负责确定性检查和计算，官方MCP负责当前数据与核价。

## 入口与范围

先执行本Skill目录下 `python3 scripts/meal_clear.py --help`，按需读 [输入与接口约定](references/data-contract.md)。命令路径相对本Skill根目录；若从其他目录调用，使用绝对脚本/数据路径。

首版：麦当劳、1–2人、同店同时间、到店自取。实际无糖替换、门店菜单与报价已做只读测试；不保证所有门店、券和条件都覆盖。非辣等证据缺失时应说明未知。

不因用户要求选餐或比较价格而领券、买卡、下单、支付、兑换、取消或联系店员。配套MCP客户端只允许白名单只读工具。

## 根据任务选择运行方式

- **体验流程：** `python3 scripts/meal_clear.py demo`，所有示例价格是模拟，不能转述为今天能买到的真实套餐。
- **真实门店查询：** `python3 scripts/meal_clear.py stores --city 城市 --keyword 关键词`。用户明确选择后才写入请求，不把技术测试门店当作默认门店。
- **真实推荐：** 将用户要求整理为请求JSON，执行 `python3 scripts/meal_clear.py live-plan --request 请求.json --save local-state/本次.json --json`。当前默认从`MCD_MCP_TOKEN`读取凭证；若用户已指定本地凭证文件，可用`--token-file`，禁止把Token本身放在命令参数、输出或代码中。
- **用户改条件：** 写修改JSON，执行 `python3 scripts/meal_clear.py update --state local-state/本次.json --patch 修改.json --save local-state/修改后.json --json`。实时状态会重新取数和核价，不复用旧报价。
- **用户提供菜单/历史资料：** `plan --request 请求.json --catalog 菜单.json --json`。数据模式必须准确标为user_supplied/historical；离线修改另需提供`--catalog`，不假装在线核价。
- **会员固定回本计算：** 仅已知卡费和同条件单次节省时，使用`break-even --fee 整数分 --saving 整数分`；输出null表示没有有限回本次数。不能据此声称已经取得当前会员条款。

## 理解要求

从用户现有话语提取门店、总预算、人数、每人硬要求与软偏好，只追问会阻止执行或改变可行性的缺失信息；不要再问已经回答过的问题。不把普通偏好擅自升级为忌口。

典型映射：不辣→`hard.non_spicy=true`，饮品无糖→`hard.sugar_free_drink=true`，“必须牛肉”→`hard.main_meat=beef`；“更想吃牛肉”放`prefer.main_meat=beef`。每位用餐者有稳定id。超出已有属性/当前数据支持的要求要说明，不能偷偷丢掉。

“不喝含糖饮料”不等于整餐无糖。过敏/医疗饮食要求不由现有口味标签证明安全。

## 解释结果

只将`recommendations`中的结果作为满足已声明硬要求、且预算内的方案。`plans`还可能包含超预算或价格未知候选，不能混作推荐。`ready`表示该次官方核价成功，未创建订单；simulation/historical/user_supplied必须显著说明模式与日期。

先给1–2个确有取舍的方案：每人吃什么、整单报价、为什么推荐、券和时段条件、下一步。更便宜但不更符合偏好的方案，以及更符合偏好但更贵的方案，可以一起解释；没有真正差异就给一套。程序会剔除更贵且偏好/购卡负担不更好的方案。

逐项展示硬要求状态：verified已核实、violated冲突、unknown未知。unknown不是通过，备注不是已支持的定制。官方总额已返回而拆分费用缺失时，说明“整单应付已核价，部分费用未拆分”，不能填0。

用户修改后读取`difference`，说明谁的餐品变了、总价差多少、哪些条件保持不变。价差为空时不编造数值。有限候选仅称“已检查方案中更划算”，不承诺全网最低价、库存或到店即取。

无推荐时区分上下文不匹配、属性未知、待核价和已检查集合超预算。说明最关键的障碍；只有用户愿意才放宽条件。工具失败不能编菜单、编码、价格或“成功”记录。

## 开发与回归

运行 `python3 -m unittest discover -s tests -v`。测试说明见 [验收场景](references/evaluation-cases.md)。不要将历史验证记录等同于此刻所有账户/门店都可用。长期偏好记忆、外送和交易能力尚未实现，不声称已经完成。
