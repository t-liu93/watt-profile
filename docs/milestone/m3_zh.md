# M3：手动管理服务端

> **中文** · [English — source of truth](m3.md)

状态：实现进行中，2026-10-12。M3-01–M3-09 实现、检查与独立审查已完成；M3-10 及所有者浏览器验收仍待执行。M1、M2 已验收。第 4 节记录服务、HTTP、浏览器及隔离容器检查；隔离合成恢复已通过，最终集成仍待执行。

## 1. 目标与 Agent 使用方式

交付可独立部署的 Centralized Server：管理员登录、粘贴 Logger 注册信息、站点/设备/凭据/授权管理、经授权的原子批量接收、分别展示联系与读数状态、筛选查询及完整 CSV 导出。包含 PostgreSQL、Caddy、容器、隔离合成验证、备份恢复说明和浏览器 walkthrough。**Logger 上传器及集成交付恢复仍属于 M4。**

向 Agent 提供本文和任务 ID。实现前阅读 [Agent Guide](../../AGENTS_zh.md)、相关 [M1 契约](m1_zh.md)、[M2 设计](m2_zh.md)及当前源码。[MVP roadmap](../mvp-roadmap_zh.md)决定范围。按 M3-01 至 M3-10 顺序推进，依赖验收后才进入下一项；每项包含自己的测试。本文不启动委派，也不授权 Git 操作。M3-01 能力记录于第 4 节；其余新增路径、命令及测试入口仍是**计划交付物**。

保留 M1 领域/上传类型、规范编码及身份语义。实现 M1 C5 的 PostgreSQL 规范，不复制 Logger 的 SQLite DDL。Logger 唯一产品变化是结构化注册摘要复制，并附带回归检查。设计、任务卡、操作说明和验收证据集中于本文及英文镜像，同步更新；不另建里程碑设计或验收文件。

排除：上传器、自动注册、凭据轮换 UI、客户账号、图表/统计、后台导出、远程配置、硬件验收及生产访问。不得根据静默或最后接收的积压数据推断会话已结束、设备当前位于哪个站点。

## 2. 共享实现契约

### C1. 运行时、配置与边界

使用 Python 3.12、现有 FastAPI/Uvicorn、SQLAlchemy 2、Alembic、Jinja2，新增 Psycopg 3 及其 binary 发行包。重新生成运行/开发锁文件，打包服务端迁移、模板和静态资源。保持现有 Logger 构建可用。使用 PostgreSQL 17、Caddy 2 镜像系列；M3-08 在 Compose 中固定实际可用的具体版本并记录，不使用 `latest`。

计划在 `src/watt_profile/server/` 下划分：settings/bootstrap、storage（schema/migrations/repositories）、registration、ingest、queries/export、auth/app、templates/static。入口为 `python -m watt_profile.cli.server` 与 `python -m watt_profile.cli.server_admin`。共享 `contracts` 不得导入任一应用。仓储/服务操作独立于 HTTP；SQLAlchemy session 短生命周期且不得跨线程共享。同步数据库工作不阻塞事件循环；流式导出在其生命周期内独占独立连接。

| 配置 | 契约 |
| --- | --- |
| `WATT_SERVER_DATABASE_URL_FILE` | 必填私有 UTF-8 文件，内容为 SQLAlchemy `postgresql+psycopg` URL；去掉一个末尾换行；不得记录内容 |
| `WATT_SERVER_ADMIN_USER` | 必填，1–64 字符 |
| `WATT_SERVER_ADMIN_PASSWORD_FILE` | 必填私有密码文件，去掉一个末尾换行后 16–256 字符；无生产默认值 |
| `WATT_SERVER_PUBLIC_ORIGIN` | 必填 HTTP/HTTPS origin，无 userinfo/path/query/fragment；用于浏览器 Origin 和可信主机校验 |
| `WATT_SERVER_HOST`、`WATT_SERVER_PORT` | 默认 `0.0.0.0`、`8080`；Compose 控制外部暴露 |
| `WATT_SERVER_COOKIE_SECURE` | 默认 true；仅显式开发或所有者管理的隧道 HTTP 环境设为 false |

单 Uvicorn worker，不启用 reload。数据库连接池有界：5 个连接、最多 5 个 overflow，获取连接超时 5 秒；普通事务局部 lock/statement timeout 为 5/30 秒，导出按 C6。启动验证配置，取得 PostgreSQL advisory 迁移锁，升级到已知 head，释放锁后进入 ready。未知/更新 revision 或迁移失败应拒绝启动，不能重置。导入不得创建文件、连接或凭据。关闭时停止接入、关闭流并释放池；Compose 给出 30 秒退出时间。

提供 `server_admin generate-password --output PATH`（0600、随机 `token_urlsafe(24)`、排他创建）及使用同一配置和迁移锁的 `server_admin migrate`。修改密码需重启应用。秘密文件父目录私有、容器只读挂载，进程 umask 为 0077。错误/日志不得暴露带绑定值的 SQL、连接串、请求体或凭据。

### C2. PostgreSQL 存储与事务规则

实现 M1 C5 全部服务端列、可空性、默认值、具名键/检查/索引，独立 revision 从 `server_0001` 开始。不加入 Logger 专属配置、采集状态、指纹或上传状态列。PostgreSQL 时区为 UTC。NUMERIC 不设精度限制且必须非负、有限；验证 UUID 版本/variant、JSON 根类型、canonical UTF-8 字节长度、hash 和复合归属。写入/读取重新验证模型及 canonical/投影一致性。Canonical TEXT 保留小数 scale、null 和数组顺序；JSONB 和数值投影不是 hash 输入。

仓储拥有事务边界。提供类型化的注册预览/提交、站点编辑/列表、凭据/授权管理、凭据认证/联系、原子接收、状态、读数分页/详情和导出迭代操作。不得返回依赖已关闭 session 的 ORM 对象。快照/描述符内容不可变。无级联删除、历史删除端点、保留期任务或自动清理；已接收快照至少保留一年，不受凭据/授权变化或 Logger 清理影响。

用数据库事务级 advisory 锁协调不同应用连接的修改：固定共享 namespace，锁键从 UUID 确定性派生，不使用 Python 随机化 hash。按 device、site、session、source 顺序加锁，同类按 UUID 排序。hash 冲突最多串行化无关操作，不得削弱正确性。注册、站点编辑、凭据/授权修改、接收均使用同一套相关锁；迁移使用独立 namespace。PK/UNIQUE/FK 仍是最终约束。遇到已有描述符/记录键时必须读取并比较不可变内容，不可盲目忽略冲突。并发凭据唯一性冲突映射为安全的注册冲突。

接收事务持有 device/site 锁时重新检查凭据启用状态与授权；撤销也取得相同锁。先取得锁的事务先生效：已获准批次可以先于撤销完成，但后续接收不能使用已撤销权限。测试使用真实 PostgreSQL 独立连接和同步屏障，而非仅顺序重试。锁超时、临时连接失败和数据库不可用返回安全可重试错误；不得确认未提交数据。

### C3. 管理员认证与 HTTP 约定

一个部署管理的管理员账号，与 Logger 操作员及 Bearer 凭据分离。沿用 M2 认证行为，但服务端代码/状态及 Cookie 独立，使用 `watt_server_session`/`watt_server_login`：随机不透明 session ID、最多 32 个 session、八小时过期、退出/重启撤销、15 分钟过期的登录前 CSRF、恒定时间凭据比较，以及有界登录限流（每远端地址每分钟 10 次，最多 1,024 个地址）。无账号数据库、角色或密码重置 UI。仅在 Logger 行为仍有回归覆盖时复用纯 helper；不导入 Logger 应用。

Cookie 为 HttpOnly、SameSite=Lax、path `/`，Secure 按配置。登录及所有认证修改请求均检查 CSRF（含注册预览），存在 Origin 时验证同源。JSON 使用 `X-CSRF-Token`，表单使用隐藏 token。不启用 CORS。渲染值转义；认证页面/API/导出使用 `Cache-Control: no-store`。不得回显携带输入的验证异常。Session Cookie 不能认证 ingest，Bearer Token 不能认证管理接口。

独立部署仅通过 Caddy 公网入口，应用/数据库不发布端口。在此边界剥离/替换不可信转发头，Uvicorn 不启用通配 forwarded-header 信任。应用 origin 判断使用配置的公开 origin；M3 登录限流可以使用直接代理 peer（保守的共享限额），不信任任意客户端头。直接本地开发使用实际 peer。关闭记录 query string/请求体的访问日志；日志可包含路由模板、状态和固定错误码。

页面路由：`GET/POST /login`、`POST /logout`、受保护的 `GET /`、`/sites/{site_id}`、`/devices/{device_id}`。未登录页面重定向登录，管理 API 返回 401。管理前缀为 `/api/admin/v1`；除 204 外成功为 `{"data": ...}`，失败为 `{"error":{"code":"...","message":"fixed safe text"}}`。CSRF 为 403，对象不存在 404，注册冲突 409，输入无效 422，存储不可用 503。代码中定义固定消息。管理 JSON 拒绝额外键，解析前限制请求体 32 KiB。Ingest 保持 M1 独立 envelope 和限制。

`GET /health/live` 检查应用存活；`/health/ready` 用有界轻量查询检查迁移初始化及数据库可用性。两者未认证响应仅含布尔值；初始化前、关闭中或 DB 失败时 ready 返回 503。空数据或禁用设备是合法 ready 状态。

### C4. 注册、凭据与授权

注册摘要为恰含 `site_id,address,description,device_id,token` 的 JSON 对象，对应当前 Logger API 的 data 对象，不带 `data` envelope。UUID 为 v4；地址/描述沿用 M2 的 500/2,000 字符限制，可为空或多行。Token 匹配 Logger 持久化的 `token_urlsafe(32)` 格式（43 个 URL-safe ASCII 字符）。拒绝重复 JSON 键、格式错误/多余字段和超限输入，使用固定安全错误。

M3-07 将 Logger 可复制 textarea 改为格式化 JSON，屏幕保留可读标签。本地注册 API 和 Token 生成不变。不要求兼容旧标签文本解析。两端页面都不得把摘要放入 localStorage/sessionStorage 或 URL；提交成功、取消、导航/退出或认证过期时清除。预览隐藏 Token，服务端不返回 Token。

预览说明 site/device/credential/grant 将创建还是复用，元数据不一致时显示已有服务端地址/描述；不写数据。提交接收原始校验后的摘要，在锁内重新评估，一次事务完成所有注册写入。不能覆盖已有站点元数据、替换凭据或恢复禁用凭据。预览不是状态不变的保证；提交返回实际应用/复用结果及当前元数据。

同 device/Token/site 重复注册幂等。已知设备以相同启用 Token 注册额外站点，保留旧授权。Token hash 属于其他设备、匹配 Token 已禁用，或已有凭据的设备提交新 Token 时返回 409，不能部分创建 site/device。已有但无凭据的设备可注册首个凭据。只存高熵 Token 的 SHA-256；普通管理输出不得返回摘要或 Token。轮换/重新启用不属于 M3。可通过以下接口分别创建设备/站点；不按地址匹配身份。

| 方法及管理相对路径 | 输入 → 结果 |
| --- | --- |
| `POST /registrations/preview` | 摘要 → 已有元数据和 site/device/credential/grant 的 `create/reuse` 动作；仅隐藏凭据的指示 |
| `POST /registrations` | 摘要 → site/device/credential ID、当前元数据、应用/复用动作 |
| `GET /sites` | 可选 `site_id` 精确 UUID 或 `q` 字面量、不区分大小写的地址子串（二者互斥），`offset=0,limit=50`（1–100）→ `{items,total}`，按站点 UUID 排序 |
| `POST /sites` | `{site_id,address,description}` → 创建/已有站点；调用方提供 UUID，已有元数据不变 |
| `PATCH /sites/{site_id}` | address/description 非空字段子集 → 显式元数据修改 |
| `GET /devices`、`POST /devices` | 同站点分页 / `{device_id}` → 列表或创建/已有设备 |
| `GET /devices/{device_id}` | → 设备状态、凭据 ID/创建/禁用时间和授权站点 ID；无 hash |
| `POST /devices/{device_id}/credentials` | `{token}` → 首个或相同启用凭据 ID；冲突规则同注册 |
| `POST /devices/{device_id}/credentials/{credential_id}/disable` | `{}` → 凭据状态；已禁用则无操作 |
| `PUT /devices/{device_id}/sites/{site_id}` | `{}` → 授权；两对象必须已存在，幂等 |
| `DELETE /devices/{device_id}/sites/{site_id}` | → 204；对象存在而授权不存在则无操作 |

站点修改及破坏性访问变更按需要在 UI 确认：禁用/撤权说明 Logger 待上传记录仍保持 pending，历史读数保留。新增站点授权从不移除旧授权。设备 ID、站点 UUID 不可编辑。

### C5. 批量接收与联系状态

严格实现 M1 C6：`POST /api/v1/ingest/batches`、Bearer Token、JSON、schema v1，最多 200 条、单快照 16,384 canonical 字节、请求体接收字节 4,194,304。即使无 Content-Length 也限制流式读取；拒绝压缩体。使用现有 `validate_upload_body`、canonical codec 和错误类型；不支持的 media/encoding 返回安全 `invalid_payload`。不泄漏框架验证详情，不将记录写入日志。

用 Token hash 和启用凭据认证。有效认证在独立短事务中更新 `last_authenticated_contact_at`，数据库单调更新（与原值取 `greatest`）。未知/禁用凭据不更新。验证失败、设备不匹配或未知/未授权站点仍可计为认证联系；入口超限拒绝可不计。联系状态存储失败返回 503。此时间证明认证，不证明接收成功或读数新鲜。

随后验证设备身份和完整批次，在 C2 锁内原子重新检查授权、创建缺少的 point/session/source、插入读数。Devices/sites/grants 必须已存在；point 从站点 UUID 派生。比较全部不可变描述符字段，包括仅重复数据批次携带的描述符。相同记录键/hash 为重复，不同 hash 为整批 `record_conflict`；描述符不一致为 `descriptor_conflict`。插入新行后遇到任一冲突，新增行及新描述符均不得提交。跨设备复用 session/source ID 必须失败，不得改写归属。

提交成功后才返回 M1 200 确认，按请求顺序包含全部记录 ID（含已存在重复记录）。401/403/409/413/422/503 使用 M1 固定错误码/状态。提交后响应丢失通过重试相同批次并确认行数不变来验证；实际 Logger 响应丢失恢复属于 M4。提交后的失败不得描述为已知回滚。

### C6. 状态、查询与完整 CSV

站点列表/详情显示 UUID/地址/描述、已接收记录数、最新已接收 `received_at` 和当前授权设备。设备分别显示 `last_authenticated_contact_at` 与全部历史已接收 `received_at` 最大值。缺失显示“Unavailable”，不编造在线/离线阈值。站点时间来自本站读数，不来自最后新增授权的设备。服务端会话列表仅显示不可变描述符，不编造 open/completed 状态。

| 方法及管理相对路径 | 契约 |
| --- | --- |
| `GET /sites/{site_id}` | 站点元数据/状态与授权设备 ID |
| `GET /sites/{site_id}/sessions` | 同 C4 分页，按 `started_at,session_id` 排序，可按设备过滤 |
| `GET /sites/{site_id}/readings` | 下述过滤器及 `offset=0,limit=50`（1–200）→ `{items,total}`；items 为完整 M1 Snapshot |
| `GET /sites/{site_id}/readings/{device_id}/{record_id}` | 属于该站点的完整 Snapshot，否则 404 |
| `GET /sites/{site_id}/readings.csv` | 同一过滤器、无分页 → 完整流式 CSV 附件 |

读数过滤器：可选 `session_id`、`device_id`、`received_from`、`received_before`。时间必须为带时区的 ISO 8601，归一化 UTC，左闭右开；同时提供上下界时要求 start < end。合法但不匹配的 session/device 返回空结果，错误 UUID/时间为 422。默认不限制时间。按 `(received_at,device_id,record_id)` 升序；单页 total/items 共用读快照。UI 标明 UTC，不得静默按本地时间解释。表格展示常用指标，详情显示全部 18 指标、脱敏字段和质量；导出全部筛选结果，而非当前页。

表格和导出共用过滤解析/条件构造器。读取 canonical Snapshot 文本，校验后序列化，保留精确小数字符串 scale。导出使用独立只读 REPEATABLE READ 事务，服务端 cursor 每块最多读取 500 行，statement timeout 为 30 分钟。无行数上限、服务端临时文件、后台任务或全量内存缓存。每进程最多两个并发导出，超出时在流开始前返回固定 503。客户端取消、超时、完成或生成器异常均关闭 cursor/事务/连接并释放名额。响应头发出后发生错误则关闭不完整下载，不能追加错误行或宣称完成。这是同步、不可续传的导出。

CSV 使用无 BOM 的 UTF-8、逗号分隔、CRLF 行和标准 CSV 引号，固定安全文件名 `watt-profile-readings.csv`。列顺序：`schema_version,device_id,record_id,site_id,session_id,source_id,measurement_point_id,meter_time_raw,meter_time_utc,received_at,sampled_at`，随后是 M1 C5 表格顺序的 18 个指标，最后为 `fields,quality`。嵌套列为紧凑 canonical JSON，保留 null/数组顺序；标量 null 为空单元格，真实零仍为零，时间按 M1 UTC 序列化。无站点自由文本、Token、指纹、内部 hash、重复 canonical 或接收落库时间。字符串来自已校验 Snapshot 语法，不是任意电子表格公式。空结果仅导出表头。分别发起的查询/导出可观察到新增数据；一致性指相同过滤/表示及每次请求内稳定快照，不承诺跨请求冻结数据集。

### C7. 浏览器行为

使用小型 English HTML 页面和最少 JavaScript，匹配 Logger 明暗主题行为（浏览器只持久化主题偏好）。首页提供站点搜索、设备和注册入口。注册流程：粘贴 JSON → 隐藏 Token、显示已有元数据差异的预览 → 确认 → 带站点/设备链接的结果。站点元数据编辑、授权/凭据操作独立。刷新不得重复提交修改。安全展示验证、冲突、登录过期、数据库不可用状态及重新加载/重试提示。

站点页提供状态、session/device/time 过滤、分页读数、完整详情和 CSV。设备页提供联系/读数时间、授权站点、凭据禁用。不得重新展示 Token、隐式撤销授权或从积压数据推断“当前站点”。首次 ingest 前空/未注册状态仍可使用。可见页面每 10 秒轮询状态，不重叠，隐藏时暂停；不在操作员阅读时自动刷新读数表。表单标签和键盘操作可用，禁用/撤销需显式确认。UI 行为不能替代服务端校验。

### C8. 容器、合成验证与恢复

交付 `deploy/server/Dockerfile`、独立 `deploy/server/compose.yml`、`Caddyfile` 和脱敏 `.env.example`；根 `docker-compose.dev.yml` 新增 `server`、`server-db` 和范围受限的开发凭据初始化。保留仅启动 Logger 的 `up logger` 行为。仅服务端 `up server` 启动其 DB/setup 依赖，不启动 Logger。使用项目专属卷/网络，无外部卷、主机串口映射或生产配置。应用 UID/GID 为 10001；root initializer 仅准备挂载卷后退出。

开发发布服务端 `127.0.0.1:${WATT_DEV_SERVER_PORT:-8081}:8080`，Logger 保留 8080，不发布 PostgreSQL，不需要 Caddy/域名。仅开发管理员为 `admin` / `testtesttesttest`；setup 将密码/DB URL 放入私有卷，不作为独立部署默认值。以 `WATT_DEV_IMAGE_TAG` 和唯一项目名隔离镜像/资源。独立部署 PostgreSQL 持久化，要求私有密码/URL 文件，Caddy HTTPS 要求域名并持久化证书/配置卷。仅 Caddy 发布 80/443。正常 down/recreate 保留卷。提供所有者管理隧道 HTTP 的显式 Caddy/origin/cookie 配置替代，不配置 WireGuard。

健康依赖等待 PostgreSQL 和应用 ready。保持 PostgreSQL durability 默认值。启动迁移不能静默替换数据库或重生成管理员凭据。全部资源打包到服务端镜像，构建/运行本地主机架构。M3 不要求服务端 ARM64，但现有 Logger 目标/构建行为必须保持有效。

提供 `python -m watt_profile.cli.server_demo prepare --output NEW_DIRECTORY` 及 `... ingest --directory DIRECTORY --server-origin ORIGIN`。这是仅开发 CLI，不是启动 hook 或公开 seed API。Prepare 创建私有合成注册 JSON 及有效 M1 批次，覆盖两站点、一设备、三会话（A→B→A）、null/零/scale 敏感指标和安全字段。以一次捕获 UTC 时间的固定相对偏移及新 UUID/Token 生成，并保存以便重复重放。Ingest 从私有 Token 文件读取，调用真实 HTTP 端点并验证完整确认；不直接写数据库、不打印凭据。默认 origin 仅允许 loopback；Compose 用已说明的显式 `--allow-development-network` 允许临时网络中的 `server` 服务。不提供任意远端主机选项。`--site A|B` 支持分步骤注册和拒绝验证。

使用 PostgreSQL `pg_dump --format=custom` 一致备份，在私有临时目录排他创建输出。用 `pg_restore --exit-on-error --no-owner --no-acl` 恢复到新建、隔离、空数据库，不能覆盖已有数据库。命令使用秘密文件/容器环境，不把密码放入命令参数/日志。验证 ID、canonical hash、描述符、凭据摘要/禁用状态、授权，并对恢复实例认证/查询。部署管理员密码和 Caddy 设置属于独立配置，不包含在 DB 备份。M3-09 必须将这些说明补全为实际测试过的 Compose 命令、路径、环境设置及 walkthrough 创建资源的清理步骤。

## 3. 原子实现任务

所有任务运行 Ruff check 和 format-check。测试使用合成数据；涉及数据库行为时使用真实隔离 PostgreSQL，不能用 SQLite 替代。单元/HTTP 测试不得依赖后续 UI/容器任务。M3-01 提供仅测试 PostgreSQL fixture launcher，使 M3-02 起可在部署 Compose 完成前验收。

### M3-01 — 运行基础

依赖：已验收 M1/M2。契约：C1、C8 打包。

- 交付服务端 package/CLI 骨架、配置校验、排他密码生成、依赖锁/资源和显式临时 PostgreSQL 测试 launcher。无导入副作用。
- Launcher 启动唯一名称的临时 PostgreSQL 17 容器，仅 loopback 随机端口、无已有卷；等待 ready、提供私有连接配置，仅清理自己创建的资源。DB 测试缺少显式 opt-in 时必须拒绝，不能使用环境里的部署 URL。
- 验收错误配置/秘密权限拒绝、密码覆盖拒绝、安装后资源访问及 launcher 启动/清理。边界：无业务 schema、注册或 HTTP 服务。
- 检查：`python -m pytest tests/test_server_bootstrap.py tests/test_package.py -q`；第 4 节记录精确 launcher/DB 测试命令。

### M3-02 — PostgreSQL schema 与仓储

依赖：M3-01。契约：C2、M1 C5。

- 交付 schema、`server_0001`、迁移锁、启动集成和基础类型化仓储，暴露 `server_admin migrate`。
- 验收首次/重复迁移、未知 revision 拒绝、并发迁移互斥、直接 SQL 约束/复合归属、全部字段 canonical/Decimal/null/hash 往返、事务回滚；检查投影列和独立 revision 链。
- 边界：仅合成 fixture，无认证、接收端点或页面。检查：通过 M3-01 DB launcher 运行 `python -m pytest tests/test_server_storage.py -q`。

### M3-03 — 注册与授权服务

依赖：M3-02。契约：C2、C4。

- 交付严格摘要模型、预览/提交、站点/设备操作、仅 hash 的凭据注册/禁用和带锁授权操作。
- 验收多行元数据、重复键拒绝、预览无写入/秘密、已知 UUID 元数据保留、幂等、A/B 授权保留、禁用/跨设备/新 Token 冲突、回滚与并发注册。所有访问变更保留历史行。
- 边界：仅服务层，无管理 HTTP 或 Logger UI 修改。检查：隔离 PostgreSQL 下 `python -m pytest tests/test_server_registration.py -q`。

### M3-04 — 原子接收服务

依赖：M3-03。契约：C2、C5、M1 C6。

- 交付独立于 HTTP 的凭据/联系和事务批次接收；复用 canonical 验证器，返回类型化 M1 确认/错误。
- 验收未知/禁用 Token、不匹配/未授权站点、拒绝载荷的联系语义、描述符/记录冲突、全重复/混合批次、新描述符/行回滚、提交后重试。独立连接/屏障覆盖相同/不同内容并发重放、跨设备描述符碰撞及并发禁用/撤权。
- 边界：无网络路由/上传器。检查：隔离 PostgreSQL 下 `python -m pytest tests/test_server_ingest.py tests/test_upload_contract.py -q`。

### M3-05 — 查询、状态与流式 CSV

依赖：M3-04。契约：C6。

- 交付类型化过滤/分页/状态服务、Snapshot 详情和有界导出迭代器，取消时清理资源。
- 验收时间区间边界、同时间排序、字面地址搜索、空结果、组合过滤、积压与联系时间区分、全部字段/null/小数 scale、并发写入下稳定导出，以及固定数据集的查询/CSV 等价。验证有界读取、导出名额限制和中断资源释放，不全量加载导出。
- 边界：无 HTML/HTTP，隔离/cursor 使用真实 PostgreSQL。检查：`python -m pytest tests/test_server_queries.py tests/test_server_export.py -q`。

### M3-06 — 认证与 HTTP 服务

依赖：M3-05。契约：C1、C3–C6。

- 交付应用生命周期、登录/session/CSRF、完整管理/ingest 路由、有界请求体读取、流式响应和 readiness，包含最小登录表单。
- 验收认证隔离、过期/退出/重启、限流、Origin/CSRF/host 检查、安全错误/隐私、chunked 超限、禁用凭据行为、精确 M1 响应及 DB 不可用健康状态。测试 HTTP 断开释放导出资源，断言无自动向外上传。
- 边界：无完整 dashboard 或容器交付。检查：隔离 PostgreSQL 下 `python -m pytest tests/test_server_api.py tests/test_server_auth.py -q`。

### M3-07 — 管理页面与 Logger 摘要互通

依赖：M3-06。契约：C4、C6–C7。

- 交付 dashboard/site/device 流程和 Logger JSON 复制变化，保留当前注册 API 和可读展示。
- 验收键盘操作、粘贴/预览/注册、多行元数据、已有站点提示且不覆盖、显式修改、授权/禁用确认、时间、过滤/详情/CSV、空/错误/过期状态和秘密清除。浏览器测试把实际 Logger 生成摘要传到服务端，不另造数据形状。
- 边界：无上传器、凭据轮换或 UI 框架迁移。检查：`python -m pytest tests/browser/test_server_ui.py tests/browser/test_logger_ui.py tests/test_logger_api.py -q`；隔离应用/DB/fake serial、开发 Chromium。

### M3-08 — 容器交付

依赖：M3-07。契约：C8。

- 交付服务端 Dockerfile/独立 Compose/Caddy/example，扩展根开发 Compose，双语 README 增加入口链接。新增构建/启动/ready/持久化自动化和第 4 节精确操作命令。
- 验收仅服务端和仅 Logger 启动、DB 迁移、非 root 应用、私有文件、开发仅 loopback、DB 不发布、重建保留数据、包资源，以及隔离环境 Caddy 配置校验（不请求公网证书）。记录具体镜像版本和运行目标。
- 边界：无生产部署、公网证书或硬件通过声明。检查：显式唯一项目下 `python -m pytest tests/integration/test_server_compose.py -q`，以及现有 Logger Compose 回归、Caddy 验证命令。

### M3-09 — 合成 walkthrough 数据与隔离恢复

依赖：M3-08。契约：C8、C4–C6。

- 交付 demo CLI、可重复的已保存批次、真实 HTTP 提交/重放，以及第 4 节精确私有备份/新数据库恢复说明。
- 验收注册前拒绝、A 后 B 注册/接收、A→B→A 归属、重放无重复、撤权/禁用后历史查询、备份恢复等价。恢复服务使用独立开发配置/端口，不触碰源环境。仅清理创建资源。
- 边界：无公开 seed 端点、自动样例插入或生产备份。检查：`python -m pytest tests/test_server_demo.py tests/integration/test_server_recovery.py -q` 及已记录的隔离 walkthrough 命令。

### M3-10 — 服务端综合验收与交接

依赖：M3-01–M3-09。契约：全部。

- 交付真实 HTTP 注册→接收→查询→CSV 集成、全部回归/Compose/浏览器检查及第 4 节准确双语证据。运行 M1/M2 回归，验证 Logger 正常记录仍为 pending。
- 验收前面全部任务条件、重建持久化、查询/导出保真、浏览器 walkthrough、备份恢复清理记录。区分自动化 walkthrough 与所有者验收；所需检查和所有者 walkthrough 验收后才标记 M3 已验收。
- 边界：无新功能、上传器/硬件验收或额外里程碑报告文件。检查：`python -m pytest tests/test_m3_pipeline.py -q` 及完整第 4 节检查。

## 4. 检查与完成记录

### 已测试的 M3-01 基础与 PostgreSQL launcher

在仓库根目录运行，Docker 必须可用。已验证环境是 `/tmp/watt-m3-run.Qsb5bu/venv` 中的 Python 3.12.13，通过 `requirements-dev.lock` 及 `pip install --no-build-isolation --no-deps -e .` 安装；其他运行应替换为自己隔离的 Python 3.12 venv/cache 路径。以下精确命令于 2026-10-11 通过：

```sh
WATT_TEST_POSTGRES=1 UV_CACHE_DIR=/tmp/watt-m3-run.Qsb5bu/cache /tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest tests/test_server_bootstrap.py tests/test_package.py -q --tb=short
/tmp/watt-m3-run.Qsb5bu/venv/bin/python -m ruff check .
/tmp/watt-m3-run.Qsb5bu/venv/bin/python -m ruff format --check .
```

结果：**42 passed**，包含真实 PostgreSQL launcher 和安装包资源；Ruff 两项均通过。严格 public-origin 回归覆盖空白/控制字符、反斜线、非法 authority/host 及合法 DNS/localhost/IPv4/IPv6/端口。Docker socket 访问需要沙箱权限提升；初次受限运行是 41 passed 和一项 Docker 权限失败，不是产品缺陷。

以下精确 launcher 示例也已通过。M3-02 及后续 DB fixture 应使用相同的 `start → try → finally → stop` 生命周期，只使用 `server.database_url` 或 `server.database_url_file`。将 `SELECT 1` 主体替换为该任务检查，并在释放 engine、停止 launcher 前关闭所有 session、stream 和 connection。不得打印 URL 或凭据。

```sh
env -u WATT_SERVER_DATABASE_URL -u WATT_SERVER_DATABASE_URL_FILE WATT_TEST_POSTGRES=1 PYTHONPATH=tests /tmp/watt-m3-run.Qsb5bu/venv/bin/python - <<'PY'
from postgres_test_server import start_postgres_test_server, stop_postgres_test_server
from sqlalchemy import create_engine, text

server = start_postgres_test_server()
engine = None
try:
    engine = create_engine(server.database_url, hide_parameters=True)
    with engine.connect() as connection:
        assert connection.scalar(text('SELECT 1')) == 1
    # Later DB tests use only this URL; close sessions/connections before cleanup.
finally:
    try:
        if engine is not None:
            engine.dispose()
    finally:
        stop_postgres_test_server(server)
assert not server.directory.exists()
print('Isolated PostgreSQL query and private-directory cleanup passed')
PY
```

Launcher 要求 `WATT_TEST_POSTGRES=1`，拒绝环境中的 `WATT_SERVER_DATABASE_URL`，从不读取部署 URL 文件。每次调用创建唯一 `watt-m3-test-...` 容器，使用 `postgres:17.6-alpine`、匿名数据卷、随机且仅 loopback 的端口，以及含 0600 URL 文件的私有临时目录。清理仅删除该容器、其卷和目录。任务测试验证目录和卷删除；示例验证查询成功和目录删除。未使用已有数据库或部署资源。M3-02 的检查结果记录在下方。

### M3-02 schema 与仓储检查

已实现独立 `server_0001`、M1 C5 PostgreSQL 全列/约束/索引、有限连接池和事务局部设置、advisory 迁移锁、未知版本拒绝、bootstrap/admin 迁移及 typed 存储基础。测试覆盖新建/无操作/失败/并发迁移、直接 SQL 约束/复合归属、canonical/Decimal/null/hash/投影往返与回滚。Launcher readiness 已改为 TCP，防止 PostgreSQL 临时初始化服务器提前通过检测。

```sh
WATT_TEST_POSTGRES=1 PYTHONDONTWRITEBYTECODE=1 /tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest tests/test_server_storage.py tests/test_server_bootstrap.py tests/test_package.py -q -o cache_dir=/tmp/watt-m3-run.Qsb5bu/pytest-cache --basetemp=/tmp/watt-m3-run.Qsb5bu/m3-02-integration
```

结果：**74 passed**（32 项存储及 42 项基础/打包），Python 3.12.13/PostgreSQL 17.6。实现阶段另执行了非浏览器/非 Compose 回归：**424 passed**。Ruff 与 diff 检查通过。缺少 DB opt-in 时在连接前拒绝。Fixture 清理核验了自己的容器、匿名卷和私有目录已删除；未使用预存资源。独立审查与集成检查通过；M3-03 及后续仍待执行。

### M3-03 注册与授权检查

已实现严格注册解析、只读隐藏凭据预览、锁内原子提交、typed 站点/设备管理、首个/同一启用凭据的仅哈希存储、禁用及幂等授权/撤销。

```sh
WATT_TEST_POSTGRES=1 PYTHONDONTWRITEBYTECODE=1 /tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest tests/test_server_registration.py tests/test_server_storage.py -q --tb=short -o cache_dir=/tmp/watt-m3-run.Qsb5bu/m3-03-cache --basetemp=/tmp/watt-m3-run.Qsb5bu/m3-03-tests-final
```

结果：**82 passed**（50 项注册及 32 项存储）；最终注册单独检查：**50 passed**。Ruff 通过。覆盖多行元数据、重复/额外键、预览无写/无秘密、UUID 保留、幂等、A/B 授权、禁用/跨设备/新 Token 冲突、回滚、字面地址搜索、历史保留，以及独立连接竞争（包含强制凭据 UNIQUE 竞争和锁超时）。自建容器/卷/私有目录及任务测试缓存已删除；共享运行工具保留供后续任务使用。独立审查与集成检查通过（84 项注册/存储检查）。


审查修复：注册事务已将连接池 checkout 超时转换为固定可重试存储失败。修复后注册测试 **52 项通过**，包含预览/提交真实十连接耗尽、释放、无部分写与成功重试；Ruff 通过，临时资源已删除。

### M3-04 原子接收检查

已实现独立单调认证联系事务与锁内原子批次接收，使用 typed M1 错误/确认。描述符/记录写入前在共享锁内重查启用凭据和站点授权；提交后才构造确认。

```sh
env -u WATT_SERVER_DATABASE_URL -u WATT_SERVER_DATABASE_URL_FILE WATT_TEST_POSTGRES=1 PYTHONDONTWRITEBYTECODE=1 /tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest tests/test_server_ingest.py tests/test_upload_contract.py -q --tb=short -o cache_dir=/tmp/watt-m3-04-impl-final-cache --basetemp=/tmp/watt-m3-04-impl-final-tests
```

结果：**105 passed**（44 项接收及 61 项上传契约），Ruff 通过。补充最后两项 contact/disable 竞争前，接收/上传/注册组合曾为 155 项通过。覆盖无效/禁用凭据、失败载荷联系语义、设备/站点授权、不可变描述符/记录冲突、重复/混合批次、回滚、已提交重试、独立连接相同/不同重放、跨设备碰撞及 disable/revoke/contact 锁顺序。两次临时 PostgreSQL 生命周期与任务缓存均已清理；独立审查与集成检查通过。未增加网络路由或上传器。


集成修复：JSON 序列化前重验内部批次/Snapshot 的 Python 类型，关闭可能包含输入的 serializer warning。修复后接收/上传测试 **115 项通过**，保留固定错误分类、Decimal scale/null/数组顺序和字节限制，同时拒绝构造的 float 且无写入；Ruff 通过，自建资源已删除。

### M3-05 查询/状态/CSV 检查

已实现共用严格筛选、完整 canonical Snapshot 查询/详情、稳定 total/items 分页、站点/设备状态和可显式关闭的 CSV 流。导出采用独立只读 REPEATABLE READ 事务、30 分钟语句超时、服务器游标每批最多读取 500 行及进程级最多两个导出。

```sh
WATT_TEST_POSTGRES=1 /tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest tests/test_server_queries.py tests/test_server_export.py -q --basetemp=/tmp/watt-m3-05-pytest -o cache_dir=/tmp/watt-m3-05-cache
```

结果：**22 passed**，Ruff 通过（93 文件）。覆盖 UTC 边界、同时间/跨设备排序、并发插入时稳定快照、18 项指标/null/scale 与嵌套 JSON、完整查询/导出等价、空结果仅表头、有限 fetch、名额分配/释放、首次 next 前关闭、超时与异常清理。真实阻塞 FETCH 取消、单个 stream 清理失败后 manager 继续清理均通过。Fixture 自建容器/卷/私有目录和任务缓存已删除。独立审查与集成检查通过；未增加 HTTP/HTML。

### M3-06 认证与 HTTP 检查

已实现独立管理员状态、CSRF/Origin/Host 校验、完整管理及 M1 接收路由、有限 UTF-8 请求体、健康检查和生命周期清理。CLI 使用单 worker，关闭 reload、转发头信任及查询参数访问日志。内部健康探测连接内部端口时，发送配置公开 origin 对应的 Host authority。

```sh
WATT_TEST_POSTGRES=1 /tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest tests/test_server_api.py tests/test_server_auth.py -q --tb=short -o cache_dir=/tmp/watt-m3-06-cache --basetemp=/tmp/watt-m3-06-tests
/tmp/watt-m3-run.Qsb5bu/venv/bin/ruff check .
/tmp/watt-m3-run.Qsb5bu/venv/bin/python -m ruff format --check .
```

结果：**17 passed**；Ruff 检查通过（97 个已格式化文件）。最终边界补充前，相关服务与 Logger API 回归 **204 项通过**。最终定向检查覆盖认证隔离/过期/退出/重启/限流、安全错误、Host/Origin、请求体限制、ready/DB 不可用和无自动上传。实际应用 CSV 路由覆盖正常断开及真实 PostgreSQL 阻塞读取中取消：在测试八秒期限内释放 stream、连接与导出名额。覆盖 UTF-8 解码及启动异常脱敏。保留一条 Starlette/httpx 弃用警告。Fixture 自建容器/卷/私有目录和任务缓存已删除。独立审查与集成检查通过；完整页面与容器交付属于后续任务。

### M3-07 管理页面与 Logger 互通检查

已实现英文 dashboard/site/device 页面及主题登录页，包含可键盘操作的表单、隐藏 Token 的注册预览、显式元数据/授权/禁用控制、独立状态时间戳、筛选/详情及原生完整 CSV 下载。可见状态每十秒无重叠轮询，不替换读数表或编辑草稿。Logger 复制内容改为格式化五字段 JSON，保留可读标签及原注册 API。摘要清理同时阻止迟到响应恢复已取消的秘密。

```sh
WATT_TEST_POSTGRES=1 PLAYWRIGHT_BROWSERS_PATH=/tmp/watt-m3-run.Qsb5bu/browser /tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest tests/browser/test_server_ui.py tests/browser/test_logger_ui.py tests/test_logger_api.py -q -p no:cacheprovider --basetemp=/tmp/watt-m3-07-synchronized
```

2026-10-12 结果：**35 passed**；相关 Server HTTP/auth/安装包检查 **18 项通过**。最后原生下载错误处理补充后，读数/筛选/详情/CSV/轮询浏览器用例单独 **1 passed**：每页一行时成功附件仍包含三条匹配记录；503 打开安全错误标签页，不将 JSON 保存成 CSV。Ruff check/format-check（98 个 Python 文件）及 diff 检查通过。浏览器将实际 Logger 生成的 JSON 传入使用临时 PostgreSQL 的 Server。覆盖多行/转义元数据、已知站点保护与显式编辑、确认/取消、独立创建/首凭据/授权、冲突/不可用/过期状态、摘要清理/迟到响应、主题及隐藏/无重叠轮询。注册结果等刷新完成再显示；Logger 键盘准备等待初始配置 DOM 加载，保留键盘断言。保留既存 Starlette 测试客户端弃用警告。八个任务 basetemp、两个应用线程、浏览器 context 及 fixture 自建 DB 资源已清理，共用运行工具保留。独立审查与集成检查通过（40 项浏览器/API 及 18 项兼容检查）；Compose 和所有者验收属于后续检查。

审查修复：注册成功后立即清理原摘要并显示已提交结果，再独立刷新状态。刷新错误不能隐藏结果、保留 Token、阻止下一次预览或覆盖后续操作。修复后指定浏览器/Logger API 测试 **40 项通过**，增加真实 HTTP 下刷新 503/断网/401，以及刷新等待期间新预览/取消分支。Ruff 与 diff 检查通过；wrapper 自建 `/tmp/watt-m3-07-fix1-*` 缓存/basetemp 及 fixture 资源已删除。

### 后续计划自动化入口

在 `/tmp` 创建唯一 Python 3.12 venv，安装 `requirements-dev.lock`，再执行 `pip install --no-build-isolation --no-deps -e .`。使用临时 cache/data 及上面已测试的 M3-01 PostgreSQL launcher，不得使用生产或继承的部署连接设置。浏览器测试需要开发 Chromium。M3-08/09 必须在验收前补齐测试过的容器/恢复命令。以下后续入口在对应任务记录证据前仍属于计划。

- 第 3 节固定各任务 pytest 入口，DB 用例通过隔离 PostgreSQL harness 运行。
- 完整离线回归：`python -m pytest -q`；显式 opt-in 临时 Compose 测试并提供项目设置，不能把跳过的集成测试当作通过。
- 静态检查：`python -m ruff check .`、`python -m ruff format --check .`。
- 安装包检查：在 checkout 外移除继承 `PYTHONPATH` 后导入 server、contracts 和包资源。
- Compose：唯一 `watt-m3-...` 项目及 `WATT_DEV_IMAGE_TAG`、未占用 loopback 端口、构建/启动/ready、浏览器 walkthrough、强制重建与持久化、隔离恢复及清理。创建前检查是否已有同名资源。
- 独立部署：以合成临时配置渲染 Compose，无公网 ACME 流量验证 Caddy，检查秘密/mount/port 和持久卷。真实公网 HTTPS 部署属于 M4。

### 计划浏览器 walkthrough

1. 启动隔离开发服务端，准备私有合成 fixture 目录；打开 `http://127.0.0.1:8081`（或指定端口），用已记录的开发管理员登录。
2. 注册前提交 A 的已准备批次：预期 401、无行。浏览器粘贴 A JSON，查看隐藏 Token 的预览并确认。再次经 demo CLI 提交，预期完整确认。
3. 按 UUID/地址查找 A，查看全部指标、null、质量、会话。按 device/session/UTC 区间筛选，导出 CSV，对比所有匹配记录 ID/值，而非仅可见页。
4. 重放后行数不变。修改复制元数据后再次预览/注册 A：已有元数据不变；通过站点表单显式编辑。
5. 同设备注册 B，接收 B 及之后的 A 会话；验证归属分离、两授权都保留。联系时间推进独立于旧积压读数时间。
6. 撤销 B，验证 B 接收被拒绝、A 仍允许；禁用凭据，后续接收均拒绝。历史读数/导出仍可用。
7. 使用原卷重建 app/DB 容器：管理员重新登录，ID、元数据、授权、禁用状态、读数持久化。执行隔离备份/恢复 walkthrough 并比较恢复状态。
8. 仅停止/移除本次容器、网络、临时卷、镜像标签、fixture/备份目录、临时凭据。不对独立部署或已有项目使用破坏性卷清理。

### 验收记录

| 项目 | 状态 / 证据 |
| --- | --- |
| 双语设计与任务边界 | 已在本文定义 |
| M3-01 基础实现与任务检查 | 42 passed；Ruff、安装包检查通过；独立审查与集成检查通过 |
| M3-02 schema/仓储 | 32 项存储检查、74 项定向检查；独立审查与集成检查通过 |
| M3-03 注册/授权 | 52 项注册、84 项定向检查；独立审查与集成检查通过 |
| M3-04 原子接收 | 115 项接收/上传检查；独立审查与集成检查通过 |
| M3-05 查询/状态/CSV | 22 项真实 PostgreSQL 检查；独立审查与集成检查通过 |
| M3-06 认证/HTTP | 17 项真实 PostgreSQL 检查；独立审查与集成检查通过 |
| M3-07 管理页面/Logger JSON | 40 项浏览器/API、18 项兼容检查；独立审查与集成检查通过 |
| M3-08 容器交付 | 2 项 Server/Caddy、1 项 Logger Compose/浏览器检查；独立审查与集成检查通过 |
| M3-09 演示/恢复 | 18 项演示/恢复检查；独立审查与集成检查通过 |
| M3-10 集成验收与交接 | 待执行 |
| 完整 M1/M2 回归与集成静态/安装包检查 | 待执行 |
| 真实 PostgreSQL 并发/原子性/CSV 检查 | 任务检查通过；最终集成检查待执行 |
| 本地 Compose、Caddy 验证、自动浏览器 walkthrough | 待执行 |
| 隔离备份恢复与资源清理 | 待执行 |
| 所有者浏览器 walkthrough 验收 | 待执行 |
| 公网部署、Logger 上传器/恢复、物理硬件 | M4，不由 M3 声明通过 |

每完成一项，在此同步双语追加实际命令、结果、环境/镜像版本、重要修复、不可用检查和清理证据。设计就绪不等于实现验收。目前已执行上面的基础/存储检查；浏览器、部署及所有者验收仍待执行。

### 容器操作（M3-08）

本机目标为 Linux AMD64。Server 构建使用 `python:3.12.13-slim-bookworm`；两个 Server Compose 使用 `postgres:17.6-alpine`，独立部署使用 `caddy:2.10.2-alpine`。Server 镜像包含已安装包、模板、静态资源和独立迁移链。应用 UID/GID 为 10001。没有数据库端口发布、宿主设备、external 卷或生产设置，保留 PostgreSQL 持久化默认值。开发初始化器以 root、无网络运行，只准备其凭据卷后退出；随机 DB 密码和开发管理员密码均以独占创建方式只生成一次。重启／迁移不重置数据库或轮换文件。应用只读挂载 0700 私有凭据目录，文件为 0600。健康探针连接内部应用 8080 端口，但使用 public origin 的 Host authority，不放宽 Host 检查。PostgreSQL readiness 使用 TCP，避免初始化阶段临时 Unix socket 误报。

从仓库根目录选择未使用的项目／镜像标签和回环端口。确认标签筛选的容器／网络／卷列表及精确镜像列表为空；发现已有资源应停止，不得复用。此示例只启动 Server 依赖，正常 `down` 保留数据库和凭据卷：

```sh
export WATT_DEV_IMAGE_TAG="watt-m3-owner-$(date +%s)-$$"
export WATT_DEV_SERVER_PORT=18081
M3_PROJECT="$WATT_DEV_IMAGE_TAG"
python3.12 - <<'PORT_CHECK'
import os, socket
with socket.socket() as listener:
    listener.bind(('127.0.0.1', int(os.environ['WATT_DEV_SERVER_PORT'])))
PORT_CHECK
docker container ls -aq --filter "label=com.docker.compose.project=$M3_PROJECT"
docker network ls -q --filter "label=com.docker.compose.project=$M3_PROJECT"
docker volume ls -q --filter "label=com.docker.compose.project=$M3_PROJECT"
docker image ls -q "watt-profile-server-dev:$WATT_DEV_IMAGE_TAG"
docker compose -p "$M3_PROJECT" -f docker-compose.dev.yml up --build -d --wait --wait-timeout 120 server
```

打开 `http://127.0.0.1:18081`，以 `admin`／`testtesttesttest` 登录空 dashboard。`up logger` 只启动 Logger 和其初始化器，保留 `operator`／`testtesttesttest`，使用 8080 端口（可用 `WATT_DEV_PORT` 覆盖）。两应用的卷和凭据互相独立。停止允许 30 秒；启动／健康检查均有有限等待。通过下列命令验证持久化，重启后重新登录（会话被撤销），检查已有注册站点／读数。初始化器保留已有秘密文件：

```sh
docker compose -p "$M3_PROJECT" -f docker-compose.dev.yml up -d --force-recreate --wait --wait-timeout 120 server
docker compose -p "$M3_PROJECT" -f docker-compose.dev.yml down
docker compose -p "$M3_PROJECT" -f docker-compose.dev.yml up -d --wait --wait-timeout 120 server
# 仅在此临时项目使用完毕后：
docker compose -p "$M3_PROJECT" -f docker-compose.dev.yml down --volumes --remove-orphans
docker image rm "watt-profile-server-dev:$WATT_DEV_IMAGE_TAG"
```

独立部署由所有者管理。将 `deploy/server/.env.example` 复制为被忽略的 `.env.local`，设置真实域名、管理员用户名和两个绝对私有目录路径。DB 目录包含 `password`；Server 目录包含 `database-url` 和 `admin-password`。目录以 0700 创建；文件使用 umask 0077 独占创建，权限为 0600。生成 URL-safe 随机 DB 密码，并在 `postgresql+psycopg://watt:<password>@server-db:5432/watt` 使用相同值；通过 `python -m watt_profile.cli.server_admin generate-password --output NEW_PATH` 生成管理员密码。Server 目录及文件必须属于 UID/GID 10001；PostgreSQL 入口以 root 读取其独立密码目录。整个私有目录只读绑定，以保留容器内私有父目录。独立部署没有开发密码默认值。所有者授权部署前先审阅解析后的配置：

```sh
docker compose --env-file deploy/server/.env.local -p OWNER_PROJECT -f deploy/server/compose.yml config
# 仅限所有者已授权部署；M3 验证未执行：
docker compose --env-file deploy/server/.env.local -p OWNER_PROJECT -f deploy/server/compose.yml up --build -d --wait --wait-timeout 120
```

只有 Caddy 发布 80／443；DB 和应用保持内部端口。Caddy data／config 和 DB data 使用持久化项目卷，正常 `down` 保留这些卷。Caddy 将 Host 替换为配置域名，移除不可信 `Forwarded` 和 `X-Forwarded-*` 输入；应用 Uvicorn 设置 `proxy_headers=False`。验证期间未申请公网证书。

若所有者自行管理 HTTP 隧道，需明确将 Caddy 站点标签替换为 `http://YOUR_TUNNEL_HOST`，保留代理块（将 `header_up Host` 设置为 `YOUR_TUNNEL_HOST`），覆盖 Server `WATT_SERVER_PUBLIC_ORIGIN=http://YOUR_TUNNEL_HOST` 和 `WATT_SERVER_COOKIE_SECURE="false"`，将 Caddy 发布端口替换成一个所有者选择的回环 HTTP 映射（如 `127.0.0.1:18082:80`），并只读挂载替代 Caddyfile。必须移除原 80／443 映射，而不是另加端口；Compose 2.24.4+ 的 override 可使用 `ports: !override ["127.0.0.1:18082:80"]`。隧道可达性和访问控制由所有者提供。这是明确的部署替代方案，不是 WireGuard 配置，也不是已验证的公网端点。

真实容器自动化创建并清理其精确唯一项目／镜像，动态选择未占用回环端口，拒绝已存在的标签资源或镜像。它只构建／启动 Server，验证私有文件所有权／权限／只读挂载、readiness／Host 拒绝、安装资源／迁移、真实 HTTP 注册／接收／查询／CSV、重启会话撤销，以及 force recreate 和正常 down／up 后数据与凭据哈希不变。独立部署配置使用合成临时私有文件；Caddy 以 `--network none` 和合成 `.invalid` 域名校验。必须显式选择测试：

```sh
WATT_TEST_SERVER_COMPOSE=1 WATT_TEST_COMPOSE_PROJECT=watt-m3-08-recheck-1791757501 WATT_DEV_IMAGE_TAG=watt-m3-08-recheck-1791757501 /tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest tests/integration/test_server_compose.py -q --tb=short
# 等效的隔离 Caddy 校验（没有部署／启动／ACME）：
docker run --rm --network none -e WATT_SERVER_DOMAIN=synthetic.example.invalid -v "$PWD/deploy/server/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.10.2-alpine caddy validate --config /etc/caddy/Caddyfile
```

2026-10-12，更正后的容器命令 **2 tests passed**，耗时 38.35 秒。精确项目及本次构建的 Server 镜像由 `finally` 删除；共享 PostgreSQL／base／Caddy 镜像和构建缓存保留。初次运行在成功构建／启动后因测试资源路径断言写错而有一项失败；其隔离项目／镜像已先清理，再完成更正与通过。Logger 回归及最终检查在完成后记录如下。

完整既有 Logger Compose／浏览器回归 **1 test passed**，耗时 106.99 秒；使用全新项目 `watt-m2-m3-08-1791757298731173833`、同名标签及动态选择的回环端口 57835。驱动先验证标签资源和精确 Logger 镜像不存在，再以 `try/finally` 清理生命周期执行下列命令（不触碰 Server 项目或其他 Docker 资源）：

```sh
export WATT_TEST_COMPOSE_PROJECT=watt-m2-m3-08-1791757298731173833
export WATT_DEV_IMAGE_TAG="$WATT_TEST_COMPOSE_PROJECT"
export WATT_DEV_PORT=57835
export PLAYWRIGHT_BROWSERS_PATH=/tmp/watt-m3-run.Qsb5bu/browser
docker compose -p "$WATT_TEST_COMPOSE_PROJECT" -f docker-compose.dev.yml up --build -d --wait --wait-timeout 120 logger
/tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest tests/integration/test_logger_compose.py -q --tb=short
# finally，即使失败也执行：
docker compose -p "$WATT_TEST_COMPOSE_PROJECT" -f docker-compose.dev.yml down --volumes --remove-orphans
docker image rm "watt-profile-logger-dev:$WATT_DEV_IMAGE_TAG"
```

全部 M2 浏览器断言保持，并验证未启动 Server 服务。原 Logger `python:3.12-slim` base 标签成功拉取，其运行／构建行为未改变。新拉取的 `python:3.12.13-slim-bookworm`、`python:3.12-slim` 和 `caddy:2.10.2-alpine` 标签保留供后续 M3 使用；预存 `postgres:17.6-alpine` 和共享构建缓存未删除。唯一 Server／Logger 项目的容器、网络、卷及应用标签均已清理。全仓库 `python -m ruff check .`、`python -m ruff format --check .` 和 `git diff --check` 通过。未测试或访问生产服务、公网 ACME、宿主设备或 ARM64 Server。M3-09 demo／恢复和 M3-10 集成验收仍属独立任务。

M3-08 独立审查通过：2 项 Server/Caddy 及完整 Logger Compose/浏览器回归（1 项）。审查自建项目、标签和临时目录已删除。另确认并清理实现自建默认 pytest 目录 `pytest-74`/`pytest-75` 及其 current 符号链接，保留预存目录。合成恢复/最终集成及所有者验收仍待执行。

### 已验证的 M3-09 私密演示与隔离恢复

仅用于开发的 `server_demo` CLI 使用标准库 HTTP 客户端，不读取环境代理、不跟随重定向、没有启动钩子或自动外发请求。`prepare` 排他创建新的 0700 目录，包含 0600 注册 JSON、Token 文件和三个保存的批次（每批两条，A→B→A）。新 UUID 与一次捕获的 UTC 基准只生成一次；重放读取保存的身份/时间，保留 Decimal 字符串（`1.020`、`0.000`）、空指标和清洗/脱敏字段。`ingest` 只接受精确回环 origin；显式开发网络选项仅允许 `http://server:8080`。此时 Host 取自容器内另外验证为回环地址的 `WATT_SERVER_PUBLIC_ORIGIN`，随机映射端口仍遵守 Server 的严格 Host 检查。非 200 或格式错误/不完整/乱序确认均返回安全错误，不输出凭据。

从仓库根目录执行以下精确自动演练；每轮选择**全新**项目、镜像、临时目录和缓存名称。演练拒绝已有项目标签的容器/网络/卷、同名镜像标签和环境中的 Server 数据库配置。只删除自己新构建的镜像标签；保留预存 PostgreSQL/Python/Caddy 镜像。

```sh
env -u WATT_SERVER_DATABASE_URL -u WATT_SERVER_DATABASE_URL_FILE \
  WATT_TEST_SERVER_COMPOSE=1 \
  WATT_TEST_COMPOSE_PROJECT=watt-m3-09-recovery-20261012-c \
  WATT_DEV_IMAGE_TAG=watt-m3-09-recovery-20261012-c \
  /tmp/watt-m3-run.Qsb5bu/venv/bin/python -m pytest \
  tests/integration/test_server_recovery.py -q --tb=short \
  --basetemp=/tmp/watt-m3-09-run-04 \
  -o cache_dir=/tmp/watt-m3-09-cache-04
```

可执行演练位于 `tests/integration/test_server_recovery.py`。它选择独立空闲回环端口，设置 `WATT_DEV_SERVER_PORT`，执行 `docker compose -p PROJECT -f docker-compose.dev.yml up --build -d --wait --wait-timeout 120 server`。通过真实 HTTP 登录隔离开发管理员，证明注册前 401 和 B 未授权 403，依次注册 A/B、提交六条读数并重放而不新增。A 归属四条读数/两个不可变会话，B 归属两条/一个。撤销 A 保留 B 授权和历史；禁用凭据阻止后续上传，但管理员仍可读取 JSON/CSV 历史。

驱动已验证以下 CLI 形式；`NEW_DIRECTORY` 必须不存在，`ORIGIN` 必须是实际隔离回环 origin。浏览器演练在注册页依次粘贴私密的 `registration-A.json`、`registration-B.json`，其间分别执行 `--site A`/`--site B`。不要在其他地方粘贴或输出 Token。

```sh
python -m watt_profile.cli.server_demo prepare --output NEW_DIRECTORY
python -m watt_profile.cli.server_demo ingest --directory NEW_DIRECTORY --server-origin ORIGIN --site A
python -m watt_profile.cli.server_demo ingest --directory NEW_DIRECTORY --server-origin ORIGIN --site B
python -m watt_profile.cli.server_demo ingest --directory NEW_DIRECTORY --server-origin ORIGIN
```

显式隔离 Compose 网络路径也执行下列精确容器命令，驱动使用 `docker compose ... cp` 将已注册的保存数据复制到该私密目录，摄取前设置文件归属 UID/GID 10001。仅准备另一份新数据不会自动注册它。

```sh
docker compose -p PROJECT -f docker-compose.dev.yml exec -T server \
  python -m watt_profile.cli.server_demo prepare --output /tmp/synthetic-demo
docker compose -p PROJECT -f docker-compose.dev.yml exec -T server \
  python -m watt_profile.cli.server_demo ingest --directory /tmp/synthetic-demo \
  --server-origin http://server:8080 --allow-development-network
```

备份/恢复刻意限定在该驱动新建的项目。它在指定 pytest 临时根目录下新建 0700 目录，排他创建 `backup/server.dump`（`open("xb")`，0600），将 `docker compose ... exec -T server-db pg_dump -U watt -d watt --format=custom` 的二进制 stdout 写入该描述符。不得使用普通可覆盖的 shell 重定向；等价手工重定向必须先执行 `umask 077` 和 `set -C`/`set -o noclobber` 再打开新路径。

驱动选择全新 `restore_<UUID-hex>` 数据库名，执行 `docker compose ... exec -T server-db createdb -U watt NEW_DATABASE`，只在成功后记录创建，并在恢复前验证 public 表为零。重复 `createdb` 必须失败；绝不删除或清空已有库以继续恢复。将私密备份以 stdin 传给 `docker compose ... exec -T server-db pg_restore -U watt -d NEW_DATABASE --exit-on-error --no-owner --no-acl`。逐列比较全部业务表，包括 UUID、canonical Snapshot 文本/哈希、投影、描述符、凭据摘要/禁用状态、联系时间及授权。

新私密 `restored-config/` 包含独立的 0600 `database-url`、`admin-password` 文件，归属 UID/GID 10001；URL 保留隔离数据库主机/密码，但仅指向新建数据库。驱动在 `PROJECT_default` 启动单独带标签的 `PROJECT-restored` 容器，映射新的 `127.0.0.1:PORT:8080`，只读挂载配置到 `/run/credentials`。设置 `WATT_SERVER_DATABASE_URL_FILE`、`WATT_SERVER_ADMIN_PASSWORD_FILE`、`WATT_SERVER_ADMIN_USER=admin`、自己的 `WATT_SERVER_PUBLIC_ORIGIN` 和 `WATT_SERVER_COOKIE_SECURE=false`。真实 HTTP 验证管理员登录、精确 JSON/CSV 历史、禁用 Token 的 401 和独立启用 Token 的认证；启用 Token 对另一设备的批次到达设备校验并返回 403 `device_mismatch`。不声称恢复后成功摄取。再次比较源库完整备份前状态，确保源环境不变。管理员密码、cookie 和 Caddy/域名/证书配置属于独立部署配置，不从数据库备份恢复。

`finally` 清理先移除恢复容器，再删除**仅限已记录成功创建**的数据库，恢复私密配置归属，对唯一新项目执行 `docker compose ... down --volumes --remove-orphans`，仅删除本轮新 app 镜像标签，并删除本测试临时根目录内创建的演示/备份/配置目录。确认项目容器/网络/卷及镜像标签均不存在。不连接外部提供的数据库、不删除已有资源。检查需要 Docker/socket 权限提升；证明的是本地隔离恢复，不是生产部署或公开 TLS。

2026-10-12 M3-09 结果：**17 项演示测试通过**（首次受限执行的四项 socket 权限失败，通过取得 socket 权限运行解决）；最终恢复驱动 **1 passed in 19.95s**。初轮恢复驱动的禁用请求漏传必需空 JSON；已修正该测试问题。全部 Ruff check/format-check 和 `git diff --check` 通过。已移除创建的项目资源、app 标签及任务私密临时根目录/缓存，未删除基础镜像。

M3-09 独立审查通过：**同次运行 18 项演示/恢复检查通过（22.27s）**，Ruff、格式及 diff 检查通过。审查创建的临时目录、私有文件、项目资源及应用标签均已清理。恢复证据区分启用 Token 的认证与成功摄取；所有者验收及最终集成仍待执行。
