# 探海灵眸 Oceanus — 前端镜像（多阶段构建）
#
# 为什么用多阶段：node_modules 有 200MB+，但运行时只需要 dist/ 里的
# 几百 KB 静态文件。单阶段镜像会把整个工具链打进生产环境，既臃肿
# 又扩大攻击面。多阶段后运行镜像只有 nginx:alpine 的 ~10MB。
#
# 两种用法：
#   1. 生产/试点：构建静态产物，由 nginx 托管（本文件的最终阶段）
#   2. 开发：compose 里覆盖 command 为 `npm run dev`
#      —— 开发阶段挂了源码卷，需要完整 node 环境，
#         所以 compose 里显式指定了 --target 或直接跑 node 镜像。

# ======================================================================
# 阶段 1：构建
# ======================================================================
FROM node:22-alpine AS builder

WORKDIR /app

# Vite reads VITE_* values at build time, not container runtime. Pass the
# browser-facing Tencent map proxy through this build argument so production
# images do not silently fall back to the development placeholder.
ARG VITE_TMAP_PROXY_URL=
ENV VITE_TMAP_PROXY_URL=$VITE_TMAP_PROXY_URL

# Subpath deployment, e.g. /seasight/. Keep / for a root-domain deployment.
ARG VITE_BASE_PATH=/
ENV VITE_BASE_PATH=$VITE_BASE_PATH

# 用国内镜像源加速（渔港/校园网环境下 npm 官方源常常超时）
RUN npm config set registry https://registry.npmmirror.com

# ★ 先 COPY 依赖清单再 install —— 利用 Docker 层缓存。
#   只要 package.json 没变，改源码时这层不会重跑，
#   构建时间从两分钟降到几秒。
COPY package.json package-lock.json* ./

# 有 lock 文件用 npm ci（可复现、更快）；没有则退回 npm install
RUN if [ -f package-lock.json ]; then \
        npm ci --no-audit --no-fund; \
    else \
        npm install --no-audit --no-fund; \
    fi

# 再 COPY 源码（.dockerignore 会挡掉 node_modules/dist）
COPY . .

RUN npm run build

# ======================================================================
# 阶段 2：开发环境（compose 开发模式用这个）
#   保留 node_modules 与源码，跑 vite dev server 做热更新
# ======================================================================
FROM node:22-alpine AS dev

WORKDIR /app

RUN npm config set registry https://registry.npmmirror.com

COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then \
        npm ci --no-audit --no-fund; \
    else \
        npm install --no-audit --no-fund; \
    fi

COPY . .

EXPOSE 5173

# --host 0.0.0.0 必须加：默认 vite 只监听 localhost，
# 容器外（宿主机浏览器）访问不到，表现为"端口映射了但连不上"。
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ======================================================================
# 阶段 3：生产运行（默认目标）
#   只搬 dist/ + nginx 配置，运行时不带 node
# ======================================================================
FROM nginx:1.27-alpine AS production

ENV TZ=Asia/Shanghai

# Alpine 官方 CDN 在部分国内云主机上会建立连接后长期无数据。
# 统一替换仓库主机，避免生产镜像卡在 apk 下载阶段。
RUN sed -i 's|dl-cdn.alpinelinux.org|mirrors.aliyun.com|g' /etc/apk/repositories \
    && apk add --no-cache tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

# 静态产物
COPY --from=builder /app/dist /usr/share/nginx/html

# 站点配置：SPA 回退 + API 代理 + 真实 WebSocket 路径 + 同源视频流
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
