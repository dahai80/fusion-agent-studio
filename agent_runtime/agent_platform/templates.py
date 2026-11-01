"""Seeded agent templates (#437 / E1, §10) — the launch samples.

Port of f-ecommerce agent_platform/templates.py, e-commerce scenarios
generalized to studio-native capabilities (graph/agent listing + workflow
mutation through the approval workbench). Each template is the
customer-facing authoring YAML (the §4 contract), served verbatim on the
Agents page: a customer duplicates one, edits, and publishes. Samples
double as format documentation — the fastest way to teach
say-what-not-how is a good example.

These are AUTHORING documents, not FAS definitions: the editor compiles
them through yaml_compiler at save/create time (model slot + capability
checks + publisher authority all apply at that boundary).
"""

from __future__ import annotations

from typing import Any, Dict, List

_RESEARCH = """name: Deep researcher
description: >-
  Conducts multi-step research with source synthesis and citations.
model:
  id: base
  effort: low
system: |-
  You are a research agent. Given a question or topic:

  1. Decompose it into 3-5 concrete sub-questions that, answered together,
     cover the topic.
  2. For each sub-question, gather the most authoritative sources
     available through your tools (prefer primary sources and official
     docs over blog posts and aggregators).
  3. Read the sources in full - don't skim. Extract specific claims, data
     points, and direct quotes with attribution.
  4. Synthesize a report that answers the original question. Structure it
     by sub-question, cite every non-obvious claim inline, and close with
     a "confidence & gaps" section noting where sources disagreed or where
     coverage was thin.

  Be skeptical. If sources conflict, say so and explain which you find
  more credible and why. Don't paper over uncertainty with
  confident-sounding prose. You are read-only: report findings, never
  propose changes.

tools:
  - agent.list

metadata:
  template: research-agent
"""

_OPS = """name: Ops monitor
description: >-
  Surfaces the state of the workspace: agents, workflows, and pending
  work; drafts workflow changes for approval.
model:
  id: base
  effort: low
system: |-
  You are an ops-monitor agent. State first, interpretation second.

  1. For status questions use agent.list first; report counts by status
     with numbers, not adjectives. Always state the moment you looked.
  2. For change requests (new workflow, rename, restructure): draft the
     exact change and hand it to the approval workbench via
     graph.create - a human decides. Never represent a change as applied
     before it is approved.
  3. Flag anomalies you notice (missing agents, empty fields, drift)
     with specifics.

tools:
  - agent.list
  - graph.create

metadata:
  template: ops-agent
"""


def _pack(key: str, yaml_text: str) -> Dict[str, Any]:
    import yaml

    doc = yaml.safe_load(yaml_text)
    return {
        "key": key,
        "name": doc["name"],
        "description": doc["description"],
        "tools": doc["tools"],
        "tool_count": len(doc["tools"]),
        "yaml": yaml_text,
    }


AGENT_TEMPLATES: List[Dict[str, Any]] = [
    _pack("research", _RESEARCH),
    _pack("ops", _OPS),
]


# #530: conversational starter library — purpose presets that pre-seed the
# builder's first message + a suggested capability set. These are NOT full
# authoring YAML (that is the model's job in the compose turn); they carry
# the ask a user would type and the capabilities the draft should start
# from, cutting the blank-page friction for non-technical users.
STARTER_TEMPLATES: List[Dict[str, Any]] = [
    {
        "key": "reconcile",
        "name": "对账",
        "description": "核对流水与账面差异，输出差异清单",
        "ask": "帮我构建一个对账智能体：核对每日流水与账面记录，"
               "输出差异清单并给出原因初判。",
        "tools": ["agent.list"],
    },
    {
        "key": "weekly-report",
        "name": "周报",
        "description": "汇总一周运行数据，产出结构化周报",
        "ask": "帮我构建一个周报智能体：每周汇总运行数据与异常事件，"
               "产出结构化周报（数据、异常、下周关注）。",
        "tools": ["agent.list"],
    },
    {
        "key": "support",
        "name": "客服",
        "description": "一线问答与工单初判，超权转人工",
        "ask": "帮我构建一个客服智能体：回答常见问题、初判工单类别，"
               "遇到超出权限或情绪激烈的用户必须转人工。",
        "tools": ["agent.list"],
    },
    {
        "key": "researcher",
        "name": "研究员",
        "description": "多步检索与引用综合，产出研究报告",
        "ask": "帮我构建一个研究员智能体：对给定主题做多步检索与交叉"
               "验证，产出带引用的研究报告并标注信心与盲区。",
        "tools": ["agent.list"],
    },
]


def get_template(key: str) -> Dict[str, Any] | None:
    for t in AGENT_TEMPLATES:
        if t["key"] == key:
            return t
    return None
