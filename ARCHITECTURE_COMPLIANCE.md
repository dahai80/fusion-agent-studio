# 架构合规整改计划

> 审计日期: 2026-08-02
> 关联 Issue: #54
> 违规等级: P0（架构性违规，必须立即整改）
> 合规评级: D

## 层级定位

**二、核心网关引擎** — 可视化智能体工作流编排内核

核心职责：Agent 工作流的可视化编排引擎，仅此一项。

## 违规项与整改

| # | 违规项 | 整改方案 | 目标去向 | 截止 |
|---|--------|----------|----------|------|
| 1 | agent_marketplace.py + agent_version.py | 市场系统整体提取 | 独立注册表服务 | P0-S1 |
| 2 | deployer.py + exporter.py | 部署/导出系统提取 | 独立部署工具 | P0-S1 |
| 3 | knowledge_engine.py + rag_pipeline.py + data_ingestion.py | RAG 子系统移除，改用 fusion-kb 客户端 | fusion-kb | P0-S1 |
| 4 | agent_package.py + agent_definition.py | 代理打包提取 | 独立代理管理产品 | P0-S1 |
| 5 | chat_engine.py | 聊天引擎提取 | 前端/聊天产品 | P0-S2 |
| 6 | style_manager.py | 样式管理提取 | 前端 | P0-S2 |
| 7 | i18n.py | 国际化提取 | 前端 | P0-S2 |
| 8 | graph_editor.py | 图编辑器提取 | 编辑器/IDE | P0-S2 |
| 9 | tools/computer_use_tools.py | 桌面自动化提取 | 平台插件 | P0-S2 |
| 10 | DaemonServer 155+ RPC | 精简至约 40 个核心编排 RPC | - | P0-S3 |

## 整改阶段

### P0-S1（立即执行）
- [ ] 市场系统提取
- [ ] 部署/导出系统提取
- [ ] RAG 子系统移除，接入 fusion-kb
- [ ] 代理打包提取

### P0-S2（S1 完成后）
- [ ] 聊天引擎/样式/国际化/图编辑器提取
- [ ] 桌面自动化提取为插件

### P0-S3（S2 完成后）
- [ ] DaemonServer 精简

## 合规标准

整改完成后，DaemonServer 应只包含约 40 个核心编排 RPC：
- 工作流 CRUD
- 节点执行/调度
- 变量/上下文管理
- 事件/状态通知
- 外部服务客户端（fusion-kb、fusion-gateway 等）

不应包含：市场、部署、RAG、聊天、样式、国际化、图编辑等能力。
