#!/bin/bash
# OVH Bot Docker 一键部署脚本

set -e

echo "🔧 构建 Docker 镜像..."
docker build -t ovh-bot:latest .

echo ""
echo "🚀 启动容器..."
docker rm -f ovh-bot 2>/dev/null || true

docker run -d --name ovh-bot --restart unless-stopped \
  -e OVH_ENDPOINT=ovh-eu \
  -e OVH_APPLICATION_KEY=c5449be2854c6c80 \
  -e OVH_APPLICATION_SECRET=0cf48df891c611f633bac800d515320d \
  -e OVH_CONSUMER_KEY=70c714e838518d456e1d14cc7cf6c7c8 \
  -e OVH_ZONE=IE \
  -e TG_BOT_TOKEN=8771867858:AAGuonJsnsf9bcS_NV_LRErWPNUl8_wKBp8 \
  -e TG_ALLOWED_USERS=5113786725 \
  -e TG_CHAT_ID=5113786725 \
  ovh-bot:latest

echo ""
echo "📋 等待启动..."
sleep 3

echo ""
echo "📜 启动日志:"
docker logs ovh-bot 2>&1

echo ""
echo "✅ 部署完成！"
echo "   查看日志: docker logs -f ovh-bot"
echo "   重启:     docker restart ovh-bot"
echo "   停止:     docker stop ovh-bot"
