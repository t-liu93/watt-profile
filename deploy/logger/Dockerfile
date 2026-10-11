FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock setuptools==80.10.2 wheel==0.48.0
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && groupadd --gid 10001 watt \
    && useradd --uid 10001 --gid 10001 --no-create-home watt
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "watt_profile.cli.logger"]
