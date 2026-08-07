# Agent 骨架项目

这是一个对齐 Hermes Agent 架构的迷你 Agent 骨架，以下是 Agent 需要知道的项目上下文。

## 项目信息
- 定位：通用 Agent 骨架，不限定业务领域（类似 Hermes 的通用助手形态）
- 核心能力：Agent Loop、工具系统、三层记忆/召回、多轮对话、上下文压缩、
  可插拔 memory provider、危险命令审批、Skills 按需加载、HTTP 服务化 + Web 前端
- 技术栈：Python + DeepSeek API（OpenAI 兼容接口）

## 代码规范
- 所有函数必须写中文 docstring
- 工具名一律使用下划线命名（如 `web_search`、`session_search`）
- 记忆文件条目之间用 § 分隔，不要混用其他分隔符

## 运维约定
- 生产环境密钥只允许通过环境变量注入，禁止硬编码
- 每次改动先查 Hermes Agent 源码确认设计对齐
