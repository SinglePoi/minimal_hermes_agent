# 最简 Agent 骨架（第二步）· DeepSeek 版

一个 Agent Loop 最小实现，现已升级到**第二步**：加入系统提示词与简单记忆。
`minimal_agent.py`

## 新增功能

1. **系统提示词**：角色/规则集中在代码的 `SYSTEM_PROMPT` 常量里；
   也可在项目目录放一个 `SYSTEM_PROMPT.md` 直接覆盖（不改代码改人设）
2. **简单记忆**：
   - 对话结束后，让模型提取"值得长期记住的用户信息"，存入 `memory.json`
   - 下次运行自动加载并注入系统提示词，实现跨会话"记得你"

## 你需要准备的

1. **Python 3.8+**：到 [python.org](https://www.python.org/downloads/) 下载安装
   （安装时勾选 "Add python.exe to PATH"）
2. **安装依赖**：

   ```powershell
   pip install -r requirements.txt
   ```

3. **一个 DeepSeek API Key**：到 [platform.deepseek.com](https://platform.deepseek.com) 注册并创建，充值后即可用

> 也可以在项目根目录创建 `.env` 文件：
>
> ```text
> DEEPSEEK_API_KEY=你的key
> ```

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

## 体验记忆（学习闭环）

```powershell
# 第一次：告诉它你的信息（对话结束后自动保存）
python minimal_agent.py "我叫小明，我喜欢喝美式咖啡"

# 第二次：换个问题，它能"想起"你
python minimal_agent.py "我是谁？我喜欢喝什么咖啡？"
```

打开 `memory.json` 可以看到记住的内容。这就是 Hermes "学习闭环"的最小原型：
**对话 → 提取值得记住的信息 → 持久化 → 下次注入上下文**。

## 加新工具

在 `TOOLS` 列表里加一段描述，再在 `run_tool()` 里加一行，即可让模型使用新工具。

## 下一步可以加什么

- 历史对话持久化（存文件/数据库）
- 危险命令执行前询问用户（审批）
- 上下文太长时压缩
- 多轮对话（保留 messages 跨轮次）
