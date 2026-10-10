# Watt Profile

> [English](README.md) · **中文**

Watt Profile P1 协议和数据契约的独立 Python 包。

## 开发环境

使用 Python 3.12，并在仓库外创建唯一的隔离虚拟环境：

```bash
M1_VENV="$(mktemp -d /tmp/watt-profile-m1-venv.XXXXXX)"
python3.12 -m venv "$M1_VENV"
"$M1_VENV/bin/python" -m pip install -r requirements-dev.lock
"$M1_VENV/bin/python" -m pip install --no-build-isolation --no-deps -e .
"$M1_VENV/bin/python" -m pytest tests/test_package.py -q
"$M1_VENV/bin/python" -m ruff check .
"$M1_VENV/bin/python" -m ruff format --check .
```

使用 pip-tools 重新生成开发依赖锁文件：

```bash
M1_PIP_CACHE="$(mktemp -d /tmp/watt-profile-pip-tools-cache.XXXXXX)"
PIP_TOOLS_CACHE_DIR="$M1_PIP_CACHE" "$M1_VENV/bin/python" -m piptools compile --extra dev --allow-unsafe --resolver=backtracking --output-file requirements-dev.lock pyproject.toml
```

保留环境供 M1-08 使用，之后只清理本 shell 创建的目录：

```bash
rm -rf -- "$M1_VENV" "$M1_PIP_CACHE"
```
