# Personal AI OS

本机单用户 Web 应用：用 DeepSeek 驱动 Orchestrator、Memory、Learning、Life、Schedule、Task 六个受限 Agent。纯学习请求沿用原五 Agent 路径；生活或混合请求加入 Life Agent。计划由用户确认后写入待办，任务反馈经人工审核成为长期记忆并影响下次规划。

演示有两条路线：[快速讨论 Demo](DEMO.md)展示本机多 Agent 核心闭环；[全功能演示流程](process.md)从命令行开始，逐步覆盖当前学习与生活功能、真实外部接入和 iPhone 审批。两条路线的数据及外部访问边界不同，见下文。

## 安装与启动

需要 Python 3.12 和 `uv`。在项目根目录运行：

```bash
uv sync --group dev
test -f .env || cp .env.example .env
chmod 600 .env
```

在本机 `.env` 中填写 `DEEPSEEK_API_KEY`。可选配置：`DEEPSEEK_MODEL_ID=deepseek-flash`、`DATABASE_PATH=.local/personal_ai_os.sqlite3`、`APP_TIMEZONE=Australia/Sydney`。密钥只从进程环境读取；`.env` 和本机数据库已被 Git 忽略。启动：

```bash
uv run --env-file .env python -m personal_ai_os start
```

打开 <http://127.0.0.1:8501>。无密钥时仍可管理本机目标、任务和时间；Agent 规划与反馈提取会显示配置提示，不会切换模型。模型调用只发往 DeepSeek，默认使用 `deepseek-flash`。

## 向别人演示

| 路线 | 适用场景 | 数据库与外部服务 |
| --- | --- | --- |
| [快速讨论 Demo](DEMO.md) | 约 8–10 分钟，展示六 Agent 规划、审批、Trace、反馈与记忆 | 每次新建示例库，使用真实 DeepSeek；iCloud、Gmail、YouTube、手机入口和通知关闭 |
| [全功能演示](process.md) | 按页面和按钮逐步展示当前全部主要功能，包括真实接入和 iPhone | 使用独立且持久的展示库；经本人逐项确认的外部写入会真正作用于已授权账号 |

快速讨论 Demo 从项目根目录运行：

```bash
uv run --env-file .env python -m personal_ai_os.demo start
```

打开 <http://127.0.0.1:8502>。它不能演示外部接入；每次启动会新建一份示例数据库。

全功能演示不要使用上述 Demo 命令。在已配置的本机 `.env` 下，从项目根目录启动独立的非 Demo 数据库：

```bash
mkdir -p .local/showcase
SHOWCASE_DB="$PWD/.local/showcase/showcase.sqlite3"
uv run --env-file .env env DATABASE_PATH="$SHOWCASE_DB" PERSONAL_AI_DEMO=0 python -m personal_ai_os start --port 8503
```

打开 <http://127.0.0.1:8503>，然后严格按 [process.md](process.md) 操作。这份展示库会跨重启保留，不会自动清空；其 iCloud、Gmail、YouTube 和 ntfy 接入可能需要在新库中重新授权或启用。日历创建／修改、邮件发送和手机审批必须在预览完整内容后由本人确认。iPhone 演示须另启仅监听回环的 `mobile-view`，通过私人 Tailscale Serve 提供 HTTPS；不要使用 Funnel，也不要开放 Streamlit 端口。结束后按流程恢复原私人路由，并自行清理真实账号中的测试事件或邮件。

## 一次完整使用

1. 在“记忆与设置”中填写目标、时区和偏好；在“任务与日程”中录入可用时间段。没有可用时段时，系统保留未排程草案。
2. 在“对话与今日计划”输入“明晚七点有 AI Agent 面试，请制定学习计划并安排任务”，或输入同时包含学习、运动和买菜的混合目标。查看 Agent 步骤、任务领域与来源、草案、记忆依据及冲突，必要时修改任务和时间，再点击“确认并加入待办”。确认前不会创建正式任务。
3. 在“任务与日程”将任务标记完成，填写反馈，例如“不要在早上安排学习，下午更合适”。
4. 在“记忆与设置”审核、修改并批准候选记忆。再次提出类似目标，计划会引用记忆 ID，并避开已批准的禁排时段。候选未批准、被拒绝或已删除时不参与规划。
5. 在“执行轨迹”按运行查看委派顺序、工具调用、状态、耗时和失败原因。

启动命令同时运行 Web 和独立本机 worker；关闭浏览器不会停止后台作业，终端按 Ctrl+C 会停止两个进程。Agent 对业务数据的写入受工具权限和审批边界控制；用户直接编辑的数据会记录审计事件。运行数据使用 SQLite，时间点存为 UTC，页面按设置时区显示。首次打开旧版数据库时会自动创建同目录备份并事务升级；请保留备份文件。

重复任务可在“任务与日程”确认创建，每天或选定星期运行。含“每周三次运动”的 Life Agent 草案会在“对话与今日计划”展示周一、周三、周五的可编辑建议；你选定三个星期并点击确认后才创建规则，计划任务的确认仍是独立操作。worker 会按已确认规则的当地日期自动扫描，仍可手动生成未来七天。规则时区固定保存，修改只影响未生成日期；无可用时间、时间冲突或夏令时异常会生成未排程任务并显示原因。在“记忆与设置”创建习惯并按习惯时区打卡，同日可更正。

“对话与今日计划”现在也显示已排／未排任务、重复实例、时间段和习惯进度。worker 每天当地 07:00 自动生成一份每日草案，也可点击“生成每日草案”手动创建。草案会安排未排程任务，并对逾期未完成或因可用时间变化而不再合法的已排程任务提出重排；修改可用时段、完成任务或新增可用时段也会自动生成相关日期的待确认草案。可以编辑建议、添加新任务或既有任务的重排动作。草案明确显示原时间、新时间、来源和冲突。用户直接修改的可用时间立即保存，既有任务时间只有点击“确认每日计划”后才改变；期间任务、时间段、重复规则或已批准记忆再次变化时，应重新生成草案。

已排程任务可在“任务与日程”设置提前提醒分钟。在“记忆与设置”调整静默时段；系统通知默认关闭，只有明确勾选且本机 macOS 提供 `osascript` 时才尝试发送。所有结果进入“对话与今日计划”的应用内收件箱；重启后过期的提醒标为 `missed`，系统通知失败或结果不确定分别标为 `failed`、`unknown`，不自动重发不确定结果。静默时段内的提醒延至结束时间；若任务已开始则标为错过。每日 21:00 当地时间，worker 自动创建复盘快照；也可在今日页面手动创建并编辑备注。备注不会直接形成长期记忆；关联已完成任务提交反馈后仍需 Memory Agent 提取和逐条人工审核。

在“Agent 控制台”可直接创建自定义 Agent，或输入目标请 DeepSeek 生成待审核提案。提案可修改名称、描述、指令和只读工具，采纳后仍默认暂停；明确启用后，可在该页面手动发起只读建议。暂停或未审核的 Agent 不可运行。每次运行的结构化结果、真实工具事件、失败原因和模型用量可在“执行轨迹”查看。

如需让自定义 Agent 参与某一次规划，在“对话与今日计划”显式选用已启用的 Agent（最多四个），可为后续步骤选择前置依赖。互不依赖的只读步骤可并行，所有建议按固定步骤顺序汇总，再交给原五/六 Agent 规划。任何选中步骤失败时，本次规划失败，依赖步骤被跳过，不生成待确认草案；失败运行可从轨迹页重试。自定义建议不会自动写入任务、日程或记忆；内置 Agent 草案仍须原审批，长期记忆仍须逐条审核。不选用时保持原路径。

“对话与今日计划”的“主动建议与策略”读取已批准记忆及实际任务、习惯打卡和每日复盘。worker 在保存每日复盘后自动扫描；手动创建或修改复盘、点击“扫描主动建议”也会扫描。建议展示生成时间、来源 ID、事实和去重键；你可编辑、接受或拒绝。拒绝后，来源状态不变时不会重复提出同一建议。接受只调整以后识别逾期、习惯落差或复盘未完成事项的阈值；历史策略版本可查看并回退。建议是本机确定性规则生成的，不额外调用模型；它不会创建任务、改变日程或把复盘备注写进长期记忆。原规划及记忆审核仍是独立步骤。

“Agent 控制台”还提供 MCP 与外部日历／邮件目录：可登记服务器、逐项启停只读或写入操作。登记默认暂停，且目录配置本身不会建立网络连接；只有代码绑定了对应适配器，且服务器与操作都已启用，才能读取或生成外部写入草案。日历读取、创建和修改，以及邮件列表、选中后读取正文、发送使用独立操作权限。写入草案显示目标账号、目标日历／事件／收件人及完整内容；逐条点击确认才会调用适配器。每项保存本机幂等键和尝试状态；超时、进程中断或结果未知时不会自动重发，同内容再次预览会返回原未决项。外部工具审计在“执行轨迹”查看。外部响应是非可信数据，不会直接改写本机任务、日程或长期记忆。当前已有 macOS EventKit iCloud 日历、Gmail 桌面 OAuth 和本机只读 YouTube MCP 适配器。iCloud 与 Gmail **已完成真实账号验收**；YouTube 公共搜索及 DeepSeek 待审学习规划也已在隔离数据库中完成真实只读验收。

### Mac iCloud 日历

需要 macOS、已登录 iCloud 的“日历”账号、Swift 编译器（安装 Xcode Command Line Tools 即可）。在“Agent 控制台 → Mac iCloud 日历”点击“授权并列出 iCloud 日历”，按 macOS 提示给予 **完整日历访问**；只读授权不足以读取事件。选择具体的 iCloud 日历并保存。首次操作会在数据库旁的 `eventkit-helper` 目录构建本机桥接程序。若授权被拒绝或弹窗未出现，请在“系统设置 → 隐私与安全性 → 日历”检查 Personal AI OS Calendar Bridge 的访问权限，然后重新列出。

选择日历只保存目标，不会自动启用操作。在同一页的外部目录里分别启用 “iCloud Calendar” 服务器和 `list_events`、`create_event`、`update_event` 操作。读取页面支持当地日期范围。创建或修改页面要求填写当地时间，自动转换为设置中的时区；夏令时不存在或重复的当地时间会被拒绝。可填写提前提醒分钟。生成草案时会检查选中日历的冲突；草案显示目标、内容、当地时间、时区和提醒。点击“确认这一项外部写入”后才会再次核查外部事件和冲突，并向 EventKit 发起一次写入。修改草案保留原事件指纹；期间外部事件或选中日历改变会拒绝旧草案。事件 ID、结果、审计和幂等状态保留在本机 SQLite。未知或超时状态必须人工核对日历，不会自动重发。此版本不提供删除事件，也不会给 Agent 自动授予日历权限。

### Gmail（真实读信和发信已验收）

先在 [Google Cloud Console](https://console.cloud.google.com/) 的本人项目启用 Gmail API，设置 OAuth 同意页面，并创建**桌面应用** OAuth 客户端；将下载的 JSON 放在 `.local/gmail_oauth_client.json`，执行 `chmod 600 .local/gmail_oauth_client.json`。在本机 `.env` 添加 `GMAIL_ACCOUNT=你的 Gmail 地址` 与 `GMAIL_OAUTH_CLIENT_PATH=.local/gmail_oauth_client.json`，不要把 JSON、授权码或令牌粘贴到聊天、文档或代码中。启动应用后，在“Agent 控制台 → Gmail 邮件”点击连接，Mac 默认浏览器会打开授权页；程序只接受你配置的账号，要求 `gmail.readonly` 和 `gmail.send`，并把刷新令牌放入 macOS Keychain。桌面 OAuth 使用本机 `127.0.0.1` 临时回调；不会开放应用网络监听。Google 对这些权限的分类和可能的验证要求见[官方作用域说明](https://developers.google.com/workspace/gmail/api/auth/scopes)与[桌面应用授权说明](https://developers.google.com/identity/protocols/oauth2/native-app)。

授权成功后，可分别启用 Gmail 服务器及 `list_messages`、`get_message`、`send_message` 操作。列表可选择收件箱或已发送，只显示发件人、主题和日期，支持分页；点击选中邮件后才读取正文，HTML 正文以纯文本展示，不自动发给模型。发送时先填写收件人、主题和正文生成草案；核对完整预览后逐项确认才会发出。失败、超时、未知状态及 Gmail 返回的邮件 ID 留在本机审批记录，未知结果不自动重发。读取的正文只在当前页面内存中显示，不写 Trace 或业务 JSON 导出；待发送草案的正文为审批需要保存在本机 SQLite，备份数据库时须同样保护。首版不处理附件、删除、批量发送或邮件正文的大规模归档。**指定 Gmail 账号已完成 OAuth 授权，重启后钥匙串刷新与账号核对成功；真实收件箱列表、用户选中的邮件正文读取和逐封确认发送均已验收。当前 Gmail 服务器及三个操作已显式启用，Agent 仍无自动邮件权限，今后每封邮件仍需单独预览和确认。**

### YouTube 公共学习资料（真实只读场景已验收）

在本人 Google Cloud 项目启用 [YouTube Data API v3](https://developers.google.com/youtube/v3/getting-started)，创建仅允许该 API 的 API key，并在本机 `.env` 填写 `YOUTUBE_API_KEY=...`；不要把 key 粘贴到聊天、代码或数据库。此接入只搜索**公开视频**并读取标题、频道、时长和链接，不使用个人 YouTube 登录，也不下载字幕或声称已观看视频。API key 独立于 Gmail OAuth。

在“Agent 控制台 → YouTube 公共学习资料”点击“登记 YouTube 本机只读服务器”，再在同页目录启用该服务器及 `search_videos`、`get_video_details` 两项只读操作。登记默认暂停、操作默认关闭。随后可手动搜索并查看可点击的视频链接。要在规划中使用，在“对话与今日计划”输入学习目标，**为本次规划**勾选 YouTube 并填写单独的搜索词；只有这段搜索词会发往 YouTube，完整生活请求不会被当作搜索词。Learning Agent 仅能引用本次查询返回的视频 ID，资料链接、时长、频道、来源和理由显示于待审草案。确认计划后链接才随任务保存；未确认时正式任务不变。纯生活目标不能选择 YouTube。搜索结果按查询与日期在本机缓存，每日最多发起十次新搜索，每次最多检查三个视频；外部配额或权限错误会明确报错并保留失败记录。

本机 MCP 客户端用固定命令启动本项目附带的 `stdio` 服务器，先核对服务器身份和工具清单，再调用工具；目录中登记其他名称不能执行任意命令。服务器通过 [YouTube `search.list`](https://developers.google.com/youtube/v3/docs/search/list) 与 [`videos.list`](https://developers.google.com/youtube/v3/docs/videos/list) 读取公开元数据。本机现已配置独立 `YOUTUBE_API_KEY`；在隔离数据库中实测搜索返回公开视频链接、频道、时长和来源，真实 DeepSeek 学习规划生成含视频资料的待审任务，确认前正式任务为零。实测范围与失败后修复记录见 [code.md](code.md)。

“记忆与设置”可签发一个设备凭据，凭据仅显示一次，数据库只保存哈希；可随时撤销设备，已有浏览器会话随即失效。独立只读视图只能查看未完成任务和应用内提醒，不能修改计划、任务或记忆。需要在本机演示此视图时，另开终端运行：

```bash
uv run --env-file .env python -m personal_ai_os remote-view --port 8765
```

在同一台电脑打开 <http://127.0.0.1:8765>，输入设备凭据登录。该命令和默认 `start` 均只绑定 `127.0.0.1`，不会把应用开放给局域网或互联网；该视图目前仅完成本机受保护访问与模拟验收。

### 普通任务的 iPhone 通知（ntfy.sh）

用户选择了 [ntfy.sh](https://docs.ntfy.sh/subscribe/phone/) 和仅含通用文字的横幅。在本机 `.env` 设置 `NTFY_TOPIC_SECRET` 为随机生成的 **64 位十六进制字符**，文件权限保持 `600`，然后重启 `start`。本机当前已生成此值；不要将其、页面显示的主题或设备凭据粘贴进聊天、截图、版本库或导出。ntfy.sh 公共主题相当于密码，服务端会暂存消息；此应用为每台设备从本机密钥派生不同的不可猜测主题，只发送固定标题 `Personal AI OS` 和正文 `有一条任务提醒，请打开 Personal AI OS 查看。`，不发送任务标题、详情、设备 ID 或邮件内容。[ntfy 发布与主题说明](https://docs.ntfy.sh/publish/)

在 Mac 本机“记忆与设置”展开**有效设备**，复制只在本机显示的 ntfy 主题；在 iPhone ntfy App 中选择服务器 `ntfy.sh` 并订阅该主题，按 iOS 提示允许 ntfy 通知。确认订阅后，回到 Mac 点击“开启此设备通知”。每台设备独立启停；撤销设备会停止后续投递。应用内收件箱仍记录完整提醒。worker 只对**开启后生成且到期**的提醒投递一次；静默时段先推迟到当地结束时间，已错过的提醒不推送。成功、明确失败、超时／结果未知与重启中断分别保存，未知结果不会自动重发。公共服务只证明 HTTP 接受，**真实 iPhone 收到**仍须在手机核对；结果见 [code.md](code.md)。

### iPhone 私人手机入口

应用继续运行在 Mac。独立 `mobile-view` 入口可在手机浏览器查看任务、待审规划、每日计划、候选记忆和外部写入草案；可编辑任务、规划草案及每日草案，并逐项确认计划、每日计划、记忆及外部写入。它复用原 Service 的 `run_id + revision`、日程冲突、任务版本和外部写入幂等检查。手机入口的设备凭据在“记忆与设置”签发、只显示一次，撤销后会话立即失效。登录后的表单要求会话绑定 CSRF、来源与 Host 校验，Cookie 设置 `Secure`、`HttpOnly`、`SameSite=Strict`；登录和会话请求有限速。独立入口默认只监听 `127.0.0.1:8766`，原 Streamlit 仍只监听 `127.0.0.1:8501`，目录或设备设置不会开放网络监听。

Mac 与 iPhone 均须登录本人同一个私人 Tailscale 网络。按 [Tailscale Serve 官方说明](https://tailscale.com/docs/features/tailscale-serve)启用私人 HTTPS，**关闭 Funnel**，只代理手机入口，不代理 Streamlit。Mac 已用 Homebrew CLI 的 userspace 模式实测；这种方式需要保持 `tailscaled` 进程运行。在一个终端启动守护进程，在另一个终端登录并查询本机的实际 `*.ts.net` 域名：

```bash
brew install tailscale
tailscaled --tun=userspace-networking --socket=/private/tmp/personal-ai-tailscale.sock
```

```bash
tailscale --socket=/private/tmp/personal-ai-tailscale.sock up
tailscale --socket=/private/tmp/personal-ai-tailscale.sock status
```

将下例中的域名替换为 `status` 显示的本机完整域名，另开终端启动手机入口，再建立私人 Serve：

```bash
uv run --env-file .env python -m personal_ai_os mobile-view --port 8766 --public-origin https://mac.example.ts.net
```

```bash
tailscale --socket=/private/tmp/personal-ai-tailscale.sock serve --bg http://127.0.0.1:8766
tailscale --socket=/private/tmp/personal-ai-tailscale.sock serve status --json
```

首次启用 HTTPS 时需本人审阅 Tailscale 页面；确认没有启用 Funnel。iPhone 连接 Tailscale 后，在 Safari 打开同一个私人 `https://…ts.net` 地址，输入 Mac“记忆与设置”页签发且只显示一次的设备凭据，并自行保存在密码管理器；手机会话约 12 小时到期，凭据到期或遗失需在 Mac 重新签发。`--public-origin` 必须与 Safari 地址完全相同，只用于 Host/来源校验，**不会改变回环监听地址**。设备丢失时在 Mac 本机撤销并重新签发。Mac 关闭、守护进程停止或手机离开私人网络时入口不可用。真实 iPhone Safari 已验证设备登录、任务编辑、旧版本拒绝、规划及每日计划审批、记忆审核和经逐项确认的 iCloud 事件修改；端口隔离与设备撤销的实机检查见 `code.md`。普通任务手机推送由上述 ntfy.sh 渠道负责；iCloud 事件提醒和应用内收件箱继续按原流程工作。

## 失败恢复、用量与导出

在“执行轨迹”查看失败步骤及原因。失败的规划或反馈运行可由你点击“重试”；系统创建关联的新运行，原失败运行和 Trace 保留。输入、只读数据、角色配置及模型一致时，已校验的只读步骤可复用；输入变化则重新执行。暂态超时和限流最多尝试三次，格式错误与权限拒绝不会自动重试。重试后生成的任务草案仍需你确认，候选记忆仍需逐条审核。

“执行轨迹”展示内置与自定义 Agent 的实际 DeepSeek 请求次数、耗时和可获得的 token 数；缺失的 token 以缺失次数单独显示，不计为零。成本默认不显示。若你知道所用 DeepSeek 模型的单价，可在“记忆与设置”填写每百万输入、输出 token 的美元单价；页面只对有真实 token 数据的请求显示估算小计，**不等于账单**。切换模型后需分别设置单价。

“记忆与设置”提供 P0/P1 业务数据 JSON 和任务 CSV 下载。JSON 包括目标、任务、时间段、重复规则及实例、习惯及打卡、反馈、记忆提案与记忆、每日草案、复盘和提醒；安全的个人设置另列。当前 JSON 不包含 P2 自定义 Agent 定义、待审提案与运行记录。导出只读，不含 `.env`、API 密钥或模型单价。下载文件保存在你选择的位置；当前版本不提供导入功能。备份整个本机应用时，还应保留数据库文件及迁移前自动创建的备份。

## 测试与验收

```bash
uv run --group dev pytest -q
RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_deepseek_smoke.py
RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p1_final_smoke.py
RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p2_custom_deepseek_smoke.py
RUN_DEEPSEEK_SMOKE=1 uv run --env-file .env --group dev pytest -q tests/test_p2_custom_planning_deepseek_smoke.py
uv run --group dev pytest -q tests/test_p2_external.py tests/test_p2_external_ui.py
uv run --group dev pytest -q tests/test_p2_icloud.py tests/test_p2_icloud_ui.py
uv run --group dev pytest -q tests/test_p2_gmail.py tests/test_p2_gmail_ui.py
uv run --group dev pytest -q tests/test_p2_remote.py tests/test_p2_remote_ui.py
uv run --group dev pytest -q tests/test_p2_ntfy.py
uv run --group dev pytest -q tests/test_p2_mobile.py
uv run --group dev pytest -q tests/test_p2_youtube_mcp.py tests/test_p2_youtube_ui.py
```

第一条命令运行确定性测试与 Streamlit AppTest，默认跳过真实 API 冒烟。后面四条命令使用本机密钥，会产生实际 DeepSeek API 用量；P1 场景覆盖混合六 Agent 规划、审批、反馈、人工记忆审核和再次规划，P2 场景覆盖提案审核、手动只读运行、显式选用自定义 Agent 后的规划，以及规划后本机建议审核与策略回退。验收还检查原五 Agent 面试路径、越权拒绝、失败记录、重启持久化和五页面真实数据展示。测试替身仅用于自动化验证，正常启动不会使用模拟模型。

## 后续开发状态

P1 已实现数据库迁移、重复规则与可审核的星期建议、习惯打卡、受限 Life Agent 混合规划、每日草案与重排、本机 worker、提醒、复盘、失败运行恢复、真实用量及本机导出。P2 已实现自定义 Agent、待审提案、显式选用的只读并行规划、主动建议及策略回退、受限外部目录，以及设备授权和受保护视图。Mac iCloud 日历的读取、经审批创建和修改已在用户选定日历上实测，且用户已核对 iPhone 同步与提醒；Gmail 的桌面 OAuth、真实邮件正文读取和逐封确认发送已验收。YouTube 真实公开视频搜索与 DeepSeek 待审资料任务已在隔离数据库实测，并已在本机正式目录启用只读操作。iPhone 私人 HTTPS 入口已实测登录、查看、编辑和逐项审批；端口隔离及设备撤销的实机结果和 ntfy 通知验收状态见 [code.md](code.md)。完整演示步骤见 [process.md](process.md)；完整需求、方案、编码轮次与当前验收状态分别见 [request.md](request.md)、[design.md](design.md)、[skeleton.md](skeleton.md)、[code.md](code.md)。
# Personal-AI
