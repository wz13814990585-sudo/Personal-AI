# Personal AI OS 实现设计（P0/P1 基线与 P2 方案）

第 1–8 节是 P0 历史设计，第 9 节是 P1 方案，第 10 节是 P2 实施方案；各轮实际状态见 `code.md`。首版的“自我学习”指用户审核后的长期记忆改变后续计划；不训练或微调模型。

## 1. 已确定的技术选择与边界

| 项目 | 方案 |
| --- | --- |
| 运行形态 | Python 3.12 单进程本地应用；Streamlit 提供五个页面，监听 `127.0.0.1`；不引入 API 服务、容器、消息队列或登录系统。 |
| Agent 基础 | Agno `Agent` 承担各角色的模型调用、工具调用与结构化输出；应用自建轻量 Harness，负责跨 Agent 的依赖调度、审批、权限和状态。首版不叠加 Agno Team/Workflow 运行时。 |
| 模型 | **本项目全部模型调用只使用 DeepSeek**：通过 Agno `DeepSeek` 适配器接入官方 API `https://api.deepseek.com`，默认模型 `deepseek-flash`；`DEEPSEEK_MODEL_ID` 仅允许在 `deepseek-flash`、`deepseek-v4-pro` 两个 DeepSeek 模型中选择。`DEEPSEEK_API_KEY` 从本机环境读取。后续阶段同样遵守此约束，除非用户另行变更。 |
| 持久化 | Python 标准库 `sqlite3` + 单个本机 SQLite 数据库；应用表是状态的唯一权威来源。P0 不另建 Agno 会话库或向量库。 |
| 契约与验证 | Pydantic 定义 Agent 输入输出、计划草案及工具参数；`pytest` 验证逻辑和端到端闭环；Streamlit AppTest 验证页面。 |
| 依赖管理 | 使用现有 `uv` 锁文件和 `agno`、`openai`、`streamlit`、`pydantic`、`pytest`；`openai` 在此仅为 DeepSeek 兼容接口的客户端库，模型请求发往 DeepSeek。P1 优先复用标准库和现有依赖。 |

本机时区默认 `Australia/Sydney`，用户可在设置中修改；数据库时间点统一存 UTC，输入、排程和展示按用户时区转换。云模型只接收当前步骤必需的目标、任务摘要、可用时段和已确认记忆。密钥不写入数据库、Trace 或页面。

模型工厂统一构造 `DeepSeek(id=DEEPSEEK_MODEL_ID, use_thinking=False)`；首版五个 Agent 使用同一默认模型及非思考模式，以控制延迟和工具调用复杂度。每个 Agent 设置 `output_schema` 与 `use_json_mode=True`，提示词明确要求 JSON 并给出字段示例；DeepSeek 的 JSON 模式只保证语法，不保证符合业务 schema，因此必须再做 Pydantic 类型和字段校验。空响应、截断、无效 JSON 或类型不符一律按失败处理。模型工厂和启动检查拒绝非 DeepSeek 模型 ID，不因 DeepSeek 故障自动切换其他提供商。当前 DeepSeek 官方模型目录将 `deepseek-v4-flash` 视为旧别名，因此本设计使用 `deepseek-flash` 正式 ID。

## 2. 组件与依赖

```text
Streamlit 五页面
    ↓ 仅调用应用服务
Application Service ──→ SQLite Repository
    ↓
Harness ──→ Orchestrator Agent：生成并校验执行图
    ├──→ Memory Agent：检索已确认记忆／提取反馈候选
    ├──→ Learning Agent：生成学习任务草案
    ├──→ Schedule Agent：提出时间安排
    └──→ Task Agent：规范化待办草案
             ↓
       Tool Gateway：身份、参数、权限、审批检查
             ↓
       Repository：事务写入业务数据、运行状态与 Trace
```

现有代码边界：`app.py`（入口与导航）、`personal_ai_os/contracts.py`（Pydantic 契约）、`storage.py`（建表与事务）、`agent_registry.py`（角色配置）、`agents.py`（Agno 构造）、`orchestrator.py`（计划图校验）、`harness.py`（状态与执行）、`tool_gateway.py`（受限工具）、`memory.py`（记忆策略）、`services.py`（UI 调用入口）、`ui/`（五页面）、`tests/`。P1 的增量文件与改动点见第 9 节和 `skeleton.md`。

依赖方向固定为 `UI → Service → Harness/Repository`，Agent 只能通过绑定身份的 Tool Gateway 使用工具；UI 不直接调用 Agent 工具或 SQL。业务数据由应用 Repository 统一读写，Agno 只负责单个 Agent 内部的模型与工具循环。由此避免两个框架各自维护一套运行状态。

## 3. 五个 Agent 的职责与权限

| Agent | 输入与输出 | P0 可用工具／权限 |
| --- | --- | --- |
| Orchestrator | 用户请求、目标摘要、Agent 能力目录 → `ExecutionPlan`：意图、步骤、依赖、预期结果；最终草案由 Harness 汇总。 | 只读 Agent 能力与目标摘要；不能修改任务、日程或记忆。 |
| Memory | 当前目标、反馈或规划关键词 → `MemoryContext` 或 `MemoryProposal`，包含来源 ID、相关性和理由。 | 检索已确认记忆、读取反馈；可提出候选记忆，不能自行批准或写入长期记忆。 |
| Learning | 目标、期限、相关记忆、现有任务 → `LearningPlan`，每项有标题、时长、优先级和理由。 | 只读目标、任务和已确认记忆；不能写入待办。 |
| Schedule | 学习任务、可用时段、占用时段、时间偏好 → `ScheduleProposal`，包含时段、依据或冲突。 | 只读日程、可用时间和已确认记忆；不能绕过程序的冲突检查。 |
| Task | 学习／日程结果 → `TaskDraft[]`，含可提交的标题、状态、时长、优先级、起止时间及来源。 | 规划时只读与规范化；审批后由 Harness 以 Task 身份调用 `commit_approved_plan`，一次事务写入待办和安排。任务完成状态的用户操作由同一任务域服务维护。 |

预置 Agent 的角色指令可在控制台编辑并持久化。每次运行保存所用配置版本；用户可在角色的**代码级权限上限**内收窄工具清单，不能通过编辑指令或界面扩大权限。新增自由 Agent 和动态赋权属于 P1/P2。

P0 工具注册表固定如下；每个工具均有 Pydantic 入参、返回值契约及代码级授权，Agent 不能访问任意 SQL、文件或网络：

| Agent | 已注册工具 | 写入边界 |
| --- | --- | --- |
| Orchestrator | `list_agent_capabilities`、`read_goal_summary` | 全部只读。 |
| Memory | `search_approved_memories`、`read_feedback` | 候选记忆由反馈运行结果交给 Service 保存为待审提案；无长期记忆写入工具。 |
| Learning | `read_goals`、`read_task_progress` | 全部只读。 |
| Schedule | `read_time_blocks`、`read_scheduled_tasks`、`read_settings` | 全部只读。 |
| Task | `read_tasks`、`commit_approved_plan` | `commit_approved_plan` 不暴露给规划阶段模型；只有用户确认后，Harness 携审批令牌以 Task 身份调用。 |

用户手动增删改查任务、编辑已批准记忆等操作由 Service 代表用户处理，走相同的参数与业务规则校验，并在审计事件中区分 `user` 与 Agent 行为。

## 4. 契约与协作流程

### 4.1 结构化契约

- `ExecutionPlan.steps[]` 至多包含 Memory、Learning、Schedule、Task 各一次；每步有 `step_id`、`agent`、`purpose`、`depends_on[]`。Orchestrator 自身作为运行首步计入 Trace。Harness 校验 Agent 名称、唯一 ID、无循环依赖、上限以及必须的前置关系；复杂学习排程固定满足 `Memory → Learning → Schedule → Task`。
- `AgentResult` 含 `step_id`、`status`、类型化 `payload`、供用户查看的简短理由与引用的业务 ID。若 Agno 返回未匹配 Pydantic 契约的内容，该步失败，不把自由文本当作有效计划。
- `TaskDraft` 含 `draft_item_id`、`title`、`estimated_minutes`、`priority`、`due_at`、`start_at`、`end_at`、`source_step_id`；可不排定具体时间，但有时间时必须满足 `start_at < end_at`。
- `PlanDraft` 含 `run_id`、`revision`、任务草案、冲突提示、引用的记忆 ID 和各 Agent 理由。用户编辑后 revision 递增，提交时使用 `run_id + revision` 校验，拒绝旧页面重复提交。
- 长期记忆分 `kind`、结构化 `value_json`、`source_feedback_id`、`status`、`created_at`、`updated_at`；`status` 为 `approved/deleted`。一个反馈可生成多个独立的 `MemoryProposal`，先保存在待审提案表，逐条批准时才进入长期记忆表。首版支持 `study_time_avoid` 等明确规则和普通文本偏好；“不要在早上安排学习”映射为用户当地时间 `00:00–12:00` 的学习排程避让规则。已批准记忆可在界面编辑或软删除，修改立即影响下次检索。

Agent 间不互发任意消息。Harness 从上一节点的已校验输出建立下一节点所需的最小上下文；每步以 `run_id/step_id` 关联结果。Orchestrator 可为简单请求选择子集，但涉及学习和日程的主验收场景必须调用全部五个角色。

记忆检索首版不依赖向量模型：先选当前目标相关的结构化规则，再用目标／任务标签及关键词匹配文本偏好，限制返回数量并附来源 ID；冲突规则先交用户处理。所有自然语言日期（如“明晚七点”）先结合当前时刻与用户时区解析为明确时间点，未能确定时不擅自生成已排期任务。

### 4.2 一次规划的状态机

```text
pending → running → waiting_approval → success
              ↘ failed          ↘ cancelled（用户拒绝）
```

1. Service 创建 `run_id` 和幂等请求键，保存原始请求、时区与配置快照，状态为 `pending`。
2. Harness 置 `running`，调用 Orchestrator，校验计划图，再按依赖顺序执行所需 Agent。每步开始、结果、工具调用和错误立即写入 Trace；前置失败时，依赖步骤标记 `skipped`。
3. Schedule Agent 提议时段后，程序按当地时区检查用户已设置的可用窗口、已占用时段、现有任务、期限和已确认的硬性时间规则。采用半开区间 `[start, end)`，仅当 `a.start < b.end 且 b.start < a.end` 时判定重叠，相邻任务可以首尾衔接。没有可用窗口、时间解析失败或没有合法时段时，返回明确原因并允许草案保持未排程；绝不虚构空闲时间或把冲突安排静默写入。
4. Task Agent 产出草案；Harness 汇总 `PlanDraft` 并置 `waiting_approval`。此时只保存运行数据与草案，不新增正式任务或已确认记忆。
5. 用户在界面修改任务、时段后确认。Service 再校验当前 revision；Task 的提交工具在一个 `BEGIN IMMEDIATE` 事务中重新检查时间冲突并写入任务和关联安排。`(source_run_id, draft_item_id)` 唯一约束和提交状态保证重复点击不会生成重复任务。成功后运行置 `success`；拒绝则置 `cancelled`。审批后的冲突反馈给用户重新编辑，不形成半提交。

任务直接 CRUD、目标和可用时间的人工编辑属于用户明确操作，由 Service 校验后写入；AI 生成的草案始终走确认门槛。任务标记完成后，用户可录入反馈；Memory Agent 在独立反馈运行中提出候选，保存在 `memory_proposals` 待审表。用户可逐条修改、批准或拒绝候选，批准后才写入 `memories`。已批准记忆也可由用户继续编辑或删除；下一次规划只检索当前有效的 `approved` 记忆，已删除和未确认记忆均不进入上下文。冲突记忆由界面提示用户选择，最新记忆不自动覆盖旧规则。计划中保存并展示所依据的记忆 ID 与人类可读解释。

反馈运行与规划运行共用状态及 Trace：提案生成后进入 `waiting_approval`，所有提案处理完毕后置 `success`；提取失败则置 `failed`，原始反馈仍保留。主验收场景先由用户设置可用时段，再检验模型是否把面试准备安排在期限之前；没有可用时段时应显示待安排原因。

### 4.3 权限、限额与失败

Tool Gateway 的 Agent 身份由 Harness 创建工具包装器时绑定，不能取自模型输出。调用先经 Pydantic 参数校验，再检查角色允许列表、代码级权限上限与审批令牌，最后执行工具并记录结果。审批令牌仅由 Service 在用户确认时生成，绑定 `run_id + revision`；模型无法自行创建。越权工具调用返回拒绝并使该步失败，业务数据保持不变。

P0 单次规划最多五个 Agent 步骤，每个 Agent 最多四次工具调用；模型请求超时 45 秒，总运行截止时间 240 秒。DeepSeek 鉴权失败、限流、超时、空／无效 JSON、工具异常或图校验失败均写入 `failed` 与可读错误；下游步骤跳过，不产生未审批业务写入。P0 不自动重试，也不切换提供商。进程重启后遗留的 `running` 步骤标记为 `failed`、错误原因标记 `interrupted`，保留 Trace；可恢复重试归 P1。Trace 记录真实状态、委派对象、工具名、经裁剪的参数和结果、耗时与错误；不展示模型隐藏推理或密钥。

## 5. 数据模型与一致性

| 表 | 关键字段和用途 |
| --- | --- |
| `settings` | 时区、作息与个人偏好。 |
| `goals` | 目标文本、类型、期限、状态。 |
| `tasks` | 标题、状态、优先级、预估时长、起止时间、目标 ID、`source_run_id`、`draft_item_id`；完成状态和人工修改。 |
| `time_blocks` | 用户维护的 `available/busy` 时间段；排程使用任务时间与占用时段共同判冲突。 |
| `agent_configs` | 五个固定角色的指令、可用工具子集、版本。 |
| `feedback` | 已完成任务 ID、用户原文、提交时间。 |
| `memory_proposals` | 反馈 ID、候选类型、结构化值、来源片段、`pending/approved/rejected` 审核状态；一个反馈可对应多条提案。 |
| `memories` | 已批准的长期记忆：类型、结构化值、来源反馈、`approved/deleted` 状态与更新时间。 |
| `runs` | `planning/feedback` 类型、会话 ID、用户请求、幂等请求键、运行状态、草案 JSON、revision、配置快照与起止时间。 |
| `run_steps` | `run_id`、Agent、依赖、`pending/running/success/failed/skipped` 状态、结构化结果与错误。 |
| `trace_events` | 可空 `run_id/step_id`、操作者 `user/agent/system`、时间、事件类型、脱敏摘要、工具、耗时、错误；无运行 ID 的用户操作也可审计。 |

数据库启用 `foreign_keys` 和 WAL。所有正式任务写入、冲突复查与运行状态更新使用同一事务；候选记忆批准和长期记忆写入也使用同一事务；失败时回滚。排程冲突通过事务内区间查询判断，SQLite 普通唯一约束不能代替时间重叠检查。业务时间用 ISO 8601 UTC 保存，时区偏好单独保存。删除记忆采用软删除以保留审计来源，检索接口只返回 `approved` 且未删除项。

## 6. 五页面交互与可观测性

| 页面 | P0 交互与真实数据来源 |
| --- | --- |
| 对话与今日计划 | 输入目标／请求；按会话 ID 展示历史请求和答复摘要；运行时显示当前 Agent、步骤和事件；展示可编辑草案、冲突及记忆依据；确认或拒绝；今日任务读取 `tasks`。后续请求只带入该会话最近三轮摘要和已确认记忆。 |
| 任务与日程 | 任务增删改查、完成与反馈；维护 `time_blocks`；显示计划时间和冲突。 |
| Agent 控制台 | 展示五个固定角色的说明、权限上限、当前配置版本、最近运行状态；编辑角色指令与允许的工具子集。 |
| 执行轨迹 | 按运行查看状态、依赖、委派、工具调用、耗时、错误及被跳过步骤，均读取 `runs/run_steps/trace_events`。 |
| 记忆与设置 | 管理目标、时区和作息；审核、编辑、批准、拒绝候选记忆；查看、编辑、软删除已批准记忆，并查看来源反馈和下次使用情况。 |

Streamlit 页面通过 Service 查询持久化状态；运行中的当前页面用 `st.status` 展示事件，其他页面重新查询数据库即可见最终状态。页面刷新或重启后，Trace 仍可查询。

## 7. 实施顺序与验证门槛

| 阶段 | 实现内容 | 通过条件 |
| --- | --- | --- |
| 0–2 小时 | 锁定依赖、契约、数据库迁移与演示输入；建立最小应用入口。 | 面试场景可表示成经校验的依赖图。 |
| 2–5 小时 | Repository、事务、用户 CRUD、时间冲突函数、Tool Gateway。 | 重启后数据保留；越权工具和重叠写入均被拒绝。 |
| 5–8 小时 | 五个 Agno Agent、Orchestrator 计划图与顺序调度。 | 复杂请求出现五个真实 Agent 步骤且结果可聚合。 |
| 8–11 小时 | Harness 状态、限额、失败处理、Trace、审批提交。 | 超时、无效输出、工具异常与重复审批均有正确状态和数据结果。 |
| 11–13 小时 | 反馈 → 候选记忆 → 审核 → 下次检索和排程避让。 | 早晨学习偏好在批准前无效、批准后改变排程并标注依据。 |
| 13–16 小时 | Streamlit 五页面和端到端操作流程。 | 用户可在 Web 界面完成计划、确认、反馈和记忆审核。 |
| 16–18 小时 | 确定性端到端测试、真实模型冒烟、启动文档和演示数据。 | `request.md` 的 P0-1 至 P0-10 逐项有通过证据。 |
| 18–24 小时 | 修复阻断问题，保持完整闭环。 | 单用户本机应用可重复运行主验收场景。 |

测试分两层：注入确定性 Fake AgentRunner 覆盖执行图、权限拒绝、原子提交、时间冲突、记忆批准／修改／删除、失败和重启；配置真实 `DEEPSEEK_API_KEY` 后，用“明晚七点有 AI Agent 面试，请制定学习计划、安排任务并记住我的学习习惯”运行一次 Agno/DeepSeek 端到端冒烟，并验证五 Agent Trace。Streamlit AppTest 检查五页面可进入、提交与数据库状态一致。真实模型结果可能有文案差异，验收结构、权限、状态与行为变化，不依赖固定措辞。还需断言五个 Agent 的模型请求均由 DeepSeek 适配器发出，非 DeepSeek 模型配置在启动时被拒绝。

按 `request.md` 的 P0 验收逐项落地：

| 需求 | 必须通过的检查 |
| --- | --- |
| P0-1 | 五个预置角色及其输入输出契约、权限上限可查询。 |
| P0-2 | 复杂面试请求有五个 Agent 的真实步骤、依赖顺序和汇总草案。 |
| P0-3 | 每个步骤的开始、成功、失败或跳过状态可从重启后的数据库读取。 |
| P0-4 | 越权写任务／记忆被 Tool Gateway 拒绝，Trace 有拒绝事件，业务表无变化。 |
| P0-5 | 偏好跨会话保留；已批准记忆修改或删除后，下次检索立即变化。 |
| P0-6 | 未批准提案不影响排程；批准“避开早上学习”后，在有替代时段时避开早晨并引用记忆 ID。 |
| P0-7 | 任务 CRUD、完成反馈、可用时段维护、学习任务生成和时间冲突检查均通过。 |
| P0-8 | 五页面通过真实 Service/SQLite 操作；执行轨迹展示委派、工具、状态和错误。 |
| P0-9 | 注入 DeepSeek 超时、空／无效输出、工具异常后，运行失败且无未经确认的任务写入。 |
| P0-10 | 确定性闭环测试、重启持久化测试和带真实 DeepSeek 密钥的主场景冒烟全部通过。 |

## 8. 风险、取舍与外部前提

- **模型接入**：真实模型调用仍依赖有效的 `DEEPSEEK_API_KEY` 和网络连通性。API 错误必须如实显示，不能用模拟输出伪装真实调用。
- **规划不稳定**：模型输出只作建议；Pydantic、执行图检查、工具权限和确定性日程检查是强制边界。若没有可用时段，保留未排程草案并说明原因。
- **状态一致性**：审批提交和冲突复查同一事务；`run_id + revision` 与唯一约束防重复；中断运行保留失败证据。单进程、顺序执行符合本机单用户 MVP。
- **记忆可靠性**：仅已批准记忆生效；来源、状态和修改时间可查；相互冲突的规则需用户处理，不能静默合并。
- **P0 范围**：本节描述首版取舍。后台提醒和可恢复运行纳入 P1；自由自定义 Agent、并行调度、MCP、外部日历和邮件仍留在 P2。

技术依据：[Agno Agent 运行](https://docs.agno.com/agents/running-agents)、[Agno 结构化输出](https://docs.agno.com/input-output/structured-output/agent)、[Agno 工具调用限制](https://docs.agno.com/tools/tool-call-limit)、[Agno DeepSeek 接入](https://docs.agno.com/models/providers/native/deepseek/overview)、[DeepSeek 当前模型名称](https://api-docs.deepseek.com/quick_start/pricing/)、[DeepSeek JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)、[Streamlit AppTest](https://docs.streamlit.io/develop/concepts/app-testing)。

## 9. P1 完整实现方案

### 9.1 架构与权限边界

沿用 `Streamlit → PersonalAIService → Harness/Repository` 与 Tool Gateway；全部模型调用仍经 DeepSeek 工厂。P1 保留五页面导航，在今日、任务、控制台和 Trace 页面增加日常管理区域。新增一个独立本机 worker 处理到期作业，与 Web 进程共用 SQLite，但各自使用短连接；拟以 `uv run --env-file .env python -m personal_ai_os start` 一条命令管理两进程，浏览器关闭不停止 worker。该命令退出或电脑关机时后台能力暂停；恢复后补处理。登录自启动和云推送不在 P1。

Agent 侧新增代码级权限受限的 **Life Agent**，输入生活目标、期限、现有生活事项和已批准偏好，输出有来源、时长和优先级的 `LifePlan`，只读且不能提交任务或批准记忆。Orchestrator 对学习、生活、混合请求分别路由 `Memory → Learning? → Life? → Schedule → Task`；混合请求最多六步（含 Orchestrator），仍顺序执行，依赖图和来源 ID 必须校验。`AgentRole`、`ExecutionPlan`、`AgentResult`、角色注册表与 Harness 输入/输出校验需同步扩展；Learning 与 Life 的任务 ID 加领域前缀后合并，Schedule 做程序化冲突复查，Task 仅生成草案。原五 Agent 面试路径及其权限测试必须保持通过。用户手动创建任务与其已确认的重复规则属于授权写入；新的 AI 任务、日程重排和记忆仍需用户审批。

### 9.2 数据模型与迁移

在现有 SQLite 库上做带 `PRAGMA user_version` 的版本迁移，将无版本号的 P0 库视为基线，先备份数据库，再逐版事务升级；迁移失败必须回滚且旧库可继续读取。现有 `agent_configs.role` 有五角色 `CHECK`，新增 Life Agent 时需安全重建该表并保留用户已编辑的五角色指令、工具子集和版本。`runs.kind` 仍使用现有 `planning/feedback`，不重建运行表；P1 草案在独立表关联规划 `run_id`。`tasks` 增加领域、来源规则和乐观版本字段；旧任务默认归 `general`，原 ID、状态、时间和来源保持不变。

| 新增存储 | 关键字段与一致性约束 |
| --- | --- |
| `recurrence_rules` / `recurrence_instances` | 规则 ID、标题、领域、每日/指定星期、当地时区、生效范围、状态和版本；实例唯一键 `(rule_id, local_date)`，关联生成任务。自然语言“每周三次”先生成三个建议星期，经用户选定后保存规则。 |
| `habits` / `habit_checkins` | 习惯目标和状态；打卡唯一键 `(habit_id, local_date)`，更正操作保留审计事件，习惯打卡不冒充任务完成。 |
| `daily_plan_proposals` | 本地日期、原因、来源运行、状态、revision、基线快照指纹、结构化新增/重排动作、冲突和新旧差异；一个触发键只产生一份待审草案。 |
| `scheduled_jobs` / `notifications` | 到期时间、类型、去重键、租约、尝试次数、最终状态；提醒收件箱与可选系统通知的发送状态分开保存。 |
| `daily_reviews` / `run_checkpoints` / `model_usage` | 复盘快照与用户补充、已校验步骤输出及输入指纹、真实模型用量和耗时；缺失用量保留空值。 |

重复规则只生成已确认规则允许的任务；生成与实例登记在同一事务中。每日生成窗口按规则的 IANA 当地日期确定，夏令时造成不存在或重复的墙上时间时以实际时区转换校验，无法确定就保留未排程并提示，不静默移动。历史实例不会因暂停或编辑规则被重复创建。用户时区变化须提示既有规则沿用其保存时区，修改规则才改变未来实例。

### 9.3 每日计划、重排与提交

每日计划先用确定性规则汇总现有学习/生活任务、重复实例、可用时段、占用、期限及已批准记忆；可选 DeepSeek 只为新目标拆解和解释提供建议，模型不可用时已有任务、提醒和打卡仍可运行。排程采用现有半开区间和冲突规则；未排程任务保留原因。`DailyPlanDraft` 动作明确区分 `create_task` 与 `reschedule_task`，展示旧时间、新时间、领域、依据和冲突。日程变更或逾期只产生待审重排建议，不静默覆盖已确认安排。

用户编辑草案使 revision 递增。提交绑定草案 ID 与 revision，在 `BEGIN IMMEDIATE` 中重新计算任务、时间段、规则和已批准记忆的基线指纹，复查期限、权限与冲突，原子执行全部动作和状态更新；指纹变化返回 `stale_plan`，同版本重复确认返回同一结果。自动生成的重复实例已有独立去重键，不经过 AI 草案提交。旧 P0 的 `run_id + revision` 审批语义保持不变。

### 9.4 Worker、提醒、复盘与恢复

worker 定期原子领取到期作业并写租约；进程异常后租约到期可再次领取。每日草案、重复实例、提醒和复盘分别有稳定去重键。恢复时只生成当前及未来窗口的重复实例；过去未运行日期记为跳过，避免成批制造陈旧待办。过期提醒记为错过并保留在收件箱，不在恢复时集中弹出。静默时段内尚未到期的提醒顺延到允许时间并保留原时间；应用内收件箱始终持久化。系统通知默认关闭，仅用户开启且本机支持时发送；外部通知结果不保证崩溃边界上的“恰好一次”，结果不确定时记录为 `unknown` 并让用户决定是否重发，不伪称已送达。每日复盘自由备注不直接写长期记忆；用户将备注关联已完成任务并提交反馈时，才复用 Memory Agent 的候选审核流程。

可恢复运行保留失败的原始 `run_id` 和 Trace；仅对输入指纹未变、结构化输出已校验的只读 Agent 步骤复用检查点，新尝试使用新运行 ID 并关联原运行。暂态超时/限流允许有上限的重试，格式错误和权限拒绝直接失败；任何任务提交都必须重新审批和事务复查。`model_usage` 记录可获得的 token 数、请求耗时、模型 ID；成本只在用户配置单价时显示为估算，不把估算当账单。导出经 Service 读取本机业务数据生成 JSON（任务可附 CSV），不包含 API 密钥或 `.env`，导出不写回数据库。

### 9.5 验证顺序与完成门槛

按 `skeleton.md` 的 P1 编码轮次实施，每轮先复跑 P0 全量测试，再运行新增确定性测试；涉及 Agent 的轮次加真实 DeepSeek 混合场景，涉及界面的轮次加 Streamlit AppTest，worker 轮次以关闭浏览器、重启进程和模拟时钟验证。最终按 [request.md](request.md) P1-1～P1-8 逐项给出运行命令、实际结果和持久化证据。任何 P1 失败不得破坏 P0 的五 Agent、审批、权限、记忆及原有数据；P2 的外部集成和动态 Agent 不进入本阶段。

### 9.6 主要风险与默认处理

| 风险 | P1 处理方式 |
| --- | --- |
| 旧库迁移损坏用户数据 | 迁移前备份、版本检查、事务升级、旧库样本回归；失败停止启动并保留备份。 |
| 混合 Agent 输出不合约或越权 | Pydantic 校验、工具白名单和图校验保持强制；运行失败留 Trace，不提交草案。 |
| 重复扫描或 worker 崩溃 | 数据库唯一键、事务领取、租约超时及幂等写入；模拟两 worker 竞争与重启。 |
| 夏令时、时区变化或过期提醒 | 以规则保存的 IANA 时区计算当地日期；无效时段明确未排程；过期提醒记录为错过。 |
| 用户日程在审批前改变 | 提交时重算基线指纹与冲突，草案过期则要求重新生成，不覆盖人工修改。 |
| 系统通知结果不确定 | 默认为应用内提醒；系统通知显式开启，结果未知时显示 `unknown`，不自动重复发送。 |

以上为 P1 设计约束；各轮实际完成内容及测试结果以 `code.md` 为准。

## 10. P2 实施方案

### 10.1 延续的边界与依赖

沿用 `Streamlit 五页面 → PersonalAIService → Harness/ToolGateway/Repository`、版本迁移和 DeepSeek 工厂。先建立可审计的自定义 Agent 注册表，再提供生成与只读执行，之后才让用户显式选用它参与依赖图与并行调度；主动建议与外部集成基于稳定的运行、审批和通知机制。内置六 Agent 的代码级权限上限与默认路径不受自定义配置影响。

### 10.2 自定义 Agent 与执行

新增独立 `custom_agents` 表：ID、唯一名称、描述、指令、只读工具子集、`active/paused`、版本和时间。迁移沿用 `PRAGMA user_version`、升级前 SQLite 快照备份与单事务回滚，不重建已有任务、记忆、运行或内置配置。Service 校验名称、长度、状态和工具子集；权限上限由代码常量定义，不能由指令、数据库或模型输出扩大。编辑与启停采用版本条件更新，审计 Trace 只记录 ID 和变更类型。首轮只管理定义，不将自定义 Agent 注册到旧 `AgentRole`、旧 ToolGateway 或 Orchestrator。

后续 DeepSeek 只能生成待审核的 Agent 提案；用户明确保存和启用后，手动执行使用独立的只读工具绑定、结构化输入输出及持久化运行/Trace。调度接入时由用户选择 Agent，校验依赖图、工具上限、超时和步数；仅互不依赖的只读节点并行，最终按图的稳定顺序汇总。计划、记忆和外部写入继续走既有审批或新增明确审批，不允许自定义 Agent 绕过。

### 10.3 主动建议、MCP 与外部服务

主动建议读取已批准记忆和可验证的任务、习惯、复盘数据，以来源 ID、规则版本和去重键保存待审项；用户接受、修改或拒绝均留审计，策略可回退。建议不会自动提交任务、改变日程或长期记忆。MCP 连接以服务器和工具为单位采用最小权限白名单；网络错误、超时、返回结构异常留 Trace，工具输出当作不可信数据。外部日历和邮件分别通过适配层接入，读取与写入权限分开；外部写入必须预览对象、目标账号及内容，确认后用本地幂等键记录尝试和不确定结果，不自动重发未知结果。

跨设备能力先验证部署、身份验证与传输保护，再扩展设备授权与撤销；默认仅本机访问。密钥保留在本机环境或系统安全凭据存储，不进入 SQLite 导出、Trace 或模型上下文。服务商虽已选定，未绑定真实适配器及完成授权前仍不得承诺可用性。

### 10.4 验证与风险

每轮先复跑 P0/P1 全量确定性测试和相关 AppTest；涉及 DeepSeek 时跑真实场景，涉及外部服务时用受控适配器故障注入再做用户授权的真实验收。重点验证升级备份和回滚、越权拒绝、禁用状态、旧版本编辑拒绝、失败不产生部分写入、并行依赖与顺序汇总、外部写入去重和密钥不泄漏。P2-1～P2-7 的交付状态逐项记在 `code.md`。

### 10.5 已选定服务的真实接入设计（已实现；验收证据见 `code.md`）

**iCloud 日历。** Python 服务沿用现有 `CalendarAdapter` 与外部写入草案；Mac 侧增加小型原生 EventKit 桥接，只访问用户在 Mac 日历中选定的 iCloud 日历。读事件、创建和修改在应用内分别授权；由于读取日历需要 macOS 的完整日历权限，操作边界仍由 Service 和外部工具白名单执行。扩展事件负载以支持可选的提前提醒分钟数，写入前显示日历、事件、当地时间、时区和提醒，确认时重读目标事件与时间冲突；保存外部事件 ID、尝试状态和来源。SQLite 与 iCloud 无法做同一事务，超时或崩溃造成的未知结果只供人工核对，不自动重发；在 iPhone 日历上验证同步与提醒。首版不删除事件。[Apple EventKit 权限](https://developer.apple.com/documentation/eventkit/accessing-the-event-store)

**Gmail。** 复用 `MailAdapter` 和逐项发送审批；使用用户指定账号的桌面 OAuth，授权后核对返回账号与用户指定地址一致，地址只在本机安全配置中保存。申请读取正文所需的 `gmail.readonly` 与发送所需的 `gmail.send`，两种操作在应用内独立启停。现有 `list_messages` 只负责分页列表，扩展按 ID 读取正文的只读 `get_message` 操作。Google 的读取授权覆盖整个邮箱且属于受限范围，因此应用默认分页展示近期邮件，只在用户选择时读取正文，并对邮件正文按不可信输入处理；不把整箱内容自动送给 DeepSeek。发送前复查账号、收件人、主题和正文，保存尝试与 Gmail 返回 ID；未知结果不自动重发。OAuth 凭据只存本机安全位置，日志、Trace 和导出不含令牌或邮件正文；授权失效或 Google 审核要求须如实处理。首版不处理附件、删除和批量发送。[Gmail 授权范围](https://developers.google.com/workspace/gmail/api/auth/scopes)

**MCP 与 YouTube。** 现有 `ExternalGateway` 只有目录、审批和适配器协议；新增固定启动命令的本地 `stdio` MCP 客户端与服务器连接，不执行用户输入的任意命令。连接时核对服务器身份、实际工具清单、输入结构与代码级只读上限，再与用户逐项启用的操作求交集；限制进程、超时、结果大小与调用次数，外部内容视为不可信。首个服务器用 YouTube Data API 提供 `search_videos`、`get_video_details` 两个只读工具，公共视频搜索使用独立 API 凭据，不复用 Gmail OAuth。用户在学习规划中选择 YouTube 来源后，Learning 路径才可检索；结果保存标题、频道、时长、链接、检索时间和来源 ID，显示推荐理由，视频进入正式学习任务仍走原计划审批。官方字幕下载要求拥有视频编辑权限，因此不能把任意第三方视频当作已读全文；没有实际正文时仅依据公开元数据推荐，不生成伪造的内容摘要。搜索设配额与缓存上限。[MCP 本地连接](https://ts.sdk.modelcontextprotocol.io/v2/serving/stdio)、[YouTube 搜索](https://developers.google.com/youtube/v3/docs/search/list)、[字幕权限](https://developers.google.com/youtube/v3/docs/captions/download)

### 10.6 iPhone 操作与通知设计（已实现；验收证据见 `code.md`）

应用仍在 Mac 上运行；推荐把独立手机 Web 入口经 Tailscale Serve 仅提供给用户已授权的 iPhone，保持后端监听 `127.0.0.1`，不使用公开 Funnel，也不暴露原 Streamlit 端口。保留现有设备签发/撤销能力，在私人 HTTPS 入口上加入短期会话、`Secure`/`HttpOnly`/`SameSite` Cookie、CSRF 与来源校验、请求频率限制和逐操作权限。手机界面只经 `PersonalAIService` 执行任务编辑、草案修改、`run_id + revision` 计划确认、每日草案审批、记忆审核及外部写入逐项确认；版本冲突、过期和撤销应明确拒绝，不能绕过原事务边界。设备丢失时在 Mac 本机撤销；恢复仍需 Mac 本机重新签发，不提供邮箱链接绕过设备授权。Mac 离线时不承诺可访问。[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)

通知保留应用内收件箱；对已确认的 iCloud 事件设置可预览的日历提醒，由 iPhone 日历接收。普通任务使用用户选定的 ntfy.sh：本机密钥按设备派生不可猜测主题，设备逐项开启；适配器固定发送通用标题和正文，不发送任务标题、邮件正文、记忆或凭据。worker 复用持久化提醒、静默时段、租约及 `delivered/failed/unknown/missed` 状态；未知结果不自动重发。服务端接受与 iPhone 实际送达分别验收。[iPhone 日历提醒](https://support.apple.com/en-ie/guide/iphone/iphdafdf98a1/ios)、[ntfy 手机客户端](https://docs.ntfy.sh/subscribe/phone/)

### 10.7 剩余真实验收与风险

每项先做无账号确定性测试、AppTest 和故障注入，再在用户本机授权后验证真实 iCloud 往返及 iPhone 同步、Gmail 读正文和逐封测试发送、YouTube 公开搜索及 DeepSeek 学习规划、iPhone Safari 编辑/审批与撤销、选定渠道的真实通知。外部服务写入与本地审批之间不能保证分布式原子提交；持久记录幂等键和未知状态，人工核对后才能决定是否再次操作。真实授权、手机连通与通知渠道缺一项时，对应 P2 验收保持未通过；P0/P1 和已通过的 P2 本机回归必须继续通过。
