# CI 与容器镜像发布

> [English — source of truth](ci-and-release.md) · **中文**

本交付步骤位于已验收的 [M3](milestone/m3_zh.md) 与 [M4](milestone/m4_zh.md) 之间，为 Logger、Server 增加 GitHub Actions 检查和 GHCR 交付。当前 M4 源码含上传、四周容量测量和恢复检查；`scripts/ci.sh` 会显式运行容量 selector。已发布的 `v0.1.0-alpha.1` Logger 仍是 M4 前镜像，不含上传器；变更镜像须另行授权发布新版本。工作流不部署或配置宿主机、树莓派、Nginx 或生产服务。详见 [M4 容量及所有者升级流程](milestone/m4_zh.md#所有者-pi-升级与验收交接)。

`v0.1.0-alpha.2` 引入 M4 上传／恢复及 LOGGER-001 修复。[Logger alpha.3](https://github.com/t-liu93/watt-profile/releases/tag/v0.1.0-alpha.3) 于 2026-10-18 从 `ae54b63` 发布，修复平方级积压组批。[main CI](https://github.com/t-liu93/watt-profile/actions/runs/39126887798)、[发布 CI](https://github.com/t-liu93/watt-profile/actions/runs/39126887699)、四个原生镜像检查及发布均成功。Logger、Server alpha.3 镜像支持 AMD64／ARM64 和匿名访问；所有者的 Server alpha.2 保持兼容，无需升级。旧标签未变，此预发布不设置 `latest`。所有者部署 Logger alpha.3，确认积压清零、采集／上传并行且未再报错，并验收 M4／MVP。详见[最终验收及证据限制](milestone/m4_zh.md#所有者最终验收与-mvp-结项2026-10-18)。

## 检查与发布契约

1. 每次分支 push、非版本 tag push、pull request 以及手动触发都会运行 `CI`。`v*` tag 则触发 `Release images`，在构建任何发布镜像前，对解析后的 tag 提交调用同一套完整 CI。版本 tag 不合法时验证失败。
2. CI 在 Python 3.12 安装 `requirements-dev.lock` 和 Chromium，执行 `bash scripts/ci.sh`：Ruff 代码及格式检查、启用临时 PostgreSQL 的默认测试集、显式 Logger/Server 浏览器测试、Server 开发/独立 Compose 测试、备份恢复验收、完整合成 241,920 条容量 selector、M4 HTTP 恢复及 Logger/Server Compose/浏览器验收。默认测试中跳过的显式启用项，会在后续指定路径的阶段实际执行。
3. 发布 tag 必须是严格的 `v主版本.次版本.补丁版本`，可带 SemVer 预发布/构建信息。四个 GitHub 托管原生构建覆盖 Logger/Server × `linux/amd64`/`linux/arm64`，分别使用 `ubuntu-24.04` 和 `ubuntu-24.04-arm`；无需自托管 runner 或注册 QEMU。
4. 每个候选镜像以 UID/GID 10001，在无网络、无物理设备条件下检查原生模块导入、打包资源/迁移与 CLI 启动。Logger 另外创建并重新打开合成 SQLite 数据，核对身份、Token 及待上传快照保持不变。
5. 完整 CI 和四个原生镜像冒烟测试全部通过后，具有 `packages: write` 的发布任务才使用内置 `GITHUB_TOKEN`，加载并发布**实际测试过的同一镜像**，随后按摘要合并为每个组件一个多架构镜像。构建任务只有仓库读取权限，无需另设镜像仓库密码。第三方 Action 均固定到提交 SHA。

普通 push 的现有 Compose 回归会构建可清理的测试镜像；**发布镜像构建及推送仓库只由版本 tag 触发**。Actions 不通过 SSH 连接设备，也不自动更新运行中的容器。本地验证不能证明 GitHub 权限、云端 runner 或 GHCR 发布已通过；这些需要首次经授权的 push/tag 执行验证。

## 镜像与标签

本仓库发布：

| 包 | 架构 | 标签 |
| --- | --- | --- |
| `ghcr.io/t-liu93/watt-profile-logger` | AMD64、ARM64 | 版本号、完整 `sha-COMMIT`、稳定版 `latest` |
| `ghcr.io/t-liu93/watt-profile-server` | AMD64、ARM64 | 版本号、完整 `sha-COMMIT`、稳定版 `latest` |

包名根据小写 GitHub 仓库名生成，因此 fork 只发布到自己的命名空间。`v0.1.0` 生成 `:0.1.0`；`v0.1.1-rc.1` 生成 `:0.1.1-rc.1`，不更新 `latest`。构建信息中的 `+build.2` 在 Docker 标签内转换为 `_build.2`。内部 `build-RUN-ATTEMPT-ARCH` 标签保留 manifest 引用的平台镜像；测试镜像 tar 制品一天后过期。部署建议固定版本或 manifest 摘要。`latest` 指向最后成功发布的稳定版 tag，即使后来发布的是较旧版本。

审阅并提交实现后，所有者可推送分支并确认 Actions，通过单独授权发布版本，例如：

```sh
git tag v0.1.0
git push origin v0.1.0
```

源代码变化应使用新版本。GitHub 仓库设置需要允许 Actions 使用 `GITHUB_TOKEN` 写入包；已有包需要授予仓库访问权限。GHCR 包首次创建时可能为私有。可在两个包的设置中选择公开，让树莓派匿名拉取；若保留私有，则在树莓派上使用有这些包读取权限的 Token 登录 Docker。不要把 Token 放进 Compose 文件或本仓库。参阅 [GitHub 镜像发布说明](https://docs.github.com/en/actions/use-cases-and-examples/publishing-packages/publishing-docker-images)及[容器仓库认证与可见性](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。

## 树莓派拉取 Logger

使用树莓派 OS **64 位**，以及 Docker Compose v2 或更新版本。`uname -m` 应返回 `aarch64`；Docker 根据 manifest 自动选择 ARM64。树莓派需要仓库中的 Compose 文件和环境配置，无需安装 Python 或在本地构建镜像。

1. 将 `deploy/logger/.env.example` 复制为被忽略的 `deploy/logger/.env.local`，填写操作员名称、私有密码文件绝对路径、稳定的 `/dev/serial/by-id/...` 设备、其**宿主机数字组 ID**、可达的绑定地址和对外端口。密码为 16–256 字符，文件权限 0600、UID/GID 为 10001，父目录权限 0700。这些由所有者配置，详见 [M2 操作说明](milestone/m2_zh.md#容器操作m2-09)。
2. 添加实际发布过的版本，例如 `WATT_LOGGER_IMAGE=ghcr.io/t-liu93/watt-profile-logger:0.1.0-alpha.3`。本地构建时不设置此变量。初始化器和 Logger 使用相同的所选镜像。局域网直接 HTTP 访问时选择树莓派合适的接口/端口，保留 `WATT_LOGGER_COOKIE_SECURE=false`；HTTPS 在容器外管理。
3. 在仓库根目录验证配置，只启动 Logger 及其初始化器：

```sh
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml config --quiet
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml up --no-build --pull always -d --wait --wait-timeout 120 logger
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml ps --all
```

打开 `http://树莓派地址:配置端口`，登录并创建现场、进行读数预览，然后创建暂停状态的部署并启动采集。单纯预览不会存储读数。采集快照保存在命名卷，重启／重建后仍在。已发布的 alpha.3 镜像含上传、预览修复和积压性能修复；alpha.1 不会上传。真实 P1 适配、存储分配及树莓派资源使用仍需所有者实机走查。升级到 alpha.3 前，遵循 [在线备份、复制到宿主机、升级和回滚步骤](milestone/m4_zh.md#所有者-pi-升级与验收交接)，保留原 Compose project 和数据卷。

普通停止会保留数据库、身份及 Token：

```sh
docker compose --env-file deploy/logger/.env.local -p watt-logger -f deploy/logger/compose.yml down
```

真实 Logger 安装不要使用 `down --volumes`。升级时修改固定镜像版本后重新执行 `up --no-build --pull always`；先按 M2 操作备份已有数据。

Server 可在其被忽略的环境文件中设置 `WATT_SERVER_IMAGE=ghcr.io/t-liu93/watt-profile-server:VERSION`，使用相同的 `up --no-build --pull always` 方式，对应 `deploy/server/compose.yml` 和 `server` 服务。PostgreSQL 不暴露宿主机端口，仅应用暴露一个 HTTP 端口；凭据和外部 Nginx 仍由所有者按 [M3](milestone/m3_zh.md#m3-11-部署操作) 管理。

## 本地前端制品与镜像构建

将 Python 3.12、Node **24.22.0** 和 npm **11.16.0** 放入 `PATH`。在仓库根目录的同一 shell 中执行：

```sh
python scripts/frontend_artifact.py build
export WATT_FRONTEND_DIGEST="$(python -c 'import json; print(json.load(open("frontend/dist/artifact.json"))["digest"])')"
python scripts/frontend_artifact.py stage --expected "$WATT_FRONTEND_DIGEST"
export WATT_DEV_IMAGE_TAG=watt-local-UNIQUE
docker compose -p "$WATT_DEV_IMAGE_TAG" -f docker-compose.dev.yml up --build -d --wait --wait-timeout 120 server
# 完成相同构建／导出／暂存后的直接 Docker 构建选项：
docker build --build-arg WATT_FRONTEND_DIGEST -f deploy/server/Dockerfile -t watt-profile-server:local .
```

启动前选择未使用的可丢弃项目名及 `WATT_DEV_IMAGE_TAG`；独立部署的凭据／配置和启动步骤见 [Server 容器](../README_zh.md#server-容器)。构建执行锁定依赖安装、组件准备／校验、lint、类型检查、测试及打包。重新构建后再次导出／暂存。CI／发布使用生成方提供的可信摘要执行 `stage --expected`；不得从不可信制品中重新提取摘要来替换它。镜像构建缺少摘要时会失败并给出配置指引。已发布镜像的 `up --no-build` 无需本地制品／摘要。

## 本地验证与清理

在独立 Python 3.12 虚拟环境安装开发锁定依赖，再将 Chromium 安装到新建的 `PLAYWRIGHT_BROWSERS_PATH`（`python -m playwright install --with-deps chromium`）。未激活虚拟环境时，设置 `WATT_CI_PYTHON` 指向其 Python，然后在仓库执行 `bash scripts/ci.sh`。需要可用的 Docker 和 Compose。数据库/浏览器服务只使用合成数据、随机名称和临时回环端口，并丢弃部署数据库 URL。脚本负责清理自己创建的临时目录和 Logger 项目/镜像；Server 与 PostgreSQL fixture 清理各自项目、卷和镜像。不接管已有资源，不进行全局 Docker prune。调用者在结束后清理自己另建的虚拟环境、浏览器目录和依赖缓存。

验收需要工作流语法检查、发布 tag 边界测试、默认及显式 CI 测试集、本机原生候选镜像冒烟测试，以及资源清理核对。云端 AMD64/ARM64 构建发布、真实树莓派走查在所有者触发版本后另行记录。

## M5 产物复用与验证状态

检查产物的 producer 固定 Node 24.22.0／npm 11.16.0 和两份前端锁，安装／准备／校验组件，运行前端检查并只构建一次。摘要随上传的产物交付。consumer 验证该可信摘要及文件／锁哈希，再将相同字节暂存进 Python 包及原生 Server 镜像；各架构不单独重建前端。发布矩阵仍在原生 AMD64、ARM64 上覆盖 Logger 和 Server；Server smoke 在无 Node、无网络条件下验证打包前端。

M5 本地完整 CI、合成 Compose、四周分析性能及两类 AMD64 离线镜像 smoke 已通过。所有者于 2026-10-22 验收 M5、冻结已审查候选，并授权推送分支、合并 main，在远端 CI 通过后发布正式 `0.1.0`。任何 CI 失败均停止并报告。原生 AMD64／ARM64 的 Logger 与 Server smoke 仍是发布 workflow 的必需门槛；待执行不能标为通过。上文 M4 历史发布结果保持独立。精确测量、所有者验收及证据限制见 [M5 验收记录](milestone/m5_zh.md#所有者最终验收与发版交接2026-10-22)；自动已知 schema 迁移及所有者备份／恢复边界见 [Server 升级说明](../README_zh.md#m5-分析工作台与升级)。
