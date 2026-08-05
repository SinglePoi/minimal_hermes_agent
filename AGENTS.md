# 夜莺（Nightingale）天气助手

这是一个内部工具项目，以下是 Agent 需要知道的项目上下文。

## 项目信息
- 产品名：夜莺（Nightingale）天气查询助手
- 目标用户：公司内部客服团队
- 核心功能：查询城市天气（通过 `get_weather` 工具）
- 技术栈：Python + DeepSeek API（OpenAI 兼容接口）

## 代码规范
- 所有函数必须写中文 docstring
- 工具名一律使用下划线命名（如 `get_weather`、`session_search`）
- 记忆文件条目之间用 § 分隔，不要混用其他分隔符

## 运维约定
- 生产环境密钥只允许通过环境变量注入，禁止硬编码
- 每次改动先查 Hermes Agent 源码确认设计对齐
