---
name: deploying-sing-box-vps
description: Use when installing, configuring, deploying, or troubleshooting a sing-box server on a VPS (Shadowsocks + ShadowTLS, Hysteria2 with port hopping, VLESS-XTLS-Vision), including certificate discovery, firewall/iptables checks, systemd service setup, and Shadowrocket QR codes. 触发词：VPS 安装 sing-box、sing-box 服务端、三合一入站、ShadowTLS、hysteria2 端口跳跃、VLESS Vision、证书、iptables、systemd、小火箭二维码。
---

# Deploying sing-box on a VPS

## 概览

在 VPS 上安装最新版 sing-box，同时配置三种入站：Shadowsocks + ShadowTLS、Hysteria2（端口跳跃）、VLESS-XTLS-rprx-Vision，托管为 systemd 服务，并一次产出三种 iOS Shadowrocket 二维码/链接（默认全部生成，不询问用户）。

核心原则：

- **VPS 是唯一外网出口。** 若本机访问 GitHub / sing-box 文档受限，所有下载、文档抓取、DNS 查询都在 VPS 上执行，不要在本机 fetch。
- **执行位置自适应。** 支持两种模式：agent 在本机（经 SSH 操作 VPS，`SB_MODE=remote`）或 agent 直接跑在 VPS 上（`SB_MODE=local`）。第 0 步自动判定，之后**一律**走 `on_vps` / `push` / `fetch` 包装，正文不再出现裸 `ssh` / `scp`。
- **版本优先。** sing-box 配置语法随版本变化。先确认已安装版本并读 migration 文档，再写配置；`sing-box check` 不通过不得重启服务。
- **凭据只在 VPS 生成。** 不在 skill、聊天、仓库里写死 IP / 密码 / UUID；生成后存 `/etc/sing-box/secrets.env`（权限 600）。
- **先备份再改。** 已有配置先 `cp -a /etc/sing-box /etc/sing-box.bak.$(date +%s)`。

## 变量

| 变量 | 说明 | 示例 |
|---|---|---|
| `VPS_HOST` | VPS 公网 IP 或域名 | `203.0.113.10` |
| `SSH_USER` | SSH 用户 | `root` |
| `SS_PORT` | ShadowTLS 公网 TCP 端口（SS 不单独暴露） | `8443` |
| `SS_LOCAL_PORT` | SS 本机回环端口 | `1080` |
| `HY2_PORT` | Hysteria2 UDP 监听端口 | `2096` |
| `HY2_HOP_RANGE` | 端口跳跃范围（UDP） | `20000:30000` |
| `VLESS_PORT` | VLESS TCP 端口 | `8444` |
| `HANDSHAKE_SNI` | 握手伪装站，**ShadowTLS 与 REALITY 共用**；**部署时从验证过的候选池随机选**（见 3b），要求 TLS 1.3 + X25519 + ALPN h2 + 证书 SAN 匹配 + VPS 可达 | `www.samsung.com` |
| `TLS_SNI` | **仅 Hysteria2** 的证书名与 `server_name`；有真实域名证书就填域名，全自签时填 `$VPS_HOST` | `example.com` |

在 bash 会话中先导出（示例值，按实际替换）：

```bash
export VPS_HOST=203.0.113.10 SSH_USER=root \
  SS_PORT=8443 SS_LOCAL_PORT=1080 HY2_PORT=2096 HY2_HOP_RANGE=20000:30000 \
  VLESS_PORT=8444 HANDSHAKE_SNI=www.microsoft.com TLS_SNI=example.com
```

## 部署流程

### 0. 判定执行位置，定义执行适配

两种模式跑同一套命令，只差一层外壳。先判定一次，后续步骤全部使用包装函数。

```bash
detect_sb_mode() {
  [ -n "${SB_MODE:-}" ] && { printf '%s' "$SB_MODE"; return; }
  # ① VPS_HOST 就是本机某块网卡的地址 → 本地
  hostname -I 2>/dev/null | tr ' ' '\n' | grep -qxF -- "$VPS_HOST" && { printf local; return; }
  ip -4 addr show 2>/dev/null | grep -qwF -- "inet $VPS_HOST" && { printf local; return; }
  # ② 本机公网出口 IP 等于 VPS_HOST → 本地
  local ip4
  ip4=$(curl -4 -fsS --max-time 5 https://ifconfig.me 2>/dev/null \
        || curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)
  [ -n "$ip4" ] && [ "$ip4" = "$VPS_HOST" ] && { printf local; return; }
  printf remote
}

SB_MODE=${SB_MODE:-$(detect_sb_mode)}
export SB_MODE
echo "SB_MODE=$SB_MODE  VPS_HOST=$VPS_HOST"

# 在 VPS 上执行一段命令；stdin 透传，heredoc 写法可用
on_vps() {
  if [ "$SB_MODE" = local ]; then bash -c "$1"; else ssh "${SSH_USER:-root}@$VPS_HOST" "$1"; fi
}
# 本机 → VPS 传文件
push() {
  if [ "$SB_MODE" = local ]; then
    [ "$(readlink -f "$1")" = "$(readlink -f "$2" 2>/dev/null)" ] || cp -f "$1" "$2"
  else
    scp "$1" "${SSH_USER:-root}@$VPS_HOST:$2"
  fi
}
# VPS → 本机取文件
fetch() {
  if [ "$SB_MODE" = local ]; then cp -f "$1" "$2"; else scp "${SSH_USER:-root}@$VPS_HOST:$1" "$2"; fi
}
```

要点：

- 判定不准或想强制指定：先 `export SB_MODE=local`（或 `remote`）再执行第 0 步，自动判定会被跳过。
- 包装函数必须与变量 `export` 在**同一个 shell 会话**里生效；新开终端要重跑第 0 步。
- 两种模式下 `VPS_HOST` **都必须是公网 IP**——它要写进二维码和客户端链接。
- `on_vps 'bash -s' <<'EOF'` 两种模式都成立：本地模式变成 `bash -c 'bash -s'`，heredoc 照常从 stdin 进入。

### 1. 探测系统与现有安装

```bash
on_vps '
  uname -m; . /etc/os-release && echo "$ID $VERSION_ID"
  for c in systemctl iptables nft ufw qrencode sing-box python3; do
    printf "%-12s" "$c"; command -v "$c" || echo "-"
  done
  ss -lntup 2>/dev/null | head -40
'
```

已有 sing-box：先备份并停止服务。

```bash
on_vps '
  systemctl stop sing-box 2>/dev/null
  [ -d /etc/sing-box ] && cp -a /etc/sing-box /etc/sing-box.bak.$(date +%s)
  true
'
```

### 2. 安装最新版

所有命令在 VPS 上执行。正确发布页是 `/releases`（`/release` 是 404）。

```bash
on_vps 'bash -s' <<'EOF'
set -e
LATEST=$(curl -fsSL https://api.github.com/repos/SagerNet/sing-box/releases/latest \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')
[ -n "$LATEST" ] || { echo "无法获取最新版本"; exit 1; }
case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "不支持的架构: $(uname -m)"; exit 1 ;;
esac
cd /tmp
curl -fLO "https://github.com/SagerNet/sing-box/releases/download/v${LATEST}/sing-box-${LATEST}-linux-${ARCH}.tar.gz"
tar -xzf "sing-box-${LATEST}-linux-${ARCH}.tar.gz"
install -m 755 "sing-box-${LATEST}-linux-${ARCH}/sing-box" /usr/local/bin/sing-box
sing-box version
command -v qrencode >/dev/null || { apt-get update -qq && apt-get install -y qrencode || dnf install -y qrencode; }
command -v zbarimg >/dev/null || { apt-get update -qq && apt-get install -y zbar-tools || dnf install -y zbar; }
EOF
```

### 3. 确认当前版本语法

在 VPS 上读文档，不要凭记忆写配置：

```bash
on_vps '
  curl -fsSL https://sing-box.sagernet.org/migration/ -o /tmp/sb-migration.html
  curl -fsSL https://sing-box.sagernet.org/configuration/ -o /tmp/sb-config.html
  grep -oE "1\.[0-9]+" /tmp/sb-migration.html | sort -u | tail
'
```

确认每个入站字段在当前版本仍有效（尤其 `shadowtls.detour`、`log.disabled`），并确认顶层 `ntp` 字段（1.14.x 为 `enabled/server/server_port/interval`）。旧教程的配置不能直接照抄。

### 3b. 选握手站（随机化）

握手站不要写死成某个固定站点（如 `www.microsoft.com`），否则一旦它下线、换 TLS 参数或被针对性封锁，VLESS 与 SS 两个入站会同时挂。改成从候选池里**随机挑一个验证通过的**：

```bash
push scripts/pick-sni.sh /root/pick-sni.sh
on_vps 'chmod +x /root/pick-sni.sh && /root/pick-sni.sh --list'
```

脚本对每个候选实发 TLS 握手，四个条件全过才算合格：TLS 1.3、X25519、ALPN `h2`、证书链可验证且 SAN 覆盖该域名。输出按握手耗时升序。

挑一个并固化下来（默认已排除当年的 stare 站点）：

```bash
on_vps '/root/pick-sni.sh --pick | tee /root/picked-sni.txt'
export HANDSHAKE_SNI=$(on_vps 'cat /root/picked-sni.txt')
echo "HANDSHAKE_SNI=$HANDSHAKE_SNI"
```

跑到部署完成后，把选定的站点钉在 `/etc/sing-box/handshake-sni`（600），后续重发二维码、换证书、写文档都从这里读，避免配置与链接不一致。

必须说清的边界：

- **“随机化”是部署粒度的**。REALITY 的客户端要把 `sni` 写死在链接里，一台机器一个入站只能对应一个握手站，做不到每连接随机。
- 多机部署时每台选不同站点，才是真正的分散；同一台机器要轮换，就得改配置重启并**重发链接**。
- 候选池只要“大厂 + 全球直连 + 不易被整站封锁”的站，不要放 Cloudflare 前沿的域名（会归到共享 IP，反而容易被识别）。

### 4. 证书

**只有 Hysteria2 需要自己的证书。** VLESS 走 REALITY，和 ShadowTLS 一样借用 `HANDSHAKE_SNI` 握手站的**真证书链**，不需要本机有证书；ShadowTLS 也只做握手伪装。所以本节全程只为 Hysteria2 服务。

先在本机搜现成**合法**证书，搜到用现成的，搜不到才自签。

```bash
on_vps '
  find /etc /root /opt /usr/local -maxdepth 5 -type f \
    \( -name "fullchain.pem" -o -name "cert.pem" -o -name "*.crt" \) 2>/dev/null
'
```

对每个候选用 openssl 验证有效期与 SAN：

```bash
openssl x509 -in "$CERT" -noout -subject -issuer -dates -ext subjectAltName
openssl x509 -in "$CERT" -noout -checkend 2592000 || echo "30 天内过期"
```

域名证书必须同时满足：

```bash
dig +short A "$TLS_SNI"
curl -4 -fsS https://ifconfig.me; echo
```

DNS 解析必须等于 VPS 公网 IP，且 SAN 覆盖 `$TLS_SNI`；否则视为不可用，改用自签（或先签好证书）。

复用现成证书时，优先直接指向续期系统的 live 路径，并加续期钩子（sing-box 只在启动时读证书）：

```bash
# certbot: /etc/letsencrypt/renewal-hooks/deploy/10-sing-box.sh
# 内容：把新证书 install 到 /etc/sing-box/ 后 systemctl restart sing-box
```

自签（EC P-256，SAN 同时覆盖域名与 IP）。没有域名时把 `TLS_SNI` 设成 `$VPS_HOST`，SAN 只留 IP：

```bash
on_vps "
  install -d -m 700 /etc/sing-box
  openssl ecparam -genkey -name prime256v1 -out /etc/sing-box/priv.key
  openssl req -new -x509 -days 3650 -key /etc/sing-box/priv.key \
    -out /etc/sing-box/cert.pem -subj '/CN=$TLS_SNI' \
    -addext 'subjectAltName=DNS:$TLS_SNI,IP:$VPS_HOST'
  chmod 600 /etc/sing-box/priv.key
"
```

自签只影响 Hysteria2 客户端（`insecure=1` / `allowInsecure`），**不影响 VLESS**——REALITY 校验的是握手站的真证书，与本地自签无关。

### 5. 生成凭据

```bash
on_vps 'bash -s' <<'EOF'
set -e
umask 077
SS_METHOD=2022-blake3-aes-128-gcm
SS_PASSWORD=$(sing-box generate rand --base64 16)   # aes-128-gcm 用 16 字节；aes-256-gcm 用 32
ST_PASSWORD=$(sing-box generate rand --hex 16)
HY2_PASSWORD=$(sing-box generate rand --hex 16)
VLESS_UUID=$(sing-box generate uuid)
# REALITY：服务端只要私钥；公钥交给客户端（二维码链接里的 pbk）
REALITY_KEYS=$(sing-box generate reality-keypair)
REALITY_PRIVATE_KEY=$(echo "$REALITY_KEYS" | awk '/PrivateKey/{print $NF}')
REALITY_PUBLIC_KEY=$(echo "$REALITY_KEYS" | awk '/PublicKey/{print $NF}')
# 必须 --hex 8（16 个十六进制字符）；超过 16 字符会让 sing-box 直接 panic
REALITY_SHORT_ID=$(sing-box generate rand 8 --hex)
cat > /etc/sing-box/secrets.env <<EOT
SS_METHOD=$SS_METHOD
SS_PASSWORD=$SS_PASSWORD
ST_PASSWORD=$ST_PASSWORD
HY2_PASSWORD=$HY2_PASSWORD
VLESS_UUID=$VLESS_UUID
REALITY_PRIVATE_KEY=$REALITY_PRIVATE_KEY
REALITY_PUBLIC_KEY=$REALITY_PUBLIC_KEY
REALITY_SHORT_ID=$REALITY_SHORT_ID
EOT
chmod 600 /etc/sing-box/secrets.env
EOF
```

注意：

- SS2022 密码本身的 `=` 填充是密钥的一部分，**不要**删；只有生成二维码时对 URL 里的 Base64 去填充（脚本已处理）。
- `REALITY_SHORT_ID` 必须是 ≤ 8 字节（16 个十六进制字符）。实测 1.14.3 上超过会直接 `panic: index out of range`，不是报错。

### 6. 写配置

把占位符替换为实际值后写入 `/etc/sing-box/config.json`。字段若与当前版本文档不符，以文档为准。

```json
{
  "log": { "disabled": true },
  "ntp": {
    "enabled": true,
    "server": "time.apple.com",
    "server_port": 123,
    "interval": "10m"
  },
  "inbounds": [
    {
      "type": "shadowsocks",
      "tag": "ss-in",
      "listen": "127.0.0.1",
      "listen_port": <SS_LOCAL_PORT>,
      "method": "2022-blake3-aes-128-gcm",
      "password": "<SS_PASSWORD>"
    },
    {
      "type": "shadowtls",
      "tag": "shadowtls-in",
      "listen": "::",
      "listen_port": <SS_PORT>,
      "version": 3,
      "users": [{ "name": "user", "password": "<ST_PASSWORD>" }],
      "handshake": { "server": "<HANDSHAKE_SNI>", "server_port": 443 },
      "detour": "ss-in"
    },
    {
      "type": "hysteria2",
      "tag": "hy2-in",
      "listen": "::",
      "listen_port": <HY2_PORT>,
      "users": [{ "name": "user", "password": "<HY2_PASSWORD>" }],
      "tls": {
        "enabled": true,
        "server_name": "<TLS_SNI>",
        "certificate_path": "/etc/sing-box/cert.pem",
        "key_path": "/etc/sing-box/priv.key",
        "alpn": ["h3"]
      }
    },
    {
      "type": "vless",
      "tag": "vless-in",
      "listen": "::",
      "listen_port": <VLESS_PORT>,
      "users": [{ "name": "user", "uuid": "<VLESS_UUID>", "flow": "xtls-rprx-vision" }],
      "tls": {
        "enabled": true,
        "server_name": "<HANDSHAKE_SNI>",
        "reality": {
          "enabled": true,
          "handshake": { "server": "<HANDSHAKE_SNI>", "server_port": 443 },
          "private_key": "<REALITY_PRIVATE_KEY>",
          "short_id": ["<REALITY_SHORT_ID>"]
        }
      }
    }
  ],
  "outbounds": [{ "type": "direct", "tag": "direct" }]
}
```

要点：

- Shadowsocks 只监听 `127.0.0.1`，公网只暴露 ShadowTLS 端口。
- **VLESS 用 REALITY：没有 `certificate_path` / `key_path`**，只写 `reality.handshake` + `private_key` + `short_id`；证书是握手站的真证书，不需要本机有证。
- **REALITY 的 `server_name` 必须等于 `handshake.server`**，且该名字在该站证书 SAN 里；`short_id` 服务端是**数组**，客户端是字符串。
- 客户端要 `utls` 指纹（小火箭链接里 `fp=chrome`）+ `pbk=<公钥>` + `sid=<short_id>`，**不用** `allowInsecure`。
- `HANDSHAKE_SNI` 同时服务 ShadowTLS 握手与 REALITY；**用 3b 随机选出的值**，不要回退成固定的 `www.microsoft.com`。
- `TLS_SNI` **只给 Hysteria2 用**（它必须有自己证书：现成合法证书，没有才自签）。
- ShadowTLS `version` 是数字 `3`；`detour` 指向 SS 入站（官方文档已不列这个词，但 1.14.3 实测仍有效）。
- Hysteria2 必须启用 TLS；端口跳跃只在客户端配置区间，服务端靠第 8 步的 NAT 转发覆盖整个区间。
- VLESS 的 `flow` 写在 user 上，不是 inbound 上。
- **不要开 `multiplex`**，尤其不要 `"padding": true`：开了 padding 服务端会拒掉未 padding 的连接，客户端必须同步开，小火箭又不支持 sing-mux，等于给自己上锁。
- `log.disabled` 若当前版本不支持，用 `"level": "fatal"` 替代（以 migration 文档为准）。
- 顶层 `ntp` 让 sing-box 自己校时，避免 VPS 时钟漂移干扰 TLS 握手/证书校验；`time.apple.com:123` 必须能从 VPS 访问（UDP 123）。REALITY 对时间跳变敏感，这一项不建议删。

CLI 无法直接读取 secrets.env 插值 JSON，用 Python 渲染（避免 heredoc 引号问题）：

```bash
on_vps 'bash -s' <<'EOF'
set -euo pipefail
umask 077
set -a; . /etc/sing-box/secrets.env; set +a
# 端口 / SNI 按变量表替换
export SS_LOCAL_PORT=1080 SS_PORT=8443 HY2_PORT=2096 VLESS_PORT=8444
export TLS_SNI=example.com HANDSHAKE_SNI=www.microsoft.com

python3 - <<'PY'
import json, os
e = os.environ
cfg = {
  "log": {"disabled": True},
  "ntp": {"enabled": True, "server": "time.apple.com",
          "server_port": 123, "interval": "10m"},
  "inbounds": [
    {"type": "shadowsocks", "tag": "ss-in", "listen": "127.0.0.1",
     "listen_port": int(e["SS_LOCAL_PORT"]), "method": e["SS_METHOD"],
     "password": e["SS_PASSWORD"]},
    {"type": "shadowtls", "tag": "shadowtls-in", "listen": "::",
     "listen_port": int(e["SS_PORT"]), "version": 3,
     "users": [{"name": "user", "password": e["ST_PASSWORD"]}],
     "handshake": {"server": e["HANDSHAKE_SNI"], "server_port": 443},
     "detour": "ss-in"},
    {"type": "hysteria2", "tag": "hy2-in", "listen": "::",
     "listen_port": int(e["HY2_PORT"]),
     "users": [{"name": "user", "password": e["HY2_PASSWORD"]}],
     "tls": {"enabled": True, "server_name": e["TLS_SNI"],
             "certificate_path": "/etc/sing-box/cert.pem",
             "key_path": "/etc/sing-box/priv.key", "alpn": ["h3"]}},
    {"type": "vless", "tag": "vless-in", "listen": "::",
     "listen_port": int(e["VLESS_PORT"]),
     "users": [{"name": "user", "uuid": e["VLESS_UUID"],
                "flow": "xtls-rprx-vision"}],
     "tls": {"enabled": True, "server_name": e["HANDSHAKE_SNI"],
             "reality": {"enabled": True,
                         "handshake": {"server": e["HANDSHAKE_SNI"],
                                       "server_port": 443},
                         "private_key": e["REALITY_PRIVATE_KEY"],
                         "short_id": [e["REALITY_SHORT_ID"]]}}}
  ],
  "outbounds": [{"type": "direct", "tag": "direct"}]
}
with open("/etc/sing-box/config.json", "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
chmod 600 /etc/sing-box/config.json
EOF
```

### 7. 校验并配置 systemd

```bash
on_vps '
  sing-box check -c /etc/sing-box/config.json
  cat > /etc/systemd/system/sing-box.service <<EOF
[Unit]
Description=sing-box service
After=network-online.target nss-lookup.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
Restart=on-failure
RestartSec=5s
LimitNOFILE=infinity

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now sing-box
  systemctl --no-pager --full status sing-box
  ss -lntup | grep sing-box
'
```

### 8. 防火墙与端口跳跃

先看现状，再放行，最后持久化。若 `nat PREROUTING` 里已有 Docker 规则，跳跃 REDIRECT 必须插到链首（`iptables -t nat -I PREROUTING 1 ...`），否则会被 Docker 规则先吃掉；`ufw` / `firewalld` 处于 active 时优先用它们放行，不要混用裸 iptables。

```bash
on_vps '
  iptables -S
  iptables -t nat -S
  nft list ruleset 2>/dev/null | head -80
  ufw status verbose 2>/dev/null
'
```

```bash
on_vps "
  iptables -I INPUT -p tcp --dport $SS_PORT -j ACCEPT
  iptables -I INPUT -p tcp --dport $VLESS_PORT -j ACCEPT
  iptables -I INPUT -p udp --dport $HY2_PORT -j ACCEPT
  iptables -t nat -C PREROUTING -p udp --dport $HY2_HOP_RANGE -j REDIRECT --to-ports $HY2_PORT 2>/dev/null \
    || iptables -t nat -A PREROUTING -p udp --dport $HY2_HOP_RANGE -j REDIRECT --to-ports $HY2_PORT
  iptables -I INPUT -p udp --dport $HY2_HOP_RANGE -j ACCEPT
"
```

持久化按发行版选择（在 VPS 上执行）：

```bash
on_vps "
  command -v netfilter-persistent >/dev/null && netfilter-persistent save
  command -v iptables-save >/dev/null && mkdir -p /etc/iptables && iptables-save > /etc/iptables/rules.v4
  command -v ufw >/dev/null && ufw allow ${SS_PORT}/tcp && ufw allow ${VLESS_PORT}/tcp && ufw allow ${HY2_PORT}/udp
  command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1 && {
    firewall-cmd --permanent --add-port=${SS_PORT}/tcp
    firewall-cmd --permanent --add-port=${VLESS_PORT}/tcp
    firewall-cmd --permanent --add-port=${HY2_PORT}/udp
    firewall-cmd --permanent --add-port=${HY2_HOP_RANGE//:/-}/udp
    firewall-cmd --reload
  }
"
```

不要忘记云厂商安全组：本机无法检查，必须让用户在控制台放行同样的 TCP/UDP 端口与跳跃区间。

### 9. 生成三种 Shadowrocket 二维码（默认全部）

**REQUIRED：一次生成 SS、Hysteria2、VLESS 三张二维码，并在终端窗口里直接打印出来。** `--protocols` 默认就是 `ss,hy2,vless`，不要只生成 SS 后再询问用户是否需要另外两种；只有明确调试单一协议时才传子集。

交付时**先让用户看到二维码**：命令里带 `--utf8`，脚本会在落盘 PNG 的同时把三张二维码直接打到当前窗口，用户抬头就能扫，不用找文件。

把 `scripts/make-qr.py` 拷到 VPS 后执行（`push` 在本地模式下只是同一个磁盘内的拷贝）：

```bash
push scripts/make-qr.py /root/make-qr.py

on_vps \
  "VPS_HOST=$VPS_HOST SS_PORT=$SS_PORT HY2_PORT=$HY2_PORT HY2_HOP_RANGE=$HY2_HOP_RANGE VLESS_PORT=$VLESS_PORT TLS_SNI=$TLS_SNI bash -s" <<'EOF'
set -euo pipefail
set -a; . /etc/sing-box/secrets.env; set +a
HANDSHAKE_SNI=$(cat /etc/sing-box/handshake-sni)   # 第 3b 步固化的握手站
python3 /root/make-qr.py \
  --server "$VPS_HOST" \
  --ss-sni "$HANDSHAKE_SNI" --tls-sni "$TLS_SNI" \
  --ss-port "$SS_PORT" --ss-cipher "$SS_METHOD" \
  --ss-password "$SS_PASSWORD" --st-password "$ST_PASSWORD" \
  --hy2-port "$HY2_PORT" --hy2-password "$HY2_PASSWORD" \
  --hy2-hop-range "$HY2_HOP_RANGE" \
  --vless-port "$VLESS_PORT" --vless-uuid "$VLESS_UUID" \
  --vless-sni "$HANDSHAKE_SNI" \
  --vless-reality-pbk "$REALITY_PUBLIC_KEY" --vless-reality-sid "$REALITY_SHORT_ID" \
  --out-dir /root --name-prefix VPS \
  --utf8
EOF
```

脚本会：

- 默认生成 `ss-qr.png`、`hy2-qr.png`、`vless-qr.png` 三张 PNG；
- **带 `--utf8` 时同时在终端打印三张二维码（默认交付方式）；**
- 写 `ss-link.txt`、`hy2-link.txt`、`vless-link.txt`（600）；
- 自检每种链接字段；安装了 `zbarimg` 时回读每张二维码并与链接逐字节比对；
- 默认只打印长度与路径，不打印含密码的链接；需要文本链接时加 `--show-links`。

取回三张 PNG（`SB_MODE=local` 时文件已在 `/root`，`fetch` 只是拷到你当前目录）：

```bash
fetch /root/ss-qr.png ./ss-qr.png
fetch /root/hy2-qr.png ./hy2-qr.png
fetch /root/vless-qr.png ./vless-qr.png
```

SS 默认保持小火箭原生导出形态（不带 `&udp=0`）；如需强制关闭 UDP，加 `--ss-udp-disable`。有受信任证书时加 `--secure-tls`，去掉 `insecure` / `allowInsecure`（只影响 Hysteria2 与回退的普通 TLS 形态，REALITY 无关）。`--name-prefix` 只改链接末尾的备注名，**输出文件名恒为** `ss-qr.png` / `hy2-qr.png` / `vless-qr.png`。**优先让用户扫窗口里打印的二维码**；若终端把块字符替换掉了（看着正常却扫不出），改用 PNG。链接含密码，**不要贴到公开场合**。格式细节、`SB_MODE` 下的文件位置对照与踩坑见 `references/shadowrocket-qr.md`；本机快速自检：`python3 scripts/make-qr.py --self-test`。

### 10. 验收与回滚

```bash
on_vps '
  systemctl is-active sing-box
  sing-box check -C /etc/sing-box
  timedatectl | grep -E "System clock synchronized|NTP service"
  ss -lntup | grep sing-box
  journalctl -u sing-box -n 30 --no-pager
'
```

`timedatectl` 应显示 `System clock synchronized: yes`；若 NTP 不通，先放行 UDP 123。从外部验证 TCP 端口——**必须在另一台机器上做**，`SB_MODE=local` 时不能在 VPS 自己身上测（回环结论无效）：

```bash
# 端口按变量表替换
nc -vz $VPS_HOST 8443
nc -vz $VPS_HOST 8444
```

UDP（Hysteria2）用客户端实际连接验证；跳跃区间内随机端口都应能连通。另外确认 SS 的本地端口**不能**从外部访问（`nc -vz $VPS_HOST 1080` 必须失败，端口按变量表替换）。

回滚：

```bash
on_vps '
  B=$(ls -1d /etc/sing-box.bak.* 2>/dev/null | sort | tail -1)
  [ -n "$B" ] && cp -a "$B"/. /etc/sing-box/
  systemctl restart sing-box
'
```

## 快速参考

| 入站 | 协议 / 端口 | 二维码 | 客户端关键参数 |
|---|---|---|---|
| ShadowTLS + SS | TCP `SS_PORT`（SS 只在 127.0.0.1） | `ss-qr.png` | ShadowTLS v3、版本 `"3"`（字符串）、`HANDSHAKE_SNI`、方法、两套密码；UDP 建议在 App 内关闭 |
| Hysteria2 | UDP `HY2_PORT` + 跳跃区间 | `hy2-qr.png` | 密码、`TLS_SNI`、`mport`（Shadowrocket）/ `server_ports`（sing-box）、`insecure`（自签时） |
| VLESS Vision | TCP `VLESS_PORT` | `vless-qr.png` | **REALITY**：UUID、`flow=xtls-rprx-vision`、`security=reality`、`sni`=`HANDSHAKE_SNI`、`pbk`（公钥）、`sid`（short_id）、`fp=chrome`；无证书，无 `allowInsecure` |

## 常见坑

| 坑 | 处理 |
|---|---|
| 本机拉 GitHub 失败 | 所有网络操作放 VPS；本机只做 SSH |
| `/release` 404 | 正确是 https://github.com/SagerNet/sing-box/releases |
| 照抄旧版配置 | 先读 `/migration/`，用 `sing-box check` 把关 |
| SS 暴露公网 | SS `listen` 必须是 `127.0.0.1`，只由 ShadowTLS detour 进入 |
| SS2022 密码长度错 | aes-128-gcm = 16 字节 base64；aes-256-gcm = 32 字节 |
| hysteria2 跳跃不通 | 服务端要 iptables NAT REDIRECT + 云安全组放行整个 UDP 区间 |
| 自签证书连不上（仅 Hysteria2） | 客户端开 `insecure=1`，或换成受信任的域名证书 |
| SS2022 密码被去填充 | 密码本身的 `=` 不能删，只有 URL 编码层去填充 |
| 证书续期后失效 | 加 certbot / acme.sh 钩子，续期后 restart sing-box |
| 域名证书误用 | 必须核对 SAN 与 DNS A 记录都指向本机 |
| 日志没关 | `log.disabled: true`；字段随版本变化则以文档为准 |
| VPS 时间漂移 | 顶层配置 `ntp`（`time.apple.com:123`，`interval: 10m`）；确认 VPS 能访问 UDP 123 |
| 只生成 SS 二维码后询问用户 | 默认一次生成三种；`--protocols` 仅在明确调试子集时使用 |
| HY2 把 `20000-30000` 写进 authority | Shadowrocket 用数字主端口 + `mport=20000-30000` |
| VLESS 漏 `security=tls` / `type=tcp` | 扫进去不是 TLS 或传输类型错，连不上 |
| VLESS 走 REALITY 却配了 `certificate_path` | 不需要自己的证书；REALITY 下多写证书反而混淆，只留 `handshake`/`private_key`/`short_id` |
| REALITY 的 `server_name` ≠ `handshake.server` | 握手站证书 SAN 对不上，客户端直接失败 |
| `short_id` 超过 16 个十六进制字符 | 1.14.3 实测直接 `panic: index out of range`，不是报错；用 `generate rand 8 --hex` |
| 开了 `multiplex.padding` | 服务端拒绝未 padding 连接，小火箭不支持 sing-mux，别开 |
| REALITY 握手站不可达/不支持 TLS 1.3 | 换站；用 `scripts/pick-sni.sh --list` 只拿合格的，别手挑 |
| 握手站写死在代码里 | 一旦该站下线或改参数，VLESS 与 SS 同时挂；部署时随机选并记到 `/etc/sing-box/handshake-sni` |
| 换了握手站却没重发链接 | 客户端 `sni` 还是旧值，REALITY 握手失败；改配置后必须重跑第 9 步 |
| 把 Cloudflare 前沿域名当握手站 | 归到共享 IP，伪装效果反而下降；用大厂自有边缘 |
| 只回读一张二维码 | 三张都要 `zbarimg` 回读；脚本默认逐张校验 |
| QR 扫不了或闪退 | 见 `references/shadowrocket-qr.md`，`version` 必须是字符串、Base64 去填充 |
| 在 VPS 上跑却仍按 SSH 写命令 | 第 0 步会先判定 `SB_MODE`；误判时 `export SB_MODE=local` 强制覆盖 |
| 新开终端后 `on_vps: command not found` | `on_vps`/`push`/`fetch` 是 shell 会话级函数，重跑第 0 步 |
| `SB_MODE=local` 时在 VPS 上自测端口 | 回环结论无效，外部可达性必须换一台机器验 |

## 交付物

- `systemctl status sing-box` 正常，开机自启
- 三个端口监听且外部可达
- `/etc/sing-box/secrets.env`（600）与 `/etc/sing-box/config.json`（600）
- 配置含顶层 `ntp`（`time.apple.com:123`，`interval: 10m`），且 `sing-box check` 通过
- 三种二维码：`ss-qr.png`、`hy2-qr.png`、`vless-qr.png`（默认一次生成，必须全部存在）
- 三份链接：`ss-link.txt`、`hy2-link.txt`、`vless-link.txt`（600）
- `/etc/sing-box/handshake-sni`（600）：第 3b 步随机选定的握手站，重发链接时以它为准
- 回滚命令
