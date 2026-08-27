# 03 配置不同类型的 Agent

> 场景：你会建智能体了。现在想让不同智能体干不同活——一个写代码、一个做研究、一个守规矩的安全员。本篇讲怎么配置出"性格各异"的智能体。

创建（`agent.create`）时给一次配置，运行中还能用 `agent.configure` 改。配置项分三层：**模型层**（用哪个模型、温度）、**行为层**（系统提示、工具、迭代上限）、**人格层**（灵魂、记忆、知识库、安全等级）。

## 本篇你将完成

- 切换模型 / 调温度 / 设 token 上限
- 配工具集（让智能体会用 file/git/http 等）
- 写系统提示 + 灵魂设定（soul.md）
- 挂知识库（RAG）
- 设安全等级
- 运行中改配置（`agent.configure`）

---

## 准备

复用 [索引页](./README.md#最小-rpc-调用脚手架) 的 `rpc.py`。本篇示例 `from rpc import rpc`。

先建一个待配置的智能体：

```python
from rpc import rpc
import asyncio
async def main():
    r = await rpc("agent.create", {"name": "coder"})
    print(r["agent_id"])
asyncio.run(main())
```

## 模型层：选模型、调温度

模型决定能力上限，温度决定输出稳定性。**写代码 / 抽取结构化数据用低温度（0.1–0.3）；创意 / 对话用高温度（0.7–0.9）。**

```python
from rpc import rpc
import asyncio

AGENT_ID = "..."   # 上一步的 agent_id

async def main():
    r = await rpc("agent.configure", {
        "agent_id": AGENT_ID,
        "config": {
            "model": "qwen2.5-coder-7b-instruct",
            "temperature": 0.2,
            "max_tokens": 8192,
            "top_p": 0.9,
        }
    })
    print(r["configured"], r["manifest"]["model"])

asyncio.run(main())
```

> 模型名必须对得上 fusion-mlx 已加载的模型。`await rpc("mlx.status")` 看当前可用模型列表。

## 行为层：系统提示、工具、迭代上限

### 系统提示

系统提示定义智能体"是谁、怎么说话、先做什么"。写好它，胜过十次调温度。

```python
await rpc("agent.configure", {"agent_id": AGENT_ID, "config": {
    "system_prompt": (
        "你是一名资深 Python 工程师。"
        "接到任务先读相关文件，再动手改代码，改完用 ruff 检查。"
        "回答用中文，代码块带语言标注。"
    ),
}})
```

### 工具集

工具是智能体的"手"。`tools` 是工具名列表，对应内置工具注册表。常见内置工具：

| 工具名 | 作用 |
|--------|------|
| `file_read` / `file_write` / `file_edit` | 读写改文件 |
| `file_grep` | 目录内正则搜索 |
| `terminal` | 执行 shell 命令（受安全网关约束） |
| `http_request` | 发 HTTP 请求 |
| `git` | Git 操作（status/diff/commit/…） |
| `browser` | 浏览器 Web 自动化（见 05 篇前置） |
| `kb_search` / `kb_ask` | 知识库检索 |
| `code_sandbox` | 多语言代码沙箱执行 |
| `memory_store` / `memory_recall` | 记忆读写 |

```python
await rpc("agent.configure", {"agent_id": AGENT_ID, "config": {
    "tools": ["file_read", "file_edit", "file_grep", "terminal", "git"],
}})
```

> 工具名必须存在于内置注册表或已加载插件。未知工具名在 `strict_validate` 下硬拒，默认软警告。`await rpc("tool.list")` 看全部可用工具。

### 迭代上限与上下文窗口

```python
await rpc("agent.configure", {"agent_id": AGENT_ID, "config": {
    "max_iterations": 15,       # agent 循环最多几轮工具调用
    "context_window": 65536,    # 上下文 token 窗口
}})
```

`max_iterations` 防止智能体陷入无限工具调用。`context_window` 大则记得多，但更费 token——按模型实际支持的窗口设。

## 人格层：灵魂、记忆、知识库、安全

### 灵魂设定（soul.md）

`agent.create` / `agent.configure` 可写 `soul`——一段长文本，落到智能体包的 `soul.md`。比 `system_prompt` 更持久、更"价值观"。系统提示是"这次怎么干活"，灵魂是"我是什么人"。

```python
await rpc("agent.create", {
    "name": "reviewer",
    "model": "qwen2.5-7b-instruct",
    "system_prompt": "审查代码改动，指出风险与简化点。",
    "soul": (
        "# 代码审查官\n\n"
        "## 价值观\n"
        "- 正确性高于简洁，简洁高于花哨\n"
        "- 不放行未验证的假设\n"
        "## 语气\n"
        "直接、就事论事，不夸奖不寒暄\n"
    ),
})
```

### 记忆

智能体自带 `memory.md`，跨会话累积。可手动写入，也可让智能体在运行中自动记（`memory_store` 工具 / runtime 自动分类）。记忆分四类：

| memory_type | 用途 |
|-------------|------|
| `user` | 关于用户的长期事实（偏好、角色） |
| `feedback` | 用户给过的纠正 / 指导 |
| `project` | 项目目标、约束、进度 |
| `reference` | 外部资源指针（URL、文档） |

手动存一条：

```python
await rpc("memory.store", {"agent_id": AGENT_ID, "content": "用户偏好中文回答", "memory_type": "user"})
await rpc("memory.recall", {"agent_id": AGENT_ID, "query": "用户偏好"})
```

### 知识库（RAG）

挂知识库让智能体能检索私有文档。先在 fusion-rag 建库（见相关项目文档），拿到 `kb_id`，再绑到智能体：

```python
await rpc("agent.configure", {"agent_id": AGENT_ID, "config": {
    "knowledge_base_ids": ["kb-internal-docs"],
    "rag_strategy": "hybrid",   # hybrid | keyword | semantic
}})
```

绑了知识库且 `tools` 含 `kb_search`/`kb_ask`，智能体就能在回答前检索。

### 安全等级

| 等级 | 行为 |
|------|------|
| `L1` | 自动放行（仅内容检测） |
| `L2` | 危险操作预览（默认） |
| `L3` | 人工审批（写工具、网络操作需确认） |

```python
await rpc("agent.configure", {"agent_id": AGENT_ID, "config": {
    "safety_level": "L3",
}})
```

> 生产环境默认 `L2`（`start.sh` 注入 `FUSION_SAFETY_LEVEL=L2`）。给能改文件 / 跑命令的智能体配 `L2` 或 `L3`；纯问答可 `L1`。`L3` 在无人工通道（headless / cron）下会阻塞——cron 场景用 `L1`/`L2`。

## 运行中改配置

所有上面字段都能用 `agent.configure` 增量改，不用重建智能体：

```python
from rpc import rpc
import asyncio

async def main():
    # 从"只读研究"切换到"能动手改代码"
    r = await rpc("agent.configure", {"agent_id": AGENT_ID, "config": {
        "system_prompt": "你是可以改代码的工程师，改完跑测试。",
        "tools": ["file_read", "file_edit", "file_grep", "terminal"],
        "temperature": 0.3,
        "safety_level": "L2",
    }})
    print(r)

asyncio.run(main())
```

`agent.configure` 参数：`agent_id`（必填）+ `config`（任意子集字段，只改传的）。返回 `{configured: true, manifest: {...最新清单}}`。

## 配置模板：四种典型智能体

直接抄，按需改 `name` / `model`。

**编码助手：**
```python
{"name":"coder","model":"qwen2.5-coder-7b-instruct","temperature":0.2,
 "system_prompt":"你是 Python 工程师，先读后改，改完验证。",
 "tools":["file_read","file_edit","file_grep","terminal","git"],"safety_level":"L2"}
```

**研究员：**
```python
{"name":"researcher","model":"qwen2.5-7b-instruct","temperature":0.4,
 "system_prompt":"你做主题研究，多源检索后给结构化报告。",
 "tools":["http_request","kb_search","kb_ask","file_write"],"safety_level":"L1"}
```

**代码审查官：**
```python
{"name":"reviewer","model":"qwen2.5-7b-instruct","temperature":0.1,
 "system_prompt":"审查改动，指出风险与简化点，直接不寒暄。",
 "tools":["file_read","file_grep","git"],"safety_level":"L1"}
```

**安全守门员：**
```python
{"name":"gatekeeper","model":"qwen2.5-7b-instruct","temperature":0.0,
 "system_prompt":"你审核所有写操作，危险即拦。",
 "tools":["file_read","file_grep"],"safety_level":"L3"}
```

> 配置持久化在 `~/.fusion-agent-studio/agents/<id>/.fusion-agent/manifest.json`，重启不丢。

---

下一篇：[04 给 Agent 安装 Skill](./04-install-skills.md)
