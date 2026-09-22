# ============================================================================
#  v3.0 容器镜像（顺序队列 / 定时批量用）
#
#  ⚠️ 本文件在本仓库的 CI 里**没有被构建验证过**（GitHub Actions 那三个 job 只跑
#  ruff / pyright / 离线测试 + 一个本机真实浏览器的 E2E）。它的定位是"给需要的人
#  一个起点"，而不是"开箱即用的部署物"。真要纳入 CI，得先加一个有 docker 的 runner。
#
#  跑法（profile 目录挂出去，跨批次复用登录态）：
#      docker build -t wjx-autofill .
#      docker run --rm -v "$PWD/data:/app/data" -v "$PWD/profiles:/profiles" wjx-autofill \
#          wjx-fill --url-file /app/urls.txt --browser chrome --headless --profile-dir /profiles/p1
#
#  容器里没有可交互的窗口 —— 智能验证一出现，本轮就判失败（见
#  src/verification.py 的无头分支），所以队列跑完请检查 UNKNOWN/FAIL 的比例。
# ============================================================================
FROM python:3.12-slim

# chromium + 对应 chromedriver 用 Debian 自带的版本，避免 Selenium Manager
# 在容器里再去联网猜驱动版本（网络受限时会直接失败）
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium chromium-driver fonts-wqy-zenhei \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    WJX_CHROME_BINARY=/usr/bin/chromium \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./
RUN pip install --no-cache-dir --no-deps .

# 非 root 运行：/app/data 与挂载的 profile 目录都要可写
RUN useradd -m -u 10001 wjx && mkdir -p /app/data /profiles && chown -R wjx /app /profiles
USER wjx

ENTRYPOINT ["wjx-fill"]
CMD ["--help"]
