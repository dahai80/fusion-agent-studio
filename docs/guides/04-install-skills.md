# 04 给 Agent 安装 Skill

> 场景：你的编码助手每次都要"读文件→改代码→跑测试→看结果"，手写系统提示让 LLM 自己编排很容易漏步。Skill 把这套固定流程写死成"技能"，一步到位、可复用、可插拔。

Skill 是一段**结构化的固定操作序列**，存在智能体包里。和系统提示的区别：系统提示是"建议"，LLM 可能不照做；Skill 是"程序"，逐步执行不跑偏。

## 本篇你将完成

- 理解 Skill 结构（system_prompt + steps）
- 写一个"终端命令"技能
- 写一个"终端→生成→终端"三步技能（带 capture_to 传值）
- 用 `agent.add_skill` 装到智能体
- 用 `skill.execute` 跑技能
- 管理技能（列出 / 删除）

---

## 准备

复用 `rpc.py`（见 [索引页](./README.md#最小-rpc-调用脚手架)）。先有个智能体：

```python
from rpc import rpc
import asyncio
async def main():
    r = await rpc("agent.create", {"name": "ops", "model": "qwen2.5-7b-instruct"})
    print(r["agent_id"])
asyncio.run(main())
```

## Skill 结构

一个 Skill 是一个 dict，落盘成 `~/.fusion-agent-studio/agents/<id>/.fusion-agent/skills/<name>.json`：

```json
{
  "system_prompt": "你是运维助手，按步骤执行并报告。",
  "steps": [
    {"name": "查磁盘", "action": "terminal",
     "command": "df -h /", "timeout": 10},
    {"name": "总结", "action": "generate",
     "prompt": "根据上一步磁盘信息，用一句话判断空间是否紧张。"}
  ]
}
```

两个顶层键：
- `system_prompt`（或别名 `systemPrompt`）：覆盖该技能执行时的系统提示
- `steps`：步骤数组，按序执行

### 两种 step action

| action | 作用 | 关键字段 |
|--------|------|----------|
| `terminal` | 执行 shell 命令 | `command`、`timeout`（秒）、`cwd`、`workdir`、`capture_to`、`name` |
| `generate` | 调 LLM 生成文本（默认 action） | `prompt`、`name`、`capture_to` |

### capture_to：步骤间传值

每个 step 可设 `capture_to: "变量名"`，把本步输出存进 `captures` 字典。**后续步骤的 `command` / `prompt` 里用 `{变量名}` 插值**引用它。这是技能能串成流水线的关键。

> 三个方向都能插值：`terminal` 步骤的 `command` 里可插前面捕获的值；`generate` 步骤的 `prompt` 里可插；`terminal → terminal` 之间也可插。

## 写技能并安装

### 技能一：单步终端（查磁盘）

```python
from rpc import rpc
import asyncio

AGENT_ID = "..."

SKILL_DISK = {
    "system_prompt": "你是运维助手，执行命令并报告结果。",
    "steps": [
        {"name": "df", "action": "terminal",
         "command": "df -h /", "timeout": 10, "capture_to": "disk_info"},
        {"name": "report", "action": "generate",
         "prompt": "根据磁盘信息：{disk_info}，判断 / 分区是否需要清理，给一句话结论。"}
    ]
}

async def main():
    r = await rpc("agent.add_skill", {
        "agent_id": AGENT_ID,
        "skill_name": "check_disk",
        "skill_def": SKILL_DISK,
    })
    print(r)

asyncio.run(main())
```

`agent.add_skill` 参数：`agent_id`、`skill_name`（技能文件名，字母数字下划线）、`skill_def`（上面那个 dict）。成功后文件落盘，返回确认。

### 技能二：三步流水线（git 状态→分析→提交）

体现 `capture_to` 串联：

```python
SKILL_AUTOCOMMIT = {
    "system_prompt": "你是提交助手，查看改动、生成提交信息、提交。",
    "steps": [
        {"name": "diff", "action": "terminal",
         "command": "git diff --stat", "cwd": "/Users/dahai/fusion/fusion-agent-studio",
         "capture_to": "diff_stat"},
        {"name": "compose", "action": "generate",
         "prompt": "根据改动统计：{diff_stat}，写一行简洁的中文提交信息（祈使句）。",
         "capture_to": "commit_msg"},
        {"name": "commit", "action": "terminal",
         "command": "git commit -m \"{commit_msg}\"",
         "cwd": "/Users/dahai/fusion/fusion-agent-studio",
         "name": "do_commit"}
    ]
}

await rpc("agent.add_skill", {
    "agent_id": AGENT_ID,
    "skill_name": "autocommit",
    "skill_def": SKILL_AUTOCOMMIT,
})
```

流程：`git diff --stat` 输出存进 `diff_stat` → LLM 据此生成提交信息存进 `commit_msg` → `git commit -m` 用 `{commit_msg}` 插值提交。

> 注意：`terminal` 步骤执行真实 shell，受安全网关约束（写/网络操作按 `safety_level` 预览或审批）。`command` 里的引号要正确转义。

## 执行技能

两种方式：直接 RPC 跑，或让智能体在对话中自己调。

### 直接 RPC

```python
from rpc import rpc
import asyncio

async def main():
    r = await rpc("skill.execute", {
        "agent_id": AGENT_ID,
        "skill_name": "check_disk",
        "input": "检查磁盘",
    })
    print("status:", r.get("status"))
    for step in r.get("steps", []):
        print(f"  [{step['name']}] {str(step.get('output',''))[:80]}")

asyncio.run(main())
```

`skill.execute` 参数：`agent_id`、`skill_name`、`input`（可选，传给技能的初始输入）。返回逐步结果。

### 对话中触发

智能体的 `tools` 里若含技能执行能力，LLM 会在合适时机调用已装的技能。日常更常用的是上面这种直接 RPC——把技能当定时任务 / 脚本入口（见 06 篇把 `skill.execute` 挂 cron）。

## 管理技能

```python
await rpc("agent.list_skills", {"agent_id": AGENT_ID})      # 列出已装技能
await rpc("agent.delete_skill", {"agent_id": AGENT_ID, "skill_name": "check_disk"})
```

技能是**每智能体独立**的，装在 A 上的技能 B 看不到。要跨智能体复用，把 `skill_def` 存成文件，给每个智能体 `add_skill`。

## 技能 vs 工具 vs 系统提示，何时用哪个

| | 系统提示 | 工具 | 技能 |
|---|---|---|---|
| 控制度 | 弱（建议） | 中（单原子操作） | 强（固定流程） |
| 灵活性 | 高（LLM 自由发挥） | 中 | 低（按步执行） |
| 适合 | 开放对话、风格 | 读文件、发请求等原子能力 | 固定多步流水线（部署、巡检、提交流水线） |

经验：**先靠系统提示 + 工具跑通；发现某套流程总重复且总漏步，就提成技能。**

---

下一篇：[05 Agent 调用 MCP 服务](./05-use-mcp.md)
