FROM python:3.12-alpine

LABEL maintainer="ovh-bot"
LABEL description="OVH 服务器抢购 Bot - TG Bot 自动下单锁定订单"

WORKDIR /app

# 安装时区数据
RUN apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone

ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1

# 数据持久化目录
ENV OVH_BOT_DATA_DIR=/app/data

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制脚本
COPY bot.py .
COPY monitor.py .
COPY config.example.toml .

# 数据目录
RUN addgroup -S app && \
    adduser -S app -G app && \
    mkdir -p /app/data && \
    chown -R app:app /app

# 环境变量（可在 docker-compose 或 run 时覆盖）
ENV OVH_ENDPOINT=ovh-eu
ENV OVH_APPLICATION_KEY=""
ENV OVH_APPLICATION_SECRET=""
ENV OVH_CONSUMER_KEY=""
ENV OVH_ZONE=IE
ENV TG_BOT_TOKEN=""
ENV TG_ALLOWED_USERS=""
ENV TG_ALLOW_ALL_USERS=false
ENV TG_CHAT_ID=""

# 配置文件挂载点
VOLUME ["/app/data"]

USER app

# 默认启动 Bot 模式
CMD ["python3", "bot.py"]
