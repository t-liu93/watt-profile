# M2：独立可用的 Logger

> [English — source of truth](m2.md) · **中文**

状态：实施中，2026-10-11。第 4 节记录任务证据；Milestone 验收仍待完成。

## 1. 目标与 agent 使用方式

交付可离线使用的 Logger：操作员登录、现场／串口配置、可选读数预览、持续 P1 采集、10 秒快照、持久化 SQLite、生命周期控制、状态、受限清理、命令行备份／恢复，以及容器化浏览器验收。**M2 不向 Centralized Server 发送数据。** 正常采集的记录全部保持 pending。M3 实现中心端；M4 实现 Logger 上传器与集成恢复。

向 agent 提供本文和任务 ID。实现前先阅读[Agent 工作指南](../../AGENTS_zh.md)、相关 [M1 契约](m1_zh.md)和当前源码。第 2 节固定共享契约；第 3 节固定顺序任务边界与检查；第 4 节维护验收说明与证据。M2 设计、任务和完成记录均集中在本文及英文镜像。[MVP 路线图](../mvp-roadmap_zh.md)负责 Milestone 范围。

按 M2-01 至 M2-10 顺序执行，依赖验收后再推进。每项任务自带测试。本文不启动委派，也不授权提交。下文所有新路径、入口和命令均为**计划交付物**，不代表已经存在。保留 M1 已验收的解析器、canonical Snapshot／上传 schema、不可变身份和纯生命周期转换。若必须修正共享契约，同一任务内同步修改代码、测试和双语文档，不得另造相互竞争的状态机。

不包含：真实上传或中心端调用、中心端注册实现、本地历史查询／CSV、Token 轮换界面、账号管理、远程管理、主机网络、其他电表 profile，以及物理硬件验收。保存服务器 origin 只是配置。预览可选；启动采集不以此前预览成功为前提。

## 2. 共享实现契约

### C1. 运行时、配置与模块边界

使用 Python 3.12、FastAPI／Uvicorn、SQLAlchemy 2、Alembic、Jinja2 和现有 Pydantic／pyserial。增加运行依赖和生产 `requirements.lock`；重新生成 `requirements-dev.lock`，包含 HTTPX、Playwright 和现有检查工具。浏览器二进制仅用于开发；Logger 镜像不包含 Node.js 或浏览器运行时。模板、静态资源和迁移须包含在安装包中，并在仓库外验证安装结果。

`src/watt_profile/logger/` 下计划模块边界：`settings.py`／`bootstrap.py`（部署与安装）、`storage/`（schema、迁移、仓储）、`sampler.py`（纯窗口）、`runtime.py`（协调）、`maintenance.py`（存储／备份）、`auth.py`／`app.py`（HTTP）、`templates/`／`static/`（UI）、`synthetic.py`（开发数据源）。现有 `p1`、`contracts` 和 `serial` 保持可复用，不反向导入 Web 应用。命令入口放在 `cli/logger.py` 和 `cli/logger_admin.py`。

`python -m watt_profile.cli.logger` 验证配置，在 `<data-dir>/logger.lock` 持有非阻塞 Linux 文件锁，升级 Logger 数据库，初始化／加载安装身份，检查存储，恢复意图，再启动单个 Uvicorn worker。锁覆盖迁移至关机的完整过程；使用同一目录的第二个应用或 restore／migrate 命令以 `instance_busy` 失败。禁用 reload 和多 worker。迁移错误或安装状态不一致时启动失败，不重置数据。模块导入不得创建文件、线程或凭据。

| 部署配置 | 默认值／契约 |
| --- | --- |
| `WATT_LOGGER_DATA_DIR` | `/var/lib/watt-profile`；持久化私有目录 |
| `WATT_LOGGER_HOST`、`WATT_LOGGER_PORT` | `0.0.0.0`、`8080`；Compose 控制宿主暴露范围 |
| `WATT_LOGGER_INPUT` | `serial`；仅允许 `serial` 或显式选择的 `synthetic` |
| `WATT_LOGGER_OPERATOR_USER` | 必填，1–64 字符 |
| `WATT_LOGGER_OPERATOR_PASSWORD_FILE` | 必填的可读 UTF-8 文件；去掉一个末尾换行后要求 16–256 字符；不得记录内容 |
| `WATT_LOGGER_COOKIE_SECURE` | 本地直连 HTTP 为 `false`；通过 HTTPS 提供服务时设为 `true` |

未知输入模式和无效部署值导致启动失败。操作员凭据归部署配置管理，不存入 SQLite 或快照；生产无默认凭据。提供设置命令，通过 `secrets.token_urlsafe(24)` 生成随机密码，写入新的 0600 文件且拒绝覆盖。改密码需要重启应用。容器以只读方式挂载密码文件。Git 忽略本地凭据／配置文件。

SQLite 保存现场元数据、服务器 origin、串口设置和活动会话意图；环境变量不得在每次重启时覆盖已保存值。使用 M1 的 DSMR 5 串口默认值，允许在高级设置编辑 path／baudrate／bytesize／parity／stopbits；UI 的 timeout 固定一秒。保留 M1 校验及小数字符串表示。未配置路径为 null；合成模式无需串口路径。Profile 固定为 `dsmr5_nl`、revision 1。服务器 origin 可空并遵循 M1 C6；不要求连通性检查。

### C2. SQLite 与仓储接口

实现 **M1 C5 的全部 Logger 列、键、检查约束和索引**，不创建 PostgreSQL 专用表。使用独立 Alembic revision `logger_0001`，随应用打包。在每个 SQLite 连接执行迁移或写入前注册确定性的语法／日历校验函数。验证实际 `foreign_keys=ON`、WAL、`synchronous=FULL` 和 `busy_timeout=5000`。测试初始／重复无变化迁移，以及畸形直接 SQL 写入；不得虚构不存在的前一项目 Logger revision。

每次操作使用短生命周期 SQLAlchemy session，不跨线程共享。应用修改通过运行协调器串行处理；仓储事务独立保证正确性。事务不得跨越串口读取、预览等待或十秒收尾。插入／加载时保留 M1 的精确 canonical UTF-8 文本、hash、Decimal scale、全部可空字段、展开投影和归属校验。快照内容不可变。相同记录键及内容重复写入为空操作；同键不同内容返回冲突，不覆盖。

必需仓储操作使用类型化输入／结果，事务由仓储负责：

| 接口 | 契约 |
| --- | --- |
| `load_installation()`／`load_lifecycle()` | 重新校验配置、活动描述符、意图、指纹及两个阻断原因；不反序列化运行时 owner |
| `save_settings(...)`／`save_site(...)` | 仅校验和更新本地可变元数据；串口变更通过 C4 协调 |
| `commit_deployment_change(expected_session_id, ...)` | 比较当前选中会话，原子完成／创建／选择描述符并保留未解决阻断 |
| `bind_meter(...)`／`update_blocks(...)` | 事务化读改写；不覆盖另一个阻断原因 |
| `insert_snapshot(snapshot)` | 校验 canonical／投影一致性，在一个事务内写入内容和 pending 状态 |
| `select_pending_group(limit, byte_limit)` | 返回最旧的同归属组、不可变描述符和精确存储记录；遵循 M1 批次限制；无网络 |
| `apply_confirmation(batch, confirmation, now)` | 完整校验 M1 confirmation 和已存内容后，原子更新整批及上传时间检查点；已确认重放无害 |
| `get_status()`／`list_sessions()` | 派生聚合及有界列表；不持久化重复计数器 |
| `cleanup_completed(session_ids)` | 一个事务内仅删除已完成会话的 uploaded 行；M2 保留元数据 |

确认支持是供 M4 和测试使用的存储原语，**不是可运行上传器或公开确认 endpoint**。正常 M2 执行不调用它。失败／不匹配的确认不修改任何行。选择与确认在重启前后保留 ID 和内容。

### C3. 安装身份与本地现场／会话归属

数据目录为 0700；进程 umask 0077 保护 SQLite、WAL／SHM、凭据及备份文件。Token 位于 `<data-dir>/upload-token`，0600，通过 `secrets.token_urlsafe(32)` 生成。不得放进 SQLite、日志、异常消息或普通状态／配置响应。

安装锁内的初始化顺序：通过临时文件／原子 rename 及父目录 fsync 创建并同步 Token；随后将设备 UUID 和初始 `logger_config` 一起提交。只有迁移、尚无安装身份的数据库，可以在中断后复用已创建的有效 Token。已有提交身份但 Token 缺失／无效、存在多个设备或 singleton 不一致时，以 `installation_inconsistent` 拒绝启动；不得静默重新生成身份或凭据。临时凭据文件保持私有，不视为已完成凭据。重启和普通编辑保留设备 UUID 与 Token。

新现场生成 UUID v4，或使用明确提供的 v4。地址／描述默认为空，分别限制 500／2,000 字符。未知 UUID 只创建本地元数据；已知 UUID 保留元数据，直到单独显式编辑。不得因选择现场而覆盖既有元数据。测量点 ID 沿用 M1 基于现场 UUID 的确定性 UUID5。

创建／导入现场本身不启动会话。操作员选择现场后创建暂停的会话／数据源；已有开放会话时选择另一现场需显式确认切换。结束会话后保留其完成状态及选中关系；即使仍在同一现场，也需显式部署操作创建下一个暂停会话。暂停／恢复及重启保留会话／数据源。A→B→A 创建三个会话／数据源并复用 A 的测量点。开放会话期间切换至当前现场返回 `same_site`；新一次访问需先完成再新建部署。

### C4. 运行协调与十秒采样

使用一个协调器串行处理生命周期／配置命令，一个独占 pyserial 的读取线程，以及一个窗口协调器。有界共享状态只保留最新候选和最新脱敏预览，不建立无限 telegram 队列。原始帧不在解析后保留；公开值剔除指纹。运行时注入 `SerialFactory` 和可控时钟供测试。不得在 FastAPI 事件循环执行阻塞读取或等待。

`WindowSampler.offer(reading, monotonic_received)` 和 `advance(monotonic_now, utc_now)` 实现纯半开窗口 `[anchor+10n, anchor+10(n+1))`，采集开始时确定 anchor。恰在边界先完成旧窗口，再接收新帧。按接收顺序保留最后有效帧，不按电表时间排序。接收时间为完整帧收到的时刻；`sampled_at` 为实际窗口完成时间。跳过的空窗口不产生记录或补填旧值。墙上时钟跳变不影响时间窗口。重启创建新 anchor，不从最后落盘记录恢复候选。

读取者／协调器同步接收与边界处理，防止延迟回调将旧窗口帧放入新窗口。使用活动不可变描述符和一个记录 ID 构造一次 Snapshot。候选仍可完成写入时不得替换 owner 或描述符。无效帧／超限模型输出被拒绝、计数，并以固定错误报告；不得使循环崩溃或落盘不安全内容。

| 操作／事件 | 必需行为 |
| --- | --- |
| 开始 | 要求选中的开放会话；真实串口模式要求已配置路径。启动前持久化 enabled 意图。同一活动会话幂等；无需预览。 |
| 暂停 | 持久化 disabled 意图，立即取消／关闭读取者，join 后在预定边界完成非空尾窗口，再进入 idle。冲突转换返回 busy。 |
| 完成／切站 | 关闭旧 enabled 意图，按上述方式收尾，再原子提交完成及可选的新暂停部署。提交前旧选中关系有效。切换中断则保留旧会话且暂停，不重放未提交目标。 |
| 串口配置变更 | 先校验，再暂停／收尾旧 owner，最后保存新设置。保留会话／数据源并要求显式开始。保存失败保留旧设置及暂停状态。仅元数据／服务器地址编辑不停止采集。 |
| 正常关机 | 关闭 DB 前取消／join 读取者，最多十秒收尾。与操作员暂停不同，保留关机前 enabled 意图，供重启恢复。Compose 停止宽限期为 30 秒。 |
| 断线／打开失败 | 清空 framer，暴露相应 M1 串口错误；enabled 意图下进入 reconnecting，按 1／2／4／8／16／32／60 秒重试，上限 60 秒。收到有效帧后重置退避。等待可取消。 |
| 无有效读数 | 连续 15 秒无有效帧则显示 `no_valid_reading`；继续读取并保留缺口，收到有效帧清除此错误。占有串口不等于数据新鲜。 |
| 电表变化 | 接受候选前检查指纹；首个有效指纹事务化绑定。不同指纹触发阻断，取消／join 读取者并丢弃当前未提交候选。显式确认关闭旧会话并在同现场创建新暂停会话；保留存储阻断。 |
| 重启 | 恢复意图前重新检查空间／写入健康。通过 M1 `restart` 仅恢复开放、enabled、无阻断会话。不恢复预览或待处理运行命令。 |

开始不得越过 stopping，旧线程退出前不得打开新 owner。close／join 未成功时保持可见的不可用状态并阻止新 owner，不得假称已释放。正常停止收尾；存储／电表故障丢弃未提交候选，不能违反阻断。突然死亡仅可能丢失该候选；已提交记录保持持久。

预览在十五秒内返回下一条有效读数。collecting／reconnecting 时订阅已有 owner，不重新打开、重置或自行绑定新电表；超时不停止采集。idle 时使用临时独占读取者并始终关闭，不创建会话、指纹绑定或 Snapshot。starting／stopping／另一个临时预览，以及持久阻断状态拒绝预览，返回 busy／blocked。断线或请求取消只清理相应订阅／临时 owner。预览返回脱敏指标、字段、质量和电表／接收时间，不含原始字节或指纹。

### C5. 存储压力、清理与备份

启动时、快照写入前及运行期间每十秒检查可用空间。低于 64 MiB 或存储写入失败时，取消／join 读取者，丢弃候选，显示 `storage_full`，并尽可能持久化阻断。写失败时不得声称阻断已保存；启动必须重查。数据库损坏／迁移失败属于致命服务错误，不能通过删除读数解决。SQLITE_BUSY 在配置等待耗尽后显示固定存储错误，不紧密重试或隐藏记录丢失。

解除存储阻断需要至少 64 MiB 可用空间，并成功提交／重新验证安全数据库写入（事务内对 config 做无变化更新），再仅移除 `storage_full`、关闭采集意图并要求显式开始。两种阻断的任意解除顺序都不得移除另一原因。测试注入空间／写失败，不填满真实磁盘。

清理展示会话总数、pending 数及可删除 uploaded 数。确认指定会话 ID；事务重查 completed／uploaded 条件。开放会话和全部 pending 行保留。M2 保留所有元数据，网页请求不执行 VACUUM。删除可能只释放 SQLite 内部可复用页而不增加文件系统可用空间；显示真实空间，不宣称清理自动解除压力。不自动驱逐、不提供手动 uploaded 开关、不删除服务器数据。

提供 `python -m watt_profile.cli.logger_admin` 子命令：

- `generate-password --output PATH`：创建 C1 密码文件，拒绝已存在目标。
- `migrate --data-dir PATH`：获取安装锁并升级 schema；应用启动也执行迁移。
- `backup --data-dir PATH --output NEW_DIRECTORY`：使用独立配置连接及在线 SQLite backup API，不只复制活动数据库主文件。保存 `logger.sqlite3`、单独的 0600 `upload-token` 和 manifest，后者包含格式版本 1、设备 UUID、迁移 revision 及两文件 SHA-256。目录为 0700 且必须不存在。不包含操作员密码。最后写完成 manifest；不完整输出不可恢复。备份可与运行写入并存；安装初始化必须已经完成。
- `restore --backup DIRECTORY --data-dir NEW_DIRECTORY`：离线获取目标锁；要求除锁文件外为新建／空目录；安装前检查 manifest／摘要、Token 语法、数据库 integrity／外键及模型／canonical 不变量。私有暂存，仅安装完整验证的一组文件；失败不得留下可运行的半初始化安装。拒绝覆盖或未知／更新的 schema／备份版本。保留身份、Token、pending／uploaded 状态、意图和阻断。不启动采集；以后应用启动遵循正常意图规则。

备份包含私有现场元数据和凭据。保护所有文件，不打印内容，不恢复覆盖已有安装。测试 WAL 活跃期间备份并恢复至另一临时目录。备份不包含数据目录外的部署凭据／设置，恢复后需另外提供。

### C6. 认证与本地 HTTP 契约

使用一个部署管理的操作员账号、表单登录和不透明的 `secrets.token_urlsafe(32)` 会话 ID。会话只存在进程内存，上限 32 个（淘汰最旧），创建八小时后过期，退出／重启失效。通过常量时间摘要比较校验凭据；不得用设备 Token hash 代替人类凭据。无账号数据库、密码重置界面或角色。使用有界内存 map，限制每远端地址每分钟十次登录尝试；默认不信任转发的客户端 header。

Cookie 为 `watt_logger_session`，HttpOnly、SameSite=Lax、path `/`，Secure 遵循配置，max age 八小时。独立随机 CSRF token 绑定登录前／已登录状态；登录及全部认证后修改请求都校验，包括注册信息显示和预览。不启用跨域访问。提供 Origin 时校验同源；转义文本，不插入不可信 HTML。未认证 API 返回 401，页面重定向登录；CSRF 缺失／错误返回 403。认证／私有响应设 `Cache-Control: no-store`。访问／应用日志及校验错误不包含请求 body、密码、Token、串口字节或指纹。

以下 JSON endpoint 使用 `/api/local/v1` 前缀。Pydantic 模型拒绝未知键；ID／时间／小数复用 M1 类型。除 204 外成功返回 `{"data": ...}`；错误返回 `{"error":{"code":"...","message":"固定安全文本"}}`。校验错误为 422 且不回显输入；对象缺失 404；冲突／busy／阻断／过期选择 409；串口不可用／预览超时 503；存储失败 507。POST 命令等待完成但不得阻塞事件循环（预览 ≤15 秒，普通收尾 ≤10 秒），之后返回最终状态。客户端超时不代表回滚；重试前重新加载状态。

| 方法和相对路径 | 输入 → 输出／语义 |
| --- | --- |
| `GET /status` | → 下述 `StatusView` |
| `GET /config` | → 已保存服务器 origin、串口字段及固定 profile；无凭据 |
| `PATCH /config` | 提供的可变配置字段及必填 `expected_session_id`（UUID 或 null）→ 已保存配置；C4 变更前比较选中会话 |
| `GET /sites` | `offset=0`、`limit=50`（1–100）→ 按 UUID 排序的 `{items,total}`；条目包含 UUID／地址／描述 |
| `POST /sites` | `{site_id?: UUID,address?: str,description?: str}` → 现场；未提供 ID 则生成 v4。已存在 ID 返回原元数据不修改。 |
| `PATCH /sites/{site_id}` | 地址和／或描述 → 更新后的现场；不改身份 |
| `POST /deployment` | `{site_id,expected_session_id,confirm_switch: bool}` → `StatusView`；比较选择，当前会话开放则要求确认；按 C3／C4 新建或切换 |
| `POST /sessions/{session_id}/start`、`/pause`、`/complete` | `{}` → `StatusView`；路径须匹配当前选中会话；M1 允许的同状态重复操作幂等 |
| `POST /preview` | `{}` → 脱敏 `ReadingView`；无需会话 |
| `POST /meter-change/confirm` | `{expected_session_id}` → 新暂停部署状态；仅存在电表变化阻断时允许 |
| `POST /storage/resolve` | `{}` → `StatusView`；移除存储阻断前执行健康／写入检查 |
| `POST /registration-summary` | `{site_id}` → `{site_id,address,description,device_id,token}`；认证后显式显示本地已知现场注册信息 |
| `GET /sessions` | 与现场相同分页 → 按 `started_at,session_id` 排序的 `{items,total}`；包含描述符 ID、state、intent、ended_at 和 total／pending／eligible 计数 |
| `POST /cleanup` | `{session_ids: [UUID],confirm: true}`，1–100 个不同 ID → `{deleted_count,retained_pending_count,free_bytes}`；删除前拒绝任何未知 ID，开放会话保持不变 |

`POST /deployment` 的 expected selection 过期时无修改失败，防止双击／重试／旧标签页多建会话。针对旧会话的 start／pause／complete 不得影响新会话。串口配置和电表确认采用相同保护。仅元数据编辑可在采集中执行。

`ReadingView` 包含 `meter_time_raw`、`meter_time_utc`、`received_at`、18 键 `metrics`、脱敏 `fields` 和 `quality`，采用 M1 序列化。`StatusView` 用 `logger` 包装不变的 M1 `LoggerStatus`，另加 `input_mode`、`upload_mode="not_implemented"`、`persisted_blocks`、`active_session_state`（open／completed／null）、`latest_live_reading`（ReadingView／null）、`active_session_latest_received_at`（落盘 MAX／null）、`runtime_error`（固定错误码／null）。M1 最新／pending 聚合覆盖全部已存会话；新增当前会话时间避免旧现场历史看似当前采集。最新实时读数只在内存，换会话／重启清空，不代表已落盘。M1 采集 enum 未包含的错误，如 `snapshot_invalid`／`storage_error`，放在 `runtime_error`，不静默扩展 wire 契约。M2 上传错误为 null，正常上传成功时间为 null；测试／恢复数据库存在检查点时，与上传模式一起展示。

页面路由：`GET /login`、`POST /login`、`POST /logout` 和受保护的 `GET /`。登录表单字段为 `username,password,csrf_token`；成功重定向 `/`，失败使用通用消息。JSON 修改通过 `X-CSRF-Token` 传递 token，服务器表单使用隐藏字段。`GET /health/live` 表示事件循环存活；`GET /health/ready` 在初始化／迁移／运行设置完成后返回 200，启动／关机或致命 DB／运行故障返回 503。未认证健康响应仅包含布尔值。缺少现场、断开的电表、持久采集阻断或缺少服务器，不使已初始化服务变为 unready；这些属于可见产品状态。

### C7. 浏览器行为

提供一个小型英文 dashboard，包含设置／现场、读数预览、采集／状态、注册和维护区。每两秒查询状态，不重叠请求。刷新页面不得重放修改。starting／stopping／previewing 时禁用冲突控件；服务器仍执行全部规则。合成模式持续显示 **Synthetic input — development only**，两种模式均显示 **Upload not implemented in M2; records remain pending**。

首次使用：登录 → 创建／导入现场 → 创建暂停部署 → 保存串口设置（真实模式）→ 可选预览 → 开始。服务器 origin 可空。也支持创建现场前预览。展示全部 18 指标及单位，null 显示 “Unavailable”，显示 UTC 和原始电表时间、脱敏字段及质量问题。区分实时预览、当前会话最新落盘时间和全部会话积压；不显示指纹。

暂停／恢复复用会话。完成、切换现场、更换电表及清理均需确认，说明后果和当前 ID。选择已知现场保留元数据；编辑为独立操作。注册信息经显式显示后可复制 UUID／地址／描述／设备／Token；离开该区域／退出时清空已显示内容，不存入 localStorage 或 URL。本地 HTTP 不支持剪贴板 API 时提供可选取的文本。

维护区分页列出会话／计数及可清理选择，解释 pending／开放会话保护及为何正常 M2 数据暂不可清理。存储恢复是显式检查，随后需要独立开始操作。登录过期、busy 冲突、过期选择、串口错误及存储失败都可见，并提示重新加载／重试，不伪装成功。不包含图表、本地历史读数表、CSV 或备份下载 UI。

### C8. 合成输入与容器

合成模式实现相同 `SerialFactory`／read／close 接口，每秒输出一条有效 DSMR 5 telegram 的有界字节块，使用合成标识、递增时间／计数及正确 CRC。字节经过真实 framer／parser／profile／runtime，不直接注入 Snapshot。合成电表指纹跨容器重建保持稳定。测试通过 fixture／fake 注入坏 CRC、静默、断线／重连和换表；不提供生产故障注入 HTTP endpoint。

交付 `deploy/logger/Dockerfile`、`deploy/logger/compose.yml`、无真实凭据的受版本管理配置示例，以及含 `logger` 服务的根目录 `docker-compose.dev.yml`。构建支持本地架构及 Linux ARM64 的 Python 3.12 slim 镜像。运行时使用固定 UID／GID 10001 的非 root 用户并记录说明；职责受限的卷初始化服务只准备其挂载数据卷的属主后退出，不接触串口。应用不以 privileged 运行。真实串口部署显式映射设备到 `/dev/p1` 并配置补充设备 GID，不映射整个 `/dev`。说明如何让应用 UID 读取密码文件而不开放给所有用户。

开发配置：合成源、`127.0.0.1:8080:8080`、可丢弃的项目数据卷，不映射宿主串口，不含服务器／PostgreSQL，不用 external volume。仅开发设置服务在私有项目卷创建明确记录的合成账号 `operator`／`watt-profile-dev-only` 的密码文件。独立部署不得复用该默认账号／设置。根 Compose 保持可供 M3 扩展服务。独立 Compose 使用 serial 模式，要求密码文件和设备／GID 变量，正常 down／重建保留数据卷。配置 readiness 健康检查及 `stop_grace_period: 30s`。

## 3. 原子实现任务

每项任务阅读所列 C 节及相关 M1 源码。实现与检查一起交付，不将本项验收推给未规定的后续任务。所有任务还执行第 4 节 Ruff 检查。以下测试文件为计划交付物，并固定检查入口。

### M2-01 — 运行基础

依赖：已验收 M1。契约：C1、C8 打包要求。

- 交付依赖声明／锁、包资源、配置校验、私有目录 helper 和安装锁；引入 CLI 结构但不宣称采集已可用。实现 `generate-password`。
- 验收无导入副作用、错误配置拒绝、私有密码创建且不覆盖、锁互斥及进程死亡后释放、仓库外访问安装包资源。
- 边界：无数据库 schema、身份生成、HTTP 或 worker。检查：`python -m pytest tests/test_logger_bootstrap.py tests/test_package.py -q`。

### M2-02 — SQLite schema 与仓储

依赖：M2-01。契约：C2；M1 C5–C6。

- 交付 schema、`logger_0001`、配置连接和事务仓储，包含 pending 选择／已校验确认。增加 `migrate` 命令。
- 验收初始／无变化迁移、实际 pragma、直接 SQL 负例和复合归属、canonical／Decimal／null／hash 精确往返、插入回滚和整批确认校验／重放。加载时核对不可变投影。
- 边界：测试使用合成描述符；无安装 Token、串口 owner、Web 服务或网络。检查：`python -m pytest tests/test_logger_storage.py -q`。

### M2-03 — 持久身份与部署操作

依赖：M2-02。契约：C3 及 C4 事务边界。

- 交付初始化／恢复、配置／现场操作及仓储与现有纯生命周期函数的集成。为运行时提供类型化部署操作；串口收尾由 M2-05 实现。
- 验收 Token 发布／DB 提交附近中断、缺失 Token 拒绝、身份／重启稳定、已有现场元数据保护、A→B→A 归属、过期会话拒绝、切换失败原子性、指纹绑定和独立阻断更新。
- 边界：无线程或 HTTP；生命周期事务使用假的收尾完成信号测试。检查：`python -m pytest tests/test_logger_identity.py tests/test_lifecycle.py -q`。

### M2-04 — 窗口采样器

依赖：M2-03。契约：C4 采样；M1 C3–C4／C7。

- 交付纯有界采样器和 Snapshot 构造接口，实际持久化／运行 owner 位于外部。
- 验收窗口最后有效帧、精确边界顺序、空／跳过窗口、新接收但重复电表时间、UTC 跳变、每次候选完成仅一个记录 ID、边界停止／无提前额外行，以及重启新 anchor。
- 边界：仅假时钟／映射读数；采样器测试无真实等待、线程、串口或 DB。检查：`python -m pytest tests/test_logger_sampler.py -q`。

### M2-05 — 串口与采集运行时

依赖：M2-04。契约：C4、C2–C3 集成。

- 交付协调器／读取者集成、可注入空间／写入健康 guard、重连与预览、事务控制、正常关机及启动意图恢复。M2-06 提供完整维护策略。
- 验收独占、有界缓冲、读取／退避／预览期间取消、共享预览不重复打开、过期命令、收尾时状态响应不阻塞、保存失败后暂停、尾窗口归属、关机保留意图、重启、无效帧和换表阻断。
- 边界：假串口及临时 DB；无物理设备、UI、上传器或合成部署源。检查：`python -m pytest tests/test_logger_runtime.py tests/test_p1_probe.py -q`。

### M2-06 — 存储维护与恢复

依赖：M2-05。契约：C5。

- 交付健康 guard、持久压力集成、安全解除、清理及 CLI 备份／恢复。
- 验收低于／等于／高于阈值、写失败／阻断落盘失败、已提交行保留、两种阻断解除顺序、completed／open／pending 混合清理、在线 WAL 备份及隔离恢复后身份／字节不变。拒绝坏 manifest、hash 不符、无效 schema、缺失 Token 和覆盖。
- 边界：注入容量故障；不填盘，不操作生产备份／恢复，无网页下载或自动驱逐。检查：`python -m pytest tests/test_logger_maintenance.py -q`。

### M2-07 — 认证与本地服务

依赖：M2-06。契约：C6、C1 启动和 C4 关机。

- 交付应用生命周期、单 worker CLI、登录／会话／CSRF、类型化本地 API 和最小登录表单；运行时和仓储保持 HTTP 无关。
- 验收认证／过期／退出／重启、限速、CSRF／origin、转义／固定错误、过期选择、幂等、注册信息私密性、健康语义和长控制期间状态可响应。断言无对外上传调用。
- 边界：不含完整 dashboard 或浏览器验收；仅临时凭据／DB 和 HTTPX 客户端。检查：`python -m pytest tests/test_logger_api.py -q`。

### M2-08 — 操作员 dashboard

依赖：M2-07。契约：C7。

- 交付英文 dashboard、有界轮询、配置／预览／采集流程、显式确认／显示及维护控制。用临时应用及假串口增加 Playwright 测试。
- 验收键盘／表单、首次离线设置、可选预览、暂停／恢复／结束／切站、可空读数、可见 busy／失败／登录过期、Token 复制回退，以及刷新不重放。核对全部会话积压与当前会话新鲜度的区分。
- 边界：无历史读数 UI、CSV 或服务器。检查：`python -m pytest tests/browser/test_logger_ui.py -q`（已安装开发 Chromium）。

### M2-09 — 合成源与容器交付

依赖：M2-08。契约：C8。

- 交付字节级合成源、Dockerfile、两套 Compose 和脱敏配置示例。增加构建／启动／readiness／持久化自动化；在第 4 节及双语 README 链接中记录精确的独立设备／GID／密码设置与开发命令。
- 验收真实解析合成帧、稳定合成指纹、非 root、仅本机开发端口／无设备映射、重建保留卷／身份，以及安装包迁移／模板可用。执行本地及 Linux ARM64 构建，区分运行过和仅构建过的目标。
- 边界：无中心端／上传器，不宣称物理硬件通过。检查：`python -m pytest tests/test_logger_synthetic.py -q`、第 4 节 Compose／构建检查，以及显式临时项目下 `python -m pytest tests/integration/test_logger_compose.py -q`。

### M2-10 — 离线验收与交接

依赖：M2-01–M2-09。契约：全部。

- 交付合成字节→Snapshot→SQLite→重启／备份集成检查及完整 Compose 浏览器走查。执行全部 Milestone 和 M1 回归、Ruff、安装包、本地容器及 ARM64 构建检查。
- 验证 pending 字节／ID 跨重启／换现场不变，无真实网络传送，维护保护 pending，且此前每项验收均有证据。双语记录实际命令、结果、限制和资源清理。
- 边界：无新功能、上传／硬件验收或独立 Milestone 报告文件。检查：`python -m pytest tests/test_m2_pipeline.py -q` 及完整第 4 节套件。只有必需本地检查与走查通过后才标记 M2 已验收；无法执行的检查保持待验收。

## 4. 检查与完成记录

### 计划自动化入口

按照 [README](../../README_zh.md) 使用唯一临时虚拟环境，M2-01 为两套依赖锁更新说明。单元／集成测试默认临时数据和假时钟／串口。浏览器和 Compose 测试必须显式选择，不得在默认套件中悄悄启动服务。

```bash
python -m pip install -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
python -m pytest tests --ignore=tests/browser --ignore=tests/integration -q
python -m ruff check .
python -m ruff format --check .
python -m playwright install chromium
python -m pytest tests/browser/test_logger_ui.py -q
```

M2-09 必须落实并验证以下计划命令，记录宿主浏览器依赖。集成 harness 读取 `WATT_TEST_COMPOSE_PROJECT`，要求非空且以 `watt-m2-` 开头，只操作该项目服务／资源；要求 8080 空闲，或说明显式临时端口覆盖。不得发现并删除无关容器。

```bash
export WATT_TEST_COMPOSE_PROJECT="watt-m2-$(date +%s)-$$"
docker compose -p "$WATT_TEST_COMPOSE_PROJECT" -f docker-compose.dev.yml config --quiet
docker compose -p "$WATT_TEST_COMPOSE_PROJECT" -f docker-compose.dev.yml up --build -d --wait
python -m pytest tests/integration/test_logger_compose.py -q
docker compose -p "$WATT_TEST_COMPOSE_PROJECT" -f docker-compose.dev.yml logs --tail=100 logger
docker buildx build --platform linux/arm64 -f deploy/logger/Dockerfile --output=type=cacheonly .
```

集成测试连接此显式项目，验证 API／浏览器／容器重建，不执行破坏性卷清理。最终清理前完成走查：

```bash
docker compose -p "$WATT_TEST_COMPOSE_PROJECT" -f docker-compose.dev.yml down --volumes --remove-orphans
unset WATT_TEST_COMPOSE_PROJECT
```

上述破坏性卷清理**仅限**可丢弃开发项目。独立部署使用 `docker compose --env-file <local-config> -p watt-logger -f deploy/logger/compose.yml up --build -d --wait`；正常 `down` 不加 `--volumes`。M2-09 提供精确示例变量及文件／设备权限设置。M2 验证不执行生产部署。

### 计划浏览器走查

打开 `http://127.0.0.1:8080`，使用开发账号 `operator`／`watt-profile-dev-only` 登录，确认合成输入和不上传标识。仅使用虚构现场元数据。

1. 未创建现场时预览，显示脱敏读数且不生成会话／落盘快照。创建现场 A，创建暂停部署，服务器 origin 留空，不要求再次预览即可开始。
2. 等待至少 25 秒。核对当前会话新鲜落盘时间、正数 pending 及单位／null。复制注册信息，确认普通状态和日志无 Token。
3. 暂停，等预定尾窗口完成后记录 pending 数，再等 12 秒：计数不变。恢复后会话／数据源 ID 不变。
4. 使用 `up -d --force-recreate --wait logger` 只重建 Logger 容器。重新登录；ID、Token 和已提交 pending 保留，enabled 采集恢复。再从暂停状态重建，确认仍暂停。
5. 确认切换到 B。A 旧记录仍 pending；B 使用新会话／数据源且暂停。开始 B、完成 B，再用保存的 UUID 回访 A；A 测量点不变，会话／数据源新建。
6. 打开维护区：正常 M2 行为 pending，不可删除。成功清理仅通过含有效确认测试行的隔离自动化 fixture 验证。确认页面不能标记 uploaded。
7. 通过自动化假设备测试覆盖串口／静默／换表／存储注入场景，包含显式恢复和双阻断。不拔生产设备或填满真实文件系统获取证据。
8. 在临时目录使用 CLI 备份／恢复，比较 canonical hash、计数、元数据、Token、意图和阻断。恢复不得启动第二读取者。停止临时环境，只删除本轮创建资源。

### 任务执行证据

- M2-01：自动检查和全新独立审查通过。`tests/test_logger_bootstrap.py tests/test_package.py -q`：11 项通过；Ruff 检查／格式和 `pip check` 通过。在仓库外验证 wheel 资源。经过一轮修复解决空目录校验、静态资源打包及双语锁文件文档。验证仅使用隔离 Python 3.12 环境和临时目录。

### 验收记录

| 证据 | 状态 |
| --- | --- |
| M2-01–M2-10 实现及各项检查 | 待执行 |
| 完整 M1／M2 回归、Ruff 和安装分发包 | 待执行 |
| 浏览器检查及本地 Compose 走查 | 待执行 |
| 本地镜像运行及 Linux ARM64 镜像构建 | 待执行 |
| 备份／恢复、持久化、故障恢复及资源清理 | 待执行 |
| 物理电表／线缆、Pi 4 1 GB 和 Pi 3B+、持续负载／磁盘写入／四周容量 | 留待 M4；不作为 M2 已通过声明 |
| 真实上传、服务器确认及端到端丢失／重试恢复 | 留待 M4；M2 存储原语检查不等于网络验收 |

实施后以实际命令／结果和相关 commit／环境引用替换待执行项，保留真实限制。M2 验收不代表 M3／M4 或多周现场观察完成。
