# 最简 Agent 骨架（第一步）· DeepSeek 版（零依赖）

一个**零依赖**的 Agent Loop 最小实现：`minimal_agent.py`
（只用 Python 标准库，不需要 `pip install` 任何东西）

## 你需要准备的

1. **Python 3.8+**：到 [python.org](https://www.python.org/downloads/) 下载安装
   （安装时勾选 "Add python.exe to PATH"）
2. **一个 DeepSeek API Key**：到 [platform.deepseek.com](https://platform.deepseek.com) 注册并创建，充值后即可用

## 运行

PowerShell 里执行：

```powershell
# 1. 设置 API Key
$env:DEEPSEEK_API_KEY="sk-你的key"

# 2. 运行（两种方式任选）
python minimal_agent.py "北京天气怎么样"
# 或直接运行，然后输入问题
python minimal_agent.py
```

如果中文显示乱码，先执行：

```powershell
$env:PYTHONIOENCODING="utf-8"
```

可选环境变量：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `DEEPSEEK_BASE_URL` | 换用其他 OpenAI 兼容接口 | `https://api.deepseek.com` |
| `MODEL` | 换模型（`deepseek-chat` 支持工具调用） | `deepseek-chat` |

## 体验一个完整循环

输入「北京天气怎么样」，你会看到：

```text
--- 第 1 轮：调用大模型 ---
  🔧 模型要调用工具：get_weather({'city': '北京'})
  📦 工具返回：晴，25°C
--- 第 2 轮：调用大模型 ---
🤖 北京今天晴，气温 25°C。
```

这就是 Agent Loop 的全部：**调工具 → 结果回传 → 再问模型 → 得到最终回答**。

> 提示：如果模型没有调用工具而是直接回答，通常是提示词没引导它用工具。
> 可以把系统提示改成「必须使用 get_weather 工具查询天气后再回答」试试。

## 加新工具

在 `TOOLS` 列表里加一段描述，再在 `run_tool()` 里加一行，即可让模型使用新工具。

## 下一步可以加什么

- 历史对话持久化（存文件/数据库）
- 危险命令执行前询问用户（审批）
- 上下文太长时压缩
- 多轮对话（保留 messages 跨轮次）
