"""Fixed Agent roles and their code-level maximum tool permissions."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .contracts import AgentRole
from .storage import Repository


@dataclass(frozen=True)
class RoleDefinition:
    role: AgentRole
    description: str
    instructions: str
    maximum_tools: frozenset[str]
    input_contract: str
    output_contract: str


ROLE_DEFINITIONS: dict[str, RoleDefinition] = {
    "orchestrator": RoleDefinition(
        "orchestrator", "拆解请求并决定 Agent 依赖顺序",
        "生成有依赖关系的执行图，不写入用户数据。",
        frozenset({"list_agent_capabilities", "read_goal_summary"}),
        "用户请求、当地时间、目标摘要、Agent 能力目录",
        "ExecutionPlan：意图与有依赖关系的步骤",
    ),
    "memory": RoleDefinition(
        "memory", "检索已批准记忆并从反馈提出待审提案",
        "只读取已批准记忆；反馈只形成候选，不自行批准。",
        frozenset({"search_approved_memories", "read_feedback"}),
        "规划请求与已批准记忆，或已完成任务的反馈",
        "MemoryContext：带来源的引用；MemoryProposalBatch：待审候选",
    ),
    "learning": RoleDefinition(
        "learning", "将学习目标拆为具体任务草案",
        "依据目标和进度提出有时长、优先级的学习任务。",
        frozenset({"read_goals", "read_task_progress"}),
        "用户目标、任务进度、选定记忆",
        "LearningPlan：具体任务、预计时长、优先级与期限",
    ),
    "life": RoleDefinition(
        "life", "将运动、家务等生活目标拆为具体任务草案",
        "仅提出生活任务建议；引用真实目标和已批准偏好，不写任务、日程或记忆。",
        frozenset({"read_goals", "read_task_progress", "search_approved_memories", "read_settings"}),
        "用户生活目标、现有生活事项、已批准偏好和当地时间",
        "LifePlan：生活任务、来源理由、预计时长、优先级与期限",
    ),
    "schedule": RoleDefinition(
        "schedule", "依据时间与偏好提出日程草案",
        "只提出时间建议，最终冲突由程序校验。",
        frozenset({"read_time_blocks", "read_scheduled_tasks", "read_settings"}),
        "学习和／或生活计划、可用与占用时段、现有任务、偏好规则",
        "ScheduleProposal：排程建议与冲突",
    ),
    "task": RoleDefinition(
        "task", "规范化任务草案并在用户确认后提交",
        "规划时只读；提交必须由用户确认后的 Harness 发起。",
        frozenset({"read_tasks", "commit_approved_plan"}),
        "学习和／或生活计划、日程建议、有效来源步骤",
        "TaskPlan：待确认任务草案；确认后由 Harness 提交",
    ),
}


class AgentRegistry:
    def __init__(self, repository: Repository):
        self.repository = repository
        for definition in ROLE_DEFINITIONS.values():
            repository.insert_agent_config(
                definition.role,
                definition.description,
                definition.instructions,
                set(definition.maximum_tools),
            )

    def list_roles(self) -> list[dict]:
        return [self.get_role(role) for role in ROLE_DEFINITIONS]

    def get_role(self, role: str) -> dict:
        definition = ROLE_DEFINITIONS.get(role)
        if definition is None:
            raise ValueError(f"Unknown Agent role: {role}")
        saved = self.repository.get_agent_config(role)
        subset = set(json.loads(saved["tool_subset_json"]))
        if not subset <= definition.maximum_tools:
            raise ValueError(f"Stored permissions exceed role maximum: {role}")
        return {
            "role": role,
            "description": definition.description,
            "input_contract": definition.input_contract,
            "output_contract": definition.output_contract,
            "instructions": saved["instructions"],
            "tool_subset": sorted(subset),
            "maximum_tools": sorted(definition.maximum_tools),
            "version": saved["version"],
        }

    def update_role_instruction(
        self, role: str, instructions: str, tool_subset: set[str]
    ) -> dict:
        definition = ROLE_DEFINITIONS.get(role)
        if definition is None:
            raise ValueError(f"Unknown Agent role: {role}")
        if not instructions.strip():
            raise ValueError("Instructions cannot be empty")
        if not tool_subset <= definition.maximum_tools:
            raise PermissionError("Tool selection exceeds code-level role maximum")
        self.repository.update_agent_config(role, instructions.strip(), tool_subset)
        return self.get_role(role)

    def allows(self, role: str, tool_name: str) -> bool:
        definition = ROLE_DEFINITIONS.get(role)
        if definition is None or tool_name not in definition.maximum_tools:
            return False
        return tool_name in self.get_role(role)["tool_subset"]
