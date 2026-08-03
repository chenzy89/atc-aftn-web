#!/bin/bash
# ATC 数据网静态路由（开机自启）
# 防止禁用 eno1（外网）后，雷达/语音组播因 rp_filter(loose) 无回程路由被内核丢弃
#
# 背景：雷达源 172.28.13.21、语音源 192.168.9.21 均不在 enx 数据网卡直连网段内，
#       回程路由依赖 eno1（默认路由/直连路由），eno1 down 后路由消失导致组播被丢。
#       这两条静态路由将回程固定走数据网卡 enx0c3d5e61539d。

IFACE="enx0c3d5e61539d"
SRC_IP="192.168.15.32"

# ── 等待数据网卡就绪（最多 60 秒）──
for i in $(seq 1 60); do
    if ip link show "$IFACE" >/dev/null 2>&1 && ip addr show "$IFACE" | grep -q "inet ${SRC_IP}"; then
        break
    fi
    sleep 1
done

if ! ip link show "$IFACE" >/dev/null 2>&1; then
    logger -t atc-static-routes "错误: 网卡 $IFACE 不存在"
    exit 1
fi

# ── 雷达数据源网段 ──
ip route replace 172.28.13.0/24 dev "$IFACE" src "$SRC_IP" 2>/dev/null \
    || ip route add 172.28.13.0/24 dev "$IFACE" src "$SRC_IP"

# ── 语音数据源网段 ──
ip route replace 192.168.9.0/24 dev "$IFACE" src "$SRC_IP" 2>/dev/null \
    || ip route add 192.168.9.0/24 dev "$IFACE" src "$SRC_IP"

logger -t atc-static-routes "静态路由已配置: 172.28.13.0/24 + 192.168.9.0/24 via $IFACE"
exit 0
