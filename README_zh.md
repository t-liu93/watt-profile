# Watt Profile

> [English](README.md) · **中文**

Watt Profile P1 协议和数据契约的独立 Python 包。

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

Logger 在本地采集 DSMR 5，快照保持 pending；M2 尚未实现上传。开发、测试、ARM64 构建、凭据／设备权限及清理的精确命令见 [M2 容器操作和独立部署设置](docs/milestone/m2_zh.md#容器操作m2-09)。

登录页和 dashboard 默认使用暗色。通过页面顶部按钮切换明暗主题；浏览器存储只保存主题偏好。

`docker-compose.dev.yml` 使用真实解析链路处理合成字节，采用仅用于开发的账号 `operator`／`testtesttesttest`、项目私有可丢弃卷和回环端口（默认 8080，可通过 `WATT_DEV_PORT` 覆盖）。选择唯一 `watt-m2-...` 项目及 `WATT_DEV_IMAGE_TAG`；显式运行 Compose／浏览器测试。`up logger` 只启动 Logger 依赖，不挂载宿主设备。

`deploy/logger/compose.yml` 用于独立串口部署，必须提供外部操作员密码文件和显式设备／GID 映射；将[脱敏示例](deploy/logger/.env.example)复制到被忽略的 `.env.local` 并替换占位符。应用 UID/GID 为 10001，数据卷权限为 0700，只读密码文件必须为 0600 且归 UID 10001 所有。受限 root 初始化服务仅准备挂载卷。正常 `down` 保留数据。本机容器运行和 ARM64 构建分别验证，均不代表真实电表验收。

## Server 容器

Server 已实现管理员注册、授权批量接收、读数和 CSV 导出；Logger 上传仍属于 M4。独立 [Compose](deploy/server/compose.yml) 仅包含应用和 PostgreSQL，只发布一个 HTTP 应用端口，默认将 `127.0.0.1:18081` 映射到容器 8080；PostgreSQL 不发布宿主端口。所有者的宿主机／上游 Nginx 独立管理 HTTPS、证书和反代。修订后的外部 HTTPS 检查和所有者验收仍待 [M3](docs/milestone/m3_zh.md) 完成。

将[脱敏示例](deploy/server/.env.example)复制到被忽略的 `deploy/server/.env.local` 并替换所有占位符。将 `WATT_SERVER_PUBLIC_ORIGIN` 设为外部浏览器源（经 Nginx 使用 HTTPS），HTTPS 保持 `WATT_SERVER_COOKIE_SECURE=true`；若上游需要可达接口或其他端口，选择 `WATT_SERVER_BIND_ADDRESS`／`WATT_SERVER_PUBLISHED_PORT`。提供私有 DB／应用凭据目录（0700）及文件（0600）；应用文件归 UID/GID 10001 所有并只读挂载。DB 目录包含 `password`；应用目录包含 `database-url` 和 `admin-password`，DB 凭据必须一致且不提供开发默认值。启动前阅读[操作说明](docs/milestone/m3_zh.md#m3-11-部署操作)：

```sh
docker compose --env-file deploy/server/.env.local -p watt-server -f deploy/server/compose.yml config --quiet
docker compose --env-file deploy/server/.env.local -p watt-server -f deploy/server/compose.yml up --build -d --wait --wait-timeout 120 server
docker compose --env-file deploy/server/.env.local -p watt-server -f deploy/server/compose.yml down
```

开发采用 `admin`／`testtesttesttest` 和回环端口 18081（`WATT_DEV_SERVER_PORT` 可覆盖）。选择新的可丢弃 `watt-m3-...` 项目及匹配的 `WATT_DEV_IMAGE_TAG`；`docker compose -p PROJECT -f docker-compose.dev.yml up --build -d --wait --wait-timeout 120 server` 只启动 PostgreSQL／初始化依赖。正常 `down` 保留数据及凭据；仅可删除验证时创建的可丢弃项目卷。
