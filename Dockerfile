# ============================================================================
#  v3.0 容器镜像（顺序队列 / 定时批量用）
#
#  ⚠️ 定位是"给需要的人一个起点"，不是"开箱即用的部署物"。
#  构建由 .github/workflows/docker-smoke.yml 每周一（+ 手动）跑一次：build →
#  `wjx-fill --help` → `import src.*`。**不在 push 的必填检查里**，所以一次
#  改坏 Dockerfile 仍可能先绿着合进去、到下周一定时任务才红。
#
#  跑法（profile 目录挂出去，跨批次复用登录态；urls.txt 是白名单外的本地
#  文件，必须自己挂进去 —— 镜像里没有它）：
#      docker build -t wjx-autofill .
#      docker run --rm -v "$PWD/data:/app/data" -v "$PWD/profiles:/profiles" \
#          -v "$PWD/urls.txt:/app/urls.txt" wjx-autofill \
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
COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir -r requirements.txt

# v4.2 全面审查 P1-8：复制**白名单**取代 COPY . . —— 构建目录里允许存在的
# 私密产物（configs/ 本地预设、根目录 history.db、导出 CSV；.gitignore 自己
# 都承认这些类别，而 Docker 的 COPY 不看 Git 规则）从此在结构上进不了镜像。
# pyproject 声明 readme=README.md，不打这个文件 wheel 构建直接失败；包清单
# 见 pyproject [tool.setuptools] packages（src / src.browser / src.interactions /
# src.pipeline_stages / webui）—— 新增子包时这里要跟着加 COPY。
COPY src/ ./src/
COPY webui/ ./webui/
RUN pip install --no-cache-dir --no-deps .

# 非 root 运行：/app/data 与挂载的 profile 目录都要可写
RUN useradd -m -u 10001 wjx && mkdir -p /app/data /profiles && chown -R wjx /app /profiles
USER wjx

ENTRYPOINT ["wjx-fill"]
CMD ["--help"]
