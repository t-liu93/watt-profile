# M4：完整试点版本

> [English — source of truth](m4.md) · **中文**

状态：软件实现与验证完成，所有者验收待完成，2026-10-15。M1–M3 已验收。M4-01–M4-10 已通过自动检查与独立评审。M4 整体仍须等待所有者浏览器、存储配额及 Pi 4 实物／72 小时证据通过。LOGGER-001 代码已验证，实机和发布仍待完成。业主报告现有 ARM64 Raspberry Pi 4 持续离线采集、积累待上传记录，尚未配置服务器 origin；代理未检查设备或其数据。

## 1. 目标与 Agent 使用方式

交付 **离线采集 → 人工登记 → 自动上传与积压恢复 → Server 查询及 CSV**，修复 [LOGGER-001](../known-issues_zh.md#logger-001--独立试读可能使-logger-卡在-previewing)，并验证集成容器及操作说明。

向 Agent 提供本文和任务编号。实现前阅读 [Agent Guide](../../AGENTS_zh.md)、相关 [M1 契约](m1_zh.md)、[M2 设计](m2_zh.md)、[M3 设计](m3_zh.md)及当前源码。第 2 节固定实现契约，第 3 节规定顺序原子任务，第 4 节包含检查、操作说明和验收证据。按 M4-01 至 M4-10 顺序执行，依赖验收后才推进。每项任务交付自己的测试。本文本身不启动委派，也不授权实现、提交、发布或设备操作。

所有者于 2026-10-14 调整硬件验收边界：必需实物目标为现有 **ARM64 树莓派 4 连续运行 72 小时**。记录实际 RAM，不要求另购 1 GB 板卡。Pi 3B+ 实机兼容留待后续，不宣称已支持。四周离线容量仍须通过合成数据验证；数周现场观察与 M4 验收分别记录。

保留不可变 ID、canonical Snapshot 字节、Token、描述符、Decimal 小数位、缺测值、pending 数据及已验收的 M1 线协议。M4 沿用 `logger_0001` 和 `server_0001`，不计划迁移 schema 或重新生成凭据。下文新增模块、测试、设置和响应字段均为**计划交付物**。M1–M3 历史验收描述当时版本，不得改写成 M4 证据。

不包含：自动登记、Token 轮换 UI、独立上传开关、向其他服务器复制已上传历史、图表／分析、其他 profile／传输、远程升级、宿主机网络、内置反代／TLS 管理、Agent 访问生产环境及额外必需 Pi 型号。变更发布必须使用另行授权的新版本，不移动或覆盖 `v0.1.0-alpha.1`。

## 2. 共享实现契约

### C1. 运行时与模块边界

保留 Python 3.12、单应用进程／worker、安装锁及现有采集 runtime。将仅开发依赖的 HTTPX 提升为运行依赖，重新生成两份 lock。使用长期复用的 `httpx.AsyncClient`，由 FastAPI lifespan 管理一个上传任务；不增加外部 worker、broker 或 Pi 服务。计划模块为 `logger/upload.py`（传输及调度）、`logger/capacity.py`（合成容量 fixture）和 `cli/logger_capacity.py`（入口）；共享契约不依赖两端应用。

分别提供可注入的 transport、时钟／等待和调度接口，使网络行为测试无需真实服务器或实际等待退避。串口和采样沿用现有接口。仓储调用使用短生命周期 session，离开事件循环执行；HTTP 或退避等待期间不得持有数据库事务或采集锁。上传器不取得串口所有权，网络等待期间采集保留自己的 reader。快照写入和上传确认仍为独立的持久事务。

安装、凭据、迁移和 runtime 初始化完成后才启动上传。通过既有身份 helper 读取受保护上传 Token，不把它放入 URL、状态、错误、指标或日志。使用 SQLite 保存的中心端 origin，不由环境变量覆盖。未配置 origin 时，启动、Retry 或关闭均不发送请求。配置 origin 后自动上传，与采集意图和当前现场独立。会话暂停／完成、采集存储／换表阻断本身不关闭已提交记录的回传。

关闭时先禁止调度、中断等待、取消并等待上传任务，关闭 HTTP 资源，再 drain 采集并释放数据库／安装锁。数据库操作已经开始时，必须等待其完成再 dispose；取消线程等待不证明事务已停止。完整有效确认可在关闭期间原子提交，否则记录保留 pending。正常关闭应满足现有 Compose 30 秒宽限；不得因网络时限再额外等待完整请求周期。

### C2. LOGGER-001 与串口资源所有权

现有 `_read()`、`_release()` 和 `_block()` 可能关闭同一句柄；释放异常又可能使 `preview()` 跳过生命周期恢复。这些路径一起修复。读取线程是其打开的每个句柄（包括重连尝试）的唯一关闭者。停止／阻断／释放路径发出取消信号并 join 捕获的 owner，不并发关闭句柄。保留真实输入最多一秒的 read timeout 和有界 join。不 join 尚未启动的线程或当前线程；延迟 join worker 不得把旧 owner 故障记到替代 owner。

临时预览的初始化（包括线程启动）纳入保证执行的清理范围。成功、超时、请求取消、串口错误、启动失败及清理失败都释放预览标记并通知等待者。即使释放失败也完成临时预览生命周期恢复，同时保留独立产生的采集阻断。订阅式预览不影响采集及其串口 owner。预览不创建会话、不绑定电表、不保存快照。

正常释放后恢复控件，可再次预览、配置、创建部署及采集。真实 close／join 失败锁存 `serial_close_failed` 或 `serial_join_failed`，保留未解决资源所有权并禁止替代 reader；不得假报释放成功，也不得留下无解释的 `previewing`。状态持续响应。API 使用固定安全消息及错误码，包括 `serial_join_failed`；不显示私有异常文本／原始报文。页面说明不可用操作，并指引在 owner 真正未释放时仅重启 Logger。不得引入新持久阻断原因或另一套生命周期 schema。

M4-01–02 在从 `main` 建立的预留 `fix/logger-preview-cleanup` 分支完成，保留无关改动；通过后先集成修复，再做上传。此处规定实现分支边界，不代表授权发布镜像。known issues 分别记录代码验证、所有者实机验证及发布状态。

### C3. 中心端 origin 与 pending 组选择

继续只接受 M1 origin：HTTP／HTTPS authority，不含 userinfo、路径、query 或 fragment。公网连接使用 HTTPS；保留所有者隧道内 HTTP 和隔离开发 HTTP。按 scheme、规范化 host 和有效端口比较 origin 身份，不改写历史或凭据。

| 已保存 origin 与修改 | 行为 |
| --- | --- |
| null → 首个非 null origin | 即使有 pending 也允许；设置事务提交后唤醒上传 |
| 相同有效 origin | 允许，不改投其他端点，也不确认记录 |
| 非 null → 不同 origin 或 null，任意会话仍有 pending | 本地 API 返回 409 `pending_uploads_prevent_server_change`，全部设置保持原样 |
| 非 null → 不同 origin 或 null，已无 pending | 允许；已上传历史留在本地／原服务器，不自动重传 |

在同一 SQLite 写事务中重查 pending 并保存设置，仓储层强制执行，不能只靠 JavaScript。仅修改 origin 被拒绝时不暂停采集。串口变更保留 M2 的暂停／drain 行为，设置行仍原子更新。继续检查 expected session。阻断组也算 pending，不得通过清空 origin、手动标记 uploaded 或删除 pending 绕过。非空旧服务器不可达且有积压时，必须先恢复旧服务器再换址。

扩展 `select_pending_group(limit, byte_limit, *, excluded_groups=())`，旧调用保持默认行为。组 key 为不可变 `(device_id, site_id, session_id, source_id, measurement_point_id)`。先选择不在排除组中的最旧 pending，再选择其同归属记录，保留现有接收时间／记录 ID 排序。返回既有 `PendingGroup`，包含原存 canonical 字节、描述符和有界 body。排除不改变内容或上传状态。最多保留一个在途批次，不把全部积压排入内存。数据库／模型损坏须明确停止回传，不跳过坏记录。

### C4. HTTP 请求与响应处理

向 `origin + /api/v1/ingest/batches` POST，携带 `Authorization: Bearer …`、JSON content type 及所选 `PendingGroup.body`。保留 M1 上限：200 条、每条 Snapshot canonical UTF-8 最多 16,384 字节、请求 body 最多 4,194,304 字节；不压缩请求。连接池最多一个连接，启用 TLS 校验，`trust_env=False`，不自动执行 transport 重试或跟随重定向。不得向另一个 origin 重发带凭据的重定向请求。

connect／pool timeout 为 5 秒，read／write timeout 为 30 秒；整个网络交换（包括响应流）另加 30 秒单调时钟总时限。HTTPX 分阶段 timeout 不能替代总时限，见 [HTTPX timeout](https://www.python-httpx.org/advanced/timeouts/) 和[异步流及资源关闭](https://www.python-httpx.org/async/)。

请求 identity 响应编码，以流方式最多读取 65,536 个实际响应字节；拒绝其他 content encoding，不无界解压。完整读取并验证响应后才确认任何记录。只有 HTTP **200**、JSON media type 且 M1 确认有效才算成功。检查版本、设备、完整且唯一的记录 ID 集，再调用既有 `apply_confirmation()` 重验持久内容，原子更新整批本地状态／检查点。普通 2xx 不算确认。

错误／缺少／额外 ID、重复 JSON key、畸形／非 JSON／超限的 200 响应及不支持的响应编码均产生固定 `invalid_confirmation` 并退避，整批保留 pending。超时、断连和不确定响应失败也保留同一批次。确认写入本地失败产生 `upload_storage_error`，pending／检查点保持原样并退避重试；损坏按既有存储故障协调作为致命错误处理。不暴露响应正文、凭据或私有异常。

### C5. 调度、重试与隔离

调度采用单调时钟，显示／持久时间采用 UTC。启动或首次保存 origin 后，有 pending 则立即尝试，否则每十秒扫描。确认成功后立即选下一可上传批次，批次之间主动让出执行，保证本地 API 和采集可响应。暂停／完成／换现场不改归属，不移除旧现场授权。

| 结果 | 调度及本地状态 |
| --- | --- |
| 有效 200 确认 | 原子确认，重置临时退避，立即选择下一可上传组 |
| 401 | 全局 `invalid_credential` 阻断，等待 Retry 或有效 origin 变更；保留所有记录 |
| 403／409／422 | 阻断该组，采用匹配的 M1 错误码或固定 `upload_http_rejected`；继续其他组 |
| 413 | 该组记录上限减半，最低一条，用原存记录重建批次；单条仍拒绝则以 `batch_too_large` 阻断该组 |
| 429／5xx／网络失败／总时限 | 对所选批次按 1／2／4／8／16／32／60 秒封顶退避 |
| 重定向／404／405 | 全局 `upload_endpoint_error` 阻断，不跟随 Location，也不自动换 endpoint |
| 其他永久 4xx | 以 `upload_http_rejected` 阻断该组 |
| 其他 2xx 或无法识别的 HTTP 状态 | `invalid_confirmation` 或 `upload_protocol_error`，有界退避，不确认 |

只有严格 M1 错误响应与 HTTP 状态一致时才信任应用错误码。代理 HTML 或畸形错误响应不确认记录。可重试响应解析 Retry-After 秒数或 HTTP-date，有效值限制在 1–60 秒；缺少／无效则采用退避序列。保留该组缩小的 413 上限，直到 Retry、origin 变更或重启，避免每次成功后再次探测过大批次。

组／全局阻断和临时计数仅存进程内存，绑定有效 origin；显式 Retry 或允许的有效 origin 变更时清除。重启从 pending 重建调度，可再次探测；不得为避免拒绝把记录标为 uploaded。Retry 清除阻断／退避，在 worker 下一个安全边界合并唤醒；不启动第二个请求或与确认事务重叠。凭据／组仍无效时按实际错误重新阻断。旧阻断组绝不能饿死有授权的新组。

### C6. 本地状态、API 与浏览器

保留 M1 `LoggerStatus`，以固定错误码填充 `last_upload_error`，并继续从持久检查点加载 `last_successful_upload_at`。`StatusView` 增加 `upload_mode: enabled|unconfigured` 及类型化 `upload` 观察值：

| 字段 | 契约 |
| --- | --- |
| `state` | `idle\|sending\|backoff\|blocked\|failed\|stopping`；未配置模式的状态为 idle |
| `next_attempt_at` | UTC／null，由单调调度换算，仅作观察 |
| `global_error` | 固定码／null；组错误不变成全局认证阻断 |
| `blocked_group_count` | 非负数；其他组成功后仍可见 |

既有分页会话响应增加该组可空 `upload_error`（MVP 每会话一个 source）。全局错误单独显示。确认成功后清除最近临时错误，但保留其他组错误。runtime 观察值不是新持久列。不包含 Token、原始异常或 fingerprint。

新增受登录及 CSRF 保护的 `POST /api/local/v1/upload/retry`，body `{}`，返回 **202** 和既有 `{data: StatusView}` envelope。只调度工作，不宣称完成上传。无 origin 的 Retry 不发送请求。保留本地固定错误 envelope、同源防护和刷新不重放修改。

替换 M2 未实现上传的 banner 和维护说明。显示当前会话持久新鲜度、所有会话 pending 数／字节／最旧时间、真实最近确认时间、模式／状态／退避、全局及组错误。保留每两秒不重叠轮询、文本转义、主题、键盘访问及登记信息显式展示隐私。已登录时，采集或预览期间也可 Retry upload；它不应被串口忙状态禁用。换址限制显示固定恢复说明，失败保留输入。串口收尾错误不得被 HTTP 上传成功掩盖。

缺少 origin、电表断开、采集阻断、授权拒绝和普通网络退避不影响 readiness。致命存储损坏使 readiness 失败，并通过既有协调释放采集。上传 worker 意外终止也使 readiness 失败且显示固定错误，但健康采集仍独立运行。状态／登录保持可达；普通远端故障不引起容器重启循环。Server 保留 M3 的联系和最新读数语义，不增加 heartbeat endpoint 或推断会话完成。

### C7. 集成与独立容器

保留 Logger／Server 独立启动、私有 PostgreSQL、Server 单 HTTP 发布端口、UID/GID 10001、受保护卷／凭据及 30 秒关闭宽限。`up logger` 仍只启动 Logger 依赖；开发不挂载宿主串口。独立 serial 模式直接使用已存公网／隧道 origin，不使用开发路由。

Server 在 ingest 也校验配置的外部 Host，因此直接访问 `http://server:8080` 并沿用该地址的 Host 会失败。增加一对可选 Logger 部署设置：

| 设置 | 开发契约 |
| --- | --- |
| `WATT_LOGGER_DEV_SERVER_ORIGIN` | 预期已存／浏览器 origin，例如 `http://127.0.0.1:18081` |
| `WATT_LOGGER_DEV_SERVER_CONNECT_ORIGIN` | 容器内 HTTP 连接 origin，`http://server:8080` |

两者同时提供或同时省略，按既有 origin 规则验证；serial 模式出现任一个就拒绝。此隔离接口两者均限 HTTP。根开发 Compose 根据 `WATT_DEV_SERVER_PORT` 设置预期 origin；页面仍须显式保存，环境不覆盖 SQLite。仅已存 origin 匹配时走内部连接，并把 Host 设为预期公开 authority（含端口）。其他 origin 直接连接。不放宽 Server 边界、不信任 forwarded header、不改 Nginx、不使用 host network、不加入内置代理。覆盖默认／自定义端口和独立部署拒绝。

在禁用网络的运行镜像 smoke 中增加已安装上传模块／依赖导入。AMD64 和 ARM64 构建与真实 ARM64 运行分别验证。保留既有发布流程／版本策略，未来发布仍须另行授权。

### C8. 容量、兼容及操作

新增仅合成用途入口 `python -m watt_profile.cli.logger_capacity --output NEW_DIRECTORY --records 241920`，默认 241920，拒绝已存在目录。仅创建新的私有 fixture 安装，不接受 live data directory、服务器 origin 或设备。采用既有 schema、canonical codec 和约束，以有界批次填充；fixture 可合并提交，不改变生产持久性。不得为 fixture 增加上传／确认 endpoint。

使用代表性脱敏三相字段／quality、递进时间戳／计数和正确身份绑定。记录条数、canonical 字节、数据库大小、WAL 和 SHM 峰值／最终值、索引／SQLite 页占用、构建／选择／状态耗时，以及包含 64 MiB 余量的空间需求。另用合法接近上限的 Snapshot 估算空间敏感性；代表性测量不保证所有合法 payload 都能容纳。新目录内提供机器可读合成结果，并用独立测试核对条数／不变量和清理保护。

四周容量通过条件：代表性 241,920 条安装能放进为所有者 Pi 记录的存储分配，保留安全余量，并且有界选择／状态／确认无资源故障。记录真实字节；分配未知或不足时标记待验／失败，并给出实测需求，不虚构通过。不通过填满真实文件系统测压力。保留 M2 停止新增阈值、手动清理限制和不自动驱逐／VACUUM。

先用既有 M2／M3 schema 和无上传器准备安装，再启动 M4：Token／ID／设置／开放会话意图／canonical 记录完全一致，首次 origin 配置可补传积压。也覆盖已有 origin。保留完成／开放／pending 清理规则，测试确认期间在线备份及向独立新目录恢复。记录哪些 uploaded 状态是确认后合法改变。升级不包含迁移、重置、批量无依据确认或强制新建监测会话。

### C9. 实物验收与证据

Agent 只在隔离合成／脱敏资源上验证软件。所有者在自己的 Pi 执行授权部署和实物检查；Agent 不连接或修改生产设备／服务／数据。受跟踪证据不复制私有读数／Token，记录脱敏环境／版本、命令形式、摘要／计数和结果。

先暂停采集并等待串口释放，再验证重复临时预览、不增加持久／pending 快照、控件恢复及同一会话继续采集。probe 或第二个 reader 不与采集并行。验证设备／线材／profile 兼容和升级保留；重启、网络恢复演练安排在连续运行前。所有者管理的宿主网络和上游 TLS 配置不属于应用实现。

随后让现有 ARM64 Pi 4 **连续运行至少 72 小时**，不计划重启或注入故障。通过条件：无无法解释的进程退出、预览／生命周期卡死或 OOM；有有效输入时持续按十秒采样；身份／归属保留；Server／连接可用时确认持续推进、积压可恢复。电表／网络缺测应解释，不标为健康采集。记录实际 RAM／OS、镜像版本／digest、不含标识的电表／profile／线材描述、内存／CPU 及实际磁盘写入观察、起止 UTC、重启／错误、积压／新鲜度和最终查询／CSV 核对。不额外规定任意 CPU／RAM 性能上限。需要修复代码时重跑受影响检查，并重新开始 72 小时验收区间。

## 3. 原子实现任务

所有任务阅读指定契约及相关当前源码，执行 Ruff lint／format 检查和 `git diff --check`，验收后更新本文双语证据。下列文件名固定计划测试入口。仅用合成数据和临时资源；不可运行的检查保留待验。

### M4-01 — 串口 owner 与预览清理修复

依赖：已验收 M1–M3。契约：C2、M2 C4–C5、known issue 验收。

- 在预留修复分支交付 reader 唯一关闭责任、取消／join 协调、保证执行的预览初始化／清理及安全 runtime 故障报告。
- 验收确定性竞争关闭测试、成功／取消／超时／启动失败、真实 close 失败及 join 挂起；无第二 reader、重复关闭或持久预览快照。覆盖采集重连、暂停、尾窗口 drain、换表／存储阻断及关闭意图。
- 边界：无上传器、HTTP 依赖变更、实物设备或发布。检查：`python -m pytest tests/test_logger_runtime.py tests/test_lifecycle.py tests/test_p1_probe.py tests/test_logger_maintenance.py -q`。

### M4-02 — 预览服务、浏览器及容器回归

依赖：M4-01。契约：C2、C6 串口故障行为。

- 交付固定 API 处理、可见的清理失败指引，以及重复预览、取消、状态响应、控件恢复、创建部署和后续采集的 API／浏览器回归。
- 验收正常预览恢复且无 pending；真实资源故障可见并拒绝不安全复用。以 fake／synthetic serial 运行既有 Logger Compose／持久性检查。
- 边界：上传前先完成并集成问题修复；登记代码验证，硬件／发布仍待验。检查：`tests/test_logger_api.py`、`tests/browser/test_logger_ui.py`，以及按既有可丢弃 `watt-m2-...` 契约显式选择的 `tests/integration/test_logger_compose.py`。

### M4-03 — 可上传 pending 组与换址保护

依赖：M4-02。契约：C3、M1 C5–C6。

- 交付带类型的组 key 排除选择、事务性 origin 限制、origin 比较及固定本地错误处理。
- 验收旧 A 阻断／新 B 可选、上限／排序、原字节、积压时 null→首次 origin、同 origin 编辑、pending 时拒绝换址／清空、积压清空后换址，以及插入／配置并发检查。仅改 origin 被拒时不暂停采集、不部分更新设置。
- 边界：仓储／部署操作，不进行真实 HTTP 或人工确认。检查：`python -m pytest tests/test_logger_storage.py tests/test_logger_identity.py tests/test_logger_runtime.py -q`。

### M4-04 — 有界异步 HTTP transport

依赖：M4-03。契约：C1、C4、C7 设置。

- 交付运行依赖／lock、复用异步 transport、设置验证、仅 synthetic 内部路由及可注入 transport 接口。
- 验收原请求字节／header、TLS 校验、无环境代理／重定向重试、连接及总时限、响应大小／编码、严格确认／错误解码、Host／端口保留和 serial 模式拒绝开发路由。
- 边界：不调度或启用应用上传。检查：`python -m pytest tests/test_logger_upload_transport.py tests/test_logger_bootstrap.py tests/test_package.py tests/test_upload_contract.py -q`。

### M4-05 — 调度、确认及恢复

依赖：M4-04。契约：C3–C5。

- 交付立即启动、十秒空闲节奏、成功后连续补传、退避／Retry-After、全局／组阻断、413 上限及合并 Retry。
- 验收单请求在途；响应丢失／无效及确认写入失败保留 pending／检查点；旧阻断组不拖住新组；重启／Retry 安全重建；transport 等待期间继续采集。
- 边界：fake transport／clock 和临时 SQLite，无公共 API 或容器。检查：`python -m pytest tests/test_logger_uploader.py tests/test_logger_storage.py -q`。

### M4-06 — Lifespan、状态及 Retry API

依赖：M4-05。契约：C1、C3、C6。

- 交付上传器启停、类型化状态／会话观察、受保护异步 Retry endpoint 及保存 origin 后唤醒；将致命存储／worker 故障接入既有 runtime／readiness。
- 将历史“没有上传器”断言改为当前未配置／已配置契约；旧人工回传 fixture 必须不自动投递时显式使用 fake transport 隔离，保留归属、隐私和持久性断言。
- 验收无 origin 离线启动且无请求、首次配置补传、独立于采集的自动上传、auth／CSRF、202 语义及合并、换址限制、网络等待期间 API 响应、关闭／重启取消及数据库生命周期。
- 边界：无完整 UI、硬件或远端操作。检查：`python -m pytest tests/test_logger_api.py tests/test_logger_uploader.py tests/test_m2_pipeline.py -q`。

### M4-07 — 上传与维护操作界面

依赖：M4-06。契约：C6。

- 交付真实上传状态、退避／错误解释、Retry、各会话阻断、pending 换址拒绝提示及维护区标签；保留登记／主题／可访问性。
- 验收未配置／离线、登记前 401 后人工恢复、组拒绝与成功并存、预览／采集期间独立 Retry、持久检查点与当前新鲜度、登录过期、不重叠轮询及不重放修改。
- 边界：fake transport／serial 和隔离浏览器应用，无客户账号／图表。检查：`python -m pytest tests/browser/test_logger_ui.py tests/test_logger_api.py tests/test_package.py -q`，使用隔离 Chromium。

### M4-08 — 端到端 HTTP、Compose 及 CI

依赖：M4-07。契约：C4–C7。

- 交付 `tests/test_m4_pipeline.py`、`tests/integration/test_m4_compose.py`、联合开发路由／配置及完整 CI 选择；保留独立构建和既有 Logger-only／Server-only 检查。
- 验收真实 Logger→Server 自动回传与匹配查询／CSV、离线采集后登记、断网继续采样、提交后响应丢失、在途 Logger 终止／重建、401／grant 恢复、A→B→A 身份隔离、清理保护及持久检查点／凭据。
- 边界：仅测试自建故障中间层／容器，无产品故障接口、生产代理或 host network。检查：使用临时 PostgreSQL 的 `python -m pytest tests/test_m4_pipeline.py -q`、显式 `tests/integration/test_m4_compose.py` 及既有 Compose／浏览器回归。

### M4-09 — 容量、升级保留及操作说明

依赖：M4-08。契约：C8–C9、C7 镜像检查。

- 交付容量 CLI／结果和 opt-in `tests/integration/test_logger_capacity.py`、旧安装／备份／恢复回归，以及本文精确升级／回滚、启动／状态／排障说明及双语入口链接。
- 验收 241,920 条代表记录、实测分配／余量、接近上限敏感性、拒绝已有路径、有界内存及可用状态／选择／确认、受保护清理、并发备份与独立恢复。扩展已安装／原生镜像 smoke，分别验证 AMD64／ARM64 构建。
- 边界：仅新合成目录；Agent 不操作真实备份、删除或部署。检查：下文容量 opt-in、`tests/test_logger_maintenance.py`、`tests/test_m4_pipeline.py`、已安装包及镜像 smoke。完整容量 selector 加入 CI，隔离输出并清理。

### M4-10 — 软件验收与所有者试点交接

依赖：M4-01–M4-09。契约：全部。

- 执行全部必需检查，定稿两端页面从配置到 CSV 的精确走查及所有者 Pi 升级／预览／恢复／72 小时说明；在本文双语记录命令、结果及清理。
- 分别验收完整软件证据和所有者浏览器走查。所有者 Pi 4／72 小时证据及容量／存储分配也通过后才标记 M4 验收。未知／不可运行的证据仍待验；不由构建推断发布或实物通过。
- 边界：无新功能、自动发布或额外 milestone 报告文件。检查：完整第 4 节入口及脱敏所有者证据。后续数周现场观察单独记录。

## 4. 检查、操作说明与完成记录

### 计划自动化入口

使用新建隔离 Python 3.12 环境、更新后的开发 lock、隔离 Chromium 和临时 Docker 资源。`bash scripts/ci.sh` 须在既有检查之外加入所有新增必需 selector。默认 pytest 仍跳过浏览器／Compose／容量 opt-in；未选中的 skip 不算通过证据。

```sh
python -m ruff check .
python -m ruff format --check .
git diff --check
env -u WATT_SERVER_DATABASE_URL -u WATT_SERVER_DATABASE_URL_FILE WATT_TEST_POSTGRES=1 python -m pytest -q
env -u WATT_SERVER_DATABASE_URL -u WATT_SERVER_DATABASE_URL_FILE WATT_TEST_POSTGRES=1 python -m pytest tests/browser tests/test_m4_pipeline.py -q
# Each integration invocation uses a distinct new project/tag/private root.
WATT_TEST_M4_COMPOSE=1 WATT_TEST_COMPOSE_PROJECT=NEW_WATT_M4_PROJECT WATT_DEV_IMAGE_TAG=NEW_WATT_M4_PROJECT python -m pytest tests/integration/test_m4_compose.py -q
WATT_TEST_M4_CAPACITY=1 python -m pytest tests/integration/test_logger_capacity.py -q
```

新 Compose fixture 在创建前验证 `watt-m4-...` 名称、空闲回环端口，以及所有项目容器（含已停止）、网络、卷和精确应用 tag 均不存在。fixture 负责启停并在最后断言资源消失。容量仅创建新的私有临时根目录。CI 显式执行两者及既有 Server 恢复、Logger／Server 独立部署回归，仅清理本次资源。实现时记录真实耗时、版本、构建／运行架构及 opt-in 结果，此处不预填成功。

### 计划的组合开发走查

这是一次使用开发环境生成的合成读数和凭据的双页面隔离验收，不代表所有者浏览器或 Pi 验收。选择新的 project 名称、私有输出目录及空闲回环端口。以下预检如发现项目容器（包括已停止）、网络、卷、精确应用 tag 已存在或端口被占用，就会失败；此时保留全部现有资源，换用新名称／端口。请在仓库根目录运行：

```sh
set -euo pipefail
M4_PROJECT="watt-m4-owner-$(date +%s)-${RANDOM}"
M4_LOGGER_PORT=18880 M4_SERVER_PORT=18881
export WATT_DEV_IMAGE_TAG="$M4_PROJECT" WATT_DEV_PORT="$M4_LOGGER_PORT" WATT_DEV_SERVER_PORT="$M4_SERVER_PORT"
test -z "$(docker container ls -aq --filter "label=com.docker.compose.project=$M4_PROJECT")"
test -z "$(docker network ls -q --filter "label=com.docker.compose.project=$M4_PROJECT")"
test -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$M4_PROJECT")"
if docker image inspect "watt-profile-logger-dev:$M4_PROJECT" >/dev/null 2>&1; then echo "Refusing existing Logger image tag" >&2; exit 1; fi
if docker image inspect "watt-profile-server-dev:$M4_PROJECT" >/dev/null 2>&1; then echo "Refusing existing Server image tag" >&2; exit 1; fi
python3 -c 'import socket,sys; ports=list(map(int,sys.argv[1:])); sockets=[]
try:
 for port in ports:
  s=socket.socket(); s.bind(("127.0.0.1",port)); sockets.append(s)
finally:
 [s.close() for s in sockets]' "$M4_LOGGER_PORT" "$M4_SERVER_PORT"
docker compose -p "$M4_PROJECT" -f docker-compose.dev.yml config --quiet
M4_PRIVATE_ROOT=$(mktemp -d /tmp/watt-m4-owner.XXXXXXXX)
docker compose -p "$M4_PROJECT" -f docker-compose.dev.yml up --build -d --wait --wait-timeout 120 logger server
docker compose -p "$M4_PROJECT" -f docker-compose.dev.yml ps --all
```

打开 Logger `http://127.0.0.1:18880` 和 Server `http://127.0.0.1:18881`。使用仅限开发环境的 `operator` 和 `admin` 账号，密码均为 `testtesttesttest`；这些页面不是所有者账号。按以下步骤操作并记录观测到的数量／时间，不要将合成数据复制到受版本管理的证据：

1. 在 Logger 尚未创建现场时重复执行读数预览。确认控件恢复且没有预览快照写入；尚未配置 origin 时 pending 保持为零。创建现场 A 和暂停部署，保持 Server origin 为空并开始合成采集，直到至少保存三条快照；观察当前会话保存时间和 pending 数量增加。origin 配置和登记期间继续采集，以验证投递时新读数持续写入。
2. 在 Logger 设置中保存 Server origin，保留串口／profile 设置。观察 `invalid_credential`，且采集持续进行。尝试替换或清除该 origin，确认 pending-origin 保护拒绝操作，已填设置保留且采集未暂停。
3. 展示并复制 Logger 登记摘要，在 Server 预览并创建登记、授予现场 A。返回 Logger，点击 Retry，观察 pending 数下降、持久确认时间推进。不要人工重放 ingest 请求。
4. 待积压清空后暂停采集，直到保存数量稳定。在 Server 查询现场 A，先把分页大小设为 1，逐页浏览结果，再导出完整筛选 CSV。将 CSV 行数和 record ID 集合与筛选 query 和 Logger 会话数量核对，并确认现场／会话归属。无论分页大小如何，导出都须包含全部匹配记录。
5. 暂停并结束 A，创建 B，再通过现有 UUID 返回 A 并创建新会话。确认记录仍归属其原现场／会话／source。按需登记并授权 B；测试故障检查阻断 A 时，确认可投递的 B 记录仍上传，恢复 A 授权后 Retry。
6. 按下方隔离 HTTP 和 Compose 故障检查执行断网、提交后响应丢失及 Logger 重建。确认断网期间采集继续、重试去重，并在恢复连接／授权后补传积压。应用不提供故障注入接口。
7. 检查清理资格：已完成会话中已上传行可以清理；开放会话及所有 pending 记录都保留。重启／重建隔离项目并确认身份、凭据、数据和 checkpoint 持久化。

查看就绪状态和日志，然后只删除本次走查创建的资源。保留基础镜像／构建缓存及其他项目：

```sh
docker compose -p "$M4_PROJECT" -f docker-compose.dev.yml ps --all
docker compose -p "$M4_PROJECT" -f docker-compose.dev.yml logs --tail=100 logger server
docker compose -p "$M4_PROJECT" -f docker-compose.dev.yml down --volumes --remove-orphans
docker image rm "watt-profile-logger-dev:$M4_PROJECT" "watt-profile-server-dev:$M4_PROJECT"
rm -rf -- "$M4_PRIVATE_ROOT"
test -z "$(docker container ls -aq --filter "label=com.docker.compose.project=$M4_PROJECT")"
test -z "$(docker network ls -q --filter "label=com.docker.compose.project=$M4_PROJECT")"
test -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$M4_PROJECT")"
if docker image inspect "watt-profile-logger-dev:$M4_PROJECT" >/dev/null 2>&1; then echo "Logger image tag remains" >&2; exit 1; fi
if docker image inspect "watt-profile-server-dev:$M4_PROJECT" >/dev/null 2>&1; then echo "Server image tag remains" >&2; exit 1; fi
```

如果启动或验收步骤失败，保留日志并将证据标为失败；不得标记走查通过。任何重跑都使用新的 project、端口及私有根目录。禁止全局 Docker prune，也不要清理预先存在的资源。

隔离 HTTP、capacity 和 Compose selector 也会管理其临时资源，并拒绝已存在的名称／资源。使用锁定 Python 环境和新的 project／basetemp 路径运行明确的故障检查：

```sh
M4_CHECK_ROOT=$(mktemp -d /tmp/watt-m4-check.XXXXXXXX)
M4_CHECK_PROJECT="watt-m4-check-$(date +%s)-${RANDOM}"
env -u WATT_SERVER_DATABASE_URL -u WATT_SERVER_DATABASE_URL_FILE WATT_TEST_POSTGRES=1 \
  python -m pytest tests/test_m4_pipeline.py -q --basetemp="$M4_CHECK_ROOT/http"
WATT_TEST_M4_COMPOSE=1 WATT_TEST_COMPOSE_PROJECT="$M4_CHECK_PROJECT" \
  WATT_DEV_IMAGE_TAG="$M4_CHECK_PROJECT" WATT_DEV_PORT=18580 WATT_DEV_SERVER_PORT=18581 \
  python -m pytest tests/integration/test_m4_compose.py -q --basetemp="$M4_CHECK_ROOT/compose"
rm -rf -- "$M4_CHECK_ROOT"
```

使用空闲端口；Compose selector 会预检项目资源、已停止容器、精确 tag 和回环端口，仅清理自己的资源并断言其已消失。HTTP selector 管理独立 PostgreSQL。两个 selector 只使用合成数据；失败调用属于失败证据，修复原因后须使用新 project 和 basetemp 重跑。

### 所有者 Pi 升级与验收交接

M4-09–10 在所有者执行前已通过可丢弃安装验证精确命令。以下命令运行前，先将 `WATT_LOGGER_PROJECT` 设置为该现有部署的准确 project 名称。遵守既有[备份／恢复契约](m2_zh.md#c5-存储压力清理与备份)和[已发布镜像部署](../ci-and-release_zh.md#树莓派拉取-logger)。沿用现有 Compose project、数据卷、设置、密码和设备映射；修改 project 可能选中空卷，并非升级。已发布的 `v0.1.0-alpha.1` 保持不变，不含上传器且仍受 LOGGER-001 影响；安装未来 M4 镜像须另行授权发布。

1. 记录当前镜像引用和设备 UUID。旧 Logger 仍运行时，使用下方[隔离集成故障 driver](#隔离集成故障驱动)中的精确在线备份及 `docker cp` 命令，指定新建宿主目标目录和唯一容器临时路径。重建前确认三个文件及权限正确；仅留在容器 `/tmp` 的副本会在重建时丢失。不打印 Token／备份内容，也不只复制 live SQLite 主文件。
2. 另行授权发布后，在现有忽略 env 文件里设置新固定 Logger 镜像，然后执行 `docker compose --env-file deploy/logger/.env.local -p "$WATT_LOGGER_PROJECT" -f deploy/logger/compose.yml up --no-build --pull always -d --wait --wait-timeout 120 logger`。沿用 project、数据卷、设置、密码文件及设备映射。不删除卷、不重新生成凭据／会话。现有所有者安装无 origin，因此已积累 pending 也允许首次保存。
3. 配置真实 Server 公网 HTTPS origin（或所有者隧道内 HTTP），登记现有设备及所需现场，然后 Retry。观察新旧记录保留原归属、积压下降和真实确认。Agent 检查不连接这些服务。
4. 暂停采集并等待 Logger 状态显示串口 owner 已释放。重复执行临时预览；确认预览控件恢复、未写入预览快照、pending 未因预览增加，并且恢复采集后仍是同一开放会话。采集期间不得并行运行 probe 或第二个串口 reader。在连续运行前，分别演练 Logger 重启和 Server／网络中断恢复，确认持久身份与积压恢复。记录 Pi 实际 RAM、与测量需求对照的可用存储配额、OS、镜像版本／digest、不含标识符的电表／profile／线缆描述、内存／CPU／磁盘写入以及运行起止 UTC。然后至少连续运行 72 小时，期间不计划重启或注入故障；记录重启／错误、有效样本缺口、上传积压／新鲜度及最终 query／CSV 检查。本文只保存脱敏结果，私有读数和备份留在本地。
5. 若新进程未就绪，执行 `docker compose --env-file deploy/logger/.env.local -p "$WATT_LOGGER_PROJECT" -f deploy/logger/compose.yml ps --all` 和 `docker compose --env-file deploy/logger/.env.local -p "$WATT_LOGGER_PROJECT" -f deploy/logger/compose.yml logs --tail=100 logger`；保留原卷。schema 未变更时，回滚即设置旧固定镜像引用并执行同一 `up --no-build --pull always -d --wait --wait-timeout 120 logger`。记录仍在，但旧版不能上传且仍有 LOGGER-001。先用合成副本验证回滚。不把旧备份覆盖 live 安装当作常规回滚，因为会丢失较新已提交记录。灾难恢复按 M2 写入新／空目录。

说明不得包含真实设备 `down --volumes`、全局 Docker prune、生产数据库恢复或上游配置修改。普通升级和实物检查仍由所有者执行。发布版本／digest 和实际命令结果在授权／执行后补入。

### 验收记录

| 必需结果 | 当前证据 |
| --- | --- |
| M4 设计及双语契约／任务卡 | 设计就绪；2026-10-15 已完成 M4-01–M4-10 软件自动验收 |
| LOGGER-001 runtime／API／浏览器／容器修复 | M4-01–02 代码检查与独立评审通过；实机／发布待完成 |
| 自动上传器、组隔离及精确确认 | 传输／调度器／应用／API／UI 及真实 HTTP／容器检查和独立评审通过 |
| 端到端 HTTP／Compose 恢复、查询／CSV 和完整 CI | 完整 `bash scripts/ci.sh` 及全部 opt-in、隔离双页面浏览器走查均已通过；所有者浏览器验收仍待完成 |
| 四周合成容量及所有者足够的存储分配 | 合成检查通过；含预留实测需求 1,374,522,219 字节；所有者配额／适配待验 |
| 升级／备份／恢复及 AMD64／ARM64 镜像检查 | 软件检查通过；AMD64 原生、ARM64 模拟；ARM64 实机待验 |
| 所有者集成浏览器走查 | 待执行 |
| Pi 4 ARM64 适配、资源及连续 72 小时 | 待执行；M4 前的现有采集不算 M4 上传验收 |
| 新版本发布授权／发布 | 独立操作，不宣称已发布 |

在本文及英文镜像追加实际任务检查／结果、重要限制、清理及脱敏所有者验收。全部必需软件、容量、走查和 Pi 4 检查通过后才关闭 M4。文档完成、临时恢复、镜像构建成功或 M4 前已有采集都不代表该阶段完成。

### 任务执行证据

- **M4-01 — 自动验收，2026-10-15：** 已在 `fix/logger-preview-cleanup` 实现 reader 独占串口关闭、捕获 owner 的取消/join 协调及 preview 保证清理。runtime/lifecycle/probe/maintenance 选择器 **120 passed**；Ruff lint/format 和 `git diff --check` 通过。全新独立评审通过。全部检查使用临时 SQLite 与假串口；API/浏览器/容器回归在 M4-02 完成。实机和发布仍待完成。

- **M4-02 — 自动验收，2026-10-15：** 固定串口 close/join API 错误及可见的重启恢复提示；验证状态可响应、重复预览后恢复部署/采集。API/runtime/synthetic **58 passed**；隔离 Chromium **7 passed**；既有 Logger Compose/persistence **1 passed，107.12 秒**。Ruff lint/format 和 diff check 通过。全新独立评审通过（API 27、浏览器 7）；评审者静态检查 Compose，未重复运行。一次性 `watt-m2-m4-1792079540-32232` 容器、卷、网络及精确应用标签均清理并复查为空。修复在 M4-03 前集成；业主实机和发布仍待完成。

- **M4-03 — 自动验收，2026-10-15：** typed 组键排除、最旧合格组选择及事务内 pending-origin 保护已实现；原样保留 canonical bytes，origin-only 拒绝不暂停采集。最终 storage/identity/runtime **113 passed**，加 API **140 passed**，Ruff lint/format 与 diff check 通过，全新独立评审通过。三轮修复解决 IDNA2003 混淆不同目标、ASCII 主机兼容及已允许但无法编码主机的配置恢复问题。增加直接运行时 idna 依赖，两份锁真实重生成且版本 pin 不变；HTTPX 提升仍留给 M4-04。仅使用临时 SQLite/假串口，无真实 HTTP。

- **M4-04 — 自动验收，2026-10-15：** 已实现连接复用、整次网络截止时间、受限原始响应流、严格确认/错误解码和合成输入专用内部路由。HTTPX 提升为运行依赖，两份锁真实重新生成（dev lock 内容不变）。原选择器 **87 passed**，Ruff lint/format、diff check 通过。全新独立评审通过，并独立检查 TLS/proxy/redirect/retry 默认值、连接限制、取消传播和流关闭、IPv6/端口及 `pip check`。仅使用 fake transport，无调度/应用激活、真实网络或容器验证。

- **M4-05 — 自动验收，2026-10-15：** 已实现单航班调度、确认后连续补传、10 秒空闲扫描、单调退避/Retry-After、全局/组阻断、保留的 413 组限额、边界合并 Retry 与确认原子写入。Uploader/storage **91 passed**；全新独立评审另跑 transport/contract **75 passed**，Ruff lint/format 与 diff check 通过。临时 SQLite/假传输验证歧义响应保持原批次、存储失败/checkpoint、损坏停止、取消等待已开始 DB 操作及合成 reader 在 HTTP 等待期间继续入库。异步跨线程测试需权限提升以绕过沙箱自唤醒限制；未连接真实网络/设备。应用/API/容器激活留给后续任务。

- **M4-06 — 自动验收，2026-10-15：** lifespan 已拥有上传任务/HTTP 资源，已接入 typed `StatusView.upload`、会话组错误、持久确认 checkpoint、受认证/CSRF 保护的 `202 {data: StatusView}` Retry 及配置提交后唤醒；M1 `LoggerStatus` 未改变。API/uploader/M2 pipeline **67 passed**，Ruff lint/format、diff check 通过。全新独立评审通过，额外两项隔离探针验证意外取消不停止健康采集、致命存储停止并释放串口。覆盖首次 origin 的 completed backlog 自动确认、网络等待状态响应、pending-origin 拒绝、worker readiness 及关闭/构造/HTTP close 异常清理。仅假传输/串口与临时 SQLite；浏览器/容器留待后续。

- **M4-07 — 自动验收，2026-10-15：** 页面已显示真实上传模式／状态／退避／错误、组错误和持久确认时间；独立 Retry、origin 拒绝后保留输入及新版维护说明已实现。最终 browser/API/package **49 passed**，Ruff lint/format、diff check 通过，全新独立复评通过。一轮修复解决维护自动刷新丢键盘焦点及在途分页导航丢失；新增确定性延迟浏览器回归。仅隔离 Chromium、假传输／串口和临时 SQLite；真实 Server／容器集成在 M4-08。

- **M4-08 — 自动验收，2026-10-15：** 实现真实 HTTP/PG 投递、提交后响应丢失、身份/checkpoint 持久化，以及完整 Compose 断网、in-flight 强杀、授权恢复和 A→B→A 隔离。管线 **1 passed**；完整 Compose **1 passed，122.54 秒**；既有 Logger-only **1 passed**、Server Compose **4 passed**、recovery **1 passed**、Logger/Server 浏览器 **11/10 passed**、CI 资源保护 **3 passed**。Ruff lint/format、shell 语法及 diff check 通过。全新独立评审通过，另跑管线 **1**、完整 Compose **1（122.21 秒）** 和 browser/transport/bootstrap **41**。所有一次性项目及精确应用标签均清理并核对为空。完整 CI／容量及架构检查在 M4-09–10 执行；未改动实机或已发布镜像。

- **M4-09 — 自动验收，2026-10-15：** 实现仅合成数据的容量 CLI、CI 显式全量容量选择器、旧安装 lifespan／确认／并发备份、备份复制出容器／重建／恢复检查、安装镜像覆盖及双语操作说明。全量容量 **1 passed，127.65 秒**；维护 **60**、真实 HTTP／旧安装管线 **3**、wheel **1**；Logger Compose **1（116.18 秒）**。四种架构镜像构建及禁网 smoke 通过（AMD64 原生、ARM64 QEMU），真实旧镜像→M4→旧镜像保留验证通过。最终独立评审通过：容量 **1（127.85 秒）**、维护／管线／wheel **64**；Ruff／format／shell／diff 检查通过。两轮修复解决中文可执行命令遗漏，以及近上限估算遗漏持久化投影的问题。最终代表性需求 **1,374,522,219 字节**；500 条真实 SQLite 近上限样本给出独立标注的 **8,048,124,887 字节**估算。所有者配额／适配、原生 ARM64 及 72 小时验收待完成。详细测量见下文；未改生产数据或发布版本。

- **M4-10 — 自动验收与所有者交接，2026-10-15：** 十项软件任务均已通过检查和独立评审。`bash scripts/ci.sh` 于 2026-10-15 退出码为 0。Ruff lint 通过；Ruff format 显示 121 个文件格式正确。默认 pytest：**681 passed、29 skipped，80.16 秒**；显式 browser：**21 passed，76.38 秒**；Server Compose：**4 passed，77.11 秒**；Server recovery：**1 passed，19.35 秒**；Logger Compose：**1 passed，116.39 秒**；M4 HTTP pipeline：**3 passed，23.84 秒**；M4 组合 Compose：**1 passed，128.66 秒**；全量 capacity selector：**1 passed，122.76 秒**。所有 opt-in 均已运行。计数存在重叠，因为默认 selector 也包含 pipeline 检查；不得相加为唯一测试总数。另有一条既有 Starlette/httpx 弃用警告。CI 清理 trap 已核对无本次容器、网络、卷或精确 Logger 镜像标签遗留，并删除私有临时子目录。隔离双页面 Chromium 走查已在一次性 project `watt-m4-owner-1792089112-30730` 通过，Logger／Server 端口为 18890／18891。验证了三条离线 pending、页面登记并 Retry、分页大小为 1 时四行 CSV 及精确 record ID／内容匹配（18 项指标、缺测、fields 和 quality）、A→B→A 复用 measurement point 且保持三个独立 session/source、清理已完成且已上传的 Logger 记录而 Server 历史仍保留，以及 Logger 重建后暂停的开放会话身份、数据和 checkpoint 均保留。最终项目清理以退出码 0 完成：核对确认全部容器（含已停止及 init）、网络、卷和两个精确应用镜像 tag 均不存在；私有临时根目录已删除。此前失败的 driver 项目也在重新走查前清理并确认无残留。隔离 builder 和架构镜像 tag 均已移除；原默认 builder 未改变。全新 M4-10 独立评审通过，核对了双语 shell 语法／等价性、本地链接及 CI／故障驱动覆盖；本次文档评审未重复完整软件测试。所有者浏览器验收、Pi 存储配额、原生 ARM64／实物适配及连续 72 小时运行仍待完成。尚未宣称 M4 验收或发布。

### 容量与镜像验证

2026-10-15 在 Linux x86_64、Python 3.12.13、Docker 29.8.0、Compose v5.5.1、Buildx v0.37.1 上测量。最终 CLI 样本包含全部 18 项三相指标及脱敏字段、递进的十秒时间戳／计数器和私有合成安装。机器结果由工具写入 `capacity.json`；不跟踪样本数据库或 Token。维护检查 **60 passed**；真实 HTTP 及两种旧安装／确认／备份变体 **3 passed**；安装 wheel 打包 **1 passed**。修改后的 Logger Compose 备份复制出容器／重建／复制回容器／恢复 **1 passed，116.18 秒**。`watt-m2-m4-1792087079-5309` 项目容器、卷、网络及精确应用标签均删除并核对为空。Ruff lint/format（121 文件）、shell 语法及 diff check 通过。

| 最终代表性测量 | 字节／结果 |
| --- | --- |
| 记录／已确认／仍 pending | 241,920 / 200 / 241,720 |
| 首条代表性 canonical Snapshot／实际 canonical 总量 | 2,697 / 658,715,925 字节 |
| SQLite 数据库／页大小／页数 | 1,299,156,992 字节 / 4,096 / 317,177 |
| WAL 峰值／SHM 峰值；最终 WAL／SHM | 8,223,552 / 32,768；0 / 0 字节 |
| 保守的数据库 + WAL 峰值 + SHM 峰值 | 1,307,413,312 字节 |
| 私有安装元数据 | 43 字节 |
| 含元数据与 64 MiB 预留的需求 | **1,374,522,219 字节（约 1.28 GiB）** |
| 合法近上限 Snapshot／500 条 SQLite 样本／数据库敏感性估算 | 16,384 / 16,633,856 / 8,048,124,887 字节；仅为估算 |
| 构造／选组／状态／200 条确认 | 109.90 / 8.55 / 0.36 / 0.47 秒 |
| 所有者分配／容量验收 | 未知／待完成；不宣称 Pi 容量通过 |

全量 opt-in 样本在 127.65 秒通过，独立检查计数、codec／身份绑定、字段脱敏、私有权限、SQLite 页／索引及受保护清理。开放会话清理不删除记录；完成会话后只删除 200 条已确认记录，保留全部 241,720 条 pending。临时输出已删除。代表性容量与时间是本机观测值，不是全部合法 Snapshot 的上限，也不是 Pi 资源测量。

近上限敏感性来自一个单独的私有 SQLite 安装，其中有 500 条合法的 16,384 字节 canonical Snapshot。样本建立自己的安装／会话／source 绑定，并通过已接受 schema 和 `_projection` 插入，因此观测到的 16,633,856 字节数据库包含 canonical 文本、重复保存的 fields／quality／metric projections、索引及 SQLite 页开销。估算以代表性数据库实测值为基线，再将近上限与代表性行的每行数据库增长差外推至 241,920 条记录。该有界样本修正了先前只按 canonical 字节增长的低估；仍属外推，页分裂、碎片、payload 形状和字段／索引分布均会影响存储。上表代表性需求包含 WAL／SHM 和预留空间，敏感性估算不包含它们。

两种产品均使用私有 `DOCKER_CONFIG` 及临时 `docker-container` builder `watt-m4-arch20261015` 构建。实际命令形式为 `docker buildx build --builder watt-m4-arch20261015 --platform linux/ARCH --load --provenance=false -f deploy/COMPONENT/Dockerfile -t watt-profile-COMPONENT:m4-20261015-ARCH .`；未 push。本地镜像 ID：

| 产品／架构 | 本地镜像 ID | Smoke 执行 |
| --- | --- | --- |
| Logger / AMD64 | `sha256:e6700750689b5ece27e8d0490e90e47dd7767f238ccf9b3e736e271877c2df41` | 原生 x86_64，通过 |
| Logger / ARM64 | `sha256:1dc14a60ce55c3ee77aa00666333a6cdaf2d0a61c626c2100d510f073d6f7747` | BuildKit QEMU，通过 |
| Server / AMD64 | `sha256:15546d24dce8a5a7949c687e40da0f94dce3694c8b57fe25aa67738efb145411` | 原生 x86_64，通过 |
| Server / ARM64 | `sha256:b683a2b439b96587d859b585315822752d49cecdbadf8f5658378cc4692689b7` | BuildKit QEMU，通过 |

原生 smoke 使用 `docker run --rm --network none -i --entrypoint python IMAGE - COMPONENT amd64 < scripts/image_smoke.py`。ARM64 使用临时 Dockerfile：完整保留产品 Dockerfile，再添加 `RUN --network=none python - COMPONENT arm64` heredoc，内嵌 smoke 脚本。仅临时模拟测试副本把 CLI 子进程期限由 10 秒延长到 120 秒；断言及应用代码不变。Logger／Server 模拟 smoke 分别在 31.75／11.08 秒通过。未注册宿主 binfmt，也未更改默认 builder；原生 ARM64／Pi 执行仍待完成。最终构建期间源码／包／smoke 输入哈希保持不变。

另一个一次性 AMD64 项目运行从 `a6359f81d270ee9c0970e5e1131e94464148ab31` 构建的真实 M4 前镜像，在同一合成卷升级到 M4，再回滚旧镜像。每次重建后，两条 pending canonical 记录、安装身份、Token、设置及暂停的开放会话意图均一致。私有在线备份在重建前复制到宿主机。该项目容器／网络／卷及两个精确临时镜像标签均删除并核对为空。这是软件兼容性证据；未更改已发布镜像或真实安装。

### 隔离集成故障驱动

HTTP、容量和 Compose 选择器只创建各自的一次性资源，不接管已有走查或生产项目。使用锁定的 Python 环境显式执行完整四周容量 fixture：

```sh
WATT_M4_CAPACITY_ROOT=$(mktemp -d /tmp/watt-m4-capacity.XXXXXXXX)
WATT_M4_CAPACITY_PYTHON=${WATT_CI_PYTHON:-python}
WATT_TEST_M4_CAPACITY=1 "$WATT_M4_CAPACITY_PYTHON" -m pytest \
  tests/integration/test_logger_capacity.py -q \
  --basetemp="$WATT_M4_CAPACITY_ROOT/pytest"
rm -rf -- "$WATT_M4_CAPACITY_ROOT"
```

此选择器为 opt-in（`WATT_TEST_M4_CAPACITY=1`）；`scripts/ci.sh` 会设置它，并使用自己临时根目录下的新子目录。若要保留机器可读测量结果以供复核，可创建私有临时父目录并单独调用 CLI：

```sh
WATT_M4_CAPACITY_ROOT=$(mktemp -d /tmp/watt-m4-capacity.XXXXXXXX)
WATT_M4_CAPACITY_OUTPUT="$WATT_M4_CAPACITY_ROOT/fixture"
python -m watt_profile.cli.logger_capacity --output "$WATT_M4_CAPACITY_OUTPUT" --records 241920
```

CLI 会创建全新私有合成安装和 `capacity.json`，不接受 live 路径或 origin。结果分别列出单条代表性快照 `representative_snapshot_canonical_bytes` 和全部 241,920 条记录的 `total_canonical_bytes`。查看 `required_bytes_with_metadata_and_64_mib_reserve`：该保守值将实测数据库大小、WAL 与 SHM 峰值、安装元数据（包括私有 Token 文件）及 64 MiB 预留相加。与所有者记录的 Pi 分配比较，然后只移除此轮临时根目录。分配值提供前，记录实测需求但容量仍待验；近上限值只是独立敏感性估计，不能代替代表性测量。

Logger Compose 验收会测试完整在线备份、复制到宿主机、容器重建后复制回来，并仅恢复到空的临时目标。所有者安装使用同样流程：为宿主备份新建 0700 私有目录，并选唯一容器临时路径。沿用现有运行 project、env、卷及 Logger：

```sh
WATT_LOGGER_PROJECT="SET_TO_EXISTING_PROJECT" # replace with the exact existing project name
OLD_PINNED_IMAGE=ghcr.io/OWNER/IMAGE@sha256:OLD_DIGEST
NEW_PINNED_IMAGE=ghcr.io/OWNER/IMAGE@sha256:NEW_DIGEST
WATT_LOGGER_BACKUP_DIR=/absolute/private/new/backup-directory
WATT_LOGGER_BACKUP_TMP=/tmp/watt-profile-backup-unique
test -n "$(docker compose --env-file deploy/logger/.env.local -p "$WATT_LOGGER_PROJECT" \
  -f deploy/logger/compose.yml ps -q logger)"
mkdir -m 700 "$WATT_LOGGER_BACKUP_DIR"
docker compose --env-file deploy/logger/.env.local -p "$WATT_LOGGER_PROJECT" \
  -f deploy/logger/compose.yml \
  exec -T logger python -m watt_profile.cli.logger_admin backup \
  --data-dir /var/lib/watt-profile --output "$WATT_LOGGER_BACKUP_TMP"
WATT_LOGGER_CONTAINER=$(docker compose --env-file deploy/logger/.env.local -p "$WATT_LOGGER_PROJECT" \
  -f deploy/logger/compose.yml ps -q logger)
docker cp "$WATT_LOGGER_CONTAINER:$WATT_LOGGER_BACKUP_TMP/." "$WATT_LOGGER_BACKUP_DIR/"
chmod 700 "$WATT_LOGGER_BACKUP_DIR"
chmod 600 "$WATT_LOGGER_BACKUP_DIR"/logger.sqlite3 \
  "$WATT_LOGGER_BACKUP_DIR"/upload-token "$WATT_LOGGER_BACKUP_DIR"/manifest.json
test -s "$WATT_LOGGER_BACKUP_DIR/logger.sqlite3" \
  && test -s "$WATT_LOGGER_BACKUP_DIR/upload-token" \
  && test -s "$WATT_LOGGER_BACKUP_DIR/manifest.json"
docker compose --env-file deploy/logger/.env.local -p "$WATT_LOGGER_PROJECT" \
  -f deploy/logger/compose.yml config --quiet
WATT_LOGGER_IMAGE="$NEW_PINNED_IMAGE" docker compose --env-file deploy/logger/.env.local \
  -p "$WATT_LOGGER_PROJECT" -f deploy/logger/compose.yml \
  up --no-build --pull always -d --wait --wait-timeout 120 logger
docker compose --env-file deploy/logger/.env.local -p "$WATT_LOGGER_PROJECT" \
  -f deploy/logger/compose.yml ps --all
docker compose --env-file deploy/logger/.env.local -p "$WATT_LOGGER_PROJECT" \
  -f deploy/logger/compose.yml logs --tail=100 logger
```

执行前先将 `WATT_LOGGER_PROJECT` 设为当前安装的准确 Compose 项目名，并确认它仍使用原数据卷、设置、密码和设备映射；更换项目名可能选中空卷。将 `OLD_PINNED_IMAGE` 与 `NEW_PINNED_IMAGE` 替换为实际软件包的不可变 digest 引用。升级前用同一 env／项目执行 `config --quiet`，并核实当前容器、命名卷、镜像引用及 manifest 文件。完成备份、复制和隔离恢复验证后，才可在另行授权发布后执行升级。妥善保管宿主副本，不打印 Token、哈希或 manifest；保留旧固定镜像引用用于回滚。

若新 Logger 不健康，保留 project／数据卷并检查上面的 `ps --all` 与日志命令。在已登录 UI 中检查上传模式／状态、下次尝试时间、全局／组错误及 pending 数；普通网络退避期间等待，修复凭据／授权／origin 后点 Retry；若状态显示 `upload_storage_error` 或 `storage_full`，先处理本地空间。存在 pending 记录时不要清空非空 origin。schema 未变时可这样回滚：

```sh
WATT_LOGGER_IMAGE="$OLD_PINNED_IMAGE" docker compose --env-file deploy/logger/.env.local \
  -p "$WATT_LOGGER_PROJECT" -f deploy/logger/compose.yml \
  up --no-build --pull always -d --wait --wait-timeout 120 logger
```

恢复仅用于灾难恢复，并非日常回滚。临时 Compose 验收会在容器重建后测试将备份复制回来，再用 CLI 恢复到新建空目录：

```sh
WATT_LOGGER_CONTAINER=$(docker compose --env-file deploy/logger/.env.local \
  -p "$WATT_LOGGER_PROJECT" -f deploy/logger/compose.yml ps -q logger)
docker exec "$WATT_LOGGER_CONTAINER" mkdir -m 700 /tmp/watt-profile-backup-copy
docker cp "$WATT_LOGGER_BACKUP_DIR/." \
  "$WATT_LOGGER_CONTAINER:/tmp/watt-profile-backup-copy/"
docker exec --user 0 "$WATT_LOGGER_CONTAINER" chown -R 10001:10001 \
  /tmp/watt-profile-backup-copy
docker exec "$WATT_LOGGER_CONTAINER" python -m watt_profile.cli.logger_admin restore \
  --backup /tmp/watt-profile-backup-copy --data-dir /tmp/watt-profile-restored
```

核对 device/session 身份、Token、Snapshot canonical 字节和上传标志与源备份一致。不要恢复覆盖 live 卷。

激活锁定的 Python 环境后，从仓库根目录执行 HTTP 与 Compose 测试：

```sh
M4_CHECK_ROOT=$(mktemp -d /tmp/watt-m4-check.XXXXXXXX)
M4_CHECK_PROJECT="watt-m4-check-$(date +%s)-${RANDOM}"
env -u WATT_SERVER_DATABASE_URL -u WATT_SERVER_DATABASE_URL_FILE WATT_TEST_POSTGRES=1 \
  python -m pytest tests/test_m4_pipeline.py -q --basetemp="$M4_CHECK_ROOT/http"
WATT_TEST_M4_COMPOSE=1 WATT_TEST_COMPOSE_PROJECT="$M4_CHECK_PROJECT" \
  WATT_DEV_IMAGE_TAG="$M4_CHECK_PROJECT" WATT_DEV_PORT=18580 WATT_DEV_SERVER_PORT=18581 \
  python -m pytest tests/integration/test_m4_compose.py -q --basetemp="$M4_CHECK_ROOT/compose"
rm -rf -- "$M4_CHECK_ROOT"
```

使用空闲 loopback 端口；Compose fixture 在创建任何资源前拒绝占用端口、已有项目资源（含停止容器）或精确应用标签。HTTP 驱动在 Server 提交后截断一次成功响应。Compose 驱动暂停自己的 Server，观察持续采样和在途请求，强制终止／重建自己的 Logger，再恢复连通／授权，并核对身份、积压、CSV 和清理保护。其 `finally` 删除自己的项目卷／网络／容器和精确应用标签，并断言已不存在。基础镜像及无关项目不在清理范围。驱动失败就是失败证据；解决后才用新项目和新 basetemp 子路径复验。
