# ---------- 基础镜像 ----------
FROM python:3.11-slim
LABEL maintainer="alex"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ---------- 系统依赖 ----------
RUN apt-get update && apt-get install -y \
        curl socat procps iproute2 \
    && rm -rf /var/lib/apt/lists/*

# ---------- Python 依赖 ----------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- 复制项目代码 ----------
COPY . .

# ---------- Echo Socket 服务器脚本 ----------
RUN printf '%s\n' \
'#!/bin/sh' \
'set -e' \
'rm -f /tmp/echo.sock' \
'exec /usr/bin/socat -d -d \\' \
'  UNIX-LISTEN:/tmp/echo.sock,mode=666,reuseaddr,fork \\' \
"  EXEC:'/bin/cat'" \
> /app/echo_server.sh && chmod +x /app/echo_server.sh

# ---------- 应用启动脚本 ----------
RUN printf '%s\n' \
'#!/bin/sh' \
'set -e' \
'/app/echo_server.sh &' \
'exec uvicorn app.api_server:app --host 0.0.0.0 --port 8000' \
> /app/start.sh \
&& chmod +x /app/start.sh

# ---------- 对外端口 ----------
EXPOSE 8000

# ---------- 容器入口 ----------
CMD ["/app/start.sh"]
    