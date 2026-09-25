# Home Automation 能源采集复用交接文档

编写日期：2026-09-24。用途：供同一台开发机上的另一个项目复用 Modbus 电表采集与 P1 串口读取能力。

## 1. 源项目位置与本次交接范围

- 源仓库：[home-automation](/home/tianyu/workspace/home-automation)。绝对路径为 `/home/tianyu/workspace/home-automation`。
- 本文核对的 Git HEAD：`3cf3843af313f469fcec59cdba0d3b2495632c8c`，提交主题 `M8-R21: patch frontend warnings and dependencies`，提交日期 2026-08-28。
- 核对时 `docker-compose.yml` 和 `docker-compose.dev.yml` 存在用户本地修改；没有改动这些文件。部署配置请以之后接手时的实际文件为准。
- 本文引用的本地文件均使用绝对路径，在另一个项目中打开本文即可定位原实现。仓库位置变化后，需要相应替换路径前缀。
- 本次仅编写交接文档、检查源码并运行离线测试。没有连接电表、打开真实串口、操作运行中的服务或修改项目代码；首页地图问题继续暂缓。

优先复用两条链路：

```text
Modbus 电表 → RS485/RTU → Modbus TCP 网关 → 块读 → profile 解码 → 新项目

电表 P1 → USB/P1 线 → serial bytes → 分帧 → CRC/OBIS 解析 → 电力字段映射 → 新项目
```

第一条已经有完整设备管理、后台轮询与存储实现。第二条已有直接串口读取、通用解析器以及电表读通的历史记录；但当前正式电力入库走的是 `DSMR Reader → MQTT JSON`。直接串口常驻服务目前专用于 WarmteLink，不能把它当成已经完成的通用电表 serial source。

## 2. 接手时先读哪些文件

| 目的 | 入口 |
| --- | --- |
| 仓库工作约定；若要修改源项目先读 | [AGENTS.md](/home/tianyu/workspace/home-automation/AGENTS.md)、[设计协作契约](/home/tianyu/workspace/home-automation/docs/design/README.md) |
| Modbus 设计与文件依赖 | [M5 IoT / Energy](/home/tianyu/workspace/home-automation/docs/design/m5-iot-energy.md)，重点 §3.0、§3.1、M5-T03～T07 |
| DDSU666 协议与真机实测 | [DDSU666-Modbus-Protocol.md](/home/tianyu/workspace/home-automation/docs/references/DDSU666-Modbus-Protocol.md)，重点 §0、§4、§6、§8 |
| DDSU666 原始手册 | [DDSU666 Single phase Smart Meter.pdf](</home/tianyu/workspace/home-automation/docs/references/DDSU666 Single phase Smart Meter.pdf>) |
| 电力 JSON 字段与单位 | [Tibber-and-NL-Energy-Pricing.md](/home/tianyu/workspace/home-automation/docs/references/Tibber-and-NL-Energy-Pricing.md)，重点 §6 |
| DSMR MQTT 原始设计 | [M6 动态电价与 DSMR](/home/tianyu/workspace/home-automation/docs/design/m6-tibber-dynamic-energy.md)，重点 §3.2、M6-T06 |
| P1 直接读取、串口参数、真机证据 | [Pre-M8 WarmteLink P1 PoC](/home/tianyu/workspace/home-automation/docs/design/pre-m8-warmtelink-p1-poc.md)，重点 §3、§4 |
| 当前多数据源与串口服务架构 | [M8 WarmteLink / MeterSource](/home/tianyu/workspace/home-automation/docs/design/m8-warmtelink-energy.md)，重点 §3 |
| USB、Docker 权限与运维 | [WarmteLink 运维手册](/home/tianyu/workspace/home-automation/docs/warmtelink-energy.md) |
| 如果还要复用换表与累计值边界 | [meter-epochs.md](/home/tianyu/workspace/home-automation/docs/meter-epochs.md) |

阅读顺序建议为“协议资料 → 当前源码 → 测试 → 历史设计”。里程碑文档保留了实施前的描述，部分注释也未随后续功能更新。例如早期 DSMR 文档写以 telegram `id` 去重，当前已经改为按数据源和时间去重；DDSU666 YAML 仍留着字序待确认的注释，而协议文档 §8 已记录实际设备大端解码通过。

## 3. Modbus：最小可复用单元

### 3.1 文件清单

| 文件 | 作用 | 复用建议 |
| --- | --- | --- |
| [driver.py](/home/tianyu/workspace/home-automation/app/integrations/modbus/driver.py) | TCP 连接、FC03/FC04 块读、异常、两个寄存器转 float32 | 必选；基本独立 |
| [profiles.py](/home/tianyu/workspace/home-automation/app/integrations/modbus/profiles.py) | YAML 加载、Pydantic 模型、指标解码、profile 枚举 | 必选；修改导入路径后复用 |
| [ddsu666.yaml](/home/tianyu/workspace/home-automation/app/integrations/modbus/profiles/ddsu666.yaml) | 当前正泰单相表的寄存器、单位、指标元数据 | 必选；作为新增型号的参考 |
| [sdm120.yaml](/home/tianyu/workspace/home-automation/app/integrations/modbus/profiles/sdm120.yaml) | 另一种单相表、FC04、不同单位的示例 | 可选；有助于理解 profile 差异 |
| [modbus_cli.py](/home/tianyu/workspace/home-automation/scripts/modbus_cli.py) | 无数据库的 `probe` / `read` 试读工具 | 建议一起带走 |
| [test_modbus_driver.py](/home/tianyu/workspace/home-automation/tests/test_modbus_driver.py) | 驱动成功、失败与解码回归 | 建议一起带走 |
| [test_modbus_profiles.py](/home/tianyu/workspace/home-automation/tests/test_modbus_profiles.py) | profile 加载及指标解码回归 | 建议一起带走 |
| [test_modbus_cli.py](/home/tianyu/workspace/home-automation/tests/test_modbus_cli.py) | 命令行行为与只读请求测试 | 保留 CLI 时一起带走 |

只要目标是“拿到读数”，不需要连带搬走 FastAPI、数据库、MQTT、前端或电费计算。搬迁时注意：

1. 更新 `app.integrations.modbus...` 导入路径；CLI 也引用这些路径。
2. 保留 `profiles.py` 旁边的 `profiles/` 数据目录。它按 `__file__` 定位 YAML，不按启动目录查找。
3. 若打包成 wheel 或镜像，明确把 YAML 作为 package data / 构建输入带上；不能只复制 Python 文件。
4. profile 名应来自可信的已安装清单。当前 `load_profile(name)` 直接拼接路径，不应把未经限制的外部字符串作为 profile 名传入。

### 3.2 当前传输和解码契约

应用连接的是提供标准 Modbus TCP 的网关，电表在网关后方以 RS485/RTU 通信。网关的 host/port 与电表的 unit ID 是两层地址。

- `read_blocks(host, port, unit_id, blocks, function_code=..., timeout=...)` 返回 `{寄存器地址: 16 位整数}`。
- 支持读取功能码 FC03 与 FC04；没有写寄存器路径。
- 每轮新建一个同步 TCP client，在同一次连接中顺序读取所有 block，最后关闭连接。
- 检查异常响应和返回寄存器数量；失败抛出 `ModbusDriverError` 的子类。
- 当前驱动使用标准 TCP client 默认 framing。没有实现直连 USB-RS485 的 RTU transport，也没有暴露 RTU-over-TCP 透传模式配置。新项目如果换传输方式，需要扩展驱动。
- `decode(profile, registers)` 返回 `{指标 key: float}`。单位在 profile 内，返回值本身不携带单位。
- 同步读取应放到采集线程或同步 job 中；接入异步 Web 服务时不要让它阻塞事件循环。

当前 profile 中虽然有 `word_order` 和 `byte_order` 字段，**解码函数实际没有根据这两个字段分支**：始终按大端字节序、高 16 位寄存器在前的 IEEE-754 float32 解释。只修改 YAML 为 `little` 不会改变结果。

同样，当前没有整数、缩放系数、偏移量、CT/PT 变比或派生公式的通用解码实现。未知类型和缺寄存器的指标会被跳过，不能把“没有抛异常”当成“所有指标都采全了”。首次集成应检查预期指标集合、数值是否有限、单位及合理范围。

### 3.3 DDSU666 已有配置和验证

当前 `ddsu666` profile 使用 FC03，指标如下：

| key | 起始地址 | 单位 |
| --- | --- | --- |
| `voltage` | `0x2000` | V |
| `current` | `0x2002` | A |
| `active_power` | `0x2004` | kW |
| `reactive_power` | `0x2006` | kvar |
| `power_factor` | `0x200A` | 无量纲 |
| `frequency` | `0x200E` | Hz |
| `import_energy` | `0x4000` | kWh |
| `export_energy` | `0x400A` | kWh |

每项占两个 16 位寄存器。瞬时量读取 `0x2000` 起的 16 个寄存器；累计电量分开读两个小块，避免跨过未说明的地址间隙。上述地址属于 DDSU666，不能直接作为 DTSU666 的地址表。

协议文档 §8 记录：2026-06-30，DDSU666 直接接入版 5(80)A 已实际读通，大端解码得到合理电压、电流、频率及累计电量；CLI、设备 test 和后台轮询链路均有工作记录。这是历史验证，不代表本文再次连接了实物。

该记录还提到极轻负载时瞬时功率/PF 寄存器为零，但累计电量仍增长。新项目遇到这一现象时，应对照原始寄存器和物理表读数，避免未经检查就修改单位或解码规则。

### 3.4 无数据库试读命令

先在现有仓库执行，命令中的 `REPLACE_...` 与 unit ID 须换成现场配置。不要因复制示例而自动启动第二套后台轮询。

```bash
cd /home/tianyu/workspace/home-automation
source .venv/bin/activate

# 最小试读：DDSU666 电压，FC03、2 个寄存器。
python -m scripts.modbus_cli probe \
  --host REPLACE_GATEWAY_HOST --port 502 --unit 1 \
  --fc 3 --address 0x2000 --count 2 --decode float32

# 按完整 DDSU666 profile 读取。
python -m scripts.modbus_cli read \
  --host REPLACE_GATEWAY_HOST --port 502 --unit 1 \
  --profile ddsu666
```

直接调用的最小形式如下（导入路径按新项目调整）：

```python
from app.integrations.modbus.driver import read_blocks
from app.integrations.modbus.profiles import decode, load_profile

profile = load_profile("ddsu666")
registers = read_blocks(
    host="REPLACE_GATEWAY_HOST",
    port=502,
    unit_id=1,
    blocks=[{"start": block.start, "count": block.count} for block in profile.blocks],
    function_code=profile.function_code,
)
values = decode(profile, registers)
units = {metric.key: metric.unit for metric in profile.metrics}
```

## 4. 从 DDSU666 扩展到三相 DTSU666

源仓库目前没有 DTSU666 profile，也没有对应 DTSU666 手册。建议在新项目新增独立的 `dtsu666.yaml` 和对应协议摘录，并保存具体型号、后缀、手册版本及来源。

**如果目标 DTSU666 的测量值使用现有驱动支持的 FC03/FC04、大端 float32，并且不需要额外换算，那么主要工作确实是补寄存器配置和测试；相数本身不会限制 JSON 指标结构。** 若编码或换算规则不同，还需补齐解码器。

接入步骤：

1. 确认具体型号及版本，包括是否带后缀、是否经互感器接入。使用这一型号的厂商手册，不从 DDSU666 按地址规律外推。
2. 核对功能码、协议原始地址、每个量的寄存器数、数据类型、字节序/字序、单位、变比和正反向语义。区分原始地址与手册中的 3xxxx/4xxxx 编号。
3. 按手册设置合法连续 block；避免为省请求而跨越不能读取的保留区。
4. 给三相量明确命名，例如 `voltage_l1/l2/l3`、`current_l1/l2/l3`、`active_power_l1/l2/l3`，并区分总有功功率与分相功率。这些名称是建议，不是已有 DTSU666 契约。
5. 在目标系统明确统一单位还是保留设备原始单位。当前 DDSU666 的功率为 kW，SDM120 为 W；现有 decoder 不自动统一。
6. 若需要字序切换、整数、倍率或 CT/PT 换算，先在 decoder 中实现并使用已知原始寄存器测试，不能只添加无实现支撑的 YAML 字段。
7. 先用单个已知量只读试测，再扩大到完整 profile；对照物理表核对各相、总量和正反向累计值。
8. 保存脱敏的原始寄存器样本和期望工程值作为测试；检查三相缺失字段不会被错误填零。

源项目的 profile 枚举、通用 readings JSON 与 metrics API 有助于自动呈现新指标，但新项目若有固定字段的卡片、聚合或 HA 发布代码，仍需检查它们是否支持分相指标。不要覆盖旧 DDSU666 profile 的单位或 key 来适配 DTSU666，以免改变已有历史数据的解释。

## 5. 如果连 Modbus 设备管理和历史记录一起复用

可按需继续取以下层：

| 层 | 文件与要点 |
| --- | --- |
| 模型 | [models/modbus.py](/home/tianyu/workspace/home-automation/app/models/modbus.py)：`ModbusDevice` 保存部署配置；`ModbusReading` 保存 `device_id / recorded_at / payload` |
| 后台轮询 | [services/modbus_poll.py](/home/tianyu/workspace/home-automation/app/services/modbus_poll.py)：`poll_device`、`poll_all_enabled_devices` |
| 调度生命周期 | [main.py](/home/tianyu/workspace/home-automation/app/main.py)：`_run_scheduled_modbus_poll`、`modbus-poll` job |
| HTTP API | [routes/api/modbus.py](/home/tianyu/workspace/home-automation/app/api/routes/api/modbus.py)、[schemas/modbus.py](/home/tianyu/workspace/home-automation/app/schemas/modbus.py) |
| 设备配置前端 | [DeviceForm.tsx](/home/tianyu/workspace/home-automation/frontend/src/energy/DeviceForm.tsx) |
| 图表前端 | [EnergyCharts.tsx](/home/tianyu/workspace/home-automation/frontend/src/energy/EnergyCharts.tsx)、[hooks.ts](/home/tianyu/workspace/home-automation/frontend/src/energy/hooks.ts) |
| 可选 HA/MQTT | [expose.py](/home/tianyu/workspace/home-automation/app/integrations/expose.py)、[ha_discovery.py](/home/tianyu/workspace/home-automation/app/services/ha_discovery.py)、[mqtt.py](/home/tianyu/workspace/home-automation/app/integrations/mqtt.py) |

现有轮询以 5 秒基础 tick 检查各设备的 `poll_interval_s`，调度设置 `max_instances=1, coalesce=True`；间隔是“本次是否到期”的判断，不能据此承诺 1 秒精确采样。单个设备失败会记录状态并继续处理其他设备。

`poll_device` 已包含数据库 commit/rollback 和成功后的 HA 发布调用，不是纯采集函数。若新项目不需要 HA，建议将存储和发布改成目标项目自己的适配层，而不是为复用它引入完整 HA 依赖。SQLAlchemy 模型引用源项目的 `app.db.Base`，API 引用源项目鉴权与 session 依赖，这些都要重接。

源项目 migration 位于 [alembic_app/versions](/home/tianyu/workspace/home-automation/alembic_app/versions)。它们属于已有的整条升级链；新项目按目标 schema 创建自己的迁移，不直接执行源项目的历史迁移链，更不要复制生产 `app.db` 作为测试数据库。

## 6. P1：直接从电表串口读取的复用入口

### 6.1 三条相关链路的当前状态

| 链路 | 当前实现与证据 |
| --- | --- |
| 电表 P1 → USB → 直接读取/解析 | 通用 parser 与 probe 已实现；Pre-M8 §3.1 记录曾用已知正常 DSMR 线在电表上读到连续、CRC 正确的 telegram |
| 电表 P1 → DSMR Reader → MQTT JSON → 本应用 | 已有正式电力摄取、存储及下游消费逻辑 |
| WarmteLink P1 → USB → 本应用 worker | 已有正式串口 worker、热量/热水解析接纳和存储；其参数与业务规则专用于 WarmteLink |

本次应优先提取第一条的 parser/probe，参考第三条的常驻服务结构，再按需要利用第二条的电力字段契约。仓库的 source 注册表目前只支持 `dsmr_mqtt` 和 `warmtelink_serial`，没有 `dsmr_serial` 类型。

### 6.2 文件清单

| 文件 | 用途 |
| --- | --- |
| [integrations/p1.py](/home/tianyu/workspace/home-automation/app/integrations/p1.py) | 纯 Python 分帧、CRC16、OBIS 解析、字段/telegram 数据类 |
| [scripts/p1_probe.py](/home/tianyu/workspace/home-automation/scripts/p1_probe.py) | pyserial 只读 CLI，可覆盖串口参数；输出完整性、字段、变化与帧间隔 |
| [test_p1_parser.py](/home/tianyu/workspace/home-automation/tests/test_p1_parser.py) | 分帧、解析、身份脱敏与错误边界 |
| [test_p1_probe.py](/home/tianyu/workspace/home-automation/tests/test_p1_probe.py) | fake serial 驱动的 CLI、参数、关闭与 CRC 测试 |
| [dsmr_p1_valid.txt](/home/tianyu/workspace/home-automation/tests/fixtures/dsmr_p1_valid.txt) | 人工构造、CRC 正确的标准 framing fixture |
| [warmtelink_p1_7n1.txt](/home/tianyu/workspace/home-automation/tests/fixtures/warmtelink_p1_7n1.txt) | 脱敏 WarmteLink 异常 framing fixture |
| [warmtelink_worker.py](/home/tianyu/workspace/home-automation/app/services/warmtelink_worker.py) | 可参考的线程、退避重连、单 owner、停止与配置协调机制 |
| [warmtelink_ingest.py](/home/tianyu/workspace/home-automation/app/services/warmtelink_ingest.py) | 质量接纳与持久化示例；电力项目需要另写领域映射 |

`integrations/p1.py` 仅依赖 Python 标准库；加串口读取才需要 `pyserial`。parser 无数据库、Web 或 MQTT 依赖，适合最先抽出。

### 6.3 解析器的数据契约

- `TelegramFramer.feed(chunk: bytes) -> list[bytes]`：接受任意分块，累积到 `!` 后的换行完成一帧；可处理分包、粘包和变长报文。
- `parse_telegram(frame: bytes) -> P1Telegram`：包含 `integrity`、原因、帧长、设备时间字符串、OBIS fields 和 M-Bus channels。
- `ObisField` 提供 `code`、括号内的 `raw_values`、可解析的 `Decimal value` 和 `unit`。
- 对多组括号的字段，数值解析使用最后一组；前面的测量时间仍在 `raw_values` 中。不要把所有字段都当成一个普通浮点数。
- `fields` 是电力 OBIS 字段的读取入口。`channels` 当前针对 `0-n:24.../96...` 的 M-Bus 附属计量通道，不表示电表 L1/L2/L3。
- parser 对标准帧的 CRC 从 `/` 到 `!`（含两端）校验。必须保留原始 bytes 与换行，不能先用文本模式重写行尾再验 CRC。
- 已知 equipment ID 字段会脱敏，并形成 SHA-256 fingerprint；未知字段保留。面向新设备扩展时应检查额外身份字段，不能假设任何厂商私有 OBIS 都已自动脱敏。

完整性状态明确区分：`valid` 为 CRC 通过，`invalid` 为可校验但不匹配，`unverifiable` 为当前格式不足以校验。parser 会返回带质量状态的结果，不负责决定是否入库；消费端必须检查质量。

现有 framer 没有对“持续输入却始终没有完整 footer”的缓冲区设置最大长度。抽成长期运行组件时，建议增加缓冲上限与重新同步策略。当前 parser 也不是覆盖全部 DSMR 版本和厂商扩展的完整协议库，目标设备需补 fixture 验证。

### 6.4 电表串口试读

先确认当前项目、DSMR Reader 或其他程序没有占用同一串口。保持每个物理串口一个读取进程；新旧项目在同一台机器开发尤其要协调。

```bash
cd /home/tianyu/workspace/home-automation
source .venv/bin/activate
ls -l /dev/serial/by-id/

python -m scripts.p1_probe \
  --device /dev/serial/by-id/REPLACE_ELECTRICITY_P1_DEVICE \
  --baudrate 115200 \
  --bytesize 8 \
  --parity N \
  --stopbits 1 \
  --duration 60 \
  --show-changes
```

此例的 `115200 8N1` 来自源项目 Pre-M8 文档引用的 DSMR 5.0.2 标准参数；使用前应核对目标电表的实际 DSMR 版本和线材要求，不能视为所有 P1 电表通用设置。

**一定显式指定电表的串口参数。** 当前 CLI 默认是本机 WarmteLink 实测的 `115200 7N1`。正式 `open_warmtelink_serial()` 更是直接固定使用 7N1，不能靠修改它的配置字典将其变成电表 8N1 reader。

`--raw-output` 是可选的原始字节捕获，会包含身份及用量信息，并按 `wb` 打开目标文件。需要诊断时选择新的受保护文件名，避免覆盖已有捕获；提交测试前先脱敏并重新计算标准 fixture 的 CRC。CLI 退出码 0 只表示读取流程正常结束或人工中断，不保证收到有效帧；要核对帧数和 `valid` 状态。

### 6.5 从通用 OBIS 到电力字段还要补什么

源项目 parser 可以枚举电力字段，但还没有独立的“直接串口电力 OBIS → 现有电力 JSON”的映射函数。新项目应从实际电表报文和对应手册建立映射，并优先复用已有字段语义：

| 现有电力 JSON key | 语义/单位 |
| --- | --- |
| `electricity_delivered_1/2` | 各费率档进口累计电量，kWh |
| `electricity_returned_1/2` | 各费率档回送累计电量，kWh |
| `electricity_currently_delivered/returned` | 总进口/回送瞬时功率，kW |
| `phase_voltage_l1/l2/l3` | 各相电压，V |
| `phase_power_current_l1/l2/l3` | 各相电流，A；虽然名称带 power，原契约表示电流 |
| `phase_currently_delivered_l1/l2/l3` | 各相进口功率，kW |
| `phase_currently_returned_l1/l2/l3` | 各相回送功率，kW |

这些名称来自仓库的 DSMR Reader JSON mapping 文档，不代表所有电表或 DSMR Reader 配置天然都使用相同名字。

建议新适配层完成以下工作：

1. 从 `telegram.fields` 按 OBIS code 查值，校验单位、缺失、重复字段与有效数值；电表主字段不要从 M-Bus `channels` 取。
2. 对目标电表支持的标准 CRC 严格检查，拒收损坏报文。旧版本或特殊设备若确实无法校验，需要单独明确策略，不能自动套用 WarmteLink 容错规则。
3. 区分报文设备时间、接收时间和附属表测量时间，规范化为带时区的 UTC。parser 的 `timestamp` 只是原始字符串，没有替调用方完成转换。
4. 累计电量计算保持 `Decimal` 精度。进口总量为两个进口费率寄存器之和，回送同理；不要将进口与回送混成一个无方向累计量。
5. 单相表缺失 L2/L3 时保留缺测，不能填 0。增加三相字段通常不会改变整表进口/回送累计量的语义。
6. 为新 serial source 增加自己的配置、生命周期和存储适配；不将 kWh 数据伪装为 `warmtelink_serial` 的 GJ/m³ channel。

`tests/fixtures/dsmr_p1_valid.txt` 虽然名字含 DSMR，正文主要是人工构造的热水/热量通道，**它证明标准 framing/CRC 路径可用，不是完整单相或三相电表字段覆盖测试**。新项目需增加脱敏电力 fixture 与映射测试，不能把现有测试通过当作 DTSU666 或三相 P1 已验收。

## 7. 可参考的 P1 常驻服务与电力 MQTT 摄取

### 7.1 WarmteLink worker 可以借鉴的机制

`WarmteLinkWorker` / `WarmteLinkWorkerManager` 已提供：只读 serial 接口、每个 source 的独立线程、可中断停止、1/2/4/8/16/32/60 秒退避重连、断线后重新创建 framer、短生命周期数据库 session，以及在现有 owner 上进行 discovery 的机制。对应测试为 [test_warmtelink_worker.py](/home/tianyu/workspace/home-automation/tests/test_warmtelink_worker.py)。

抽取时将 serial 参数和消费回调改成电表所需接口，重新接目标项目存储与日志。保留“重连后丢弃旧半帧”“停止后释放串口”“旧线程退出后再交接 owner”的行为。manager 属于进程内协调，不能替代两个独立应用或多个 Web worker 之间的硬件占用协调。

WarmteLink ingestor 则含设备专用约束：canonical channel 1=热水 m³、channel 2=热量 GJ；对 `unverifiable` 要求连续两帧、设备时间相隔 10 秒、身份一致且累计不下降；接纳后仍保留 `unverifiable`。它还包含针对 WarmteLink 时间异常的 Europe/Amsterdam 推断，以及每分钟最多一条 history、每个有效帧更新 latest 的策略。上述规则不应直接复制为通用电表规则。

### 7.2 已有 DSMR MQTT 路线

| 文件 | 参考价值 |
| --- | --- |
| [dsmr_ingest.py](/home/tianyu/workspace/home-automation/app/services/dsmr_ingest.py) | JSON 接收、source 配置协调、采样、存储、费率消息 |
| [integrations/mqtt.py](/home/tianyu/workspace/home-automation/app/integrations/mqtt.py) | MQTT 连接、source 订阅与回调管理 |
| [integrations/meter_sources.py](/home/tianyu/workspace/home-automation/app/integrations/meter_sources.py) | `dsmr_mqtt` 与 `warmtelink_serial` 配置注册表 |
| [services/meter_sources.py](/home/tianyu/workspace/home-automation/app/services/meter_sources.py) | source/channel/binding 业务服务 |
| [models/energy.py](/home/tianyu/workspace/home-automation/app/models/energy.py) | `DsmrReading` 和 Meter 等电力模型 |
| [models/meter_source.py](/home/tianyu/workspace/home-automation/app/models/meter_source.py) | 采集 source、channel、binding 及 WarmteLink readings |
| [DsmrPanel.tsx](/home/tianyu/workspace/home-automation/frontend/src/energy/DsmrPanel.tsx) | 电力数据在现有前端的消费方式 |
| [test_dsmr_ingest.py](/home/tianyu/workspace/home-automation/tests/test_dsmr_ingest.py) | 电力摄取回归测试 |

当前 DSMR source 配置包含 broker 地址、凭据、TLS、`topic`（默认 `dsmr/json`）、独立 `tariff_topic` 和 `sample_interval_s`（默认 10）。正式配置入口已随 M8 迁入 source；不要仅照旧 M6 文档复制全局 `dsmr_*` 开关。

摄取函数接收已解析 JSON，不接收 raw P1 bytes。它保留整帧 JSON，按 `(meter_source_id, recorded_at)` 去重，`telegram_id` 仅保留原消息 id。默认采样方式是设备时间的秒数满足 `second % 10 == 0`，并非“距上次落库至少 10 秒”；目标设备节拍不同，应重新选择采样方式。

若新项目选择继续依赖 DSMR Reader，可直接沿用 MQTT 路线；若选择直接串口读取，就在新项目明确实现电力字段映射与入库层。后续如需累计量差分、计费或换表，应参考 source binding 与 Meter epoch 边界，避免把不同物理表或数据源的累计值直接相减。

## 8. 同机开发的环境与依赖

源项目声明 Python `>=3.11`，Docker runtime 为 Python 3.12。依赖以 [requirements.txt](/home/tianyu/workspace/home-automation/requirements.txt) 的已锁定版本为基线；[requirements.in](/home/tianyu/workspace/home-automation/requirements.in) 是范围声明。

| 复用范围 | 当前锁定依赖 |
| --- | --- |
| Modbus 驱动 | `pymodbus==3.13.1` |
| Modbus profile | `pydantic==2.13.2`、`pyyaml==6.0.3` |
| P1 纯 parser | Python 标准库，无额外包 |
| P1 串口读取 | `pyserial==3.5` |
| 可选数据库/调度 | `sqlalchemy==2.0.49`、`apscheduler==3.11.2` |
| 可选 MQTT | `paho-mqtt==2.1.0` |

当前 pymodbus 调用使用 `device_id=`。旧协议摘录可能出现 `slave=`、旧 `BinaryPayloadDecoder` 示例，搬迁以当前 driver 为准；不要只依据 `pymodbus>=3.6,<4.0` 的宽范围任意换版本。

新项目使用自己的虚拟环境、数据库和配置。首次只读验证可借用源仓库现有 `.venv` 运行 CLI，无需启动整套应用。

串口部署注意：

- 使用 `/dev/serial/by-id/...` 稳定路径，确认它指向目标 P1 线。不要默认 `/dev/ttyUSB0` 永远是同一设备。
- 同机两个项目不能各自打开同一物理串口；若都需要读数，可由一个进程拥有串口，再向另一个项目发布数据。
- 宿主机用户按实际设备组配置访问权限；检查符号链接目标的 GID 可用 `stat -Lc '%g %n' /dev/serial/by-id/REPLACE_DEVICE`，其中 `-L` 表示跟随符号链接。
- Docker 下 pyserial 在 POSIX 以 `O_RDWR` 打开设备，故 device permission 需要 `rw`；业务仍只调用 read/close，无须 `privileged`、root 或设备创建权限 `m`。
- 串口 raw capture、设备标识和真实连接信息留在本地。用于测试的报文单独脱敏；不拷贝源项目 `.env`、运行数据库或 MQTT 凭据到新仓库。

## 9. 建议的新项目落地顺序与验收

1. 提取 Modbus driver/profile 与 P1 parser/probe，调整包路径，建立目标项目独立依赖；先不连接实物。
2. 迁入相关离线测试和 fixtures；确认 YAML 能从包安装位置找到，P1 raw fixture 不被工具改写换行。
3. DDSU666 先复用已有 profile 做对照；拿到 DTSU666 具体手册后增加独立 profile 与寄存器回归。
4. 电表 P1 明确串口参数，采集脱敏标准报文，新增电力 OBIS 映射与单相/三相缺测测试。
5. 实物验证按“单个量 → 全量 → 连续读取 → 断线恢复”顺序进行，对照物理表确认单位和累计方向。
6. 之后再接入新项目存储、后台生命周期、可选 API/图表/HA；不要为了读数先搬整个能源计费系统。

建议验收项：Modbus 错误功能码/地址和断网能明确报错；不发写寄存器命令；三相指标不混相；P1 分包/粘包/CRC 错误能正确处理；设备时间和接收时间不混用；缺测不填零；累计值下降、换表和 source 切换不产生虚假消费；退出后串口释放，重连不拼接旧半帧。

本次已在源项目 `.venv` 中实际运行以下离线测试：

```bash
cd /home/tianyu/workspace/home-automation
.venv/bin/python -m pytest \
  tests/test_modbus_driver.py \
  tests/test_modbus_profiles.py \
  tests/test_modbus_cli.py \
  tests/test_p1_parser.py \
  tests/test_p1_probe.py -q
```

结果：**68 passed in 0.15s**。另核对了 `p1_probe --help` 与 `modbus_cli read --help` 的实际参数。本文中的网络/串口命令仅作交接示例，本次未对真实硬件执行。

如复用更高层服务，还可按选取范围运行 `tests/test_modbus_poll.py`、`tests/test_api_modbus.py`、`tests/test_dsmr_ingest.py`、`tests/test_warmtelink_worker.py`、`tests/test_warmtelink_ingest.py` 及 source/binding 测试。这些测试的数据库 fixture 可能依赖源项目的 `tests/conftest.py`，迁入新项目时需重接。

本次交付为仓库外 Markdown 文档，没有实现任务或代码改动；未运行完整 pytest、ruff、前端 lint/typecheck/test/build 或 OpenAPI/codegen 闸门。上述 68 项通过用于说明本次核对过的复用基础，不代表对新项目或新硬件的验收。

## 10. 可直接交给下一个开发者的任务说明

> 参考 `/home/tianyu/home-automation-energy-handover.md`，从 `/home/tianyu/workspace/home-automation` 提取只读 Modbus TCP 驱动、YAML profile 框架、P1 增量分帧/CRC/OBIS parser 及 CLI。先用离线 tests/fixtures 建立新项目的最小采集模块。
>
> DDSU666 已有 profile 和历史真机验证；DTSU666 尚无实现，需按具体型号手册新增配置。当前 Modbus 解码只支持固定大端 float32，YAML 的 order 字段未实际驱动解码，必要时先扩展。
>
> 电表 P1 直接串口读通有历史记录，parser/probe 可以复用；当前正式电力摄取来自 DSMR Reader MQTT JSON。WarmteLink worker 固定 7N1，ingestor 专用于热水/热量；新项目的电表 serial 参数、电力 OBIS 映射和常驻入库需明确实现。协调同机串口 owner，使用新项目自己的配置、测试数据库和虚拟环境。
