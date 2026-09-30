# 探海灵眸 SeaSight — 后端镜像
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 把仓库根固定进 sys.path。
# 少了这一行，`python scripts/xxx.py` 会把 sys.path[0] 设成 /app/scripts（脚本所在目录），
# 于是脚本里的 `from app.xxx import ...` 直接 ModuleNotFoundError: No module named 'app'。
# 表现是 `make bootstrap-demo-users` / `make prod-create-admin` 在容器里必失败。
# 本仓的 scripts/*.py 一律按「仓库根为 import 根」写，所以这里显式声明。
ENV PYTHONPATH=/app

# 系统依赖（psycopg/geo 相关编译工具 + 时区数据）
# libglib2.0-0 / libgomp1 是 opencv-python-headless 的运行时依赖：
#   headless 版不带 GUI（不需要 libGL），但仍链接 glib 与 OpenMP。
#   漏装的表现是 `import cv2` 直接抛 ImportError，
#   于是 /ai/analyze-image 与智能助手的图片链路全线 500。
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libglib2.0-0 \
        libgomp1 \
        curl \
        tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/ready || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
