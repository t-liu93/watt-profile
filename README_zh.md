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
