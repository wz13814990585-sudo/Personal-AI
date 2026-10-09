"""Reviewed custom Agent proposals and bounded manual read-only advice."""

from __future__ import annotations

import json
import time
from threading import local
from typing import Any, Callable, Protocol

from agno.agent import Agent
from agno.exceptions import ModelProviderError
from openai import APITimeoutError
from pydantic import ValidationError

from .agents import _agno_tools
from .config import AppConfig
from .contracts import CustomAgentAdvice, CustomAgentCreate, CustomAgentProposalOutput
from .custom_agent_store import CustomAgentStore
from .custom_agents import CUSTOM_AGENT_READ_TOOLS, validate_custom_agent
from .model_factory import make_deepseek_model
from .storage import Repository
from .tool_gateway import ToolGateway


class CustomModelRunner(Protocol):
    def propose(
        self, goal: str, allowed_tools: list[str], existing_names: list[str],
    ) -> CustomAgentProposalOutput: ...

    def advise(
        self, agent: dict[str, Any], request: str, tools: "CustomBoundTools",
    ) -> CustomAgentAdvice: ...

    def consume_usage(self) -> list[dict[str, Any]]: ...


class AgnoCustomRunner:
    def __init__(self, config: AppConfig):
        self.config = config
        self._thread_state = local()

    def consume_usage(self) -> list[dict[str, Any]]:
        result = getattr(self._thread_state, "last_usage", [])
        self._thread_state.last_usage = []
        return result

    def _run(self, name: str, instructions: list[str], payload: dict[str, Any],
             schema: type, tools: list) -> Any:
        model = make_deepseek_model(self.config)
        self._thread_state.last_usage = []
        agent = Agent(
            name=name, model=model,
            instructions=[*instructions,
                "面向用户的名称、描述、建议和解释使用简体中文；"
                "这些自然语言字段不使用英文角色名、字段名或枚举代码；"
                "JSON 键名、枚举值、ID 和工具名保持原样。"],
            tools=tools,
            tool_call_limit=self.config.max_tool_calls_per_agent,
            output_schema=schema, use_json_mode=True, retries=0, telemetry=False,
        )
        prompt = json.dumps(
            {"input": payload, "output_json_schema": schema.model_json_schema()},
            ensure_ascii=False,
        )
        try:
            response = agent.run(prompt)
        finally:
            self._thread_state.last_usage = list(model.request_usage)
        if response is None or response.content is None:
            raise ValueError("empty_model_output")
        if not isinstance(response.content, schema):
            raise ValueError("invalid_model_output")
        return schema.model_validate(response.content.model_dump())

    def propose(
        self, goal: str, allowed_tools: list[str], existing_names: list[str],
    ) -> CustomAgentProposalOutput:
        return self._run(
            "custom_agent_designer",
            [
                "根据用户目标只提出一个可审核的个人助理 Agent 定义，不声称已创建或启用。",
                "指令限定为只读分析和建议，禁止要求提交任务、修改日程、批准记忆、发送消息或更改配置。",
                "suggested_tools 只能逐字选择 input.allowed_read_tools 中的名称；可返回空列表。",
                "name 不得与 input.existing_names 相同，也不得使用内置角色名。",
                "仅返回符合 JSON schema 的单个 JSON 对象，不附加 Markdown。",
            ],
            {"goal": goal, "allowed_read_tools": allowed_tools,
             "existing_names": existing_names},
            CustomAgentProposalOutput, [],
        )

    def advise(
        self, agent: dict[str, Any], request: str, tools: "CustomBoundTools",
    ) -> CustomAgentAdvice:
        return self._run(
            agent["name"],
            [agent["instructions"],
             "你只能读取明确开放的工具并给出建议；不能写任务、日程、记忆或 Agent 配置。",
             "需要个人事实时先调用已授权工具；没有资料时在 limitations 说明，不得编造工具结果。",
             "建议中的 source_ids 只能来自真实工具结果；不要声称已经执行建议。",
             "只返回满足 JSON schema 的单个 JSON 对象。"],
            {"request": request, "agent_id": agent["id"],
             "agent_version": agent["version"], "allowed_tools": tools.available_names},
            CustomAgentAdvice, _agno_tools(tools),
        )


class CustomBoundTools:
    """Every call checks the persisted Agent version and code read-only ceiling."""

    def __init__(
        self, repository: Repository, store: CustomAgentStore,
        gateway: ToolGateway, agent: dict[str, Any], run_id: str, max_calls: int,
    ):
        self.repository = repository
        self.store = store
        self.gateway = gateway
        self.agent = agent
        self.run_id = run_id
        self.max_calls = max_calls
        self.calls = 0
        self.failure_code: str | None = None
        self.seen_ids: set[str] = set()

    @property
    def available_names(self) -> list[str]:
        return sorted(set(self.agent["tool_subset"]) & CUSTOM_AGENT_READ_TOOLS)

    def _deny(self, tool_name: str, reason: str) -> None:
        self.failure_code = "permission_denied"
        self.store.append_event(
            self.run_id, "tool_denied", {"tool": tool_name, "reason": reason}, actor="agent"
        )
        raise PermissionError(f"Tool denied: {reason}")

    def invoke(self, tool_name: str, args: dict[str, Any]) -> Any:
        if self.calls >= self.max_calls:
            self._deny(tool_name, "tool_call_limit")
        self.calls += 1
        current = self.repository.get_custom_agent(self.agent["id"])
        if current["status"] != "active" or current["version"] != self.agent["version"]:
            self._deny(tool_name, "agent_changed_or_paused")
        if (tool_name not in CUSTOM_AGENT_READ_TOOLS
            or tool_name not in current["tool_subset"]
            or tool_name not in self.gateway._specs):
            self._deny(tool_name, "custom_agent_tool_not_allowed")
        spec = self.gateway._specs[tool_name]
        try:
            validated = spec.input_schema.model_validate(args)
        except ValidationError:
            self._deny(tool_name, "invalid_arguments")
        started = time.monotonic()
        try:
            result = spec.output_schema.model_validate(spec.handler(validated))
        except Exception as exc:
            self.failure_code = "tool_error"
            self.store.append_event(
                self.run_id, "tool_failed", {"tool": tool_name, "error_type": type(exc).__name__},
                actor="agent", duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        for item in getattr(result, "items", []):
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                self.seen_ids.add(item["id"])
        self.store.append_event(
            self.run_id, "tool_called", {"tool": tool_name, "arg_keys": sorted(args)},
            actor="agent", duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result


def _error_code(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ModelProviderError) and getattr(exc, "status_code", None) == 429:
        return "rate_limited"
    if isinstance(exc, ModelProviderError) and isinstance(exc.__cause__, (TimeoutError, APITimeoutError)):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, (ValueError, ValidationError)):
        return "invalid_output"
    return "agent_error"


class CustomAgentRuntime:
    def __init__(
        self, repository: Repository, gateway: ToolGateway, config: AppConfig,
        runner: CustomModelRunner | None = None,
    ):
        self.repository = repository
        self.gateway = gateway
        self.config = config
        self.store = CustomAgentStore(repository)
        self.runner = runner or AgnoCustomRunner(config)

    @staticmethod
    def _request(text: str) -> str:
        value = text.strip()
        if not value or len(value) > 2000:
            raise ValueError("custom_agent_request_length")
        return value

    def _usage(self, run_id: str, error_code: str | None) -> None:
        consume = getattr(self.runner, "consume_usage", None)
        requests = consume() if callable(consume) else []
        if requests:
            self.store.record_usage(run_id, requests, error_code)

    def propose(self, goal: str) -> dict[str, Any]:
        request = self._request(goal)
        if not self.config.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for Agent runs")
        run = self.store.create_run("proposal", request, self.config.deepseek_model_id)
        try:
            output = self.runner.propose(
                request, sorted(CUSTOM_AGENT_READ_TOOLS),
                [agent["name"] for agent in self.repository.list_custom_agents()],
            )
            if not isinstance(output, CustomAgentProposalOutput):
                raise ValueError("invalid_custom_proposal_output")
            output = CustomAgentProposalOutput.model_validate(output.model_dump())
            validate_custom_agent(CustomAgentCreate(
                name=output.name, description=output.description,
                instructions=output.instructions, tool_subset=set(output.suggested_tools),
            ))
            if len(set(output.suggested_tools)) != len(output.suggested_tools):
                raise ValueError("duplicate_suggested_tool")
            self._usage(run["id"], None)
            return self.store.complete_proposal_run(run["id"], output)
        except Exception as exc:
            code = _error_code(exc)
            self._usage(run["id"], code)
            self.store.finish_run(run["id"], "failed", error_code=code)
            raise

    def advise(
        self, agent_id: str, request_text: str,
        *, on_started: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        request = self._request(request_text)
        if not self.config.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for Agent runs")
        agent = self.repository.get_custom_agent(agent_id)
        if agent["status"] != "active":
            raise PermissionError("custom_agent_paused")
        validate_custom_agent(CustomAgentCreate(
            name=agent["name"], description=agent["description"],
            instructions=agent["instructions"], tool_subset=set(agent["tool_subset"]),
        ))
        run = self.store.create_run("advice", request, self.config.deepseek_model_id, agent)
        bound = CustomBoundTools(
            self.repository, self.store, self.gateway, agent, run["id"],
            self.config.max_tool_calls_per_agent,
        )
        started = time.monotonic()
        try:
            if on_started is not None:
                on_started(run["id"])
            output = self.runner.advise(agent, request, bound)
            if bound.failure_code:
                raise PermissionError("tool_denied_during_custom_run") if bound.failure_code == "permission_denied" else RuntimeError("tool_failed_during_custom_run")
            if time.monotonic() - started > self.config.model_timeout_seconds:
                raise TimeoutError("custom_model_timeout")
            current = self.repository.get_custom_agent(agent_id)
            if current["status"] != "active" or current["version"] != agent["version"]:
                raise PermissionError("custom_agent_changed_during_run")
            if not isinstance(output, CustomAgentAdvice):
                raise ValueError("invalid_custom_advice_output")
            output = CustomAgentAdvice.model_validate(output.model_dump())
            if any(not set(item.source_ids) <= bound.seen_ids
                   for item in output.recommendations):
                raise ValueError("unread_custom_advice_source")
            self._usage(run["id"], None)
            return self.store.finish_run(run["id"], "success", output=output.model_dump(mode="json"))
        except Exception as exc:
            code = _error_code(exc)
            self._usage(run["id"], code)
            self.store.finish_run(run["id"], "failed", error_code=code)
            raise
