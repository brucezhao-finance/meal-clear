# 麦麦明白点

Python 3.9+、标准库实现的到店自取选餐 Skill。`scripts/meal_clear` 为业务代码，`examples` 是明确标注的模拟数据，`tests` 是离线行为回归。

- 运行：`python3 scripts/meal_clear.py demo`；测试：`python3 -m unittest discover -s tests -v`。
- 修改后必须运行上述回归。新增逻辑用类型注解、docstring，并兼容 Python 3.9。
- 线上 MCP 客户端只允许明确列出的只读工具；禁止新增下单、领券、支付、兑换、取消等写操作。
- 不记录 Token、券码、手机号、地址或完整账户回包；不复制根目录的敏感文件。
- 演示数据不得伪装成真实菜单；历史回放不得伪装成当前报价。
- Unknown 不能当作满足硬约束。公式、上下文匹配、逐人约束、金额单位和报价有效性是交付重点。

当前工作目录无需依赖安装。`pyproject.toml` 无运行时第三方依赖；优先保持小范围可审查实现。
