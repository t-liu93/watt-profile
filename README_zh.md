# Watt Profile

> [English](README.md) · **中文**

Watt Profile 通过独立 Logger 采集 P1 电力读数，并在中心 Server 存储，供内部人员使用。

## 路线图

- [MVP 路线图](docs/mvp-roadmap_zh.md)：已完成的 M1–M4 范围与验收。
- [Post-MVP 路线图](docs/post-mvp-roadmap_zh.md)：持续维护的后续开发方向，从 M5 Server 分析工作台开始，随后推进 M6 报告。
- [高层架构](docs/architecture-and-roadmap_zh.md)：系统边界和跨阶段技术原则。Milestone 详细设计与验收记录继续放在 `docs/milestone/` 下。

## CI 与发布镜像

每次 push 执行合成数据 CI，包含显式四周容量 selector。版本 tag 在检查和原生镜像测试通过后，将独立 Logger、Server 的 AMD64／ARM64 镜像发布到 GHCR。参阅 [CI 与发布说明](docs/ci-and-release_zh.md)。[已发布的 `v0.1.0-alpha.3`](https://github.com/t-liu93/watt-profile/releases/tag/v0.1.0-alpha.3) 修复 Logger 积压组批性能；alpha.2 引入 M4 上传／恢复及 LOGGER-001 修复。alpha.1 保持未变，仍为 M4 前版本。

**所有者于 2026-10-18 验收 M4，MVP 已完成。** 所有者验证 Server 部署、HTTPS／登录及手动本地／远端备份，保留既有数据将 ARM64 Pi Logger 升级至 alpha.3，并确认真实积压清零。随后采集与上传并行运行：新 pending 记录被确认，上传器回到 idle，未再报告错误。详见 [M4 最终验收及证据限制](docs/milestone/m4_zh.md#所有者最终验收与-mvp-结项2026-10-18)和 [MVP 路线图](docs/mvp-roadmap_zh.md)。[Post-MVP 路线图](docs/post-mvp-roadmap_zh.md)记录已确定的后续方向，实现尚未开始。

## 开发环境

使用 Python 3.12，并在仓库外创建唯一的隔离虚拟环境。Logger 运行环境安装 `requirements.lock`；项目开发和运行检查安装 `requirements-dev.lock`：

```bash
M2_VENV="$(mktemp -d /tmp/watt-profile-m2-venv.XXXXXX)"
python3.12 -m venv "$M2_VENV"
"$M2_VENV/bin/python" -m pip install -r requirements-dev.lock
"$M2_VENV/bin/python" -m pip install --no-build-isolation --no-deps -e .
"$M2_VENV/bin/python" -m pytest tests/test_package.py -q
"$M2_VENV/bin/python" -m ruff check .
"$M2_VENV/bin/python" -m ruff format --check .
```

使用 pip-tools 重新生成两个锁文件。第一个包含运行依赖，第二个还包含开发依赖：

```bash
M2_PIP_CACHE="$(mktemp -d /tmp/watt-profile-pip-tools-cache.XXXXXX)"
PIP_TOOLS_CACHE_DIR="$M2_PIP_CACHE" "$M2_VENV/bin/python" -m piptools compile --allow-unsafe --resolver=backtracking --output-file requirements.lock pyproject.toml
PIP_TOOLS_CACHE_DIR="$M2_PIP_CACHE" "$M2_VENV/bin/python" -m piptools compile --extra dev --allow-unsafe --resolver=backtracking --output-file requirements-dev.lock pyproject.toml
```

当前 M2 工作期间保留环境，之后只清理本 shell 创建的目录：

```bash
rm -rf -- "$M2_VENV" "$M2_PIP_CACHE"
```

## Logger 容器

部署缺陷及恢复方式见受版本管理的[已知问题](docs/known-issues_zh.md)。`v0.1.0-alpha.1` 存在待修的独立预览卡死问题（LOGGER-001）。

Logger 本地采集 DSMR 5，并在上传前持久化快照。配置 origin 后，alpha.2 镜像支持自动上传和积压恢复；未改动的 alpha.1 镜像仍为 M4 前版本。开发、测试、ARM64 构建、凭据／设备权限及清理的精确命令见 [M2 容器操作和独立部署设置](docs/milestone/m2_zh.md#容器操作m2-09)；合成数据配置到 CSV 的验收见 [M4 集成走查](docs/milestone/m4_zh.md#计划的组合开发走查)。

登录页和 dashboard 默认使用暗色。通过页面顶部按钮切换明暗主题；浏览器存储只保存主题偏好。

`docker-compose.dev.yml` 使用真实解析链路处理合成字节，采用仅用于开发的账号 `operator`／`testtesttesttest`、项目私有可丢弃卷和回环端口（默认 8080，可通过 `WATT_DEV_PORT` 覆盖）。选择唯一 `watt-m2-...` 项目及 `WATT_DEV_IMAGE_TAG`；显式运行 Compose／浏览器测试。`up logger` 只启动 Logger 依赖，不挂载宿主设备。

`deploy/logger/compose.yml` 用于独立串口部署，必须提供外部操作员密码文件和显式设备／GID 映射；将[脱敏示例](deploy/logger/.env.example)复制到被忽略的 `.env.local` 并替换占位符。应用 UID/GID 为 10001，数据卷权限为 0700，只读密码文件必须为 0600 且归 UID 10001 所有。受限 root 初始化服务仅准备挂载卷。正常 `down` 保留数据。本机容器运行和 ARM64 构建分别验证，均不代表真实电表验收。

## Server 容器

Server 已实现管理员注册、授权批量接收、读数和 CSV 导出；当前 M4 Logger 源码会自动上传已确认批次。独立 [Compose](deploy/server/compose.yml) 仅包含应用和 PostgreSQL，只发布一个 HTTP 应用端口，默认将 `127.0.0.1:18081` 映射到容器 8080；PostgreSQL 不发布宿主端口。所有者的宿主机／上游 Nginx 独立管理 HTTPS、证书和反代。2026-10-18 所有者使用独立的 [Linux Install Helper 部署脚本](https://github.com/t-liu93/Linux-install-helper/blob/10197f8/watt-profile/README_zh.md) 部署已发布的 alpha.2 Server，确认 HTTPS／管理员登录正常，并手动验证本地备份和对应远端归档。每日定时执行及使用该实际归档恢复尚未观察验证。外部 origin/HTTP 验证及修订自动回归已通过；所有者于 2026-10-14 验收 [M3](docs/milestone/m3_zh.md)，并确认走查资源已清理。

将[脱敏示例](deploy/server/.env.example)复制到被忽略的 `deploy/server/.env.local` 并替换所有占位符。将 `WATT_SERVER_PUBLIC_ORIGIN` 设为外部浏览器源（经 Nginx 使用 HTTPS），HTTPS 保持 `WATT_SERVER_COOKIE_SECURE=true`；若上游需要可达接口或其他端口，选择 `WATT_SERVER_BIND_ADDRESS`／`WATT_SERVER_PUBLISHED_PORT`。提供私有 DB／应用凭据目录（0700）及文件（0600）；应用文件归 UID/GID 10001 所有并只读挂载。DB 目录包含 `password`；应用目录包含 `database-url` 和 `admin-password`，DB 凭据必须一致且不提供开发默认值。启动前阅读[操作说明](docs/milestone/m3_zh.md#m3-11-部署操作)：

本地源码构建前，先激活 Python 3.12，并将准确版本 Node **24.22.0** 和 npm **11.16.0** 放入 `PATH`。在仓库根目录完成一次构建／检查，导出记录的摘要并暂存同一份字节，然后启动 Compose（在同一 shell 中保留该导出变量）：

```sh
python scripts/frontend_artifact.py build
export WATT_FRONTEND_DIGEST="$(python -c 'import json; print(json.load(open("frontend/dist/artifact.json"))["digest"])')"
python scripts/frontend_artifact.py stage --expected "$WATT_FRONTEND_DIGEST"
docker compose --env-file deploy/server/.env.local -p watt-server -f deploy/server/compose.yml config --quiet
docker compose --env-file deploy/server/.env.local -p watt-server -f deploy/server/compose.yml up --build -d --wait --wait-timeout 120 server
docker compose --env-file deploy/server/.env.local -p watt-server -f deploy/server/compose.yml down
```

先完成上述相同的前端构建／导出／暂存步骤，再启动开发环境。开发采用 `admin`／`testtesttesttest` 和回环端口 18081（`WATT_DEV_SERVER_PORT` 可覆盖）。选择新的可丢弃 `watt-m3-...` 项目及匹配的 `WATT_DEV_IMAGE_TAG`；`docker compose -p PROJECT -f docker-compose.dev.yml up --build -d --wait --wait-timeout 120 server` 只启动 PostgreSQL／初始化依赖。正常 `down` 保留数据及凭据；仅可删除验证时创建的可丢弃项目卷。

构建命令安装锁定的 npm 依赖，准备／校验组件，执行 lint／类型检查／测试，并写入 `frontend/dist/artifact.json`。前端变更后重新构建并导出摘要。直接使用 Docker 构建时，还必须显式传入已导出的变量：

```sh
docker build --build-arg WATT_FRONTEND_DIGEST -f deploy/server/Dockerfile -t watt-profile-server:local .
```

通过 `up --no-build --pull always` 启动已发布镜像无需本地 Node／静态资源或该构建参数。CI／发布的消费方使用已检查制品生成方提供的可信摘要；不要用不可信下载制品中读取的摘要替换它。参阅 [CI 与发布说明](docs/ci-and-release_zh.md)。
