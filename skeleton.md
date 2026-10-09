# Personal AI OS 代码骨架

第 1–8 节保留 P0 历史骨架，第 9–11 节记录 P1 原定轮次，第 12 节记录 P2 增量轮次，第 13 节安排已选定的真实服务与 iPhone 接入；实际实现与测试状态见 `code.md`。开发时以 [design.md](design.md) 的审批、权限、记忆、状态和迁移规则为准。

## 1. 目标目录

```text
.
├── request.md                       # 产品需求与验收
├── design.md                        # 已确定的实现方案
├── skeleton.md                      # 本开发骨架
├── pyproject.toml                   # Python 3.12、依赖和 pytest 配置
├── uv.lock                          # 开发时生成的锁文件
├── .env.example                     # 环境变量名称示例，不存真实密钥
├── .gitignore                       # 忽略 .env、.venv、.local、缓存
├── README.md                        # 安装、启动、配置、验收说明
├── app.py                           # Streamlit 入口与五页面导航
├── personal_ai_os/
│   ├── __init__.py
│   ├── config.py                     # 环境变量、时区、模型白名单、运行限额
│   ├── contracts.py                  # Pydantic 输入输出与状态枚举
│   ├── storage.py                    # SQLite 建表、短连接、Repository、事务
│   ├── model_factory.py              # 唯一模型入口：Agno DeepSeek
│   ├── agent_registry.py             # 五角色元数据、指令与工具权限上限
│   ├── agents.py                     # AgnoAgentRunner 与角色工具绑定
│   ├── orchestrator.py               # ExecutionPlan 依赖图校验
│   ├── tool_gateway.py               # 工具注册、参数校验、鉴权和 Trace
│   ├── schedule_rules.py             # 时区、可用时段、期限与冲突检查
│   ├── memory.py                     # 已批准记忆检索、提案与生效规则
│   ├── harness.py                    # Agent 顺序执行、状态机、审批提交
│   ├── services.py                   # UI 使用的唯一业务入口
│   └── ui/
│       ├── __init__.py
│       ├── chat_today.py             # 对话与今日计划
│       ├── tasks_schedule.py         # 任务与日程
│       ├── agent_console.py          # Agent 控制台
│       ├── trace_view.py             # 执行轨迹
│       └── memory_settings.py        # 记忆与设置
└── tests/
    ├── conftest.py                   # 临时 SQLite 与 FakeAgentRunner
    ├── test_plan_and_state.py        # 五 Agent 依赖、状态与 Trace
    ├── test_permissions.py           # 工具越权、审批边界
    ├── test_schedule.py              # 可用时间、冲突、时区和期限
    ├── test_memory_loop.py           # 反馈、审核、跨会话行为变化
    ├── test_commit_and_failure.py    # 原子提交、幂等与故障注入
    ├── test_ui.py                    # Streamlit 五页面冒烟
    └── test_deepseek_smoke.py        # 有真实密钥时才运行的调用测试
```

运行时数据库放在 `.local/personal_ai_os.sqlite3`。`pyproject.toml` 的 P0 依赖为 `agno`、`openai`（仅作为 DeepSeek 兼容接口客户端）、`streamlit`、`pydantic` 和测试用 `pytest`；数据库直接使用标准库 `sqlite3`。不新增 API 服务器、队列、向量库或第二个 Agent 框架。

## 2. 启动与依赖方向

`app.py` 读取配置、初始化 SQLite、组装 `Repository → ToolGateway → AgentRunner → Harness → PersonalAIService`，再用 Streamlit 导航显示五个页面。每个页面只接收 `PersonalAIService`，不导入 `sqlite3`、Agno Agent 或工具处理函数。

```text
ui / app.py → services.py → harness.py → agents.py → model_factory.py
                         ↘          ↘ tool_gateway.py → storage.py
                          ↘ storage.py
contracts.py、config.py 为各模块共享的底层定义
```

`storage.py` 每次操作获取短连接，事务在 Repository 层集中管理；不把 SQLite 连接放进 Streamlit `session_state`。`app.py` 启动时调用 `Repository.initialize()`，并把上次进程遗留的 `running` 运行标为 `failed`、原因记为 `interrupted`。

## 3. 核心类型与公开接口

以下是接口形状，函数体在开发阶段实现；使用一个 `run_id` 贯穿请求、步骤、工具事件和草案。

```python
# contracts.py：Pydantic 模型，字段校验在这里集中定义
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

AgentRole = Literal["orchestrator", "memory", "learning", "schedule", "task"]
RunKind = Literal["planning", "feedback"]
RunStatus = Literal["pending", "running", "waiting_approval", "success", "failed", "cancelled"]
StepStatus = Literal["pending", "running", "success", "failed", "skipped"]

class StepSpec(BaseModel):
    step_id: str
    agent: AgentRole
    purpose: str
    depends_on: list[str]

class ExecutionPlan(BaseModel):
    intent: str
    steps: list[StepSpec]           # 最多四个工作 Agent；Orchestrator 另计

class TaskDraft(BaseModel):
    draft_item_id: str
    title: str
    estimated_minutes: int
    priority: Literal["low", "medium", "high"]
    due_at: datetime | None
    start_at: datetime | None
    end_at: datetime | None
    source_step_id: str

class PlanDraft(BaseModel):
    run_id: str
    revision: int
    tasks: list[TaskDraft]
    conflicts: list[str]
    memory_ids: list[str]
    explanations: list[str]

class MemoryProposal(BaseModel):
    feedback_id: str
    kind: str
    value: dict
    source_excerpt: str
    explanation: str

class MemoryProposalBatch(BaseModel):
    proposals: list[MemoryProposal]
```

`contracts.py` 还需定义 `MemoryContext`、`LearningPlan`、`ScheduleProposal`、`AgentResult`、工具入参／返回模型及异常类型。所有模型输出必须再次通过 Pydantic 校验；`response.content` 不是预期类型时，该步骤失败。由服务器核对模型返回的 `feedback_id`、`source_step_id` 是否属于当前运行，不信任模型自行填入的关联 ID。`TaskDraft` 的时间必须为带时区的时间点，持久化前转 UTC；`estimated_minutes > 0`，且有安排时 `start_at < end_at`。

| 模块 | 最小接口 | 责任边界 |
| --- | --- | --- |
| `config.py` | `load_config(require_model: bool) -> AppConfig` | 验证时区、路径、`DEEPSEEK_MODEL_ID` 白名单；真实调用才要求密钥。 |
| `model_factory.py` | `make_deepseek_model(config) -> DeepSeek` | 唯一模型构造点；默认 `deepseek-flash`、`use_thinking=False`，不允许其他提供商。 |
| `agent_registry.py` | `list_roles()`、`get_role(role)`、`update_role_instruction(role, text, tool_subset)` | 注册五角色；配置的工具只能是代码级上限的子集；版本递增。 |
| `agents.py` | `AgentRunner.run(role, payload, output_schema, tools) -> BaseModel` | 定义可注入协议；生产实现用 Agno，测试实现用 Fake；按角色配置指令、`use_json_mode=True` 和工具。 |
| `orchestrator.py` | `validate_execution_plan(plan) -> list[StepSpec]` | 校验 ID、角色、节点数、依赖存在、无环及 `Memory → Learning → Schedule → Task` 的必要顺序。 |
| `tool_gateway.py` | `invoke(bound_role, tool_name, args, approval_context=None)` | 先验证参数，再验证角色、阶段和审批，最后执行并记 Trace；角色不能由模型入参决定。 |
| `schedule_rules.py` | `validate_schedule(draft, availability, busy, tasks, memories, timezone)` | 解析／校验时区与期限，采用半开时间区间检查重叠和禁排规则。 |
| `memory.py` | `search_approved(query, goal_tags)`、`propose_from_feedback(feedback)`、`resolve_proposal(id, decision, edited_value)` | 只让已批准记忆参与规划；提案逐条审核、保留来源，编辑／删除即时生效。 |
| `harness.py` | `execute_plan(run_id)`、`execute_feedback(run_id)`、`commit_plan(run_id, revision, approval_context)` | 运行 Agent 图、限制步数／时间、持久化状态和 Trace；提交仅在审批后。 |
| `services.py` | `start_plan()`、`edit_plan()`、`approve_plan()`、`reject_plan()`、`complete_task()`、`submit_feedback()`、`resolve_memory()` 及查询／CRUD | UI 唯一入口；用户直接操作与 Agent 建议分开；返回页面可显示的结果和错误。 |

Memory Agent 在规划运行中输出 `MemoryContext`，在反馈运行中输出 `MemoryProposalBatch`；同一角色可按运行类型指定不同 `output_schema`。Orchestrator 只生成执行图，Harness 校验并汇总结果。Task Agent 在规划时只生成草案；`commit_approved_plan` 不注册到规划阶段模型的可用工具中。

## 4. 工具、状态和事务的落点

工具白名单直接落在 `agent_registry.py` 与 `tool_gateway.py`，分别对应：

- Orchestrator：`list_agent_capabilities`、`read_goal_summary`。
- Memory：`search_approved_memories`、`read_feedback`。
- Learning：`read_goals`、`read_task_progress`。
- Schedule：`read_time_blocks`、`read_scheduled_tasks`、`read_settings`。
- Task：`read_tasks`；`commit_approved_plan` 仅供 Harness 在确认后调用。

`ToolGateway.invoke()` 对未知工具、越权角色、未通过 Pydantic 的参数或缺少审批上下文的写入返回拒绝，记录 `tool_denied` 事件，且不调用 Repository 写方法。审批上下文由 Service 根据用户确认动作生成，绑定 `run_id + revision`，模型不能提供或修改。

`Harness.execute_plan()` 的固定骨架：创建／读取 `pending` 运行 → 写入 Orchestrator `running` → 取得并验证执行图 → 按依赖执行各 Agent → 每步写入 `run_steps` 和 `trace_events` → 用 `schedule_rules` 复查时间 → 汇总 `PlanDraft` → 置 `waiting_approval`。失败步骤置 `failed`，依赖步骤置 `skipped`，运行置 `failed`；保留真实错误，不创建正式任务。单次最多五个 Agent 步骤、每个 Agent 最多四次工具调用、单个模型请求 45 秒、总运行 240 秒；P0 不自动重试。

`commit_plan()` 在一个 `BEGIN IMMEDIATE` 事务中依次检查：运行仍为 `waiting_approval` → revision 一致 → 草案字段有效 → 可用时段、期限、已批准记忆与现有任务无冲突 → 插入全部任务 → 运行置 `success` → 提交事务。任何一步失败都回滚。生成任务约束 `UNIQUE(source_run_id, draft_item_id)`，重复确认不产生重复待办。用户拒绝则运行置 `cancelled`。

反馈流程：只允许对已完成任务提交反馈 → 保存原文并启动 `feedback` 运行 → Memory Agent 生成零至多条 `memory_proposals` → 有待审提案时置 `waiting_approval` → 用户逐条修改、批准或拒绝 → 批准时在同一事务中插入 `memories` 并更新提案状态 → 全部处理完置 `success`；若没有候选，直接置 `success` 并保留反馈。提取失败仍保留原始反馈。未确认、已拒绝或已删除记忆不会进入新的规划上下文。

## 5. SQLite 最小表与约束

`storage.py` 创建以下表；字段命名和状态值与 `design.md` 对齐：

| 表 | 最少字段／约束 |
| --- | --- |
| `settings` | `key PRIMARY KEY`、`value_json`；含时区、作息、偏好。 |
| `goals` | `id PRIMARY KEY`、`title`、`description`、`due_at_utc`、`status`。 |
| `tasks` | `id PRIMARY KEY`、`goal_id`、`title`、`status`、`priority`、`estimated_minutes`、`due_at_utc`、`start_at_utc`、`end_at_utc`、`source_run_id`、`draft_item_id`；生成任务的来源组合唯一。 |
| `time_blocks` | `id PRIMARY KEY`、`kind`（`available/busy`）、`start_at_utc`、`end_at_utc`、`label`。 |
| `agent_configs` | `role PRIMARY KEY`、`instructions`、`tool_subset_json`、`version`。 |
| `feedback` | `id PRIMARY KEY`、`task_id`、`body`、`created_at_utc`。 |
| `memory_proposals` | `id PRIMARY KEY`、`feedback_id`、`kind`、`value_json`、`source_excerpt`、`status`。 |
| `memories` | `id PRIMARY KEY`、`source_feedback_id`、`kind`、`value_json`、`status`、`created_at_utc`、`updated_at_utc`。 |
| `runs` | `id PRIMARY KEY`、`kind`、`conversation_id`、`request_text`、`idempotency_key UNIQUE`、`status`、`draft_json`、`revision`、`config_snapshot_json`、起止时间。 |
| `run_steps` | `id PRIMARY KEY`、`run_id`、`step_id`、`agent`、`depends_on_json`、`status`、`result_json`、`error_code`；`UNIQUE(run_id, step_id)`。 |
| `trace_events` | `id PRIMARY KEY`、可空 `run_id/step_id`、`actor`、`event_type`、`summary_json`、`duration_ms`、`created_at_utc`。 |

每个连接执行 `PRAGMA foreign_keys=ON`，初始化启用 WAL；查询按 `run_id`、记忆状态和任务时间加索引。时间统一保存 UTC，展示时使用用户时区。`time_blocks` 表示用户设置的可用／占用区间，已排任务的起止时间直接保存在 `tasks`；冲突判断同时读取二者。没有可用时间时保留未排程草案并提示用户，不能假设空闲。

## 6. 页面骨架

`app.py` 用 Streamlit 导航注册五个页面；每个 `ui/*.py` 暴露 `render(service: PersonalAIService) -> None`。页面只调用 Service，不直接操纵 Agent 或数据库。

| 页面文件 | 第一批可见控件与数据 |
| --- | --- |
| `chat_today.py` | 请求输入框、最近三轮会话摘要、`st.status` 当前步骤、可编辑 `PlanDraft`、确认／拒绝按钮、今日任务。 |
| `tasks_schedule.py` | 任务列表与状态编辑、完成和反馈表单、可用／占用时段录入、冲突提示。 |
| `agent_console.py` | 五角色卡片、硬性权限上限、可用工具子集、指令编辑、配置版本、最近运行状态。 |
| `trace_view.py` | 运行列表、步骤依赖、状态、委派、工具与拒绝事件、耗时、错误。 |
| `memory_settings.py` | 目标／时区／作息设置、待审候选逐条处理、已批准记忆编辑／删除、来源反馈。 |

页面刷新后从 SQLite 重取状态。Trace 只显示可解释的行动摘要和脱敏参数，不显示 API 密钥或模型隐藏推理。

## 7. 最小开发与验证顺序

1. 创建 `pyproject.toml`、`config.py`、`contracts.py`、`storage.py`，先让本机数据增删改查、事务与重启读取通过。
2. 实现 `schedule_rules.py`、`memory.py` 和 `tool_gateway.py`，验证冲突与越权都不会写入数据。
3. 实现 `model_factory.py`、`agent_registry.py`、`agents.py`、`orchestrator.py`，先用 FakeAgentRunner 跑通五角色依赖图，再连接 DeepSeek。
4. 实现 `harness.py` 与 `services.py`，跑通草案审批、任务原子提交、反馈提案和跨会话记忆。
5. 实现五个 `ui/` 页面和 `app.py`，以真实 SQLite 数据展示状态与 Trace。
6. 运行确定性端到端测试；配置 `DEEPSEEK_API_KEY` 后执行真实 DeepSeek 主场景冒烟，逐项核对 `design.md` 的 P0-1～P0-10。

首个纵向可运行切片应是：输入面试目标 → 五 Agent 形成草案 → 用户确认 → 任务出现在今日计划。随后加入反馈、记忆审批和再次规划。测试替身只用于自动化验证，正式运行不使用非 DeepSeek 模型或伪造 Agent Trace。

## 8. 配置与交付检查

`.env.example` 只列变量名与安全示例值：`DEEPSEEK_API_KEY`（留空）、`DEEPSEEK_MODEL_ID=deepseek-flash`、`DATABASE_PATH=.local/personal_ai_os.sqlite3`、`APP_TIMEZONE=Australia/Sydney`。启动说明要求在启动进程前通过 shell `export` 或载入本机 `.env` 设置这些环境变量；应用本身只从进程环境读取。`.env`、数据库和虚拟环境必须被 Git 忽略。无真实密钥时应用可显示明确配置状态，但真实 Agent 调用应报配置错误，不能默默切换模型。

开发完成的最小交付：`uv` 锁定依赖、`README.md` 启动说明、可运行的五页面、SQLite 数据、P0 确定性测试结果，以及配置密钥后的 DeepSeek 真实调用与 Trace。未完成这些检查前，不将 P1/P2 功能计入首版交付。

## 9. P1 增量文件与依赖（计划，未创建）

优先改动现有模块，只有独立业务才新增文件：

| 位置 | P1 责任与依赖 |
| --- | --- |
| `storage.py`、`contracts.py` | 版本化迁移、P1 表和字段、严格输入输出；先于其他工作。 |
| `recurrence.py`、`habits.py` | 按当地日期生成已确认的重复实例与习惯打卡；只经 Repository 事务写入。 |
| `agent_registry.py`、`agents.py`、`orchestrator.py`、`harness.py` | 增加只读 Life Agent、`LifePlan` 契约与混合执行图；保留 P0 五 Agent 路径。 |
| `daily_planning.py`、`schedule_rules.py`、`tool_gateway.py` | 每日草案、差异、基线指纹、冲突与受审批的事务提交；不由模型直接写任务。 |
| `worker.py`、`__main__.py`、`notifications.py` | 单命令启动 Web 与 worker；领取持久化作业、提醒收件箱和显式开启的系统通知。 |
| `usage.py`、`export.py` | 真实用量/估算成本及只读本机 JSON/CSV 导出。 |
| `services.py`、现有 `ui/*.py` | 页面唯一读写入口；在五页面内增加规则、打卡、每日草案、提醒和复盘，不先建第六页。 |
| `tests/test_p1_*.py` | 新增迁移、重复规则、混合 Agent、重排、worker、恢复、导出与 AppTest；文件名按轮次确定。 |

依赖顺序为 `迁移/契约 → 重复任务与习惯 → Life Agent → 每日计划/重排 → worker/提醒/复盘 → 恢复/统计/导出 → 总验收`。旧数据库迁移前备份；`agent_configs` 五角色约束必须安全升级并保留配置；不要清空 SQLite 或重建用户数据。`runs.kind` 沿用 `planning/feedback`；P1 日常草案与作业用新表关联现有运行和 Trace。

## 10. P1 契约与 Service 草图（计划，非现有 API）

- `LifePlan.items[]`：使用领域前缀的唯一 `item_id`、标题、预计分钟、优先级、期限、原因；Life Agent 只读目标/任务/已批准记忆。混合执行图允许 `Memory → Learning → Life → Schedule → Task`，Orchestrator 另计，最多六步；纯学习保持原路径。
- `RecurrenceRule`：`daily` 或 `weekly_days`、当地时区、选定星期、有效起止日期、任务模板、提醒提前量、状态、版本。自然语言“每周三次”先由用户选择或确认三个具体星期；实例键 `(rule_id, local_date)`。暂停不删除历史任务。
- `HabitCheckin`：习惯 ID、本地日期、完成状态、可选备注；同日更正更新记录并记审计。任务完成不自动替代习惯打卡。
- `DailyPlanDraft`：本地日期、草案 ID、revision、基线指纹、`create_task/reschedule_task` 动作、旧/新时间、冲突、理由；审批在事务内复算指纹和时间冲突。旧 revision 拒绝，重复提交返回同一结果。
- `ScheduledJob`：类型、到期 UTC、去重键、租约截止、尝试次数和状态；worker 使用注入时钟，领取与完成都用事务。已过期的历史提醒记为错过，不在恢复时集中弹出；重复任务仅生成当前及未来窗口的实例，过去未运行的日期记录为跳过，避免回补大量陈旧待办。
- `PersonalAIService` 拟新增 `create/update/pause_recurrence_rule`、`list_today`、`checkin_habit`、`propose/edit/approve_daily_plan`、`list_notifications`、`save_daily_review`、`retry_run`、`usage_summary`、`export_data` 等方法；名称在编码时可调整，页面仍不得绕过 Service。

## 11. P1 编码轮次与验证门槛（以下测试文件均拟新增）

每轮先执行 `uv run --group dev pytest -q` 复测 P0 和已完成的 P1；完成后更新 `code.md`，记录实际命令、结果和剩余轮次。真实模型测试继续用本机 `.env`，不把密钥写入文档或测试输出。

| 轮次 | 本轮完成 | 新功能验证与通过结果 | 后续 |
| --- | --- | --- | --- |
| P1-第 1 轮 | 数据库版本迁移、重复规则、实例去重、习惯及五页面最小管理入口。 | 拟运行 `uv run --group dev pytest -q tests/test_p1_migration_recurrence.py tests/test_p1_habits_ui.py`：旧库数据不丢；多次扫描/重启只有一个实例；跨时区和夏令时规则可解释；AppTest 可增改停规则和打卡。 | Life Agent、日计划、worker 等。 |
| P1-第 2 轮 | Life Agent、混合学习/生活执行图、受限工具和草案汇总。 | 拟运行 `uv run --group dev pytest -q tests/test_p1_life_plan.py` 及可用密钥下的真实 DeepSeek 混合样例：六步依赖合法、越权拒绝、确认前不写新任务；旧面试场景仍通过。 | 每日重排、后台等。 |
| P1-第 3 轮 | 统一今日视图、每日草案、差异展示、编辑与事务性重排审批。 | 拟运行 `uv run --group dev pytest -q tests/test_p1_daily_plan.py tests/test_p1_daily_ui.py`：冲突显示、基线变更/旧 revision 拒绝、重复确认幂等，失败整单回滚。 | worker、复盘等。 |
| P1-第 4 轮 | 独立 worker、单命令启动、提醒收件箱/可选系统通知、每日复盘。 | 拟运行 `uv run --group dev pytest -q tests/test_p1_worker.py tests/test_p1_review_ui.py` 并实际关闭浏览器、重启 worker：租约恢复、静默时段、提醒去重、复盘持久化通过。 | 恢复、用量、导出。 |
| P1-第 5 轮 | 有限重试与检查点、用量和估算成本、数据导出、完整使用说明。 | 拟运行全量 pytest、全部 AppTest、真实 DeepSeek 混合端到端及本机启动检查；按 `request.md` P1-1～P1-8 逐项通过。 | P2 保留不做。 |

本节保留原定验证门槛；实际接口、文件及测试结果以当前代码和 `code.md` 为准。

## 12. P2 增量骨架与编码轮次

依赖顺序：`自定义定义/权限/迁移 → 生成提案与只读执行 → 显式委派/并行 → 主动建议/策略 → MCP/日历/邮件 → 跨设备访问与通知`。继续使用五页面，不为扩展功能新增页面；页面只调用 `PersonalAIService`。每轮先运行 `uv run --group dev pytest -q`，结束后记录实测结果和下一轮任务。

| 轮次 | 本轮功能和代码落点 | 测试操作与通过结果 | 留到后续 |
| --- | --- | --- | --- |
| P2 第 1 轮 | `migrations.py` 增加版本和 `custom_agents`；`contracts.py`/`storage.py`/`services.py` 处理定义、版本编辑、启停和只读权限上限；现有 Agent 控制台提供入口。 | `uv run --group dev pytest -q tests/test_p2_custom_agents.py tests/test_p2_custom_agents_ui.py`：旧库备份且数据不丢，重启后可见配置，越权工具与旧版本编辑被拒，禁用状态生效，内置六 Agent 不变；全量旧测试通过。 | 模型生成、自定义 Agent 执行与委派。 |
| P2 第 2 轮 | DeepSeek 生成待审 Agent 提案；审核后手动只读执行，保存结构化结果、运行状态、Trace 与用量。 | 新增确定性故障/权限测试和 AppTest；真实 DeepSeek 只读运行有输出和 Trace，未审核/禁用 Agent 不运行，无业务写入。 | 自动委派与并行。 |
| P2 第 3 轮 | 用户显式选用自定义 Agent 的图校验与独立只读节点并行；稳定汇总和失败传播。 | 并发与依赖测试证明无依赖节点可并行，依赖节点等待，失败阻断下游；旧五/六 Agent 场景与审批回归通过。 | 主动建议和外部集成。 |
| P2 第 4 轮 | 根据已批准记忆和日常记录提出待审建议，保存接受/拒绝和可回退策略版本。 | AppTest 与确定性测试覆盖依据、去重、拒绝、回退及确认前零正式写入。 | MCP 与外部集成。 |
| P2 第 5 轮 | 受限 MCP 工具目录；在选定服务商后接入日历、邮件适配器及外部写入审批。 | 模拟工具超时/越权/重复请求；真实账号验收须由用户提供服务商和授权，确认前不得写外部数据。 | 跨设备访问与通知。 |
| P2 第 6 轮 | 确定部署与认证方案，做设备授权、跨设备视图和通知。 | 未授权访问拒绝；通知启用、撤销、去重、失败与未知结果均可复测；P0/P1/P2 全量回归。 | 后续版本另行确定。 |

以上第 1–6 轮是历史实施计划；真实服务与 iPhone 完整操作尚未通过验收。服务商与目标设备已选定，剩余工作按第 13 节推进。

## 13. P2 真实接入与总验收轮次（历史计划；执行结果见 `code.md`）

依赖顺序：`既有 P0/P1/P2 确定性基线 → iCloud EventKit → Gmail OAuth → 本地 MCP/YouTube → iPhone 安全读写与审批 → 手机通知/总验收`。每轮只通过 Service 访问业务数据，先复跑 `uv run --group dev pytest -q`，新增测试文件名为计划名称，不表示文件已存在；涉及 DeepSeek 的轮次做真实调用。服务或设备未授权时先完成模拟测试，真实验收明确留待授权后进行。

| 轮次 | 具体功能 | 计划测试与通过条件 | 留到后续 |
| --- | --- | --- | --- |
| 真实接入第 1 轮 | 绑定 macOS EventKit 日历适配器；在 Mac 日历中选择 iCloud 目标日历；读取、创建、修改事件及可预览的提前提醒，保留逐项审批、时区和未知结果处理。 | 新增 `tests/test_p2_icloud.py` 与相关 AppTest，运行全量旧测试；授权后实际读取目标日历，确认前 iCloud 无变化，确认后 Mac 与 iPhone 各出现一个正确事件并在 iPhone 验证提醒；修改冲突、重复确认及未知结果不重复写入。 | Gmail、MCP/YouTube、手机操作与通知。 |
| 真实接入第 2 轮 | 绑定 Gmail 适配器与桌面 OAuth；增加列表后按 ID 读取正文的只读操作，逐封预览并发送，令牌安全存储。 | 新增 `tests/test_p2_gmail.py` 与相关 AppTest，故障注入覆盖权限、超时、重复确认；用户授权后实测读取正文、由用户指定测试收件人确认一封测试邮件，收件箱/已发送结果可核对，未知结果不自动重发；全量旧测试通过。 | MCP/YouTube、手机操作与通知。 |
| 真实接入第 3 轮 | 在现有 MCP 目录上实现受限本地 `stdio` 客户端；绑定 YouTube 只读服务器和 `search_videos` / `get_video_details`；用户选择 YouTube 时供 Learning 路径推荐资料。 | 新增 `tests/test_p2_youtube_mcp.py` 与相关 AppTest，覆盖白名单、停用、超时、错误数据及配额；凭据可用时真实搜索并显示链接、时长和来源，DeepSeek 学习场景产生待审资料任务、审批前零正式写入；全量旧测试通过。 | iPhone 操作与通知；个人 YouTube 数据/字幕下载不在本轮。 |
| 真实接入第 4 轮 | Mac 继续运行应用；经私人 HTTPS 通道为 iPhone 提供受保护的核心查看、编辑与审批界面，复用现有设备撤销、Service、草案 revision 与外部写入审批。 | 新增 `tests/test_p2_mobile.py` 与相关浏览器测试；未认证/过期/撤销/CSRF/旧 revision 请求拒绝，重复确认幂等；用户完成私人连接后在真实 iPhone Safari 读取任务、编辑任务及逐项审批，原 Streamlit 端口仍不可从手机直达；全量旧测试通过。 | 普通任务手机推送、最终综合验收。 |
| 真实接入第 5 轮 | 保留应用内收件箱和已确认 iCloud 事件的手机日历提醒；按用户选定的 ntfy.sh 与通用横幅接入普通任务推送并逐设备开启；完成 P2-1～P2-7 综合验收。 | 通知适配器故障/去重测试和相关 AppTest；真实 iPhone 接收确认过的测试提醒，失败/未知/错过与重启状态准确；全量确定性测试、真实 DeepSeek、外部读写审批、手机撤销及旧 P0/P1 回归均通过。 | 无；未通过项继续修复，不宣称总验收完成。 |

实施时的日历系统授权、Gmail OAuth、YouTube API 凭据、Tailscale 设备接入和测试邮件目标确认均已完成；普通任务推送已选择 ntfy.sh 且仅显示通用提醒。各轮真实验收证据记录在 `code.md`，凭据不粘贴进文档或 Trace。
