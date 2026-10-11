# Watt Profile：高层架构与路线图

> [English](architecture-and-roadmap.md) · **中文**

状态：高层规划基线，2026-10-11 按已确定的 MVP 更新。本文记录系统方向。M1 离线协议／契约／probe 实现已通过独立审查及所有者要求的全新子代理走查，完成验收；M2 正在实施；M2 验收及 M3–M4 实施仍待完成。各应用 Milestone 须按路线图同步交付容器构建、Compose 文件及本地浏览器走查。[MVP 路线图](mvp-roadmap_zh.md)定义首版边界、Milestone 和验收检查。

## 目标与边界

Watt Profile 在客户家中监测数周用电情况，供内部方案设计人员评估储能电池和光伏方案。系统分为两部分：

- **Data Logger：** 部署在现场的容器化设备。MVP 从电表 P1 接口采集，仅支持荷兰 DSMR 5 profile（`dsmr5_nl`，依据 DSMR 5.0.2）；其他 profile 和 Modbus 后续接入。目标为树莓派 4（1 GB），兼容树莓派 3B+ 是 MVP 验收要求；树莓派 5 是后续硬件目标。
- **Centralized Server：** 可从公网访问的自管应用，包含内部 Web 界面、API 和数据库，首版容器化部署在所有者家中的服务器上。VPS 部署和报告生成后续扩展。

试点从一台 Logger 开始，实际投入后预计也只有几台。一个监测会话（部署）先使用一个来源，但数据模型区分设备、现场、会话、测量点和来源，支持将来多电表部署。Logger 离线生成新现场的 UUID v4，中心端沿用并关联地址和简介。同一现场再次监测时复用 Site UUID、建立新会话。停止／启动和重启延续未结束会话；明确结束、换现场或确认换表后新建会话／source。同一现场并网测量点保持稳定。Site UUID 与设备 Bearer Token 独立，中心端人工登记授予设备访问该现场的权限。

MVP 提供可靠采集与回传、现场／设备人工登记、状态、基本查询和 CSV 导出。图表和面向内部人员的 AI 用电画像报告后续迭代；报告将描述用电特征、数据质量和待核查事项。具体电池或光伏容量建议，待预计光伏功率等额外现场输入明确后再设计。首版不需要客户登录。

## 架构与技术选型

| 层 | Centralized Server | Data Logger |
| --- | --- | --- |
| 运行与部署 | 在所有者家中的 Linux 服务器上使用 Docker Compose；Caddy 提供公网 HTTPS 入口。 | Pi 4 1 GB 和 Pi 3B+ 使用 Raspberry Pi OS Lite 64 位和 ARM64 容器。一个 Python 应用进程负责采集、上传和本地 API。 |
| 后端 | Python 与 FastAPI 处理接收、登记、查询、导出和鉴权；后续增加独立报告 worker。 | Python 与 FastAPI 提供本地配置和试读，P1 使用 `pyserial`。`pymodbus` 属于后续 Modbus 扩展。原协议代码是起点，并非已完成的 Logger 服务。 |
| 前端 | MVP 由后端提供 HTML 表单、列表和数据表；React、TypeScript、Vite 作为后续图表及报告审阅选项。 | Python 提供简单 HTML 和少量 JavaScript；Pi 上不运行 Node.js。 |
| 存储 | PostgreSQL 保存设备、现场、会话、授权和快照。常用指标及时间使用可查询字段，其余脱敏字段可用 JSONB；接纳的快照至少保留一年。报告存储后续增加。 | SQLite 持久化卷保存配置、快照及上传状态，容量须支持至少四周离线；保留整次监测，仅手动清理已完成会话中的已上传记录。 |
| AI | MVP 后再做。未来独立 worker 通过 OpenRouter Responses API 调用可配置模型，保留统计输入、模型／提示词版本和报告供审阅。 | 不运行模型，也不保存模型 API 密钥。 |

采集层以[交接文档](home-automation-energy-handover.md)中的 P1 分帧、CRC、OBIS 解析器和只读 Modbus TCP 驱动及 profile 为起点。源项目更高层的数据库、MQTT、Home Assistant、计费和 WarmteLink 专用规则不属于本架构的复用范围。

Logger 向配置的中心端 URL 批量回传：公网使用 HTTPS，也支持所有者管理的 WireGuard 隧道内 HTTP。SSH、网口地址、DHCP、Wi-Fi 和 WireGuard 均在项目外管理。Onboarding 和采集支持离线，应用不配置宿主机网络，也不要求 WireGuard。操作人员先建立电脑与树莓派互通，再通过可达地址打开本地页面。本地配置凭据、中心端账号和设备上传凭据分别授权。Logger 生成并保存上传 Token，管理员在中心端人工登记 Token 和 Site UUID；中心端保存 Token 哈希并检查现场授权。

## 采集、回传与报告

- **P1：** 持续解析完整报文并检查完整性；每 10 秒保存该窗口内最新一条新有效报文，包括全部脱敏解析字段、时间和质量信息。保留安全标准值；设备标识、自由文本和未知字段值脱敏，保留其 code 和脱敏标记。空窗口保留缺测，不重复保存旧值。原始串口字节不长期保存。现场人员选择串口参数和已安装电表 profile，预览字段并试读；解析规则和 OBIS 映射由应用维护。
- **Modbus，后续：** 以可配置间隔轮询，初始约 10 秒。标准 Modbus TCP 为首个传输方式，以 DDSU666 为历史验证参考。DTSU666 需要确切型号手册和实物验证。串口 RTU 与 RTU over TCP 分别扩展，GPIO/RS485 硬件设计不在本轮范围。
- **离线回传：** 上传前提交快照和待上传状态；正常每 10 秒上传，恢复后以有上限的批次补传，最多一个请求在途，不阻塞采集。中心端验证凭据及现场授权，原子、幂等保存批次后才确认；Logger 此后才标记已上传。界面展示质量、联系时间、积压、错误和存储压力。容量不足时告警并停止新增记录，不丢弃未上传数据。Logger 转移现场时旧记录保留原现场／会话归属。
- **中心端分析，后续：** 基于快照计算覆盖率、缺测、进口／回送电量、采样峰值和日内形态。电量使用累计读数，10 秒功率快照可能漏掉短时峰值。增加图表后再向 OpenRouter 发送不含身份标识的统计，生成文字报告；保存快照供核对，内部人员审阅后导出 PDF。

未来报告 worker 不应阻塞接收，任务状态可存 PostgreSQL。消息队列和专用时序数据库不是初始依赖，按实测数据量和延迟判断需要。PostgreSQL 的 [JSONB 支持](https://www.postgresql.org/docs/current/datatype-json.html)和[原生范围分区](https://www.postgresql.org/docs/current/ddl-partitioning.html)是可用存储工具，分区实现不单列为 MVP 要求。OpenRouter 接入和模型能力验证属于后续报告阶段。

## 路线图与验证

MVP 按依赖关系推进：**M1 协议与契约 → M2 独立 Logger → M3 人工纳管中心端 → M4 完整试点版本**。验收和估算见[MVP 路线图](mvp-roadmap_zh.md)。后续增加图表与确定性分析、AI 报告、Modbus，以及 provisioning／运维增强。

现场试点验收前，应在隔离服务中使用合成或脱敏样本验证：

- P1 分帧、缓冲上限、CRC 拒收、字段映射、时间、脱敏、10 秒选取和空窗口处理。
- Pi 4 1 GB 与 Pi 3B+ 的资源占用、持续采集、实际磁盘写入和四周离线容量。10 秒存储四周最多 241,920 条快照，实际字节量须测量。
- 凭据／现场授权、UUID 稳定性、换现场不改旧记录归属、采集／上传期间重启、长期断网、响应丢失、重放和去重。
- 查询与 CSV 一致。图表一致性、报告数字可追溯、模型不可用和 Modbus 实物验证属于后续阶段检查。

[M1 文档](milestone/m1_zh.md)内包含首个 P1 profile、具体 API／schema 契约及八个原子实现任务；可执行协议／领域／上传契约、诊断 probe 和离线检查已经实现并通过独立审查；存储是供 M2／M3 实施的规范。参见[完成记录](milestone/m1_zh.md#4-检查与完成记录)。[M2 设计](milestone/m2_zh.md)规定独立 Logger 及十个原子任务；M2 将记录本地保存为 pending，不实现网络上传。每个 milestone 在 `docs/milestone/` 下的一份英文 Markdown 及中文镜像内维护设计、任务和验收记录。快照大小和容量在 MVP 验证中测量。未来 Modbus 具体型号、AI 模型、报告呈现和远程配置／升级机制留待后续设计。
