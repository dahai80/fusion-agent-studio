"""Sub-dispatcher: AgentDispatcher."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable

from ..daemon_server import MLX_PORT
from ..graph import AgentGraph, NodeConfig
from .base import SubDispatcher

logger = logging.getLogger(__name__)


class AgentDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "agent.create": self._handle_agent_create,
            "agent.get": self._handle_agent_get,
            "agent.list": self._handle_agent_list,
            "agent.update": self._handle_agent_update,
            "agent.delete": self._handle_agent_delete,
            "agent.configure": self._handle_agent_configure,
            "agent.execute": self._handle_agent_execute,
            "agent.test": self._handle_agent_execute,
            "agent.list_skills": self._handle_agent_list_skills,
            "agent.add_skill": self._handle_agent_add_skill,
            "agent.delete_skill": self._handle_agent_delete_skill,
            "skill.execute": self._handle_skill_execute,
            "research.adaptive": self._handle_research_adaptive,
            "agent.get_soul": self._handle_agent_get_soul,
            "agent.update_soul": self._handle_agent_update_soul,
            "agent.submit_code_task": self._handle_agent_submit_code_task,
            "agent.task_status": self._handle_agent_task_status,
            "agent.cancel_task": self._handle_agent_cancel_task,
            "agent.tasks": self._handle_agent_tasks,
            "agent.publish": self._handle_agent_publish,
            "agent.archive": self._handle_agent_archive,
            "agent.unpublish": self._handle_agent_unpublish,
            "agent.clone": self._handle_agent_clone,
            "agent.get_api_endpoint": self._handle_agent_get_api_endpoint,
            "agent.execute_stream": self._handle_agent_execute_stream,
            "agent.preview": self._handle_agent_preview,
            "agent.test_with_project": self._handle_agent_test_with_project,
            "agent.published_list": self._handle_agent_published_list,
            "agent.get_definition": self._handle_agent_get_definition,
            "agent.status": self._handle_agent_status,
            "agent.history": self._handle_agent_history,
            "agent.cowork.list": self._handle_agent_cowork_list,
            "agent.cowork.add": self._handle_agent_cowork_add,
            "agent.cowork.remove": self._handle_agent_cowork_remove,
            "agent.cowork.call": self._handle_agent_cowork_call,
            "agent.cowork.status": self._handle_agent_cowork_status,
            "agent.context_inject": self._handle_agent_context_inject,
            "agent.diff_review": self._handle_agent_diff_review,
            "permission.list": self._handle_permission_list,
            "permission.update": self._handle_permission_update,
        }

    async def _handle_agent_create(self, params: dict) -> dict:
        import uuid

        name = params.get("name", "")
        if not name:
            return {"status": "error", "message": "name parameter required"}

        self._daemon._load_agents_index()
        agent_id = params.get("id", uuid.uuid4().hex[:12])

        from ..agent_package import AgentManifest, AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        manifest = AgentManifest(
            name=name,
            model=params.get("model", ""),
            system_prompt=params.get("system_prompt", f"You are {name}."),
            temperature=params.get("temperature", 0.7),
            max_tokens=params.get("max_tokens", 4096),
            tools=params.get("tools", []),
            capabilities=params.get("capabilities", []),
            safety_level=params.get("safety_level", "L1"),
            tags=params.get("tags", []),
            author=params.get("author", ""),
            description=params.get("description", ""),
            status=params.get("status", "draft"),
            version_int=params.get("version_int", 1),
            published_at=params.get("published_at"),
            knowledge_base_ids=params.get("knowledge_base_ids", []),
            visibility=params.get("visibility", "private"),
            rag_strategy=params.get("rag_strategy", "hybrid"),
            web_search_enabled=params.get("web_search_enabled", False),
            deep_research_enabled=params.get("deep_research_enabled", False),
            connector_ids=params.get("connector_ids", []),
            style=params.get("style", ""),
            top_p=params.get("top_p", 1.0),
            context_window=params.get("context_window", 32768),
            rate_limit_qps=params.get("rate_limit_qps", 0),
        )
        pkg = AgentPackage(agent_dir)
        pkg.init(
            manifest=manifest,
            soul=params.get("soul", ""),
            memory=params.get("memory", ""),
            agents_md=params.get("agents_md", ""),
        )

        self._daemon._agents[agent_id] = manifest.to_dict()
        self._daemon._agents[agent_id]["id"] = agent_id
        self._daemon._agents[agent_id]["created_at"] = time.time()
        self._daemon._persist_agents_index()

        logger.info("agent.create: id=%s name=%s", agent_id, name)
        return {"agent_id": agent_id, "manifest": manifest.to_dict()}

    async def _handle_agent_get(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        manifest = pkg.load_manifest()
        result = manifest.to_dict()
        result["id"] = agent_id
        result["skills"] = pkg.list_skills()
        result["has_soul"] = bool(pkg.load_soul().strip())
        return {"agent": result}

    async def _handle_agent_list(self, params: dict) -> dict:
        self._daemon._load_agents_index()
        tag_filter = params.get("tags", [])
        capability_filter = params.get("capabilities", [])
        usable_in_project = params.get("usableInProject", False)
        has_rag_support = params.get("hasRagSupport", False)

        results = []
        for aid, meta in self._daemon._agents.items():
            if tag_filter:
                agent_tags = meta.get("tags", [])
                if not any(t in agent_tags for t in tag_filter):
                    continue
            if capability_filter:
                agent_caps = meta.get("capabilities", [])
                if not any(c in agent_caps for c in capability_filter):
                    continue
            if usable_in_project:
                status = meta.get("status", "")
                visibility = meta.get("visibility", "private")
                if status not in ("published", "active") and visibility != "public":
                    continue
            if has_rag_support:
                kb_ids = meta.get("knowledge_base_ids", [])
                rag_strategy = meta.get("rag_strategy", "")
                if not kb_ids and rag_strategy in ("none", ""):
                    continue
            entry = dict(meta)
            entry["id"] = aid
            results.append(entry)

        return {"agents": results}

    async def _handle_agent_update(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        manifest = pkg.load_manifest()
        for key in (
            "name",
            "model",
            "system_prompt",
            "temperature",
            "max_tokens",
            "safety_level",
            "description",
            "author",
            "version",
            "status",
            "version_int",
            "published_at",
            "knowledge_base_ids",
            "visibility",
            "rag_strategy",
            "web_search_enabled",
            "deep_research_enabled",
            "connector_ids",
            "style",
            "top_p",
            "context_window",
            "rate_limit_qps",
        ):
            if key in params:
                setattr(manifest, key, params[key])
        if "tools" in params:
            manifest.tools = params["tools"]
        if "capabilities" in params:
            manifest.capabilities = params["capabilities"]
        if "tags" in params:
            manifest.tags = params["tags"]

        pkg.save_manifest(manifest)

        self._daemon._load_agents_index()
        if agent_id in self._daemon._agents:
            self._daemon._agents[agent_id].update(manifest.to_dict())
            self._daemon._persist_agents_index()

        logger.info("agent.update: id=%s", agent_id)
        return {"updated": True, "manifest": manifest.to_dict()}

    async def _handle_agent_delete(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if pkg.exists:
            pkg.destroy()

        self._daemon._load_agents_index()
        removed = self._daemon._agents.pop(agent_id, None)
        if removed is not None:
            self._daemon._persist_agents_index()

        logger.info(
            "agent.delete: id=%s existed=%s",
            agent_id,
            pkg.exists or removed is not None,
        )
        return {"deleted": True}

    async def _handle_agent_configure(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        config = params.get("config", {})
        if not config:
            return {"status": "error", "message": "config parameter required"}

        manifest = pkg.load_manifest()
        if "model" in config:
            manifest.model = config["model"]
        if "temperature" in config:
            manifest.temperature = config["temperature"]
        if "max_tokens" in config:
            manifest.max_tokens = config["max_tokens"]
        if "system_prompt" in config:
            manifest.system_prompt = config["system_prompt"]
        if "tools" in config:
            manifest.tools = config["tools"]
        if "capabilities" in config:
            manifest.capabilities = config["capabilities"]
        if "safety_level" in config:
            manifest.safety_level = config["safety_level"]

        pkg.save_manifest(manifest)

        self._daemon._load_agents_index()
        if agent_id in self._daemon._agents:
            self._daemon._agents[agent_id].update(manifest.to_dict())
            self._daemon._persist_agents_index()

        logger.info("agent.configure: id=%s keys=%s", agent_id, list(config.keys()))
        return {"configured": True, "manifest": manifest.to_dict()}

    async def _handle_agent_execute(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        input_text = params.get("input", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        graph_config = pkg.to_graph_config()
        manifest = pkg.load_manifest()

        effective_prompt = graph_config.get("system_prompt", manifest.system_prompt)

        if manifest.knowledge_base_ids:
            kb_context = await self._daemon._inject_knowledge_context(
                manifest.knowledge_base_ids, input_text, manifest.rag_strategy
            )
            if kb_context:
                effective_prompt = f"{effective_prompt}\n\n{kb_context}"

        if manifest.style:
            style_mgr = self._daemon._get_style_manager()
            style_result = style_mgr.apply(effective_prompt, manifest.style)
            if "system_prompt" in style_result:
                effective_prompt = style_result["system_prompt"]

        graph = AgentGraph(name=manifest.name or agent_id)
        graph.description = manifest.description

        start_id = "start"
        llm_id = "llm-1"
        start_node = NodeConfig(type="start", label="Start")
        llm_node = NodeConfig(
            type="llm",
            label="Agent LLM",
            model=manifest.model,
            system_prompt=effective_prompt,
        )
        graph.add_node(start_id, start_node)
        graph.add_node(llm_id, llm_node)
        graph.add_edge(start_id, llm_id)

        for i, tool_name in enumerate(manifest.tools):
            tool_id = f"tool-{i + 1}"
            tool_node = NodeConfig(type="tool", label=tool_name)
            graph.add_node(tool_id, tool_node)
            if i == 0:
                graph.add_edge(llm_id, tool_id)
            else:
                graph.add_edge(f"tool-{i}", tool_id)

        self._daemon.store.save_graph(graph)
        rt = self._daemon._get_runtime()

        events = []
        try:
            async for event in rt.execute_graph(graph, input_text):
                ev_dict = event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
                events.append(ev_dict)
        except Exception as e:
            logger.warning("agent.execute runtime error: %s", e)
            return {
                "agent_id": agent_id,
                "events": events,
                "status": "error",
                "message": str(e),
                "session_id": f"sess-{int(time.time())}",
            }

        logger.info("agent.execute: id=%s events=%d", agent_id, len(events))
        return {
            "agent_id": agent_id,
            "events": events,
            "status": "completed",
            "session_id": f"sess-{int(time.time())}",
        }

    async def _handle_agent_list_skills(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        skills = pkg.list_skills()
        return {"skills": skills}

    async def _handle_agent_add_skill(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        skill_name = params.get("skill_name", "")
        if not agent_id or not skill_name:
            return {"status": "error", "message": "agent_id and skill_name required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        skill_def = params.get("skill_def", {})
        pkg.save_skill(skill_name, skill_def)
        logger.info("agent.add_skill: agent=%s skill=%s", agent_id, skill_name)
        return {"added": True, "skill_name": skill_name}

    async def _handle_agent_delete_skill(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        skill_name = params.get("skill_name", "")
        if not agent_id or not skill_name:
            return {"status": "error", "message": "agent_id and skill_name required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        deleted = pkg.delete_skill(skill_name)
        return {"deleted": deleted}

    async def _handle_skill_execute(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        skill_name = params.get("skill_name", "")
        user_input = params.get("input", "")
        _tool_names = params.get("tools", [])
        if not agent_id or not skill_name:
            return {"status": "error", "message": "agent_id and skill_name required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        if skill_name not in pkg.list_skills():
            return {"status": "error", "message": f"Skill not found: {skill_name}"}
        skill_def = pkg.load_skill(skill_name)
        if not skill_def:
            return {"status": "error", "message": f"Skill not found: {skill_name}"}

        system_prompt = skill_def.get("system_prompt", skill_def.get("systemPrompt", ""))
        steps = skill_def.get("steps", [])
        results = []
        chat_engine = self._daemon._get_chat_engine()
        session = chat_engine.create_session(
            mode="simple",
            title=f"skill-{skill_name}",
            metadata={"agent_id": agent_id, "skill_name": skill_name},
        )
        session_id = session.id

        if steps:
            step_results = []
            captures = {}
            for i, step in enumerate(steps):
                step_prompt = step.get("prompt", "")
                action = step.get("action", "generate")
                if action == "terminal":
                    command = step.get("command", "")
                    for cname, cval in captures.items():
                        command = command.replace("{" + cname + "}", cval)
                    if not command:
                        results.append(
                            {
                                "step": i + 1,
                                "name": step.get("name", f"Step {i + 1}"),
                                "action": action,
                                "status": "error",
                                "error": "terminal step missing command",
                            }
                        )
                        step_results.append("Error: terminal step missing command")
                        break
                    try:
                        registry = self._daemon._get_tool_registry()
                        tool = registry.get("terminal")
                        tool_out = await tool.execute(
                            command=command,
                            timeout=int(step.get("timeout", 30)),
                            workdir=step.get("cwd", step.get("workdir", "")),
                        )
                        capture = step.get("capture_to", "")
                        if capture:
                            captures[capture] = str(tool_out)
                        step_results.append(str(tool_out)[:4000])
                        results.append(
                            {
                                "step": i + 1,
                                "name": step.get("name", f"Step {i + 1}"),
                                "action": action,
                                "status": "completed",
                                "output": tool_out,
                                "output_length": len(str(tool_out)),
                                "capture_to": capture,
                            }
                        )
                        logger.info(
                            "skill.execute: terminal step %d/%d completed, %d chars",
                            i + 1,
                            len(steps),
                            len(str(tool_out)),
                        )
                    except Exception as e:
                        step_results.append(f"Error: {e}")
                        results.append(
                            {
                                "step": i + 1,
                                "name": step.get("name", f"Step {i + 1}"),
                                "action": action,
                                "status": "error",
                                "error": str(e),
                            }
                        )
                        break
                    continue
                step_input = (
                    step_prompt.replace("{input}", user_input) if step_prompt else user_input
                )
                for cname, cval in captures.items():
                    step_input = step_input.replace("{" + cname + "}", cval)
                if step_results:
                    step_input += "\n\nPrevious step results:\n" + "\n".join(
                        f"[Step {j + 1}]: {r}" for j, r in enumerate(step_results)
                    )

                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": step_input})

                try:
                    response_text = ""
                    async for ev in chat_engine.send(session_id, step_input, mode="skill"):
                        if ev.type.value == "token":
                            response_text += ev.content
                    step_results.append(response_text[:4000])
                    gen_capture = step.get("capture_to", "")
                    if gen_capture:
                        captures[gen_capture] = response_text
                    results.append(
                        {
                            "step": i + 1,
                            "name": step.get("name", f"Step {i + 1}"),
                            "action": action,
                            "status": "completed",
                            "output": response_text,
                            "output_length": len(response_text),
                            "capture_to": gen_capture,
                        }
                    )
                    logger.info(
                        "skill.execute: step %d/%d completed, %d chars",
                        i + 1,
                        len(steps),
                        len(response_text),
                    )
                except Exception as e:
                    step_results.append(f"Error: {e}")
                    results.append(
                        {
                            "step": i + 1,
                            "name": step.get("name", f"Step {i + 1}"),
                            "action": action,
                            "status": "error",
                            "error": str(e),
                        }
                    )
                    break

            final_result = step_results[-1] if step_results else ""
        else:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_input})

            try:
                final_result = ""
                async for ev in chat_engine.send(session_id, user_input, mode="skill"):
                    if ev.type.value == "token":
                        final_result += ev.content
                results.append(
                    {
                        "step": 1,
                        "name": skill_name,
                        "action": "generate",
                        "status": "completed",
                        "output": final_result,
                        "output_length": len(final_result),
                    }
                )
            except Exception as e:
                final_result = f"Error: {e}"
                results.append(
                    {
                        "step": 1,
                        "name": skill_name,
                        "action": "generate",
                        "status": "error",
                        "error": str(e),
                    }
                )

        logger.info(
            "skill.execute: agent=%s skill=%s steps=%d",
            agent_id,
            skill_name,
            len(results),
        )
        return {"steps": results, "result": final_result, "skill_name": skill_name}

    async def _handle_research_adaptive(self, params: dict) -> dict:
        question = params.get("question", "")
        max_steps = min(params.get("max_steps", 10), 20)
        _web_search = params.get("web_search", True)
        if not question:
            return {"status": "error", "message": "question parameter required"}

        chat_engine = self._daemon._get_chat_engine()
        session_id = f"research-adaptive-{id(params):012x}"
        findings = []
        citations = []
        steps_taken = 0

        decompose_prompt = (
            f"Break down the following question into 2-4 key sub-questions that need to be researched. "
            f"Output each sub-question on a separate line, prefixed with '## Sub-question N:'.\n\n"
            f"Question: {question}"
        )
        try:
            decomp_text = ""
            async for ev in chat_engine.send(session_id, decompose_prompt, mode="research"):
                if ev.type.value == "token":
                    decomp_text += ev.content
            steps_taken += 1
            import re

            sub_questions = re.findall(r"## Sub-question \d+:\s*(.+)", decomp_text)
            if not sub_questions:
                sub_questions = [
                    line.strip()
                    for line in decomp_text.split("\n")
                    if line.strip() and not line.startswith("#")
                ][:4]
            if not sub_questions:
                sub_questions = [question]
            findings.append(
                {
                    "step": "decompose",
                    "sub_questions": sub_questions,
                    "raw": decomp_text[:2000],
                }
            )
            logger.info(
                "research.adaptive: decomposed into %d sub-questions",
                len(sub_questions),
            )
        except Exception as e:
            findings.append({"step": "decompose", "error": str(e)})
            sub_questions = [question]

        for sq in sub_questions:
            if steps_taken >= max_steps:
                break
            search_prompt = (
                f"Research this sub-question thoroughly. Provide specific facts, data, and cite sources.\n\n"
                f"Sub-question: {sq}\n\n"
                f"Original question: {question}"
            )
            try:
                search_text = ""
                async for ev in chat_engine.send(session_id, search_prompt, mode="research"):
                    if ev.type.value == "token":
                        search_text += ev.content
                steps_taken += 1
                findings.append(
                    {"step": "search", "sub_question": sq, "result": search_text[:4000]}
                )
                url_pattern = re.findall(r'https?://[^\s)\]<>"]+', search_text)
                for url in url_pattern[:3]:
                    citations.append({"url": url, "context": sq})
            except Exception as e:
                findings.append({"step": "search", "sub_question": sq, "error": str(e)})

        sufficient = False
        sufficiency_prompt = (
            f"Given the following research findings, determine if they sufficiently answer the original question. "
            f"Respond with ONLY 'SUFFICIENT' or 'INSUFFICIENT' followed by a brief reason.\n\n"
            f"Original question: {question}\n\n"
            f"Findings so far:\n"
            + "\n".join(
                f"- {f.get('sub_question', f.get('step', ''))}: {f.get('result', f.get('raw', ''))[:500]}"
                for f in findings
                if "error" not in f
            )
        )
        try:
            suff_text = ""
            async for ev in chat_engine.send(session_id, sufficiency_prompt, mode="research"):
                if ev.type.value == "token":
                    suff_text += ev.content
            sufficient = "SUFFICIENT" in suff_text.upper()
            steps_taken += 1
        except Exception:
            sufficient = True

        if not sufficient and steps_taken < max_steps:
            extra_prompt = (
                f"The previous research was deemed insufficient. Provide additional information "
                f"to fully answer the question.\n\n"
                f"Original question: {question}\n\n"
                f"What's missing or needs more depth?"
            )
            try:
                extra_text = ""
                async for ev in chat_engine.send(session_id, extra_prompt, mode="research"):
                    if ev.type.value == "token":
                        extra_text += ev.content
                steps_taken += 1
                findings.append({"step": "supplement", "result": extra_text[:4000]})
            except Exception as e:
                findings.append({"step": "supplement", "error": str(e)})

        synthesize_prompt = (
            f"Synthesize all the research findings into a comprehensive, well-structured response. "
            f"Include specific facts and cite sources where possible.\n\n"
            f"Original question: {question}\n\n"
            f"Research findings:\n"
            + "\n\n".join(
                f"[{f.get('step', 'step')}] {f.get('sub_question', '')}\n{f.get('result', f.get('raw', ''))[:2000]}"
                for f in findings
                if "error" not in f
            )
        )
        try:
            final_answer = ""
            async for ev in chat_engine.send(session_id, synthesize_prompt, mode="research"):
                if ev.type.value == "token":
                    final_answer += ev.content
            steps_taken += 1
        except Exception as e:
            final_answer = f"Synthesis error: {e}"

        logger.info(
            "research.adaptive: question=%s steps=%d sufficient=%s citations=%d",
            question[:50],
            steps_taken,
            sufficient,
            len(citations),
        )
        return {
            "answer": final_answer,
            "citations": citations,
            "steps_taken": steps_taken,
            "sufficient": sufficient,
            "findings": findings,
        }

    async def _handle_agent_get_soul(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        return {"soul": pkg.load_soul()}

    async def _handle_agent_update_soul(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        soul = params.get("soul", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}

        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        pkg.save_soul(soul)
        logger.info("agent.update_soul: agent=%s len=%d", agent_id, len(soul))
        return {"updated": True}

    # ── Agent task routing handlers ──

    async def _handle_agent_submit_code_task(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        code = params.get("code", "")
        language = params.get("language", "python")
        timeout = params.get("timeout", 60)
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        if not code:
            return {"status": "error", "message": "code parameter required"}

        import uuid

        task_id = params.get("task_id") or str(uuid.uuid4())
        task = {
            "task_id": task_id,
            "agent_id": agent_id,
            "code": code,
            "language": language,
            "timeout": timeout,
            "status": "pending",
            "result": None,
            "error": None,
            "created_at": __import__("time").time(),
        }
        self._daemon._code_tasks[task_id] = task
        logger.info("agent.submit_code_task: task=%s agent=%s", task_id, agent_id)

        try:
            task["status"] = "running"
            import asyncio

            async def _run():
                try:
                    result = await self._daemon._execute_code_task(task)
                    task["status"] = "completed"
                    task["result"] = result
                except asyncio.CancelledError:
                    task["status"] = "cancelled"
                except Exception as exc:
                    task["status"] = "failed"
                    task["error"] = str(exc)
                    logger.error("agent task %s failed: %s", task_id, exc)

            handle = asyncio.ensure_future(_run())
            task["_handle"] = handle
            await asyncio.sleep(0)
        except Exception as exc:
            task["status"] = "failed"
            task["error"] = str(exc)
            logger.error("agent.submit_code_task: task=%s error=%s", task_id, exc)

        return {
            "task_id": task_id,
            "status": task["status"],
        }

    async def _execute_code_task(self, task: dict):
        _agent_id = task["agent_id"]
        code = task["code"]
        language = task["language"]
        timeout = task.get("timeout", 60)
        logger.info(
            "_execute_code_task: task=%s lang=%s timeout=%s",
            task["task_id"],
            language,
            timeout,
        )
        if language != "python":
            return {"output": f"Unsupported language: {language}", "exit_code": 1}

        try:
            from ..code_sandbox import CodeSandbox

            sandbox = CodeSandbox(timeout=timeout, use_sandbox=True)
            result = await asyncio.to_thread(sandbox.execute, code, language)
            output = result.stdout
            if result.stderr:
                output = (output + "\n" + result.stderr) if output else result.stderr
            if result.timed_out:
                output = (output + "\nExecution timed out") if output else "Execution timed out"
            logger.info(
                "_execute_code_task done: task=%s exit=%s success=%s exec_id=%s",
                task["task_id"],
                result.exit_code,
                result.success,
                result.execution_id,
            )
            return {"output": output, "exit_code": result.exit_code}
        except Exception as exc:
            logger.error("_execute_code_task error: task=%s error=%s", task["task_id"], exc)
            return {"output": str(exc), "exit_code": 1}

    async def _handle_agent_task_status(self, params: dict) -> dict:
        task_id = params.get("task_id", "")
        if not task_id:
            return {"status": "error", "message": "task_id parameter required"}
        task = self._daemon._code_tasks.get(task_id)
        if not task:
            return {"status": "error", "message": f"Task not found: {task_id}"}
        return {
            "task_id": task_id,
            "status": task["status"],
            "result": task.get("result"),
            "error": task.get("error"),
        }

    async def _handle_agent_cancel_task(self, params: dict) -> dict:
        task_id = params.get("task_id", "")
        if not task_id:
            return {"status": "error", "message": "task_id parameter required"}
        task = self._daemon._code_tasks.get(task_id)
        if not task:
            return {"status": "error", "message": f"Task not found: {task_id}"}
        handle = task.get("_handle")
        if handle and not handle.done():
            handle.cancel()
            task["status"] = "cancelled"
            logger.info("agent.cancel_task: task=%s cancelled", task_id)
        return {"task_id": task_id, "status": task["status"]}

    async def _handle_agent_tasks(self, params: dict) -> dict:
        agent_id = params.get("agent_id")
        status_filter = params.get("status")
        tasks = list(self._daemon._code_tasks.values())
        if agent_id:
            tasks = [t for t in tasks if t["agent_id"] == agent_id]
        if status_filter:
            tasks = [t for t in tasks if t["status"] == status_filter]
        items = []
        for t in tasks:
            items.append(
                {
                    "task_id": t["task_id"],
                    "agent_id": t["agent_id"],
                    "status": t["status"],
                    "language": t["language"],
                    "created_at": t["created_at"],
                    "error": t.get("error"),
                }
            )
        return {"tasks": items}

    async def _inject_knowledge_context(
        self, knowledge_base_ids: list[str], query: str, strategy: str = "hybrid"
    ) -> str:
        if not knowledge_base_ids or not query:
            return ""
        try:
            from ..knowledge_engine import KnowledgeEngine

            ke = KnowledgeEngine()
            all_contexts = []
            for kb_id in knowledge_base_ids:
                results = ke.search(query, mode=strategy, scope=kb_id)
                for r in results[:5]:
                    content = r.get("content", "") if isinstance(r, dict) else str(r)
                    if content:
                        all_contexts.append(content)
            if all_contexts:
                return f"[Knowledge Base Context]\n{'—'.join(all_contexts[:10])}\n[/Knowledge Base Context]"
        except Exception as exc:
            logger.warning("Knowledge injection failed: %s", exc)
        return ""

    async def _handle_agent_publish(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = pkg.load_manifest()
        if manifest.status == "published":
            return {"status": "error", "message": "Agent already published"}
        manifest.status = "published"
        manifest.version_int = manifest.version_int + 1
        manifest.published_at = time.time()
        pkg.save_manifest(manifest)
        self._daemon._load_agents_index()
        if agent_id in self._daemon._agents:
            self._daemon._agents[agent_id].update(manifest.to_dict())
            self._daemon._persist_agents_index()
        endpoint = f"http://localhost:{MLX_PORT}/v1/agents/{agent_id}/chat"
        logger.info("agent.publish: id=%s version=%d", agent_id, manifest.version_int)
        return {
            "agent_id": agent_id,
            "status": "published",
            "version": manifest.version_int,
            "published_at": manifest.published_at,
            "api_endpoint": endpoint,
        }

    async def _handle_agent_archive(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = pkg.load_manifest()
        manifest.status = "archived"
        pkg.save_manifest(manifest)
        self._daemon._load_agents_index()
        if agent_id in self._daemon._agents:
            self._daemon._agents[agent_id].update(manifest.to_dict())
            self._daemon._persist_agents_index()
        logger.info("agent.archive: id=%s", agent_id)
        return {"agent_id": agent_id, "status": "archived"}

    async def _handle_agent_unpublish(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = pkg.load_manifest()
        previous_status = manifest.status
        if previous_status not in ("published", "archived"):
            return {
                "status": "error",
                "message": f"Agent not published/archived (current: {previous_status})",
            }
        manifest.status = "draft"
        manifest.published_at = None
        pkg.save_manifest(manifest)
        self._daemon._load_agents_index()
        if agent_id in self._daemon._agents:
            self._daemon._agents[agent_id].update(manifest.to_dict())
            self._daemon._persist_agents_index()
        logger.info("agent.unpublish: id=%s prev=%s", agent_id, previous_status)
        return {
            "agent_id": agent_id,
            "status": "draft",
            "previous_status": previous_status,
        }

    async def _handle_agent_clone(self, params: dict) -> dict:
        import uuid

        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from ..agent_package import AgentPackage

        src_dir = self._daemon._agent_dir(agent_id)
        src_pkg = AgentPackage(src_dir)
        if not src_pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = src_pkg.load_manifest()
        cloned_id = uuid.uuid4().hex[:12]
        cloned_name = params.get("name", f"{manifest.name} (copy)")
        manifest.name = cloned_name
        manifest.status = "draft"
        manifest.version_int = 1
        manifest.published_at = None
        dest_dir = self._daemon._agent_dir(cloned_id)
        dest_pkg = AgentPackage(dest_dir)
        dest_pkg.init(
            manifest=manifest,
            soul=src_pkg.load_soul(),
            memory=src_pkg.load_memory(),
            agents_md=src_pkg.load_agents(),
        )
        self._daemon._load_agents_index()
        self._daemon._agents[cloned_id] = manifest.to_dict()
        self._daemon._agents[cloned_id]["id"] = cloned_id
        self._daemon._agents[cloned_id]["created_at"] = time.time()
        self._daemon._persist_agents_index()
        logger.info("agent.clone: src=%s cloned=%s", agent_id, cloned_id)
        return {"agent_id": cloned_id, "manifest": manifest.to_dict()}

    async def _handle_agent_get_api_endpoint(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = pkg.load_manifest()
        endpoint = f"http://localhost:{MLX_PORT}/v1/agents/{agent_id}/chat"
        logger.info("agent.get_api_endpoint: id=%s endpoint=%s", agent_id, endpoint)
        return {"agent_id": agent_id, "endpoint": endpoint, "status": manifest.status}

    async def _handle_agent_execute_stream(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        input_text = params.get("input", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}

        graph_config = pkg.to_graph_config()
        manifest = pkg.load_manifest()

        effective_prompt = graph_config.get("system_prompt", manifest.system_prompt)

        if manifest.knowledge_base_ids:
            kb_context = await self._daemon._inject_knowledge_context(
                manifest.knowledge_base_ids, input_text, manifest.rag_strategy
            )
            if kb_context:
                effective_prompt = f"{effective_prompt}\n\n{kb_context}"

        if manifest.style:
            style_mgr = self._daemon._get_style_manager()
            style_result = style_mgr.apply(effective_prompt, manifest.style)
            if "system_prompt" in style_result:
                effective_prompt = style_result["system_prompt"]

        graph = AgentGraph(name=manifest.name or agent_id)
        graph.description = manifest.description

        start_id = "start"
        llm_id = "llm-1"
        start_node = NodeConfig(type="start", label="Start")
        llm_node = NodeConfig(
            type="llm",
            label="Agent LLM",
            model=manifest.model,
            system_prompt=effective_prompt,
        )
        graph.add_node(start_id, start_node)
        graph.add_node(llm_id, llm_node)
        graph.add_edge(start_id, llm_id)

        for i, tool_name in enumerate(manifest.tools):
            tool_id = f"tool-{i + 1}"
            tool_node = NodeConfig(type="tool", label=tool_name)
            graph.add_node(tool_id, tool_node)
            if i == 0:
                graph.add_edge(llm_id, tool_id)
            else:
                graph.add_edge(f"tool-{i}", tool_id)

        self._daemon.store.save_graph(graph)
        rt = self._daemon._get_runtime()

        events = []
        execution_id = f"exec-{int(time.time())}-{agent_id}"
        tool_calls_log = []
        knowledge_retrieved = []
        total_input_tokens = 0
        total_output_tokens = 0
        try:
            async for event in rt.execute_graph(graph, input_text):
                ev_dict = event.to_dict() if hasattr(event, "to_dict") else {"type": str(event)}
                events.append(ev_dict)
                ev_type = ev_dict.get("type", "")
                if ev_type == "TOOL_CALL":
                    tool_calls_log.append(ev_dict)
                if ev_type == "TOOL_RESULT":
                    tool_calls_log.append(ev_dict)
                if "token" in str(ev_type).lower():
                    total_input_tokens += ev_dict.get("input_tokens", 0)
                    total_output_tokens += ev_dict.get("output_tokens", 0)
        except Exception as e:
            logger.warning("agent.execute_stream runtime error: %s", e)
            return {
                "execution_id": execution_id,
                "agent_id": agent_id,
                "events": events,
                "status": "error",
                "message": str(e),
                "tool_calls": tool_calls_log,
                "knowledge_retrieved": knowledge_retrieved,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }

        logger.info(
            "agent.execute_stream: id=%s events=%d tools=%d",
            agent_id,
            len(events),
            len(tool_calls_log),
        )
        return {
            "execution_id": execution_id,
            "agent_id": agent_id,
            "events": events,
            "status": "completed",
            "tool_calls": tool_calls_log,
            "knowledge_retrieved": knowledge_retrieved,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "duration_ms": int(
                (events[-1].get("timestamp", 0) - events[0].get("timestamp", 0)) * 1000
            )
            if len(events) > 1
            else 0,
        }

    # ── Connector handlers ──

    async def _handle_agent_preview(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = pkg.load_manifest()
        rag_enabled = bool(manifest.knowledge_base_ids) or manifest.rag_strategy != "none"
        permissions = self._daemon._get_agent_permissions(agent_id, manifest)
        preview = {
            "agentId": agent_id,
            "name": manifest.name,
            "description": manifest.description,
            "avatar": manifest.style if manifest.style else "🤖",
            "tools": manifest.tools,
            "ragEnabled": rag_enabled,
            "permissions": permissions,
        }
        logger.info("agent.preview: agent_id=%s", agent_id)
        return {"preview": preview}

    async def _handle_agent_test_with_project(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        project_id = params.get("project_id", "")
        kb_id = params.get("kb_id", "")
        message = params.get("message", "")
        if not agent_id or not message:
            return {"status": "error", "message": "agent_id and message are required"}
        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        manifest = pkg.load_manifest()
        override_kb = (
            kb_id
            if kb_id
            else (manifest.knowledge_base_ids[0] if manifest.knowledge_base_ids else "")
        )
        execute_params = {
            "agent_id": agent_id,
            "message": message,
            "knowledge_base_ids": [override_kb] if override_kb else manifest.knowledge_base_ids,
            "project_context": {
                "project_id": project_id,
                "kb_id": override_kb,
            },
        }
        result = await self._handle_agent_execute(execute_params)
        result["project_id"] = project_id
        result["kb_id"] = override_kb
        logger.info(
            "agent.test_with_project: agent=%s project=%s kb=%s",
            agent_id,
            project_id,
            override_kb,
        )
        return result

    def _get_agent_permissions(self, agent_id: str, manifest=None) -> dict:
        if manifest is None:
            from ..agent_package import AgentPackage

            pkg = AgentPackage(self._daemon._agent_dir(agent_id))
            if not pkg.exists:
                return {}
            manifest = pkg.load_manifest()
        agent_dir = self._daemon._agent_dir(agent_id)
        defn_path = os.path.join(agent_dir, "definition.json")
        if os.path.exists(defn_path):
            try:
                import json

                with open(defn_path) as f:
                    defn_data = json.load(f)
                perms = defn_data.get("permissions", {})
                if perms:
                    return perms
            except Exception:
                pass
        return {
            "readKnowledge": bool(manifest.knowledge_base_ids),
            "writeKnowledge": False,
            "deleteKnowledge": False,
            "executeCode": "code_execution" in manifest.tools,
            "accessNetwork": manifest.web_search_enabled,
        }

    # ── LangGraph handlers (#35) ──

    async def _handle_agent_published_list(self, params: dict) -> dict:
        tracker = self._daemon._get_status_tracker()
        agents = tracker.list_published(self._daemon._agents)
        return {"agents": agents}

    async def _handle_agent_get_definition(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        tracker = self._daemon._get_status_tracker()
        manifest_data = self._daemon._agents.get(agent_id)
        if not manifest_data:
            self._daemon._load_agents_index()
            manifest_data = self._daemon._agents.get(agent_id)
        if not manifest_data:
            return {"status": "error", "message": f"Agent {agent_id} not found"}
        result = tracker.get_definition(manifest_data)
        return result

    async def _handle_agent_status(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        tracker = self._daemon._get_status_tracker()
        status = tracker.get_status(agent_id)
        return {
            "agent_id": agent_id,
            "status": status.to_dict() if hasattr(status, "to_dict") else status,
        }

    async def _handle_agent_history(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        limit = params.get("limit", 20)
        tracker = self._daemon._get_status_tracker()
        history = tracker.get_history(agent_id, limit=limit)
        return {
            "agent_id": agent_id,
            "history": [h.to_dict() if hasattr(h, "to_dict") else h for h in history],
        }

    # ── Agent Cowork handlers (#36, #37) ──

    async def _handle_agent_cowork_list(self, params: dict) -> dict:
        space_id = params.get("space_id", "")
        mgr = self._daemon._get_cowork_manager()
        agents = mgr.list_agents(space_id)
        return {"agents": agents}

    async def _handle_agent_cowork_add(self, params: dict) -> dict:
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        role = params.get("role", "member")
        permission = params.get("permission", "all_member")
        mgr = self._daemon._get_cowork_manager()
        result = mgr.add_agent(space_id, agent_id, role=role, permission=permission)
        logger.info("agent.cowork.add: space=%s agent=%s", space_id, agent_id)
        return result

    async def _handle_agent_cowork_remove(self, params: dict) -> dict:
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        mgr = self._daemon._get_cowork_manager()
        result = mgr.remove_agent(space_id, agent_id)
        logger.info("agent.cowork.remove: space=%s agent=%s", space_id, agent_id)
        return result

    async def _handle_agent_cowork_call(self, params: dict) -> dict:
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        caller_id = params.get("caller_id", "")
        message = params.get("message", "")
        mgr = self._daemon._get_cowork_manager()
        result = await mgr.call_agent(space_id, agent_id, caller_id=caller_id, message=message)
        logger.info(
            "agent.cowork.call: space=%s agent=%s caller=%s",
            space_id,
            agent_id,
            caller_id,
        )
        return result

    async def _handle_agent_cowork_status(self, params: dict) -> dict:
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        mgr = self._daemon._get_cowork_manager()
        status = mgr.get_agent_status(space_id, agent_id)
        return {"status": status}

    async def _handle_agent_context_inject(self, params: dict) -> dict:
        space_id = params.get("space_id", "")
        agent_id = params.get("agent_id", "")
        mode = params.get("mode", "recent_n")
        recent_n = params.get("recent_n", 10)
        mgr = self._daemon._get_cowork_manager()
        context = mgr.inject_context(space_id, agent_id, mode=mode, recent_n=recent_n)
        return {"context": context}

    async def _handle_agent_diff_review(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        fmt = params.get("format", "markdown")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        version_store = self._daemon._get_version_store()
        versions = version_store.list_versions(agent_id)
        entries = []
        for v in versions:
            v_dict = v.to_dict() if hasattr(v, "to_dict") else v
            entries.append(v_dict)
        markdown = ""
        if fmt == "markdown" and entries:
            lines = [f"# Diff Review: {agent_id}", ""]
            for e in entries:
                label = e.get("label", e.get("version_id", "unknown"))
                ts = e.get("created_at", "")
                lines.append(f"## {label} ({ts})")
                lines.append("")
                snapshot = e.get("snapshot_data", {})
                if isinstance(snapshot, dict):
                    for k, val in snapshot.items():
                        lines.append(f"- **{k}**: {val}")
                lines.append("")
            markdown = "\n".join(lines)
        return {"entries": entries, "markdown": markdown}

    async def _handle_permission_list(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        if agent_id:
            perms = self._daemon._get_agent_permissions(agent_id)
            from ..agent_package import AgentPackage

            agent_dir = self._daemon._agent_dir(agent_id)
            pkg = AgentPackage(agent_dir)
            manifest = pkg.load_manifest() if pkg.exists else None
            tools_list = manifest.tools if manifest else []
            denied = []
            definition_path = os.path.join(agent_dir, "definition.json")
            if os.path.exists(definition_path):
                try:
                    import json

                    with open(definition_path) as f:
                        defn = json.load(f)
                    denied = defn.get("denied_tools", [])
                except Exception:
                    pass
            return {"permissions": perms, "denied_tools": denied, "tools": tools_list}
        import os as _os

        agents_dir = str(Path.home() / ".fusion-agent-studio" / "agents")
        all_perms = []
        if _os.path.isdir(agents_dir):
            for name in _os.listdir(agents_dir):
                adir = _os.path.join(agents_dir, name)
                if os.path.isdir(adir):
                    p = self._daemon._get_agent_permissions(name)
                    p["agent_id"] = name
                    all_perms.append(p)
        return {"permissions": all_perms, "denied_tools": []}

    async def _handle_permission_update(self, params: dict) -> dict:
        agent_id = params.get("agent_id", "")
        tool = params.get("tool", "")
        level = params.get("level", "allow")
        if not agent_id:
            return {"status": "error", "message": "agent_id parameter required"}
        from ..agent_package import AgentPackage

        agent_dir = self._daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not pkg.exists:
            return {"status": "error", "message": f"Agent not found: {agent_id}"}
        import json

        definition_path = os.path.join(agent_dir, "definition.json")
        defn = {}
        if os.path.exists(definition_path):
            with open(definition_path) as f:
                defn = json.load(f)
        denied = defn.get("denied_tools", [])
        if level == "deny":
            if tool and tool not in denied:
                denied.append(tool)
        elif level == "allow":
            if tool in denied:
                denied.remove(tool)
        defn["denied_tools"] = denied
        with open(definition_path, "w") as f:
            json.dump(defn, f, indent=2, ensure_ascii=False)
        logger.info(
            "permission.update: agent=%s tool=%s level=%s denied=%s",
            agent_id,
            tool,
            level,
            denied,
        )
        return {"ok": True, "denied_tools": denied}
