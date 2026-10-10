# M1：协议与契约

> [English — 权威版本](m1.md) · **中文**

状态：2026-10-10 实施中。M1-01–M1-03 已通过离线检查与独立审查；M1-04–M1-08 待实施。人工走查与硬件验证尚未进行。

## 1. 目标与 Agent 读取方式

建立独立 P1 包、荷兰 DSMR 5 电力映射、脱敏 fixtures、可执行共享类型、存储／上传契约和只读 probe。唯一 profile 为 `dsmr5_nl`，覆盖单相和三相电表。其他 profile、Modbus、MQTT、计费、Home Assistant 和 WarmteLink 接纳规则后续再做。

本文件包含 M1 完整设计和原子任务。交给 agent 时指定本文件与待实现任务 ID。Agent 先读本文件及仓库 [Agent 指引](../../AGENTS_zh.md)，再检查相关源码。第 2 节定义公共契约，第 3 节定义任务边界、依赖和验收，第 4 节记录检查及未来完成情况。不将这些章节再拆成其他 milestone 文档。

按 M1-01 至 M1-08 顺序执行，先验收依赖再推进。本文件不自动启用委派或授权 Git 提交。规划代码路径和命令只有对应任务实现后才存在。[MVP 路线图](../mvp-roadmap_zh.md)管理首版范围；[交接文档](../home-automation-energy-handover.md)说明 `/home/tianyu/workspace/home-automation` 内可复用 parser／probe 及其限制。

M1 实现纯协议／模型／诊断代码和离线检查。Logger UI、常驻 worker 和 SQLite 运行层属于 M2；中心管理、PostgreSQL 和接收服务属于 M3；上传器、部署集成及目标硬件验收属于 M4。

## 2. 公共实现契约

### C1. 包结构与实现边界

使用 Python 3.12、setuptools 和 `src/watt_profile` 布局。规划模块：`p1/parser.py`、`p1/profile.py`、`contracts/domain.py`、`contracts/upload.py`、`contracts/lifecycle.py`、`serial/interfaces.py` 和 `cli/p1_probe.py`。Parser/profile 只依赖标准库；领域／上传类型使用 Pydantic v2，串口使用 pyserial。M1 不需要 FastAPI、SQLAlchemy、PostgreSQL driver、MQTT、Modbus 或源项目应用导入。

`pyproject.toml` 声明运行依赖和含 pytest、Ruff、pip-tools、setuptools、wheel 的 `dev` extra。实现时用 pip-tools 将解析后的直接及间接依赖锁定到 `requirements-dev.lock`，记录重新生成命令；基于锁文件的 editable install 必须在独立虚拟环境可用。测试放在 `tests/`，fixtures 在 `tests/fixtures/`，传输样例在 `examples/ingest/`。Fixtures 不依赖包安装位置；profile 使用 Python 常量，不使用外部可编辑 YAML。

M1-01 建立工具后，通用规划检查如下：

```bash
python -m pip install -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps -e .
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

验证时不得使用会重写文件的格式化命令。实际 Logger／Server 入口、容器、迁移和部署命令是后续交付物。

### C2. P1 分帧、隐私与接纳

#### 协议依据

唯一 profile 为 `dsmr5_nl`，revision 为 `1`，依据 [Netbeheer Nederland DSMR 5.0.2 规范](https://www.netbeheernederland.nl/sites/default/files/2024-02/dsmr_5.0.2_p1_companion_standard.pdf)，重点参阅 §6.1–6.4、6.12–6.13 和 7.1。默认串口为 115200 baud、8 数据位、无校验、1 停止位。CRC 覆盖 `/` 到 `!` 的原始字节，包含两端；保留 CRLF。DSMR 版本字段 `1-3:0.2.8` 必须等于 `50`。规范表格中的 OBIS group F 后缀不属于线上 code。

应用只读：不发送串口数据，不管理 GPIO／request line。使用负责物理接口及激活的兼容 USB/P1 线材。即使 parser 能报告其完整性，该 profile 也拒收其他 DSMR 版本和无法校验的报文。

#### Parser 接口

- `TelegramFramer(max_frame_bytes=16384).feed(chunk: bytes) -> list[bytes]` 增量返回完整帧；`reset()` 丢弃半帧。诊断只暴露丢弃帧计数。
- `parse_telegram(frame: bytes) -> P1Telegram` 报告 `valid`、`invalid` 或 `unverifiable`；结构错误抛出带固定安全原因码（`malformed_frame`、`non_ascii`、`frame_too_large`）的 `P1ParseError`。
- `map_telegram(telegram, received_at) -> MappedReading | None` 执行 profile 接纳和指标映射。`None` 表示拒收，同时独立暴露安全拒收码（`invalid_crc`、`unverifiable_crc`、`unsupported_profile_version`、`no_electricity_metrics`）。`MappedReading` 使用标准库 dataclass；M1-04 增加共享类型转换，不让 mapper 导入 Pydantic。

可用源项目 parser／数据类作为起点，不照搬宽松流处理。分帧要求 `/` 起点、行首 `!` footer、四位十六进制 CRC 和 CRLF 终止。完整的异常 footer 可进入 parser 得到 `unverifiable` 诊断，但不能通过 profile。忽略前导噪声；半帧中出现新的行首 `/` 时替换旧半帧。超长时丢弃该帧并寻找下一起点。大 chunk 增量处理，保留缓冲始终不超过 16 KiB，含 header/footer。重连不得拼接旧半帧。

先校验 CRC，再解码或脱敏。拒绝非 ASCII 正文和异常 OBIS 行，不静默丢弃其含义。保留字段顺序和重复出现；数值错误属于映射问题，不产生带原始文本的异常。返回脱敏对象后不保留 frame bytes。CRC 诊断和错误不得含原始报文内容。

#### 可持久化字段

`SanitizedField` 包含 `code: str`、`raw_values: list[str]`、`value: Decimal | null`、`unit: str | null`、`redacted: bool`。Decimal 序列化为字符串。重复字段按列表保留，不转成以 code 为 key 的字典。

安全值清单封闭：版本、P1 时间、费率标识、C3 指标、标准断电计数／事件日志（`0-0:96.7.21`、`0-0:96.7.9`、`1-0:99.97.0`）、分相电压跌落／升高计数（`1-0:32/52/72.32.0` 和 `.36.0`），以及 M-Bus 类型／读数（`0-n:24.1.0`、`0-n:24.2.1`，n=1–4）。只保存满足 profile 所声明数值、时间、单位及事件日志语法的值。保留多组括号的结构，不把每个事件日志组解释成标量。

设备标识、消息文本 `0-0:96.13.0`、其他自由文本和未知 OBIS 值变为 `raw_values=["<redacted>"]`、`value=null`、`unit=null`、`redacted=true`。安全 code 内的不安全值也脱敏并记录字段问题。不得因值是数字或 code 含 `96` 就信任。未知 code 保留但不保留其值；不符合 OBIS 语法的未知行拒收。

Parser 可计算 SHA-256 设备指纹供本地比较，设置 `repr=False`；线上／持久化序列化均排除内部比较字段。仅本地 source 绑定可保存主电表指纹，M-Bus 指纹不保存。厂家 header 文本不保存或打印。Fixtures 使用合成 ID，最后对最终字节计算 CRC；不使用生产捕获、Token 或真实地址。

#### 接纳与质量

只接纳结构解析成功、CRC 有效、版本 50 且至少有一个有效常用电力指标的报文。无效／无法验证帧绝不接纳。常用指标缺失、格式错误、负值、单位错误或重复时，该指标为 `null`，不补零或任意选重复项。其他有效指标仍可用；不从其他相或 M-Bus 替代。

质量字段为 `integrity="valid"`、`frame_length: int`（1–16384）、`issues: list[FieldIssue]`。每个 issue 含 `code: str | null` 和原因之一：`missing`、`invalid_value`、`invalid_unit`、`duplicate`、`redacted`、`invalid_timestamp`、`clock_offset_mismatch`。相同 issue 去重，按 code 再 reason 排序。仅缺失 L2/L3 不算错误，可选诊断缺失也不算错误。Profile 必需版本不符则拒收；问题消息不回显接收值。输出映射读数前，将格式错误／负值／单位错误的安全字段归一为脱敏值，数值／单位为 null，将原安全原因码带入 quality。Missing issue 覆盖六个总量／累计指标及 L1 指标；L2/L3 缺失正常。排序时 null code 在非 null 之前。

### C3. 电力与时间映射

所有名称以客户并网点为视角：import 为向客户供电，export 为回送电网。指标均为非负 `Decimal | null`。标准中的零值是真实数据，不是缺测。

| 指标 | 线上 OBIS | 单位 |
| --- | --- | --- |
| `energy_import_t1_kwh` | `1-0:1.8.1` | kWh |
| `energy_import_t2_kwh` | `1-0:1.8.2` | kWh |
| `energy_export_t1_kwh` | `1-0:2.8.1` | kWh |
| `energy_export_t2_kwh` | `1-0:2.8.2` | kWh |
| `power_import_kw` | `1-0:1.7.0` | kW |
| `power_export_kw` | `1-0:2.7.0` | kW |
| `voltage_l1_v`、`voltage_l2_v`、`voltage_l3_v` | `1-0:32.7.0`、`1-0:52.7.0`、`1-0:72.7.0` | V |
| `current_l1_a`、`current_l2_a`、`current_l3_a` | `1-0:31.7.0`、`1-0:51.7.0`、`1-0:71.7.0` | A |
| `power_import_l1_kw`、`power_import_l2_kw`、`power_import_l3_kw` | `1-0:21.7.0`、`1-0:41.7.0`、`1-0:61.7.0` | kW |
| `power_export_l1_kw`、`power_export_l2_kw`、`power_export_l3_kw` | `1-0:22.7.0`、`1-0:42.7.0`、`1-0:62.7.0` | kW |

不使用旧契约含义误导的 `phase_power_current` 名称。不持久化派生累计总量；未来分析只在两档费率均存在时求和。保持输入精度，校验单位拼写，不使用二进制浮点运算。单相 fixture 缺 L2/L3，三相 fixture 各相取不同值以暴露串相错误。

`meter_time_raw` 为脱敏后的 `0-0:1.0.0` 字符串或 null。仅保留词法格式 `YYMMDDhhmmss[S|W]`；不安全字符串在 fields 内脱敏，此处变 null。年份映射为 2000–2099。`S` 为 UTC+02:00，`W` 为 UTC+01:00，转换为带时区 UTC 的 `meter_time_utc`。时间缺失产生 `missing` issue。不可能的日期得到 null UTC 和 `invalid_timestamp`。核对该时刻的 `Europe/Amsterdam` 与后缀是否一致；不一致时保留按后缀转换的 UTC，并标记 `clock_offset_mismatch`。不猜后缀，不用 Pi 本地时区解析，不以接收时间代替。

`received_at` 为完整帧提取时的 UTC 系统时间。一次 read 含多帧时，即使时间相同也保留处理顺序。`sampled_at` 是窗口最终确认时的 UTC，不是电表时间。后续查询默认使用 `received_at`，电表时间单独可查。电表时间和累计值不强制单调；下降保留供后续分析。

### C4. 领域与生命周期类型

#### 身份与不可变描述

| 身份 | 创建及生命周期 |
| --- | --- |
| `site_id` | 新现场离线生成 UUID v4；复用流程接受已有 v4，不按地址匹配 |
| `device_id` | 每次 Logger 安装生成一次 UUID v4，更换 Token 后仍保持 |
| `session_id` | 每次监测部署的 UUID v4；暂停和重启延续 |
| `measurement_point_id` | `uuid.uuid5(site_id, "grid-connection")`；MVP 每个现场一个并网点 |
| `source_id` | 每个会话的 UUID v4；kind 为 `p1_serial`，profile 为 `dsmr5_nl`，revision 为 1 |
| `record_id` | 创建快照时生成一次 UUID v4；重试不重新生成 |

`SessionDescriptor` 含 `session_id`、`device_id`、`site_id`、`started_at`（UTC）。`SourceDescriptor` 含 `source_id`、`session_id`、`measurement_point_id`、`kind`、`profile_id`、`profile_revision`。描述不可变，每次上传重带，不含地址、Token、串口路径或指纹。每条快照含六个 ID；验证器检查其与两种描述一致，测量点 ID 必须符合现场派生结果。

`Snapshot` 含六个 ID、`schema_version=1`、`meter_time_raw`、`meter_time_utc`、`received_at`、`sampled_at`、`metrics`（C3 全部 18 个 key，缺测显式 null）、`fields`（脱敏列表）和 `quality`。不含原始帧或凭据。18 个 key 包括四个累计、两个总功率和十二个分相指标。

Pydantic 共享模型拒绝未知 key 和无时区时间。接受时间偏移并归一为 UTC，序列化为带 `Z` 和六位小数秒的 ISO 8601。JSON 的指标／字段十进制只接受 null 或非负有限普通十进制字符串，不接受 JSON 数字、科学计数法、Infinity、NaN。内部构造接受 `Decimal`，不接受 float。序列化保留全部声明的 nullable key。除标量外还验证字段隐私、已知单位及大小，不只信任客户端的 `redacted` 标志；传输边界再次校验安全字段语法。

#### 本地配置、状态与展示

地址／简介及中心端 URL 放在本地配置，不进入快照。串口配置含路径和 profile 默认值；允许诊断时显式覆盖参数，不宣称支持其他 profile。修改串口路径／参数先暂停旧 owner 并清空 framer，不改历史归属。Profile/source 绑定不可变；将来更换 profile 需要新会话。

使用 `secrets.token_urlsafe(32)` 生成一次上传 Token，保存于 0700 数据目录内独立的 0600 文件，仅在已认证登记摘要操作中暴露。中心端登记保存 SHA-256 摘要，验证时不记录 Token，支持停用。这种哈希策略用于高熵设备 Token，不用于人类密码。本地及管理员鉴权实现属于 M2/M3。

持久化会话状态 `open|completed` 和操作意图 `collection_enabled: bool`。未结束的暂停会话可恢复，已结束会话不可恢复。明确结束或切换现场时，关闭旧会话并在采集前创建新会话／source。已有现场新会话保留测量点 ID。首次接纳的主电表指纹在本地绑定 source；后续不同指纹停止采集并报告 `meter_changed`，须明确确认后新建会话。缺少指纹时不建立或替换身份，记录标识缺失 issue，不虚构指纹。

运行采集状态为 `idle|previewing|starting|collecting|reconnecting|stopping|blocked`。稳定错误码含 `serial_busy`、`serial_unavailable`、`serial_permission`、`no_valid_reading`、`meter_changed`、`storage_full`。仅预览不创建快照或会话。重启只恢复 enabled 且未结束的会话；存储／换表阻断保留至解决。

`LoggerStatus` 含采集状态、当前 ID（可 null）、最新接纳 `received_at`、最近采集错误码、最近上传成功 UTC、最近上传错误码、pending 条数／字节数、最旧 pending 接收时间、可用字节数和存储阻断标志。`DeviceStatus` 区分 `last_authenticated_contact_at` 与 `latest_reading_received_at`；连接正常但补传旧数据不能标为正在产生新测量。

### C5. 存储与迁移契约

#### 关系结构

M1-05 在本文件内将下表展开为明确的逐列定义。这是设计产物，不是已运行迁移或仓储实现。

| 表 | SQLite Logger | PostgreSQL Server |
| --- | --- | --- |
| `devices` | `device_id`、`created_at` | 相同，加 nullable `last_authenticated_contact_at` |
| `sites` | `site_id`、`address`、`description` | 相同；信息由管理员维护 |
| `measurement_points` | `measurement_point_id`、`site_id`、`kind="grid-connection"` | 相同 |
| `sessions` | SessionDescriptor 列；`state`、nullable `ended_at`、`collection_enabled` | 仅不可变 SessionDescriptor；不从失联推断结束 |
| `sources` | SourceDescriptor 列、nullable 本地 `meter_fingerprint` | 仅 SourceDescriptor |
| `snapshots` | 快照 ID／时间／版本、平铺指标、脱敏 fields、quality；`payload_sha256`；`upload_state`、nullable `uploaded_at` | 相同快照与 hash，加 `ingested_at`；无上传状态 |
| `logger_config` | singleton key、nullable active session、中心 URL、串口路径／参数、持久化阻断码 | 不适用 |
| `device_credentials` | 不适用；Token 用受保护文件 | Credential UUID v4、设备 FK、唯一 64 字符 Token 摘要、创建时间、nullable 停用时间 |
| `device_site_authorizations` | 不适用 | 设备 FK、现场 FK；复合主键 |

UUID 在 SQLite 为小写规范文本，PostgreSQL 为原生 UUID；时间分别为 UTC ISO 文本、`TIMESTAMPTZ`。指标为 nullable 十进制 TEXT 和无固定精度限制的 `NUMERIC`，不使用 REAL／double precision。Fields、quality 分别为 JSON 文本、JSONB。版本／计数为整数，标志为布尔值（SQLite 0/1），摘要为 64 字符文本。地址／简介为文本，不是身份键。

主键为各命名 ID，快照主键为 `(device_id, record_id)`，配置为 singleton。测量点添加唯一 `(site_id, kind)`，MVP source 添加唯一 `session_id`。通过描述的复合候选键及相应 FK 强制快照归属：session `(session_id, device_id, site_id)`、source `(source_id, session_id, measurement_point_id)`、point `(measurement_point_id, site_id)`。停用凭据、修改配置、移除授权不得级联删除测量数据。

本地会话 state 限制为 `open|completed`，open 的 `ended_at` 为 null，completed 的 `ended_at` 非 null 且 collection disabled。上传 state 限制为 `pending|uploaded`，pending 的 `uploaded_at` 为 null，uploaded 非 null。除明确可空列外，其余列非 null；地址／简介可为空文本，指标／时间缺测遵循 C4。

索引：Logger `(upload_state, received_at, record_id)` 和 `(session_id, upload_state)`；Server `(site_id, received_at)`、`(site_id, session_id, received_at)`、`(device_id, received_at)`、`(source_id, received_at)`。查询使用 UTC 接收时间，起点包含、终点排除。最新读数取接纳记录的最大接收时间，不取最后处理的积压行时间。

#### 持久化、清理及首次迁移

M2 的 SQLite 启用 FK、WAL、`synchronous=FULL` 和 5 秒 busy timeout。一个事务创建快照、hash 及 `upload_state="pending"`；确认批次后，一个事务将对应行改为 `uploaded` 并设置 `uploaded_at`。确认不更新不可变快照内容。

保留未结束会话的所有记录；手动清理只删除已结束会话的 uploaded 行，保留 pending 行及其引用元数据。不自动过期或按存储压力删除。数据卷可用空间小于 64 MiB 或写入失败时，停止新增，展示 `storage_full`，释放串口。此前成功提交的状态保持不变。压力解决后显式恢复；容量实测属于 M4，不宣称该阈值在所有文件系统都能预留足够空间。

M2 建立自身 SQLite 迁移链，首版为 `logger_0001`；M3 建立独立 PostgreSQL 迁移链，首版为 `server_0001`；引入运行依赖后均使用 Alembic/SQLAlchemy。首次迁移实现本表清单及约束。空库初始化和从项目上一版本升级均使用临时数据库。不执行源项目迁移、不复制其数据库、不提供破坏性的生产降级命令。中心端至少保留一年，MVP 不自动清理中心历史。SQLite 备份使用一致的 SQLite backup 操作，不单独复制缺少 WAL 的运行中主文件。

### C6. 上传 schema、确认与重试

#### Endpoint 与 payload

`POST /api/v1/ingest/batches`，`Authorization: Bearer <token>`，`Content-Type: application/json`。配置中心端 origin（HTTP 或 HTTPS，不含凭据／query／fragment／path），追加该路径。HTTPS 校验证书，HTTP 用于所有者管理的隧道。不依赖 VPN provisioning。

`UploadBatch` 仅含 `schema_version=1`、`device_id`、`site_id`、`measurement_point_id`、`session: SessionDescriptor`、`source: SourceDescriptor`、`records: list[Snapshot]`。非空批次属于同一设备／现场／会话／source／测量点，请求内不能重复 record ID。授权接收可创建会话／source 描述；设备、现场及授权必须预先存在。已有描述必须一致；接收不覆盖现场信息或重绑描述。

限制：200 条、每个序列化 Snapshot 不超过 16,384 UTF-8 字节、请求 body 不超过 4,194,304 字节。快照使用 C6 规范序列化器计算大小。请求 body 在完整 JSON 解析前限制实际接收字节；MVP 拒绝压缩 body。超过条数或字节返回 `413`，schema 错误为 `422`。限制包含 fields、quality，不只统计常用指标。

传输数值采用 C4 十进制字符串规则。规范记录序列化为模型完整 JSON，时间归一 UTC、保留 decimal scale，key 排序、compact separators、不使用 ASCII 转义，以 UTF-8 编码；列表顺序及 null 保留。`payload_sha256` 对该字节序列计算。不同十进制 scale／字段列表顺序算不同的不可变内容；Logger 保存并重试同一表示，不从当前配置重建快照。

#### 事务与响应

认证 Token，检查批次设备身份、现场存在及授权。有效凭据的 `last_authenticated_contact_at` 独立于读数接纳更新，包括授权后校验失败；未获现场授权的已认证请求也可计为联系。未知／停用 Token 不计。入口大小拒收不必计联系。

校验全部记录和绑定后，在一个事务创建缺少的测量点／会话／source 元数据并插入读数。已有相同 `(device_id, record_id, payload_sha256)` 为重放；ID 相同但 hash 不同返回 `409`，回滚整批。并发依靠唯一约束并在事务内复核冲突；只做 `ON CONFLICT DO NOTHING` 不足。提交后返回 `200`：

```json
{"schema_version":1,"device_id":"00000000-0000-4000-8000-000000000002","confirmed_record_ids":["00000000-0000-4000-8000-000000000006"]}
```

确认 ID 与请求 ID 集合完全一致，并保持请求顺序，包含已存在的重放记录；不部分确认。Logger 校验版本、设备和完整且唯一的 ID 集合后才更新任何本地行。确认缺失、额外、格式错误或 ID 不符则整批 pending，报告 `invalid_confirmation`。不能只按 HTTP 2xx 确认。

错误格式为 `{"schema_version":1,"error":{"code":"site_not_authorized","message":"Device is not authorized for this site.","retryable":false}}`。消息使用固定安全文本，不含请求值、凭据或读数。反向代理／非 JSON 错误同样保留 pending。

| HTTP | 错误码 | Logger 行为 |
| --- | --- | --- |
| 401 | `invalid_credential` | 保留 pending；全局阻断上传，直至凭据／配置变化或手动重试 |
| 403 | `device_mismatch`、`site_not_authorized`（也用于未知现场） | 保留 pending；阻断此归属组，直至登记／配置变化或手动重试 |
| 409 | `record_conflict`、`descriptor_conflict` | 保留 pending；阻断组，展示需修正 |
| 413 | `batch_too_large` | 多记录批次缩小；单条超大则阻断其组 |
| 422 | `invalid_payload`、`unsupported_schema` | 保留 pending；阻断组，展示需修正 |
| 429／5xx | 应用生成时为 `temporarily_unavailable` | 保留 pending；退避重试 |

#### M4 上传器行为

最多一个请求在途，连接超时 5 秒，总 deadline 30 秒。正常每 10 秒尝试；积压批次成功后立即下一批。退避为 1/2/4/8/16/32/60 秒，确认成功后重置；有效 `Retry-After` 限制为 1–60 秒。超时、响应丢失、不确定失败均保留同一 ID 为 pending。

在一个归属组内选择最旧 pending，按条数和字节限制分批。旧组被阻断不能阻止新授权组上传；跳过不等于删除或确认。永久错误不紧密循环重试。上传不可用时，采集和本地提交独立继续。已结束本地会话状态及串口配置不上传；本版本中心端不宣称掌握会话结束状态。

### C7. 串口独占、采样与诊断 probe

`ReadOnlySerial` 只暴露 `read(size: int) -> bytes` 和 `close() -> None`；`SerialFactory` 接收不可变 `SerialConfig`（path、baudrate、bytesize、parity、stopbits、timeout=1）。`Clock` 暴露单调秒、当前 UTC、可中断等待。Linux 实际打开请求 pyserial exclusive，第二 owner 明确失败。应用内串行化 owner 切换；不宣称进程内锁能保护其他应用。

M2 在一个应用进程中拥有一个后台 reader 和一个采样协调器。`start_collection`、`stop_collection`、`complete_session`、`switch_site`、`preview`、`get_status` 是领域操作，不是已承诺的 HTTP 路径；HTTP／页面细节属于 M2 设计。M1 纯验证生命周期状态转换，线程和数据库接入属于 M2。

同一会话的 start／stop 幂等，冲突转换返回 busy。停止立即发取消信号并关闭串口；正常停止／切换会话／退出，在原计划边界最多等待 10 秒，将当前非空窗口最终保存一次后完成，不提前额外生成快照。Start 不得越过 `stopping`。空窗口不产生行。进程异常死亡可能丢失未提交候选，但不能删除已提交快照。

窗口使用单调耗时，包含该进程半开 10 秒区间内接收的帧；按接收顺序取最后接纳的新帧，不复用前窗口候选。接收／最终确认 UTC 来自系统时间，系统时间跳变不缩短或延长窗口。正常退出释放串口并处理最后窗口；断连清空 framer，按可中断退避重连；旧 reader 退出后才能交接。

采集时，预览最多订阅当前 owner 15 秒，不重开、不重置、不打断。Idle 时临时预览最多独占 15 秒，在所有结果下关闭，不创建会话或快照。转换期间预览返回 busy。错误及没有有效帧的超时可见，不能伪造成功。

M1 提供 `python -m watt_profile.cli.p1_probe --device <path> --duration 15`，使用 profile／默认串口参数，可显式覆盖。输出脱敏映射值、总帧／接纳／拒收计数及固定错误码；不输出原始字节、header、指纹或标识。不提供 `--raw-output`。Duration 必须有限且为正。只有至少一个接纳读数且无串口错误才 exit 0；串口失败、超时／无接纳读数或中断为 1；argparse 用法错误为 2。所有退出关闭串口。CLI 是独立诊断工具，不应与 Logger 并行读取同一串口。

M1 测试只使用 fake serial 和时钟。目标电表／线材试验和两款 Pi 持续采集属于后续隔离硬件验证。

## 3. 原子实现任务

所有任务均**待实施**。每个任务同时交付代码与对应的有效检查。阅读本文件指定的 C 节，不让后续任务重新猜测设计。改动遵守任务边界；契约变化时同步更新本文件英中两版。

### M1-01 — 独立包

依赖：无。契约：C1。

- 交付 setuptools 包结构、包标记、锁定依赖和 Ruff／pytest 入口，添加 `tests/test_package.py`；在本文件第 4 节记录临时 venv、锁文件重新生成及清理步骤。忽略本地环境、缓存、运行秘密和 `development-notes/`。
- 验收：锁定 editable install 可在两仓库之外、清除继承 `PYTHONPATH` 后导入 `watt_profile`，包／Ruff 检查通过。不依赖源项目运行层，不含本机秘密。
- 边界：仅包和检查，不实现 parser、服务、数据库或容器。检查：`python -m pytest tests/test_package.py -q`，加 C1 Ruff 检查。

### M1-02 — 有上限 Parser 与脱敏

依赖：M1-01。契约：C2。

- 交付 `p1/parser.py`、合成分帧／隐私 fixtures 和 `tests/test_p1_parser.py`；迁入适用源 parser／数据类并保留相应声明，实现有界分帧、reset、CRC 状态、安全异常、括号组／重复字段及隐私规则。
- 验收：逐字节／所有单分割点、粘包、噪声、半帧、新 header 重同步、坏 CRC／footer、非 ASCII、超长和大 chunk 恢复。缓冲有界；合成身份／文本及未知数字 sentinel 不进入安全输出或异常。
- 边界：仅纯 parser，不做映射或串口 I/O。检查：`python -m pytest tests/test_p1_parser.py -q`，加 C1 Ruff 检查。

### M1-03 — DSMR 5 映射与 Fixtures

依赖：M1-02。契约：C2–C3。

- 交付 `p1/profile.py`、标准库 `MappedReading`、单相／三相合成字节 fixtures、期望值、测试 CRC builder 和 `tests/test_p1_profile.py`。保留行尾，在最终内容修改后计算 CRC。
- 验收：18 指标的方向／相／单位、真实零、缺失／负值／格式错／单位错／重复、安全附属数据及无可用电力值。覆盖大 Decimal、夏冬／DST 切换、秋季重复小时、缺失／异常时间和后缀不符。坏 CRC／不可验证／非 50 拒收。
- 边界：纯 profile／映射，不导入 Pydantic，不做采样或持久化。检查：`python -m pytest tests/test_p1_parser.py tests/test_p1_profile.py -q`，加 C1 Ruff 检查。

### M1-04 — 可执行领域与生命周期契约

依赖：M1-03。契约：C3–C4、C7。

- 交付领域／生命周期类型、mapper 适配、UUID／测量点 helper 和唯一规范快照 codec；添加 `tests/test_domain.py`、`tests/test_lifecycle.py`。导入时不创建运行凭据或状态。
- 验收：身份一致、绑定不可变、安全字段复核及大小限制。拒绝未知 key、无时区时间、float、科学计数／非有限 decimal 和不安全字段。往返保持 Decimal scale／null／时间／括号组，对象 key 顺序不改规范字节。纯转换覆盖暂停恢复、结束／换现场／换表、预览、busy／幂等、重启和持久化阻断。
- 边界：仅类型／helper，不做数据库、HTTP 鉴权、Token 文件、线程或 UI。检查：`python -m pytest tests/test_domain.py tests/test_lifecycle.py -q`，加 C1 Ruff 检查。

### M1-05 — 存储与迁移规范

依赖：M1-04。契约：C4–C6。

- 在本文件 C5 补齐逐列 SQLite／PostgreSQL 类型、nullable、默认值、PK／FK／unique／check 和索引，与已验收模型一致；在此加入合成换现场和 pending／uploaded 混合清理例子，不另建 schema 文档。
- 验收：每个 Snapshot 字段／18 个指标都有去向，复合归属约束、设备／记录去重、秘密分离和清理保留 pending 及引用元数据明确。规定独立首次迁移、事务／备份／存储压力检查及中心保留独立性。
- 边界：仅文档规范，不引入数据库依赖或仓储代码，不执行 SQL／迁移，不访问生产。对照已验收领域模型及双语镜像检查；仅在必要代码变动时重跑领域测试。

### M1-06 — 可执行上传契约

依赖：M1-04、M1-05。契约：C4–C6。

- 交付 `contracts/upload.py`、纯 body／确认验证器、`tests/test_upload_contract.py` 和 `examples/ingest/` 合成请求／重放／确认／拒收 JSON。样例用法记录在本文件，复用领域 codec。
- 验收：同组批次和恰好上限；拒绝空／混组、描述不符、重复请求 ID、未知 schema、不安全字段、201 条和超大记录／body。确认要求版本／设备正确和完整唯一 ID 集合，允许 ID 调序。重放 hash 一致、内容改变则不同；错误样例使用固定安全消息，不含可用凭据。
- 边界：仅传输类型／样例，不实现 endpoint、网络、凭据查询、事务或重试 worker。检查：`python -m pytest tests/test_domain.py tests/test_upload_contract.py -q`，加 C1 Ruff 检查。真实冲突／原子性／并发留给 M3。

### M1-07 — 串口接口与诊断 Probe

依赖：M1-03、M1-04。契约：C2–C4、C7。

- 交付串口／时钟协议、`cli/p1_probe.py`、`tests/test_p1_probe.py`，注入 factory／clock／output；复用源 probe 机制，修正 WarmteLink 默认和零读数也成功的行为。用法写在此文件。
- Fake serial 验证 115200 8N1、timeout 1、exclusive 及覆盖参数，无串口数据写入。成功、无／仅拒收帧、busy／权限／断连／read 错误、时长结束和中断均关闭并符合退出码；拒绝零／负／非有限 duration；安全 stdout／stderr 排除身份／header／指纹 sentinel。
- 边界：仅 CLI 和假设备检查，不访问真实串口，不做常驻 worker、数据库或 UI。检查：`python -m pytest tests/test_p1_probe.py tests/test_lifecycle.py -q`，加 C1 Ruff 检查。真实共享 owner／停止／重连／预览集成属于 M2。

### M1-08 — 离线验收与交接

依赖：M1-01–M1-07。契约：C1–C7。

- 交付 `tests/test_m1_pipeline.py`：合成字节 → 分帧／parser → 映射 → Snapshot → 规范批次 → 完整确认；全程保持全部归属 ID、Decimal／null 和安全字段。
- 执行全套测试、Ruff 和隔离包导入，在第 4 节记录实际命令／结果、限制、临时环境清理；验收后才更新状态，不另建 milestone 验收／交接文件。
- 边界：仅离线集成和准确交接，不实现运行接收／部署，不宣称硬件通过。检查：`python -m pytest -q`，加 C1 Ruff 检查；前序任务标准也必须通过。

## 4. 检查与完成记录

在实现存在前，C1 及任务命令均为计划检查。M1-01 在仓库外使用唯一的隔离 Python 3.12 虚拟环境：

```bash
M1_VENV="$(mktemp -d /tmp/watt-profile-m1-venv.XXXXXX)"
python3.12 -m venv "$M1_VENV"
"$M1_VENV/bin/python" -m pip install -r requirements-dev.lock
"$M1_VENV/bin/python" -m pip install --no-build-isolation --no-deps -e .
"$M1_VENV/bin/python" -m pytest tests/test_package.py -q
"$M1_VENV/bin/python" -m ruff check .
"$M1_VENV/bin/python" -m ruff format --check .
```

包测试使用 pytest 的 `tmp_path` 创建仓库外临时工作目录，并从子进程环境中移除继承的 `PYTHONPATH`。重新生成锁文件时，先运行 `M1_PIP_CACHE="$(mktemp -d /tmp/watt-profile-pip-tools-cache.XXXXXX)"`，再运行 `PIP_TOOLS_CACHE_DIR="$M1_PIP_CACHE" "$M1_VENV/bin/python" -m piptools compile --extra dev --allow-unsafe --resolver=backtracking --output-file requirements-dev.lock pyproject.toml`。保留环境供 M1-08 使用；之后只用 `rm -rf -- "$M1_VENV" "$M1_PIP_CACHE"` 删除本 shell 精确创建的目录。本次运行的实际临时路径记录在 `development-notes/m1-20261010/M1-01-resources.txt`。使用合成／脱敏数据及隔离临时资源，不连接生产服务，不与已有串口 reader 争用。

八项任务验收全部通过且英中一致后，M1 才完成。分别记录命令、结果、未执行项及重要限制；类型／测试不代表真实数据库原子性或回传通过。

实现结果：**未运行；实现待执行**。后续检查仍包括：M2 持久采集、清理及串口集成；M3 真实授权／事务／并发去重／查询／CSV；M4 响应丢失／断网／重启恢复、实物电表／线材兼容、两款 Pi 的内存／CPU／磁盘写入、持续采集和四周离线容量。
