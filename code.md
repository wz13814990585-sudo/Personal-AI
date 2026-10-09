# 开发记录

## 第 1 轮：数据与安全基础（2026-10-08）

**状态：已通过实际验证。** 已建立 Python 3.12／uv 项目、DeepSeek 专用配置、Pydantic 契约、11 张 SQLite 业务与运行表、目标／任务／时间段／设置 CRUD、五个固定 Agent 的配置与权限上限、Tool Gateway 拒绝和 Trace，以及确定性日程冲突检查。`commit_approved_plan` 在本轮尚不可执行，即使传入审批对象也不会写任务。

**运行与结果：**

- `uv sync --group dev`：成功，生成 `uv.lock` 和 `.venv`。首次受沙箱对 uv 缓存目录的限制，调整执行权限后重新运行成功。
- `uv run --group dev pytest -q tests/test_config.py tests/test_storage.py tests/test_schedule.py tests/test_permissions.py`：14 passed。
- `uv run --group dev pytest -q`：14 passed（最终复测）。
- 用临时 SQLite 执行“明晚七点 AI Agent 面试”基础数据样例：建立目标、可用时间和学习任务；重叠任务返回 `task_overlap`；Memory 越权提交返回 `role_not_allowed`；重新打开数据库后保留 1 个任务、5 个角色和 1 条拒绝 Trace。
- 在本机 `.env` 配置 `DEEPSEEK_API_KEY` 后，通过 Agno 的 `DeepSeek` 模型执行一次真实请求：使用 `deepseek-flash` 得到符合 Pydantic 契约的结构化响应（`structured_ok=True`）；随后复跑完整测试，仍为 14 passed。密钥保存在被忽略的 `.env`（权限 `0600`），`.env.example` 仅保留空白示例值。

**当时剩余：** 第 2 轮的五 Agent 调度、Harness 状态与完整执行 Trace；第 3 轮的计划审批提交和反馈记忆；第 4 轮的五页面与端到端验收。

## 第 2 轮：五 Agent 规划与可观测执行（2026-10-08）

**状态：已通过实际验证。** 新增唯一 DeepSeek 模型工厂、Agno AgentRunner、五角色结构化输出、执行图校验和顺序 Harness。运行及各步骤状态、依赖、委派、工具调用、错误、Agent 结果与配置版本快照保存到 SQLite。日程草案经程序检查可用时间、占用、现有任务、期限和时长；不合法安排保留为未排程草案。规划阶段只生成 `waiting_approval` 草案，不写正式任务。工具拒绝即使被 Agent 运行库吞掉，也使步骤失败。

**运行与结果：**

- 开始前复跑 `uv run --group dev pytest -q`：上一轮 14 passed。
- `uv run --group dev pytest -q tests/test_plan_and_state.py`：9 passed，覆盖五角色依赖、状态及持久化 Trace、超时、无效输出、越权、冲突安排、配置快照与中断恢复。
- `uv run --group dev pytest -q`：23 passed、1 skipped；默认跳过需要真实密钥和 API 用量的冒烟测试，上一轮测试全部保留通过。
- 以 `.venv/bin/python -` 运行内联脚本，从本机 `.env` 载入密钥、设置 `RUN_DEEPSEEK_SMOKE=1`，再调用 `pytest.main(['-q', 'tests/test_deepseek_smoke.py'])`：1 passed。真实 DeepSeek 请求产生五个成功 Agent 步骤、待确认任务草案和可查询 Trace，正式任务数为 0。一次早期试运行暴露了 Orchestrator 依赖图缺边；加强指令并保持图校验后，最终真实冒烟通过。

**当时剩余：** 第 3 轮实现草案编辑、用户审批、事务性任务提交、反馈与可审核长期记忆；第 4 轮实现五个 Streamlit 页面、启动说明及完整端到端验收。

## 第 3 轮：计划确认与反馈记忆闭环（2026-10-08）

**状态：已通过实际验证。** 增加用户操作 Service：编辑草案会递增 revision 并重新显示冲突；只有用户确认后，Harness 才以 Task 身份通过带运行 ID 和 revision 的审批上下文调用提交工具。提交在一个 SQLite 事务中复查来源、可用时间、占用、期限、已批准记忆及草案内任务冲突，全部成功才写任务并将运行置为 `success`；旧 revision、越权、重复确认和冲突均有明确结果。已完成任务可提交反馈，Memory Agent 生成待审提案；用户可编辑、批准或拒绝，已批准记忆可修改和软删除。规划只检索有效记忆，已批准的学习时段规则由程序在草案和提交时双重检查。

**运行与结果：**

- 开始前 `uv run --group dev pytest -q`：23 passed、1 skipped，确认上一轮通过。
- `uv run --group dev pytest -q tests/test_commit_and_failure.py tests/test_memory_loop.py`：11 passed。覆盖 revision、审批令牌、幂等、整单回滚、编辑后冲突、反馈资格与失败、多候选审核、重启后记忆生效及编辑/删除、“早上不学习”规则。
- `uv run --group dev pytest -q`：34 passed、2 skipped；默认跳过真实 API 测试，前两轮测试无回归。
- 以 `.venv/bin/python -` 从本机 `.env` 加载密钥并设置 `RUN_DEEPSEEK_SMOKE=1`，调用 `pytest.main(['-q', 'tests/test_deepseek_smoke.py'])`：2 passed。真实 DeepSeek 的五 Agent 规划及反馈记忆提案均通过；测试使用临时数据库，不输出密钥。
- 面试准备样例的确定性闭环验证：确认前正式任务为 0；确认后任务入库；反馈“不要在早上安排学习”形成待审提案，待审时下次计划仍可排早上；批准并重启后新计划改到下午并标明记忆 ID；修改或删除记忆后，新规划立即反映变更。即使 Schedule Agent 建议早上，已批准规则仍阻止该安排。

**剩余：** 第 4 轮实现五个 Streamlit 页面、启动说明和通过界面操作的完整端到端验收。真实 DeepSeek 冒烟目前分别覆盖规划和反馈；通过 Web 界面串联全部步骤及最终 P0-1～P0-10 总验收仍待完成。

## 第 4 轮：五页面与 P0 闭环验收（2026-10-08）

**状态：已通过实际验证。** 新增本机 Streamlit 五页面与 Service 查询／用户 CRUD：对话和今日计划支持最近三次答复摘要、运行时真实事件、草案编辑、冲突显示及按 revision 确认；任务与日程支持任务和时间段维护、完成与反馈；Agent 控制台展示五角色职责、输入输出契约、权限、配置版本与最近步骤状态；执行轨迹读取真实运行、依赖、工具事件和错误；记忆与设置支持目标、时区、偏好以及候选／已批准记忆的逐条处理。页面只调用 `PersonalAIService`，SQLite 仍是状态来源。后续会话只向 Orchestrator 提供同会话最近三轮简要摘要。补充 `README.md` 的安装、DeepSeek 配置、启动与验收步骤。

**运行与结果：**

- 开始前 `uv run --group dev pytest -q`：34 passed、2 skipped，前三轮复测通过。
- `uv run --group dev pytest -q tests/test_ui.py`：4 passed。AppTest 验证五页面进入、真实存储显示、计划编辑与冲突拦截、确认、完成与反馈、候选修改和批准、记忆修改和删除、再次规划、失败 Trace，以及人工目标／时区／任务／时间段／Agent 配置操作。
- `uv run --group dev pytest -q`：38 passed、3 skipped，全部旧测试与本轮确定性测试通过；3 项真实 API 测试默认跳过。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_deepseek_smoke.py`：最终 3 passed。真实 DeepSeek 主场景用“明晚七点有 AI Agent 面试，请制定学习计划、安排任务并记住我的学习习惯”完成五 Agent 草案、用户确认、任务完成、反馈、记忆审核和二次规划；二次草案引用记忆 ID，并在下午可用时段安排任务。此前一次真实试运行遇到 Orchestrator 无效 JSON，系统如实置 `failed` 且未写任务；修正 JSON 指令后复跑全组通过。模型输出仍可能偶发不合约，按设计保留失败 Trace，需用户重新发起运行。
- `uv run --env-file .env streamlit run app.py --server.headless true --server.address 127.0.0.1 --server.port 8502`：本机启动成功；`curl -fsS http://127.0.0.1:8502/_stcore/health` 返回 `ok`，随后停止测试服务。

**P0 逐项核对：**

| 条目 | 通过证据 |
| --- | --- |
| P0-1 | 五角色注册、职责、输入输出契约和权限在控制台可见；AppTest 页面通过。 |
| P0-2 | 确定性及真实主场景均产生五 Agent 依赖步骤和草案；Trace 有委派记录。 |
| P0-3 | 运行与步骤状态存入 SQLite；旧状态测试及失败页面测试通过。 |
| P0-4 | 越权工具拒绝、Trace 与业务数据不变由 `test_permissions.py` 和旧 Harness 测试验证。 |
| P0-5 | 记忆和偏好重启后保留，编辑／删除立即影响检索；`test_memory_loop.py` 与 AppTest 通过。 |
| P0-6 | 待审候选不生效；批准晨间避让后下午安排并引用记忆 ID；确定性及真实主场景通过。 |
| P0-7 | 任务和时间段 CRUD、完成、学习任务、冲突检查与重启读取由旧测试及 AppTest 通过。 |
| P0-8 | 五页面 AppTest 通过，计划冲突、真实状态／依赖／工具事件／错误、记忆依据可见。 |
| P0-9 | 超时、无效输出、工具异常置失败且无未经确认写入；旧故障注入测试及页面失败测试通过。 |
| P0-10 | 确定性全量 38 passed，真实 DeepSeek 三项 3 passed，本机 Streamlit 健康检查 `ok`。 |

**剩余 P0：无；进入总验收。** P1/P2 尚未开发。

## 后续版本需求登记（2026-10-08，未开发）

用户希望平台持续管理日常学习与生活。已在 `request.md` 将混合目标规划、重复任务、习惯打卡、每日计划与复盘、变化后的重排建议、本机后台提醒列为 P1 优先目标，并在 `design.md` 记录数据、Agent、后台作业、审批边界及验收顺序。外部日历和跨设备通知仍属 P2。本次仅更新规划文档，未修改代码或改变第 4 轮测试结论。

## P1 开发前设计更新（2026-10-08，未编码）

已将 P1-1～P1-8 的需求与可测试验收写入 `request.md`，把六角色混合规划、旧库版本迁移、重复任务/习惯、每日草案与重排事务、本机 worker、提醒、复盘、可恢复运行、用量与导出写入 `design.md`，并在 `skeleton.md` 拆成五个按依赖排序的编码轮次。`README.md` 标明 P1 仍是规划。此项为文档更新，未运行代码测试；上一条记录的 P0 测试结果是上次编码验收结果，不代表 P1 已实现。

## P1 第 1 轮：迁移、重复规则与习惯（2026-10-08）

**状态：已通过实际验证。** 在启动时用 `PRAGMA user_version` 识别 P0 无版本库，升级前以 SQLite 快照备份，逐项事务迁移到 v1；故障注入验证回滚且旧数据仍可读取。旧任务、记忆、运行、步骤、Trace 和五个 Agent 的已编辑配置均保留。任务新增领域、来源规则及版本字段；新增重复规则／实例与习惯／打卡表。

用户可在“任务与日程”确认、修改、暂停／恢复每天或指定星期的规则，并手动扫描未来七天；规则按自身 IANA 时区生成，`(rule_id, local_date)` 唯一且任务与实例同事务写入。重启、竞争扫描及再次扫描不会重复创建；无法排程时保留任务并显示原因，夏令时缺失或重复的当地时间不会被静默移动。任务编辑与状态修改会递增版本。在“记忆与设置”可创建／修改／暂停习惯，按习惯时区日期打卡、同日更正和查看本周进度；更正写入用户 Trace。两个页面只经 `PersonalAIService` 读写，仍是原五页面。

**运行与结果：**

- 编码前 `uv run --group dev pytest -q`：38 passed、3 skipped，P0 基线通过。
- `uv run --group dev pytest -q tests/test_p1_migration_recurrence.py tests/test_p1_habits_ui.py`：9 passed。覆盖备份保真与失败回滚、五角色配置、每日／指定星期、暂停恢复、重启与并发去重、规则时区与夏令时缺失／重复时间、无可用时段和任务冲突、规则修改后历史实例保持、习惯更正与审计、Streamlit AppTest 增改停规则和打卡。
- 最终 `uv run --group dev pytest -q`：47 passed、3 skipped。全部 P0 旧测试、新确定性测试和 AppTest 均通过；3 项跳过的是需真实 DeepSeek 调用的既有冒烟测试，本轮无模型调用改动。

**后续：** P1 第 2 轮新增受限 Life Agent 与混合学习／生活规划；每日计划、重排、worker、提醒、复盘、恢复、用量和导出分别留在之后轮次。本轮规则扫描由页面手动触发，后台自动扫描尚未实现。

## P1 第 2 轮：Life Agent 与混合规划（2026-10-08）

**状态：已通过实际验证。** 增加只读 Life Agent，代码级工具上限只包含读取目标、任务进度、已批准记忆和设置；提交任务仍须用户经 Service 确认。Orchestrator 对纯学习沿用原五 Agent 路径，对纯生活使用 `Memory → Life → Schedule → Task`，对混合请求使用 `Memory → Learning → Life → Schedule → Task`；加上 Orchestrator 最多六步。Harness 校验依赖图、学习／生活 item ID、Schedule 引用和 Task 草案的领域、来源步骤、时长及优先级。混合草案展示任务领域、来源、时长、优先级与来源理由，确认前不写正式任务；确认后事务提交到带领域的待办。旧五角色配置通过 v1→v2 事务迁移保留，迁移前备份，失败回滚；首次注册 Life 不覆盖用户已编辑的五角色配置。

**运行与结果：**

- 编码前 `uv run --group dev pytest -q`：47 passed、3 skipped，P0 和 P1 第 1 轮基线通过。
- `uv run --group dev pytest -q tests/test_p1_life_plan.py`：6 passed、1 skipped（真实模型测试默认跳过）。确定性覆盖混合六步、纯生活五步、审批前零写入、确认后两类任务持久化、越权拒绝、非法来源／依赖拒绝、旧库配置保留与迁移回滚、现有页面的混合草案展示和确认。
- 最终 `uv run --group dev pytest -q`：53 passed、4 skipped；全部旧确定性测试与新 AppTest 通过。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p1_life_plan.py -k real_deepseek_mixed_study_life_plan`：最终 1 passed、6 deselected，真实 DeepSeek 生成六角色学习／生活混合草案，正式任务数保持 0。初次真实试运行因 Task 来源字段不匹配失败，另一次因 Schedule 返回非单一 JSON 失败；两次均留下失败状态与 Trace，未写待办。补充精确来源表和单对象输出指令后，最终代码实测通过。模型输出仍可能偶发不合约，按设计失败并允许重新发起。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_deepseek_smoke.py -k five_agent_planning`：1 passed、2 deselected，原真实五 Agent 面试规划保持通过。

**后续：** P1 第 3 轮统一今日视图、每日草案与事务性重排审批；worker、提醒、复盘、恢复、用量及导出仍属之后轮次，本轮未实现。

## P1 第 3 轮：统一今日视图与每日草案审批（2026-10-08）

**状态：已通过实际验证。** 原“对话与今日计划”页面加入统一日期视图，展示当天及跨午夜的学习／生活任务、到期的未排程事项、重复实例及原因、可用／占用时段、按习惯自身时区计算的打卡进度。未来日期的重复实例不会提前排入今天。确定性排程按实际 UTC 时间寻找当地日期内的合法时段，复用可用时间、占用、任务重叠、期限和已批准学习避让规则；无合法时间保留明确原因。

每日草案使用新增 v3 表持久化，升级前备份并在事务内迁移。草案动作区分 `create_task` 与 `reschedule_task`，展示原／新时间、领域、来源、优先级、时长、依据和冲突；页面可删除或编辑建议，并添加用户新增任务或既有任务重排。生成与编辑只改草案，不改正式任务。每次编辑递增 revision；用户确认时以草案 ID、revision 和任务／时间段／重复规则／已批准记忆的基线指纹为条件，在 `BEGIN IMMEDIATE` 中重新校验来源、期限、权限和全部时间冲突，再整单提交。旧 revision 或基线变化拒绝；同版本重复确认返回原结果；中途失败全部回滚。P0 的 `run_id + revision` 审批流程未改动。

**运行与结果：**

- 编码前 `uv run --group dev pytest -q`：53 passed、4 skipped，P0 与 P1 前两轮基线通过。
- `uv run --group dev pytest -q tests/test_p1_daily_plan.py tests/test_p1_daily_ui.py`：18 passed。覆盖未确认前零改动、学习／生活无重叠排程、无可用时间和编辑后冲突、任务／时间／规则／记忆变化后的过期拒绝、旧 revision、重复确认、故障注入整单回滚、用户来源约束、既有任务重排、旧库 v2→v3 备份与失败回滚、未来重复实例筛选、夏令时实际时长、跨时区习惯日期、跨午夜任务，以及 Streamlit AppTest 的查看、编辑、添加和确认流程。AppTest 初次发现空期限被当作字符串解析导致页面错误，已修复并复测通过。
- 最终 `uv run --group dev pytest -q`：71 passed、4 skipped；全部旧确定性测试与本轮 AppTest 通过。4 项跳过的是需要真实 DeepSeek API 的既有测试，本轮未改动 Agent 模型调用。

**后续：** P1 第 4 轮实现独立 worker、单命令启动、提醒收件箱／可选系统通知及每日复盘。当前重复规则仍需手动扫描，提醒和后台作业尚未实现；恢复、用量和导出仍在第 5 轮。

## P1 第 4 轮：本机 worker、提醒与每日复盘（2026-10-09）

**状态：已通过实际验证。** SQLite 升至 v4，迁移前备份且故障回滚；新增持久化 `scheduled_jobs`、租约、去重键、任务提醒、应用内通知和每日复盘。单命令启动 Web 与独立 worker。worker 按规则当地日期扫描重复任务，先生成实例再生成每日草案；当地 21:00 创建复盘。过期提醒保留为 `missed`，静默时段将未过期提醒延后，系统通知默认关闭，仅明确开启且本机支持时尝试。失败、超时或崩溃边界分别保留 `failed`、`unknown`，不自动重发不确定结果。复盘备注可编辑、跨重启保留，不直接写记忆；关联任务反馈复用原人工审核流程。五页面未增加新页面，界面只经 Service 读写。

**运行与结果：**

- 编码前 `uv run --group dev pytest -q`：71 passed、4 skipped，P0 和前三轮 P1 基线通过。
- `uv run --group dev pytest -q tests/test_p1_worker.py tests/test_p1_review_ui.py`：6 passed。覆盖租约到期接管、重复规则与每日草案去重、静默时段与任务已开始边界、重启后的错过提醒、系统通知成功／失败／超时未知／不支持、崩溃后不重复发送、v3→v4 备份与回滚、自动复盘与修订持久化、Streamlit 页面入口及收件箱。
- `uv run --group dev python - <<'PY' ... PY`：以临时 SQLite 和随机端口实际启动 `python -m personal_ai_os start --poll-seconds 0.2`；Web 健康检查 200，关闭 HTTP 连接后 worker 仍记录 `delivered`；停止进程、创建过期任务并重启后记录 `missed`；再次扫描仍只有 2 条通知。脚本未调用模型，也未读取 API 密钥。
- 最终 `uv run --group dev pytest -q`：77 passed、4 skipped。4 项为需真实 DeepSeek 的旧冒烟测试；本轮未改动 Agent 模型路径。曾有测试夹具缺少可用时间、以及自动每日草案新增后作业顺序预期不符，均修正并重测通过。

**后续：** P1 第 5 轮的安全检查点复用、有限重试、真实用量和可选估算成本、本机数据导出及 P1 总验收；P2 不在本轮。

## P1 第 5 轮：运行恢复、真实用量与本机导出（2026-10-09）

**状态：已通过实际验证。** SQLite 事务迁移至 v5，升级前备份，失败回滚并保留原数据。失败规划可由用户发起关联的新运行；已校验的只读 Agent 步骤仅在输入、角色配置、模型和业务读取快照指纹一致且重新通过契约/业务校验时复用。原失败运行、步骤和 Trace 不覆盖；检查点损坏会拒绝复用。暂态超时或 429 每步最多三次尝试，格式错误及权限拒绝不自动重试。重试得到的计划仍需按新 `run_id + revision` 审批，反馈记忆仍需逐条审核。

每次实际 DeepSeek 请求（含工具轮次及失败请求）记录模型、耗时与可获得的 token；缺失值单独统计。成本仅在用户填写该模型每百万输入/输出 token 单价时按已知用量估算，不冒充账单。通过 Service 提供只读业务 JSON 和任务 CSV 导出；导出排除环境密钥、模型单价和内部运行数据。五页面仅增加失败重试、用量/估算与下载入口；`README.md` 补齐日常使用、后台、恢复、导出及验收命令。P2 未实现。

**运行与实际结果：**

- 编码前 `uv run --group dev pytest -q`：77 passed、4 skipped，P0 和前四轮 P1 基线通过。
- `uv run --group dev pytest -q tests/test_p1_recovery_usage_export.py tests/test_p1_recovery_ui.py`：最终 9 passed。验证指纹复用与状态变化失效、损坏检查点拒绝、429 有限重试、格式错误不重试、反馈重试仍须审核、真实请求计数与缺失 token、可选价格、导出不含密钥且只读、v4→v5 备份回滚，以及 Streamlit 重试/用量/下载入口。一次新增测试的预期过严：Memory 检查点损坏后，Learning 输入未变化仍可安全复用；修正断言后通过。
- `uv run --group dev pytest -q`：最终 86 passed、5 skipped；全部 P0/P1 确定性测试和相关 AppTest 通过。5 项跳过的是需显式启用的真实 DeepSeek 测试。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_deepseek_smoke.py tests/test_p1_life_plan.py tests/test_p1_final_smoke.py -k 'real_'`：首次 4 passed、1 failed、6 deselected。原五 Agent 三个真实场景和新增混合端到端通过；旧混合冒烟的一次 Learning 响应不是合法 JSON，运行如实失败且未提交任务。旧冒烟改为验证失败状态后显式调用 `retry_run`；`RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p1_life_plan.py -k real_deepseek_mixed_study_life_plan` 随后 1 passed、6 deselected。真实模型格式偶发不合约仍保留失败记录，不对格式错误进行无条件自动重试。新增真实混合端到端覆盖六 Agent、确认后写任务、反馈、人工批准记忆、再次规划和真实用量；至少 13 次实际请求写入数据库，未配置单价时无估算成本。
- `uv run --group dev python - <<'PY' ... PY`：临时 SQLite、随机本机端口启动 `python -m personal_ai_os start --poll-seconds 0.2`；Web `/_stcore/health` 返回 `200 ok`，主进程与 worker 仍运行且数据库已建立，随后正常停止。前一轮已有关闭浏览器仍处理提醒、重启补处理及去重的实际进程测试。

**P1 逐项核对：**

| 条目 | 实际通过证据 |
| --- | --- |
| P1-1 | `test_p1_life_plan.py` 确定性混合六 Agent、生活来源/时长/优先级及确认前零写入；旧五 Agent 三项真实冒烟与真实混合规划通过。 |
| P1-2 | 重复规则、暂停恢复、当地日期去重、重启、夏令时及习惯更正的测试通过；但“每周三次”由 Life Agent 提议三个具体星期几并直接供用户确认成规则，尚无结构化建议和验收测试。 |
| P1-3 | `test_p1_daily_plan.py`、`test_p1_daily_ui.py` 覆盖统一今日视图、无重叠排程、未排程原因、确认前不改任务；全量测试通过。 |
| P1-4 | 现有测试验证未排程任务的建议、草案 revision、过期拒绝、幂等、冲突复查和整单回滚；逾期但已排程任务及可用时间变更后的重排触发未通过下面的补充核查。 |
| P1-5 | `test_p1_worker.py` 的租约、重启、去重、静默时段及通知结果测试通过；本轮单命令 Web/worker 启动检查通过，前轮已实际验证关闭浏览器后 worker 继续工作。 |
| P1-6 | `test_p1_review_ui.py` 与记忆闭环测试验证复盘持久化、备注不直接入记忆、反馈候选仍须审核；全量测试通过。 |
| P1-7 | 本轮恢复测试验证原失败 Trace 保留、只复用同指纹已校验只读步骤、损坏拒绝、有限重试及重新审批；迁移失败保留旧库。 |
| P1-8 | 本轮用量/导出测试与真实混合端到端验证实际请求与 token 记录、缺失值、单价可选估算、无密钥 JSON/CSV 及只读导出；AppTest 入口通过。 |

**总验收补充核查及剩余问题：** 使用 `uv run --group dev python - <<'PY' ... PY` 分别运行两个临时 SQLite 业务样例。昨天已排程而未完成的任务，在今天有可用时段时调用 `propose_daily_plan`：预期至少一个 `reschedule_task`，实际 `suggested_actions=0`。删除包含已排程任务的旧可用时段：预期能保存变化并得到待确认的重排建议，实际抛出 `scheduled_task_outside_availability`，没有建议。P1-2 的“每周三次”目前只有 Life Agent 提示词中的文字建议和独立的手动规则表单，缺少经验证的三个具体星期几建议到规则确认的连续流程。以上不属于第 5 轮编码范围，本轮恢复、用量和导出已通过，但 **P1-2 与 P1-4 尚未通过总验收**；不能宣布“进入总验收”。P2 未实现。

## P1 总验收补缺：星期建议与已排程重排（2026-10-09）

**状态：已通过实际验证。** 修改前全量确定性基线为 86 passed、5 skipped。针对 P1-2，平台从已校验的 Life 任务和“每周三次运动”请求生成周一、周三、周五的结构化建议；在现有“对话与今日计划”页面可改选三个星期并显式确认。建议从已保存的草案重建，跨重启可查看；确认绑定规划 `run_id + revision`，旧版本、非法星期和已拒绝计划不能创建规则。规则 ID 按运行和来源项确定，重复确认不创建第二条规则；确认前无规则或正式任务写入。原计划任务仍由既有审批流程单独确认。

针对 P1-4，每日草案现在识别逾期已排程但未完成、或原安排不再符合可用时间的任务，展示原时间、新时间、来源、理由及无合法时段的冲突。用户经 Service 修改可用时间、增加可用时段或完成任务时，自动生成相关日期的待确认草案；既有任务时间在确认前不变。Repository 默认仍拒绝破坏旧日程约束的低层写入；仅用户经 Service 明确修改可用时间时允许暂存该变化，并引导审核重排。草案沿用 revision、基线指纹、事务冲突复查、幂等提交和失败整单回滚。合法的跨午夜旧安排不会因只查看其中一天而被误判为需重排。五页面和原 Agent 权限边界未变，未实现 P2。

**运行命令与实际结果：**

- 编码前 `uv run --group dev pytest -q`：86 passed、5 skipped，满足旧功能基线。
- `uv run --group dev pytest -q tests/test_p1_acceptance_gaps.py tests/test_p1_acceptance_ui.py`：9 passed。预期涵盖三天建议审核、revision 与重复确认、当地日期实例去重、逾期重排、可用时间变更、任务完成/新时段触发、无合法新时段、跨午夜不误报，以及两个现有页面的真实 Service 操作；实际均通过。
- `uv run --group dev pytest -q`：95 passed、5 skipped。曾发现一次旧 P0 Repository 测试因直接放宽可用时间校验而失败；改为仅经用户 Service 操作开启待重排路径，低层默认约束复原后全量通过。5 项跳过的是需显式启用的真实 API 测试。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p1_final_smoke.py tests/test_p1_life_plan.py -k 'real_'`：2 passed、6 deselected。真实 DeepSeek 六 Agent 混合规划中生成三星期建议，用户确认规则后仍须单独确认任务；反馈、记忆审核与再次规划继续通过。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_deepseek_smoke.py`：3 passed，原五 Agent 面试、反馈与记忆再规划场景通过。
- `uv run --group dev python - <<'PY' ... PY`：临时库与随机本机端口启动 `python -m personal_ai_os start --poll-seconds 0.2`；Web 健康检查 `200 ok`，主进程和 worker 运行、SQLite 建立，随后正常停止。

**P1-1～P1-8 总验收：** P1-1 的真实六 Agent 混合规划及旧五 Agent 回归通过；P1-2 的三天建议确认、重复实例和习惯测试通过；P1-3 的统一视图与待审每日草案通过；P1-4 的逾期、可用时间/完成状态变化、差异、revision、冲突及事务测试通过；P1-5 的 worker、租约与提醒测试及实际启动通过；P1-6 的复盘/人工记忆审核测试通过；P1-7 的检查点/有限重试/再审批测试通过；P1-8 的真实请求统计、可选估算及无密钥导出测试通过。剩余 P1 缺口：无。P2 保留后续版本。

## P2 开发前复核与第 1 轮：自定义 Agent 定义（2026-10-09）

**基线复核：** 编码前 `uv run --group dev pytest -q` 为 95 passed、5 skipped。真实 DeepSeek 首次复跑出现无效 JSON、错误任务 ID 和把不可用时间当成可用时间；运行被正确标为失败或草案未排程，未发生未经批准的任务写入。为改善真实规划，Schedule 输入增加精确的当地可用窗口及合法任务 ID，提示词要求逐字复制 ID 并只在窗口内安排。随后 `uv run --group dev pytest -q` 为 95 passed、5 skipped；`RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p1_final_smoke.py tests/test_deepseek_smoke.py` 为 4 passed。真实模型仍可能产生不合约输出；既有失败记录和用户重试机制保留。经此复核，P0/P1 无阻断 P2 的已知缺口。

**文档：** `request.md` 增加 P2-1～P2-6 的产品需求与可测验收，`design.md` 确定权限、迁移、模型和外部写入边界，`skeleton.md` 拆成六个依赖排序轮次。外部服务商、MCP 服务器和跨设备部署方案留待对应轮次确定。

**本轮实现：** SQLite schema 升至 v6，迁移前备份且失败回滚；独立 `custom_agents` 表保存名称、描述、指令、只读工具子集、启停状态、版本和时间。新增严格输入契约、代码级只读白名单、Service 创建/编辑/启停及版本条件更新；现有 Agent 控制台提供操作入口，默认创建为暂停。界面仅经 Service 读写。自定义 Agent 尚不能运行，不参与内置六 Agent 图，也不能通过指令或配置获得写工具。

**运行命令与实际结果：**

- `uv run --group dev pytest -q tests/test_p2_custom_agents.py tests/test_p2_custom_agents_ui.py`：4 passed；覆盖 v5→v6 备份、故障回滚、旧任务/记忆/运行及六角色配置保留，越权拒绝、重复名称、旧版本拒绝、重启持久化、内置路径回归和控制台创建/编辑/启停。
- `uv run --group dev pytest -q`：最终 99 passed、5 skipped；包含当地可用窗口和合法任务 ID 的回归断言。5 项为默认跳过的真实 DeepSeek 测试。
- `uv run --group dev python - <<'PY' ... PY`：临时数据库、随机本机端口实际启动 `python -m personal_ai_os start`；Web 健康检查 `200`，双进程主启动保持运行，SQLite 版本为 6 且自定义 Agent 表存在，随后正常停止。

**剩余：** P2-1 的实际执行前禁用检查和 P2-2 的 DeepSeek 生成提案、手动只读运行、结构化结果、Trace/用量进入第 2 轮。显式委派/并行、主动建议、MCP 与外部集成、跨设备能力仍按 `skeleton.md` 后续轮次推进；本轮不宣称 P2 全部完成。

## P2 第 2 轮：提案审核与手动只读运行（2026-10-09）

**状态：已通过实际验证。** 编码前 `uv run --group dev pytest -q` 为 99 passed、5 skipped。沿用原五页面与 Service 边界，数据库升级至 v7，升级前自动备份、单事务迁移；独立保存自定义 Agent 的待审提案、手动运行、Trace 和真实 DeepSeek 请求用量。启动时遗留的运行中记录标为 `interrupted` 并保留轨迹。

DeepSeek 根据用户目标生成结构化 Agent 提案，只能建议代码级只读工具；提案不会自动创建或启用 Agent。用户可修改提案内容、拒绝，或按提案 ID 与 revision 原子采纳为默认暂停的 Agent，再显式启用。手动只读运行保存所用 Agent 版本、结构化建议、请求次数、耗时与可获得的 token。工具每次调用都复查实际状态、版本及只读白名单；无效参数、越权和工具错误留 Trace，即使模型忽略异常也判运行失败。运行结束再次检查 Agent 是否仍启用且版本未变；建议引用的来源 ID 必须来自本次真实工具结果。失败保留原记录，不写任务、日程、记忆或配置。原内置六 Agent 路径不变。

**运行命令与实际结果：**

- `uv run --group dev pytest -q tests/test_p2_custom_runtime.py tests/test_p2_custom_runtime_ui.py`：9 passed；覆盖提案采纳/拒绝、旧 revision、暂停拒绝、越权工具、超时、无效输出、运行中停用、虚构来源、v6→v7 备份/故障回滚/重启恢复，以及控制台审核/运行和轨迹页。
- `uv run --group dev pytest -q`：108 passed、6 skipped；P0/P1/P2 已完成轮次全部确定性测试与 AppTest 通过。6 项默认跳过的测试需要真实 DeepSeek 密钥。实施中旧迁移测试曾因模拟 v5 数据库时未移除新增 v7 表而失败；修正历史库测试夹具后全量重测通过。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p2_custom_deepseek_smoke.py`：1 passed；真实 DeepSeek 生成提案，用户修改后采纳并启用，手动调用 `read_goals` 给出建议，记录工具事件与用量，任务/记忆未变化。
- `uv run --group dev python - <<'PY' ... PY`：临时库与随机端口启动本机 Web/worker，健康检查 `200`，SQLite 版本 7 且五个自定义 Agent 相关表存在，随后正常停止。

**P2 核对：** P2-1 的配置持久化、权限上限、启停与禁用拒绝运行已通过；P2-2 的提案审核、手动只读执行、结构化结果、真实 Trace、失败和用量已通过。P2-3 显式委派及只读并行、P2-4 主动建议/策略、P2-5 MCP/外部日历邮件、P2-6 跨设备能力仍未实现。现有 JSON 业务导出不含 P2 自定义 Agent 配置与运行记录，完整备份请保留 SQLite 数据库。

## P2 第 3 轮：显式选用、只读依赖图与并行规划（2026-10-09）

**状态：已通过实际验证。** 开发前 `uv run --group dev pytest -q` 为 108 passed、6 skipped，P0、P1 与已完成的 P2 基线通过。现有“对话与今日计划”页面允许用户仅在本次规划中显式选用已启用的自定义 Agent，并为步骤选取前置依赖；未选用时仍走原五/六 Agent 路径。Service 在创建运行前验证步骤数、唯一性、只读权限、启用状态和无环依赖图。每层互不依赖的步骤并行运行，依赖步骤等待前层完成；自定义运行沿用 P2-2 的结构化输出、工具权限、逐次状态复查、超时、Trace 和用量记录。汇总按拓扑层及用户选择顺序固定，建议作为参考交给原 Orchestrator 与相关工作 Agent。步骤失败或超时使依赖步骤跳过并使整个规划失败，不进入内置规划，也不产生未经审批的任务、日程或记忆写入。规划运行自身保存步骤、依赖、状态、结果和关联的自定义运行 ID；失败重试创建新运行并重新验证选用的 Agent。启动恢复会将中断的运行中规划标为失败。README 已补充操作与验收命令；未增加页面或外部集成。

**运行命令与实际结果：**

- `uv run --group dev pytest -q tests/test_p2_custom_planning.py tests/test_p2_custom_planning_ui.py`：6 passed。并发屏障证明无依赖节点真实并行；依赖等待、逆完成顺序稳定汇总、草案确认前零正式写入、失败传播与重试、暂停/越权/环路/超限拒绝、伪造上下文拒绝、超时、默认路径及界面选择和轨迹均通过。
- `uv run --group dev pytest -q tests/test_p2_custom_planning_ui.py tests/test_ui.py tests/test_p2_custom_runtime_ui.py`：6 passed。实施中发现 Streamlit 表单内动态依赖控件不会即时出现；将自定义 Agent 与依赖选择移到表单外后重测通过。
- `uv run --group dev pytest -q`：114 passed、7 skipped。预期旧功能和新增确定性测试全部通过；实际通过。7 项为默认关闭的真实 DeepSeek 场景。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p2_custom_planning_deepseek_smoke.py`：1 passed，且在最终 Harness 校验改动后再次运行仍为 1 passed。真实 DeepSeek 自定义只读建议进入原五 Agent 面试规划；草案等待确认、正式任务为空，双方运行轨迹及用量均存在。

**剩余：** P2-4 的主动建议、可审核策略版本与回退；P2-5 的受限 MCP/外部集成；P2-6 的跨设备能力。P2-3 未自动选用 Agent，也未改变原计划审批或长期记忆人工审核。当前 JSON 业务导出仍不包含 P2 自定义 Agent 记录，完整备份应保存 SQLite 文件。

## P2 第 4 轮：有来源的主动建议与策略回退（2026-10-09）

**状态：已通过实际验证。** 开发前 `uv run --group dev pytest -q` 为 114 passed、7 skipped，P0、P1 和已完成 P2 基线通过。SQLite schema 升至 v8，旧库升级前备份，迁移失败事务回滚。新增待审核主动建议、唯一去重键，以及不可变的建议策略版本和当前版本指针。建议只从未完成逾期任务、最近七个当地日期的真实习惯打卡、最近复盘的未完成/冲突数据触发；可引用的长期记忆只从 `approved` 行中检索，并保存来源 ID、事实快照与生成时间。没有习惯打卡时不推断习惯趋势。相同事实状态的建议无论重复扫描、重启或拒绝后都不会自动重提；事实或已批准记忆变化后，旧证据不能直接获批准。

本机 worker 在保存每日复盘后自动扫描，用户手动创建/修改复盘或在现有今日页面点击扫描也可触发。扫描失败留 Trace，不使已保存的 P1 复盘失败。页面显示建议、来源、生成时间、去重键和策略版本；用户可按 revision 编辑、接受或拒绝。接受仅在事务内调整以后识别逾期、习惯落差或复盘未完成事项的阈值，不创建正式任务、不更改日程、不批准或写入长期记忆；先前基于旧策略生成但证据仍有效的待审建议仍可逐条审核。每次实际策略变化保存新版本，回退也复制旧阈值形成新版本，保留完整历史并拒绝旧版本并发回退。建议生成采用本机确定性规则，不额外调用模型；所有仍需模型的路径继续仅调用 DeepSeek。

**运行命令与实际结果：**

- `uv run --group dev pytest -q tests/test_p2_proactive.py tests/test_p2_proactive_ui.py tests/test_p1_review_ui.py tests/test_ui.py`：14 passed。覆盖已批准/已删除记忆边界、真实任务/习惯/复盘来源、去重与拒绝后不重提、跨重启、旧证据拒绝、revision、策略改变后新信号触发与回退后的行为、同批建议逐条接受、worker 自动扫描及故障隔离、v7→v8 备份/回滚、五页面内的编辑/接受/拒绝/回退，以及原复盘和计划 UI 回归。
- `uv run --group dev pytest -q`：124 passed、7 skipped。旧迁移测试最初有五项因把最终版本固定为 v7 而失败；改为断言当前 schema 版本后，全量重测通过。7 项为默认关闭的真实 DeepSeek 测试。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p2_custom_planning_deepseek_smoke.py`：最终 1 passed。真实 DeepSeek 自定义只读步骤和原五 Agent 面试规划成功，随后扫描、接受建议并回退策略；正式任务和长期记忆仍未被建议直接改写。最终验收前有一次真实 Orchestrator 返回非法 JSON，运行正确失败且未提交；样例加入与产品一致的用户显式重试路径后重新实跑通过，原失败记录保留，不做格式错误静默自动重试。

**剩余：** P2-5 受限 MCP 工具目录及在用户选定服务商后的外部日历/邮件接入；P2-6 跨设备访问与通知。外部服务商、MCP 服务器和跨设备部署方式仍待确认。当前业务 JSON 导出不包含 P2 自定义 Agent、主动建议与策略历史；完整备份请保存 SQLite 数据库。

## P2 第 5 轮：受限外部工具目录与待审写入（2026-10-09）

**状态：无需外部账号的实现与模拟验收已通过；真实服务接入待用户选型与授权。** 开发前 `uv run --group dev pytest -q`：124 passed、7 skipped。SQLite schema 升至 v9，旧库迁移前自动备份，失败回滚且保留任务等已有数据。新增按服务器及操作独立启停的 MCP／日历／邮件目录；服务器登记默认暂停，操作默认关闭，代码绑定适配器是额外执行前提。页面仍在原“Agent 控制台”和“执行轨迹”，只经 `PersonalAIService` 读写；外部工具没有进入内置或自定义 Agent 的本机工具权限集。

日历列出／创建／修改和邮件列出／发送分别授权。读取返回服务器、操作与账号来源；超时、越权及调用结果留本机审计。写入先保存展示目标账号、目标对象和完整内容的单项草案，逐项确认时再复查服务器、操作及代码绑定。确认前没有外部调用；每项保存独立幂等键和持久尝试状态。成功、明确失败、超时与未知结果分开记录；重复确认不重复调用，未决同内容预览复用原草案，进程中断超过租约后标为未知且不自动重发。适配器协议已定义，正常启动未绑定任何真实服务，不能对外读写；模拟适配器只存在于测试。

**运行命令与实际结果：**

- `uv run --group dev pytest -q tests/test_p2_external.py tests/test_p2_external_ui.py`：12 passed。覆盖目录启停、读写权限分离、来源、MCP 代码白名单、越权、超时／明确失败／未知结果、重复确认、跨重启、租约中断恢复、v8→v9 备份回滚、页面逐项预览确认与审计。预期的确认前零外部写入和失败后无自动重发均满足。
- `uv run --group dev pytest -q`：最终 136 passed、7 skipped。最初全量有两项旧测试通过人工降低 `user_version` 留下 v9 表，导致升级冲突；将 v9 建表改为可重复执行后重新运行通过。7 项是默认关闭的真实模型测试。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p2_custom_planning_deepseek_smoke.py`：1 passed。真实 DeepSeek 自定义只读步骤、原五 Agent 规划与待确认草案路径保持正常；本轮外部能力未插入 Agent 执行或本机审批。

**未验收范围与待选型：** 尚未选定日历服务商、邮件服务商或 MCP 服务器，因此没有 OAuth／账号授权、真实适配器或真实读写验收，不能宣称接入已通过。接入前需要选定日历账号与目标日历，并逐项授权读取、创建、修改事件；选定邮件账号及读取范围（仅元数据或含正文）与发送权限；提供每个 MCP 服务器的地址、传输方式、身份验证方式及精确操作白名单。密钥仅进本机安全配置，不能进数据库／Trace。P2-6 跨设备访问与通知仍未开始；当前业务 JSON 导出不包含新外部目录和审计记录，完整备份应保存 SQLite 文件。

## P2 第 6 轮：设备授权、受保护只读视图与模拟远端通知（2026-10-09）

**状态：无需部署和通知账号的实现与模拟验收已通过；真实跨设备访问／推送待选型。** 开发前 `uv run --group dev pytest -q`：136 passed、7 skipped。SQLite schema 升至 v10；迁移前备份，失败事务回滚。设置页可签发设备凭据（仅显示一次，库中只存 SHA-256 哈希）、查看设备和撤销；设备凭据有有效期，浏览器会话有效期为 12 小时，撤销后会话立即失效。独立只读 HTTP 视图通过短期会话查看未完成任务与应用内提醒；未认证请求返回 401，输出做 HTML 转义、禁止缓存，接口不提供任务或记忆写入。`remote-view` 命令须显式启动，并硬编码绑定 `127.0.0.1`；默认 `start` 的 Web 与 worker 启动方式不变，不能由设备或 MCP 目录配置打开网络监听。远端只读服务不触发原 Harness 中断恢复。

远端通知基于 P1 已落库的提醒，按设备显式启用，且只有代码绑定通知适配器时才能开启；默认关闭。每设备／每提醒建立唯一投递记录，发送前持久化尝试和幂等键；成功、明确失败、超时／未知、错过、撤销分别记录。只有启用后的新提醒进入投递；浏览器关闭后 worker 可独立处理，重启重复扫描不会重复发送，已超时的中断尝试变为未知而不自动重发。正常启动未绑定真实远端通知通道，测试替身仅在自动化测试注入。

**运行命令与实际结果：**

- `uv run --group dev pytest -q tests/test_p2_remote.py tests/test_p2_remote_ui.py`：13 passed。覆盖凭据只存哈希、无凭据拒绝、会话过期／撤销、只读视图 HTML 转义、无写路由、逐设备通知开关、失败／超时／未知／错过、启用前提醒不发送、worker 无浏览器投递、跨重启去重、过期尝试恢复、v9→v10 备份回滚、设置页签发一次性凭据和撤销。
- `uv run --group dev pytest -q`：最终 149 passed、7 skipped；P0、P1 及已完成 P2 全量确定性测试和 Streamlit AppTest 均通过。7 项为默认关闭的真实模型测试。
- `RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p2_custom_planning_deepseek_smoke.py`：1 passed；真实 DeepSeek 自定义只读步骤及原五 Agent 规划审批边界正常。
- `env DATABASE_PATH=/private/tmp/personal-ai-remote-startup-20261009.sqlite3 uv run python -m personal_ai_os remote-view --port 18766` 实际启动；`curl --silent --show-error --output /dev/null --write-out '%{http_code} %{remote_ip}\n' http://127.0.0.1:18766/dashboard` 实际输出 `401 127.0.0.1`，未认证访问受拒；Ctrl+C 后正常退出。

**未验收范围与待选型：** 尚未指定跨设备部署位置、TLS／传输保护终止点、设备认证与恢复方式、真实通知渠道和授权范围，因此未开放局域网／公网，也未作真实异机访问或推送验收。P2-5 的真实日历、邮件及 MCP 服务同样仍待服务商、服务器和账号授权；模拟通过不能替代真实接入。业务 JSON 导出不包含 P2 外部目录、设备凭据与投递审计；完整备份请保存 SQLite 文件，并单独保护包含凭据哈希的数据库。

## P2 真实接入方案更新（2026-10-09，仅文档）

用户已选定 Mac 日历中的 iCloud 日历（读取、创建、修改）、指定 Gmail 账号（读取正文、发送）、iPhone 上查看/编辑/审批，并希望经本地只读 MCP 工具从 YouTube 获取学习资料。`request.md` 增加 P2-7 和真实接入验收范围，`design.md` 确定 EventKit、Gmail OAuth、本地 MCP/YouTube 及手机安全入口，`skeleton.md` 增加五轮后续实施与真实验收顺序；`README.md` 更新当前能力与计划的区别。历史轮次的实测结论不变。本次没有修改业务代码或新增测试；真实适配器、iPhone 写入界面、YouTube 工具和普通任务手机推送仍未实现或验收。下一轮开始前须先复跑已有全量确定性测试。日历系统授权、Gmail OAuth、YouTube API 凭据、iPhone 私人连接和测试邮件目标仍需在相应轮次由用户在本机确认；普通任务推送渠道仍待选定。

## P2 真实接入第 1 轮：Mac EventKit iCloud 日历（2026-10-09）

**实现：** 新增本机 Swift EventKit 桥接程序和 `ICloudCalendarAdapter`。用户显式请求权限后列出可写的 iCloud 日历，选择具体日历并持久保存；服务器和读取、创建、修改权限仍须逐项启用。现有 `PersonalAIService`、外部工具目录、待审草案、幂等状态及审计负责全部页面操作。当地日期读取、当地时间转 UTC、夏令时不存在/重复时间拒绝、提前提醒、草案生成时冲突检查、确认时事件指纹和冲突复查均已实现。更换目标日历会拒绝旧修改草案；失败、超时、结果未知分别留状态，未知结果不自动重发。没有删除功能，Agent 未获得日历权限。

**实际测试：** 编码前 `.venv/bin/python -m pytest -q` 为 `149 passed, 7 skipped`。`uv run --group dev pytest -q` 在本次 Codex 沙箱内因无法访问 `/Users/april/.cache/uv/sdists-v9/.git` 未启动测试，因此使用现有 `.venv` 执行；这是命令环境限制，不是 pytest 失败。新增确定性测试与相关 AppTest：`.venv/bin/python -m pytest -q tests/test_p2_icloud.py tests/test_p2_icloud_ui.py tests/test_p2_external.py tests/test_p2_external_ui.py` 最终 `23 passed`；`.venv/bin/python -m pytest -q` 最终 `160 passed, 7 skipped`。覆盖权限拒绝、目标选择与重启、读写白名单、确认前无写入、时间冲突、更新指纹变化、选择变化、重复确认、夏令时、提醒、超时及未知结果去重。新加的夏令时读取测试第一次因测试期望把包含末日的范围少算一天而失败，修正期望并重跑后通过。`.venv/bin/python` 调用 `MacEventKitBridge.ensure_built()` 返回 `swift_bridge_built: True`。

**真实验收：已通过本轮适用场景。** 用户同意触发 macOS 完整日历访问后，受限进程仍显示 `not_determined`；在正常系统权限下重试，状态由 `write_only` 变为 `full_access`，EventKit 实际列出 5 个 iCloud 日历。用户明确选定“个人”；通过 `PersonalAIService` 持久选择并读取 2026-10-09 当地日期，返回 0 条事件且来源操作为 `list_events`。经用户查看具体预览并确认，2026-10-10 15:00–15:15（Australia/Sydney）的测试事件通过单次审批创建，EventKit 返回成功、外部事件 ID 和选中日历 ID；再次读取同一日历找到同一 ID、标题及提前 15 分钟提醒。用户在 iPhone 日历确认该事件和提醒均正确。创建草案的附加审计核查脚本曾因把 `event_type` 误写为 `status` 退出；随后只读核查确认草案仍为 `pending`、只有 `write_proposed`，因此没有重复创建。

用户又逐项确认了同一事件的修改草案：时间改为 15:30–15:45、标题加“（已修改）”、提前提醒改为 30 分钟。`.venv/bin/python` 通过 `PersonalAIService.approve_external_write(<草案 ID>, 0)` 单次执行，实际返回 `succeeded`、revision 1、原外部事件 ID；再次通过 Service 读取目标日历，找到同一 ID 且新标题、UTC 时间与提前 30 分钟提醒均正确。用户已在 iPhone 日历核对三项都正确。对已成功草案再次以原 revision 确认，实际仍返回 `succeeded`，审计的 `write_attempt_started` 数量保持 `1 → 1`，没有第二次外部写入。真实修改后复跑 `.venv/bin/python -m pytest -q tests/test_p2_icloud.py tests/test_p2_icloud_ui.py tests/test_p2_external.py tests/test_p2_external_ui.py` 为 `23 passed`；`.venv/bin/python -m pytest -q` 为 `160 passed, 7 skipped`。本轮未制造真实超时或外部冲突；这些由确定性故障测试验证，不能视为真实故障演练。

**剩余：** Gmail、YouTube MCP、手机编辑审批及独立通知均不在本轮，尚未开始。iCloud 测试事件仍保留在用户“个人”日历中，本版应用未提供删除操作；需要时可在 Mac 或 iPhone 日历 App 中手动删除。

## P2 真实接入第 2 轮：Gmail 桌面 OAuth 与逐封邮件审批（2026-10-09）

**状态：无账号实现与模拟验收通过；真实 Gmail 验收未通过。** 开发前 `.venv/bin/python -m pytest -q` 为 `160 passed, 7 skipped`。SQLite schema 升至 v11，给旧邮件服务器补充默认关闭的 `get_message` 正文读取操作；升级前备份，失败事务回滚。新增 macOS Keychain 桥接、桌面 OAuth 的本机回环回调、PKCE、双作用域核查及 Gmail 账号核对。刷新令牌仅存 Keychain，客户端 JSON 必须是本机仅本人可读文件；令牌、客户端密钥及读取的邮件正文不写入 Trace、审计或业务导出。授权前不会绑定真实账号，授权后服务器和三个操作仍默认暂停/关闭，必须逐项启用。

复用 `PersonalAIService`、`MailAdapter`、外部工具目录、草案 revision、逐项审批、幂等状态和审计。`list_messages` 可选择收件箱或已发送，分页读取邮件 ID 及标题、发件人、日期元数据；`get_message` 仅在用户选中后读取正文，HTML 以纯文本显示；两者有独立白名单。`send_message` 在草案中保存收件人、主题及正文，用户确认时重查账号、操作权限与内容后只发送一次。Gmail 的明确拒绝、超时、结果未知分别记录；未知结果不自动重发。发送成功时只保存 Gmail 返回的邮件与线程 ID。读取正文只保留在页面内存；发送草案正文为人工审批保留在本机 SQLite，不进 Trace 或 JSON 导出。未赋予 Agent 邮件权限，不处理附件、删除或批量发送。

**运行命令与实际结果：**

- `.venv/bin/python -m pytest -q tests/test_p2_gmail.py`：`10 passed`。覆盖账号不匹配不存令牌、作用域与本机回环 OAuth 的 state/PKCE、收件箱/已发送分页与按需正文、读写白名单、内容验证、明确失败/超时/未知去重、v10→v11 备份及回滚、HTTP 故障分类。
- `.venv/bin/python -m pytest -q tests/test_p2_gmail_ui.py tests/test_p2_gmail.py tests/test_p2_external.py tests/test_p2_external_ui.py`：`24 passed`。相关 Streamlit AppTest 验证连接必须显式点击、连接后默认不开放操作、列表不预取正文、选中后只读显示、邮件夹切换、分页、发送预览及确认前零发送；旧外部目录页面仍正常。
- `.venv/bin/python -m pytest -q`：`172 passed, 7 skipped`。全部 P0/P1/P2 确定性测试及相关 AppTest 通过；7 项仍是需显式启用的真实 DeepSeek 冒烟，与本轮邮件路径无关。
- `.venv/bin/python` 调用 `MacKeychainStore._build()`：`gmail_keychain_compiled: True`。在正常系统权限下，用无效临时测试值实际执行 Keychain `set → get → delete`，输出 `dummy_roundtrip: True`、`dummy_deleted: True`；受限沙箱直接查询 Keychain 会返回 `gmail_keychain_unavailable`，未影响正常系统权限下的验证。
- `.venv/bin/python` 调用 `build_service()` 升级本机数据库：`schema_version: 11`、`icloud_selection_preserved: True`、`gmail_connected: False`。旧 iCloud 选择保留，未向 Gmail 发起网络调用。

**真实验收限制：** 用户明确表示尚无 Google Cloud 桌面 OAuth 客户端 JSON，要求先完成模拟实现。故没有触发 OAuth、没有读取真实邮件正文，也没有发送测试邮件；不能用模拟结果代替真实验收。用户已私下指定测试收件人，但地址不写公开文档或 Trace。`.env.example` 已加入配置项占位，真实 `.env` 未写入 Gmail 凭据。下一步需要用户在本人 Google Cloud 项目启用 Gmail API、配置同意页面并下载桌面客户端 JSON，保存在本机 `.local` 且权限为 `0600`，再在 `.env` 配置预期 Gmail 账号和文件路径。授权成功后须核对账号、实读一封选中邮件的正文；测试邮件仍需展示完整收件人、主题、正文并由用户逐封确认后才能发送和核对收件箱/已发送。YouTube MCP、手机编辑审批和独立推送未开始。

### 真实 OAuth 配置续验（2026-10-09）

用户提供了本机桌面 OAuth 客户端 JSON 路径。已核查文件类型与必需字段，复制到仅本人可读的 `.local/gmail_oauth_client.json`（`0600`），并在本机 `.env` 配置指定 Gmail 账号及客户端路径（`0600`）；未记录或输出客户端密钥与令牌。首次授权被 Google 以 `org_internal` 拒绝。用户明确同意调整后，Google Cloud 控制台的受众已核对为“外部 / 测试中”，测试用户列表仅有指定账号，应用没有发布。再次发起授权，Google 显示“此应用未经验证”提示；该提示及后续 Gmail 读取、发送权限须用户本人在浏览器审阅。两次本机 OAuth 回调均超时并返回 `gmail_oauth_not_completed`，因此没有建立 Gmail 绑定、读取邮件或发送邮件；真实验收仍未通过。用户完成浏览器授权后需重新发起连接，并按原审批流程分别验证按 ID 读取正文及逐封发送。

本次复跑 `.venv/bin/python -m pytest -q`：`172 passed, 7 skipped in 45.30s`。本机私有配置和文档更新未改变业务代码；7 项仍为显式启用的真实 DeepSeek 冒烟。

### Gmail 真实授权成功（2026-10-09）

重新发起本机 OAuth 后，用户本人通过 Google 未验证应用提示并审阅读取、发送权限。`PersonalAIService.connect_gmail()` 实际返回 `connected: True`、指定账号匹配；新进程通过 `gmail_status()` 确认绑定保留，并用钥匙串刷新令牌、调用 Gmail profile 再次核对账号成功，均未输出令牌。Google Cloud 控制台显示 Gmail API 已启用。授权回调的临时 `127.0.0.1` 监听在收到一次请求后关闭，用户浏览器后来显示 `ERR_CONNECTION_REFUSED` 不影响已经完成的授权。Gmail 目录目前仍为 `paused`，`list_messages`、`get_message`、`send_message` 均未启用；真实列表、按用户选择读取正文和逐封发送尚未验收。没有读取邮件或发送邮件。

### Gmail 真实读发验收（2026-10-09）

用户要求继续后，经 `PersonalAIService` 显式启用 Gmail 服务器及 `list_messages`、`get_message`，最初保持 `send_message` 关闭。真实收件箱列表返回 5 封元数据和下一页标记；用户选定第 5 封后，按其 ID 读取正文，实际校验 ID、标题匹配且纯文本正文非空，正文内容没有输出到聊天或 Trace。随后启用 `send_message`，为用户指定的测试收件人生成草案：`pending`、revision 0。用户看到完整发件账号、收件人、主题和正文后明确确认；`approve_external_write(<草案 ID>, 0)` 单次返回 `succeeded`、revision 1 和 Gmail 外部邮件 ID。按同一 ID 在 Gmail“已发送”列表找到该邮件且主题匹配，审计中 `write_attempt_started` 仅 1 条；用户已在收件端核对收到邮件。没有重发，也没有读取其他邮件正文。

验证命令：`.venv/bin/python -m pytest -q tests/test_p2_gmail.py tests/test_p2_gmail_ui.py` 为 `12 passed`；`.venv/bin/python -m pytest -q` 为 `172 passed, 7 skipped in 27.36s`。7 项仍是显式启用的真实 DeepSeek 冒烟。Gmail 真实授权、读信和发信适用场景已通过；发送失败、超时和未知结果由无账号确定性故障测试覆盖，本次没有制造真实故障。YouTube MCP、手机编辑审批和独立通知仍属于后续轮次。

## P2 真实接入第 3 轮：本机 YouTube MCP 与待审学习资料（2026-10-09）

**状态：无凭据实现与确定性验收通过；真实 YouTube/DeepSeek 联动尚未验收。** 开发前 `.venv/bin/python -m pytest -q` 为 `172 passed, 7 skipped in 19.13s`。新增固定命令启动的本机 `stdio` MCP 服务器与受限客户端，仅有 `search_videos` 和 `get_video_details` 两项只读操作；客户端先校验服务器身份、协议和完整工具清单／输入 schema，再调用工具。YouTube Data API v3 独立使用本机 `YOUTUBE_API_KEY`，不复用 Gmail OAuth；不读取个人 YouTube 数据或字幕。工具返回公开视频标题、频道、时长、固定视频链接及来源，校验 ID、类型、长度和响应上限。超时会结束子进程；错误与配额返回受控错误码，不把 key 或 API 原始错误写入 Trace。每次最多检查 3 个视频，本机按 UTC 日期限制 10 次新搜索并缓存最多 10 组结果。

YouTube 服务器登记后默认暂停，两项读取操作默认关闭；代码适配器、服务器状态和操作白名单同时允许才执行。用户在规划页面为**本次**勾选 YouTube 并填写独立搜索词时，Service 才读取公共元数据并交给 Learning 步骤；Orchestrator、Life 和其他 Agent 没有新增 MCP 工具权限。Learning 必须至少引用一个已返回的视频 ID，不能引用其他视频；Task Agent 不能自行设置外部资源字段。草案展示来源、频道、时长、链接及理由，确认前正式任务为零；用户确认后链接与来源写入任务。草案编辑不能偷偷更换资源链接，原 `run_id + revision` 审批、冲突复查、事务提交与幂等保持不变。SQLite schema 升至 v12，新增任务资源元数据列，旧库迁移前备份、失败回滚。

**运行命令与实际结果：**

- `.venv/bin/python -m pytest -q tests/test_p2_youtube_mcp.py tests/test_p2_youtube_ui.py`：`9 passed`。覆盖默认停用、两项白名单和只读权限、无 key、错误服务器身份在工具调用前拒绝、超时、错误 JSON/视频 ID、模拟配额、搜索预算和跨 Service 缓存、待审资料任务与确认后链接、篡改链接拒绝、失败时零正式任务、v11→v12 备份和回滚，以及现有 Agent 控制台/规划页 AppTest。
- `.venv/bin/python -m pytest -q`：`181 passed, 7 skipped in 19.91s`。P0/P1/已完成 P2 确定性测试及相关 AppTest 均通过；7 项为需显式启用的真实 DeepSeek 冒烟。
- 只检查配置项是否存在、未输出秘密：进程环境与本机 `.env` 的 `YOUTUBE_API_KEY` 均未配置。因此**没有**真实 YouTube API 搜索，也没有声称真实 DeepSeek 学习场景已通过；模拟结果不替代真实接入验收。

**真实验收待办：** 用户在本人 Google Cloud 项目启用 YouTube Data API v3，创建仅允许该 API 的独立 key，并仅写入本机 `.env` 的 `YOUTUBE_API_KEY`；重启应用，显式启用 YouTube 服务器和两项读取操作。随后实际搜索公开视频、逐项核对标题/频道/时长/链接/来源，再显式选 YouTube 运行真实 DeepSeek 学习目标，确认出现待审资料任务且审批前正式任务不变。暂不开始 iPhone 编辑审批、手机通知或个人 YouTube 数据/字幕。

## P2 真实接入第 4 轮：YouTube 补验与 iPhone 私人入口（2026-10-09）

**基线：** 开发前 `.venv/bin/python -m pytest -q` 为 `181 passed, 7 skipped in 30.62s`，P0/P1/已完成 P2 的确定性测试通过。只检查本机配置项是否存在，`.env` 中 `YOUTUBE_API_KEY` 与 `DEEPSEEK_API_KEY` 均已配置；未输出密钥。初次在受限执行环境调用真实 YouTube 因网络不可达返回连接错误，同时发现 MCP 客户端把该错误归成笼统调用失败；已修正为 `youtube_transport_error` 等受控错误码。经获准联网的执行环境，使用**隔离临时 SQLite 数据库**，真实 `search_videos`/`get_video_details` 返回 2 条公开视频，含有效视频 ID、标题、频道、秒数、标准 YouTube 链接及 `YouTube Data API v3` 来源。未访问个人 YouTube 数据。

第一次真实 DeepSeek 学习规划因 Task Agent 自行填写外部资源字段被代码校验拒绝，运行失败且正式任务数为 0；已补充 Task Agent 指令，资源字段由程序按已验证的 Learning 视频 ID 注入。再次在隔离临时数据库运行真实 DeepSeek 场景：Orchestrator、Memory、Learning、Schedule、Task 五步均为 `success`；运行状态 `waiting_approval`，生成 4 项草案，其中 1 项含真实 YouTube 链接、频道、时长与来源，`youtube_mcp_read` 和资源选择事件可追溯；**审批前正式任务为 0**。本次没有点击计划确认，也没有修改用户现有任务数据库。该真实只读场景已通过；真实外部故障仍由确定性测试覆盖。

**手机入口实现：** 新增独立 `mobile-view` HTML 入口及命令，硬编码绑定 `127.0.0.1`，默认端口 8766；只有另行配置准确的私人 HTTPS `--public-origin` 并手动设置代理后，iPhone 才可访问。原 Streamlit 仍仅监听 `127.0.0.1:8501`，不通过此入口对外开放。沿用既有设备凭据与 12 小时会话，手机 Cookie 为 `Secure`/`HttpOnly`/`SameSite=Strict`，所有写表单验证会话绑定 CSRF、固定来源及 Host；设置请求体和速率上限，响应禁止缓存，HTML 转义用户数据。不信任代理转发的身份头，设备撤销会立即使手机会话失效。

手机页面经 `PersonalAIService` 查看和编辑任务、规划草案及每日计划草案，审批 `run_id + revision` 计划、每日草案、候选记忆及逐项外部写入。任务编辑新增可选版本复查，旧表单拒绝；原 Streamlit 编辑流程保持兼容。规划与每日草案编辑均递增 revision，审批由原 Harness/Service 事务、冲突和幂等规则执行；外部结果为 `unknown` 时仅显示状态，不自动重发。没有给手机新增直接数据库写入或 Agent 工具权限，也没有开始普通任务手机推送。

**运行命令与实际结果：**

- `.venv/bin/python -m pytest -q tests/test_p2_mobile.py tests/test_p2_remote.py tests/test_p2_remote_ui.py tests/test_p2_youtube_mcp.py tests/test_p2_youtube_ui.py`：`30 passed in 4.48s`。新增手机测试覆盖未认证、非法 Host、来源与 CSRF、过期/撤销、登录限速、任务旧版本、规划/每日草案旧 revision 和重复确认、每日计划基线变化拒绝、候选记忆审批、外部写入预览及未知结果去重；实际回环 HTTP 表单往返与 CLI 测试确认固定 `127.0.0.1` 监听。
- `.venv/bin/python -m pytest -q`：`189 passed, 7 skipped in 32.96s`。P0/P1/P2 全量确定性测试及现有 Streamlit AppTest 通过；7 项为需显式启用的其他真实 DeepSeek 冒烟。每日计划新增用例首次因测试直接比较 UTC 与悉尼时间字符串而失败；改为比较时刻后复测通过，业务代码无需调整。
- 用独立临时数据库启动 `.venv/bin/python -m personal_ai_os mobile-view --port <临时回环端口>`，实际 GET 登录页为 HTTP 200，进程正常；受限沙箱不允许临时套接字绑定，改在获准的本机执行环境下完成该检查。
- 实际 Chrome 打开独立临时数据库的回环登录页，页面正常；浏览器在纯 HTTP 测试入口提交表单时发送 `Origin: null`，服务器按严格来源校验返回 403，未创建会话或改写任务。带准确 Origin 的自动化 HTTP 表单往返已验证登录、查看与任务编辑；这不等于真实浏览器登录。真实 iPhone Safari 登录仍须在私人 HTTPS 下核对，不把回环 Chrome 结果冒充异机通过。

**真实 iPhone 验收未通过：** 当前未检测到 `tailscale` CLI，尚无用户提供的私人 HTTPS 设备域名，也未在真实 iPhone Safari 上验证任务读取/编辑、计划及外部写入审批、设备撤销和 Streamlit 端口不可直达。真实跨设备能力须 Mac 与 iPhone 加入同一受控 tailnet、启用 Tailscale Serve（不可用 Funnel）、把 8766 反代为私人 HTTPS，并以该实际 URL 启动 `mobile-view --public-origin` 后逐项实测。模拟及回环结果不能替代异机验收；普通任务手机推送留在真实接入第 5 轮。

### 第 4 轮续验：私人 HTTPS 与 iPhone Safari（2026-10-09）

上段记录的是实施时的阶段状态；本节记录其后的实机续验。Mac 安装 Tailscale CLI 1.104.1，在 userspace 模式启动本机 `tailscaled`；用户本人完成 Mac 登录，iPhone 加入同一私人 tailnet。Tailscale Serve 的实际配置只有 HTTPS 443 → `http://127.0.0.1:8766`，无 Funnel 项；`lsof -nP -iTCP:8501 -iTCP:8766 -sTCP:LISTEN` 确认两个应用入口均只监听 `127.0.0.1`。`mobile-view` 以实际私人域名设定 `--public-origin`，Streamlit 与 worker 仍通过原 `start` 命令运行。

首次 iPhone 登录被拒绝时，临时诊断只记录 Host、Origin 和 Referer，不记录凭据、Cookie 或请求体；发现 Safari 的 HTTPS 表单发送 `Origin: null`，原因是响应的 `Referrer-Policy: no-referrer`。将其改为 `same-origin` 后，iPhone 实际登录成功；来源不匹配或 `Origin: null` 仍拒绝。`tests/test_p2_mobile.py` 新增响应头与空来源拒绝断言。手机外部日历预览增加已选日历名称、悉尼当地时间、时区与提前提醒，保持完整目标和 JSON 内容可见；`tests/test_p2_mobile.py` 增加对应断言。修复后 `.venv/bin/python -m pytest -q tests/test_p2_mobile.py tests/test_p2_icloud.py tests/test_p2_icloud_ui.py` 为 `19 passed`；`.venv/bin/python -m pytest -q` 为 `189 passed, 7 skipped in 30.31s`。

真实 iPhone Safari 已用用户在 Mac 本机签发的设备凭据登录并打开“今日与审批”。手机修改测试任务后，数据库标题及版本同步；Mac 再更新版本，手机旧页面保存返回 HTTP 409，未覆盖 Mac 改动。真实 DeepSeek 五 Agent 规划生成一项 10 分钟待审学习任务；Mac 将草案升到 revision 1 后，手机旧 revision 审批返回 409、正式任务数不变；手机编辑到 revision 2 后，用户在手机确认，正式任务实际新增一项，重复 Service 确认没有新增任务。

2026-10-11 每日草案包含两项测试任务。手机旧 revision 审批返回 409，任务均未排程；手机编辑草案后 revision 递增，其中一次 09:00–09:10 修改触发 `outside_availability` 并阻止确认。随后修正到无冲突 revision 4，用户在手机确认，两项正式任务实际排到 10:15–10:25 和 10:30–10:40（Australia/Sydney）。再次经 Service 确认返回原两项结果，任务版本保持不变。手机也批准了一条来自测试反馈的 `text_preference` 候选记忆；核对 `approved` 后已删除该测试记忆，另一条测试候选已拒绝，避免测试偏好影响实际规划。

用户逐项确认后，在 iPhone 预览页批准对原 iCloud「个人」测试事件的修改，仅把标题改为“Personal AI OS 日历接入验收测试（手机审批）”、说明改为手机验收文字；2026-10-10 15:30–15:45（Australia/Sydney）与提前 30 分钟提醒不变。外部草案状态为 `succeeded`、revision 1，EventKit 再读到同一事件 ID、标题、时间和提醒；用户在 iPhone 日历 App 核对三项正确。重复经 Service 确认仍返回已成功结果，外部审计条数保持 `34 → 34`，未再次写入。普通任务手机推送留待第 5 轮。

iPhone Safari 访问原 Streamlit 的私人设备域名 `:8501` 没有出现 Streamlit 页面；返回私人 HTTPS 入口仍能打开“今日与审批”。这与 Mac 上仅 `127.0.0.1:8501` 监听、Serve 仅转发 443→8766 的检查一致。Mac 随后撤销正在使用的 `iPhone 182` 设备，数据库状态为 `revoked`；手机刷新请求实际返回 HTTP 401，用户看到登录页，无法再看任务。Mac 本机签发同等权限、30 天有效的替换设备凭据；用户本人输入 iPhone Safari 后，登录 POST 返回 303、首页 GET 返回 200；旧设备仍 `revoked`，新设备为 `active`。凭据始终只显示在 Mac 本机页面，没有进入聊天、Trace 或文档。用户选择暂不保存新凭据，Mac 页面已刷新隐藏它；约 12 小时后手机会话到期时需重新签发。

重新登录后，用户在 iPhone Safari 打开已成功的外部写入草案，实际看到 `succeeded` 且确认按钮已消失；重复调用同一审批的 Service 审计条数也保持不变。手机页面本身阻止从结果页再次点击，后端对重复请求幂等。实机续验后，`.venv/bin/python -m pytest -q tests/test_p2_mobile.py tests/test_p2_icloud.py tests/test_p2_icloud_ui.py` 为 `19 passed in 3.70s`；`.venv/bin/python -m pytest -q` 为 `189 passed, 7 skipped in 39.18s`。7 个跳过项仍是需显式启用的真实模型冒烟，不能算作本轮失败。备份当前 SQLite 至 `.local/acceptance-backup-20261009-1302.sqlite3` 后清理测试数据：一次尝试整体删除已完成反馈测试记录触发外键约束，事务完全回滚；随后通过 Service 删除两条未完成测试任务和测试可用时间段，将只引用这两项的过期待审每日草案标为 `rejected`。清理后 SQLite `PRAGMA integrity_check` 返回 `ok`，`foreign_key_check` 为 0 项。已完成反馈测试任务与对应运行 Trace 保留作为审计；测试记忆已软删除，其他真实用户数据未清理。iCloud 验收事件仍保留在「个人」日历，当前版本没有删除事件功能，可在日历 App 手动删除。第 4 轮适用的实机查看、编辑、审批、隔离和撤销恢复已通过；普通任务手机推送与 P2 总验收尚未开始。

## P2 真实接入第 5 轮：ntfy.sh 普通任务通知与综合复核（2026-10-09）

**实现与边界：** 用户选择 ntfy.sh，横幅只含固定通用标题和正文。新增 `NtfyNotifier`，本机 256 位随机密钥按设备派生独立不可猜测主题；HTTP 发送不会带任务标题、正文、设备 ID 或幂等键。主题只在 Mac 本机“记忆与设置”的有效设备展开区显示，不进入手机入口、Trace 或 JSON 导出。`PersonalAIService` 与独立 worker 从同一 `.env` 绑定通道；设备通知默认关闭，用户逐设备开启后才发送新到期提醒。复用 P1 静默时段、本机收件箱，以及 P2 的持久化去重、租约、明确失败、未知结果、错过和撤销状态；未知结果不自动重发。本机 `.env` 已安全生成 `NTFY_TOPIC_SECRET` 并保持 `0600`，值未输出或写入文档。YouTube 只读 MCP 服务器及两项操作也已在正式数据库启用；每次规划仍须用户显式勾选。

**确定性验证：** 开发前 `.venv/bin/python -m pytest -q` 为 `189 passed, 7 skipped in 28.66s`；新增 ntfy HTTP 状态/超时、通用载荷、每设备主题、静默时段、重启去重、未知结果不重发和 Streamlit AppTest 后，`.venv/bin/python -m pytest -q tests/test_p2_ntfy.py tests/test_p2_remote.py tests/test_p2_remote_ui.py` 为 `20 passed in 1.94s`，全量 `.venv/bin/python -m pytest -q` 为 `197 passed, 7 skipped in 32.01s`。7 项真实模型测试默认跳过；明确启用后在获准联网的执行环境运行五个真实测试文件的 7 个场景，结果 `7 passed in 125.92s`。受限执行环境首次联网尝试 7 项均因连接错误失败，获准联网后无需改代码即通过；`uv` 的共享缓存也受沙箱限制，故使用项目 `.venv/bin/python`。

**真实服务复核：** EventKit 从已选 iCloud「个人」日历重新读到先前经手机审批的测试事件；Gmail 真实列表读出 10 封并有下一页，按用户之前选中的邮件 ID 再次读到非空正文，正文未输出；YouTube 真实公共搜索读到 1 条具有标准链接、秒数和来源的视频。隔离临时 SQLite 下，真实 YouTube＋DeepSeek 五 Agent 学习规划五步均成功，`waiting_approval` 草案 5 项，其中 1 项含有效视频链接、频道和时长，审批前正式任务为 0。没有再次发送邮件或修改日历；先前真实发送、创建/修改、iPhone 同步及逐项审批证据见上文。

**真实 iPhone 通知：** 用户在 iPhone ntfy App 订阅本机页面显示的主题，并亲自在 Mac 开启 `iPhone 182 重新签发` 的设备通知。向用户预览固定标题 `Personal AI OS` 和正文 `有一条任务提醒，请打开 Personal AI OS 查看。` 后，用户明确确认发送一条测试提醒。独立 worker 读取持久化测试通知，ntfy.sh 接受后本机投递为 `delivered`；用户在 iPhone 实际收到且确认标题、正文正确。投递记录恰好 1 条；重启 Web 与 worker、再等待扫描后仍恰好 1 条且为 `delivered`。当前 Mac 的 Web 为 `127.0.0.1:8501`、手机入口为 `127.0.0.1:8766`，Tailscale 私人 Serve 仅把 HTTPS 443 转到 8766；没有开放原 Streamlit 端口或 Funnel。

**P2-1～P2-7 核对：** P2-1 自定义 Agent 配置/启停、P2-2 真实 DeepSeek 提案及只读运行、P2-3 显式选用与依赖并行、P2-4 主动建议/策略回退均由全量确定性测试及上述真实模型回归覆盖。P2-5 的 iCloud、Gmail 和受限 MCP 真实适用场景已验收：只读本轮复测、外部写入与未知结果控制沿用前轮逐项验收。P2-6 的手机查看/编辑/审批/撤销已在前轮真实 iPhone 验收，普通任务 ntfy 通知本轮实际送达且故障边界由确定性测试覆盖。P2-7 的 YouTube 元数据及待审学习任务本轮再次真实验证。P0/P1 与全部旧 AppTest 包含在全量测试中；综合适用场景通过，进入总验收。没有重复执行真实邮件发送、日历写入或再次撤销当前手机设备。

## 第一版讨论 Demo（2026-10-09）

**交付：** 新增 `personal_ai_os.demo` 启动入口和 `DEMO.md`。`uv run --env-file .env python -m personal_ai_os.demo start` 每次在 `.local/demo/runs/` 创建一份新的 SQLite 示例数据：面试与运动目标、今天的待办、运动习惯、明天上午/下午可用时间及一段已有安排；Web 与 worker 仍由原单命令运行，但只在 `127.0.0.1:8502` 提供演示。演示环境强制覆盖数据库路径，保留 DeepSeek 密钥，清空 Gmail、YouTube 和 ntfy 配置；Service 对日历授权、Gmail 连接、YouTube MCP、外部读写和系统通知再加代码级拒绝。现有五页面显示演示标识，首页预填混合目标请求并给出讲解顺序，Agent 控制台不展示外部接入操作。真实个人数据库、日历、邮箱和手机入口未被此 Demo 启动流程使用。

**验证：** `.venv/bin/python -m pytest -q tests/test_demo.py` 为 `4 passed`，覆盖隔离环境、示例数据、原数据库哨兵不变、外部访问拒绝及 Streamlit AppTest 的演示标识、预填请求和页面数据。最终全量 `.venv/bin/python -m pytest -q` 为 `201 passed, 7 skipped in 28.94s`；7 项为默认关闭的真实模型测试。实际启动后的 `http://127.0.0.1:8502/` 和 `/_stcore/health` 均为 HTTP 200。

**真实演示样例：** 在当前 Demo 专用数据库中，用 DeepSeek 处理“明晚七点 AI Agent 面试＋20 分钟运动”。首次 Life Agent 返回无法解析的结构化输出，运行按既有规则标为失败、正式任务仍为 1；经 Service 的失败运行重试后，Orchestrator、Memory、Learning、Life、Schedule、Task 六步均成功，状态为 `waiting_approval`，生成 4 项学习/生活草案、零冲突，正式任务仍为预置的 1 项。保留两次真实 Trace，便于展示失败恢复，但现场建议优先展示成功草案。另在隔离测试数据库、`PERSONAL_AI_DEMO=1` 下实际运行 `tests/test_deepseek_smoke.py::test_real_deepseek_interview_feedback_replan`，结果 `1 passed in 34.05s`，验证审批任务、完成、反馈提取、人工批准记忆与再次规划。Demo 没有自动审批或虚构 Agent 输出；现场模型调用仍可能因网络或格式失败，排练后可展示已保存的真实待审草案及轨迹。

**使用边界：** 这是本机共享屏幕用的讨论 Demo；没有开放公网、移交他人登录或将个人服务接入此数据库。详见 `DEMO.md`。

## 界面中文化（2026-10-09）

五个 Streamlit 页面、手机审批页和只读视图改为显示中文状态、优先级、领域、日程类型、智能体角色、权限操作、冲突原因与执行事件；手机优先级改成中文选项，提交值仍是原枚举。常见错误先显示中文说明，未知错误保留在可展开的技术详情中。真实 DeepSeek 的内置与自定义智能体指令补充“面向用户的自然语言使用简体中文”，既有解释中的固定角色前缀与字段词在展示时翻译；JSON 键、枚举、ID、工具名、时间格式与数据库内容保持原契约。外部写入的原始 JSON 预览及技术标识保留，避免影响逐项审批和审计。演示操作说明 `process.md` 同步使用中文页面状态。

验证：`.venv/bin/python -m compileall -q personal_ai_os app.py pages` 成功；`.venv/bin/python -m pytest -q` 为 `203 passed, 7 skipped in 38.19s`，覆盖中文显示相关 AppTest、手机编辑及审批边界。配置密钥后在隔离数据库运行真实 DeepSeek 六智能体混合规划 `tests/test_p1_life_plan.py::test_real_deepseek_mixed_study_life_plan`，结果 `1 passed in 16.15s`；任务标题为中文、草案待确认，未提交任务。沙箱内首次测试因网络连接被拒绝，获准联网重跑通过。未更改历史用户内容或既有模型输出。
