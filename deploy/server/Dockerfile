FROM python:3.12.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock setuptools==80.10.2 wheel==0.48.0
COPY pyproject.toml README.md ./
COPY src ./src
ARG WATT_FRONTEND_DIGEST
# Refuse incomplete or modified checked artifacts before installing the wheel.
RUN PYTHONPATH=src python -c "import os; from watt_profile.server.frontend_artifact import verify; verify('src/watt_profile/server/static/frontend', os.environ.get('WATT_FRONTEND_DIGEST', ''))"
RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && groupadd --gid 10001 watt \
    && useradd --uid 10001 --gid 10001 --no-create-home watt
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "watt_profile.cli.server"]
