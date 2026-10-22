# Shadowrocket 三种协议二维码/链接生成

> SS 格式来源：iOS Shadowrocket 本机「分享/导出」出来的真实链接（设备导出为准，不是推测）。
> Hysteria2 / VLESS 按 Shadowrocket 原生 URI 格式与社区约定（`mport` 端口跳跃）整理。
> 生成器统一为 `scripts/make-qr.py`；改密码 / 端口 / SNI 后重跑即可，**不要重新"研究"格式**。
> 默认一次生成三种，不要只做 SS 再询问用户。

## 0. 执行位置

本文件的命令跑在**生成器所在那台机器**上，不关心 agent 在本机还是 VPS（对应 SKILL 第 0 步的 `SB_MODE`）。但要清楚 `/root` 指谁：

| 模式 | `--out-dir /root` 的含义 | 如何拿到 PNG |
|---|---|---|
| `SB_MODE=remote` | VPS 的 `/root` | `fetch /root/ss-qr.png ./ss-qr.png` |
| `SB_MODE=local` | 就是本机 `/root` | 文件已在本地，`fetch` 只是拷贝 |

生成器只依赖 `qrencode` / `zbarimg`，**不联网**，两台机器上输出逐字节一致。所以“在哪生成”不影响链接内容，只影响把 PNG 送到你手机的那一步。

## 1. Shadowsocks + ShadowTLS（v3）

```
ss://<B64(method:password@host:port)>?shadow-tls=<B64(json)>[&udp=0][#备注名]
```

- 第一段 **必须** 是 `Base64("method:password@host:port")` 整块编码，
  **不是** 明文的 `cipher:password@host:port`。
- 两个 Base64 **都去掉 `=` 填充**（小火箭自己导出时就是去掉的）。
- `shadow-tls` 的值 = `Base64(compact JSON)`，JSON **只允许且必须** 这三个键、这个顺序：

  ```json
  {"version":"3","host":"<握手SNI>","password":"<ShadowTLS密码>"}
  ```

- `&udp=0` 可选：ShadowTLS 只承载 TCP，开着 UDP 会让 QUIC/语音卡死（也可导入后在节点里关）。
- `#备注名` 可选；小火箭原生导出不带，若导入后异常先退回不带名字的形态。

字段与服务端配置的对应关系：

| 链接字段 | sing-box 服务端 |
|---|---|
| `host:port` | `inbounds[shadowtls].listen_port`（SS 用 `detour` 指向，不单独暴露端口） |
| `method` | `inbounds[shadowsocks].method`（如 `2022-blake3-aes-128-gcm`） |
| `password`(第一段) | `inbounds[shadowsocks].password` |
| `shadow-tls.host` | `inbounds[shadowtls].handshake.server` |
| `shadow-tls.password` | `inbounds[shadowtls].users[0].password` |
| `shadow-tls.version` | `inbounds[shadowtls].version`（字符串 `"3"`） |

## 2. Hysteria2

```
hysteria2://<URL编码密码>@<host>:<主端口>/?sni=<sni>&insecure=1&mport=<start>-<end>#<备注名>
```

- **主端口必须是数字**（如 `443`），服务端 NAT 把跳跃区间重定向到这个端口。
- 端口跳跃用 `mport=20000-30000`，**不要**把区间写进 authority（`host:20000-30000`）。
  官方 Hysteria2 URI 允许 authority 多端口，但 Shadowrocket 的原生 URI 解析对数字端口更稳；
  `mport` 是 Shadowrocket / v2rayN 兼容的社区约定。
- `insecure=1` 仅用于自签证书；有受信任证书时删掉这个参数。
- 本 skill 不使用 `obfs`；若服务端启用 salamander，再加 `obfs=salamander&obfs-password=...`。
- `pinSHA256` 可选，用于证书固定；一般不需要。

字段对应：

| 链接字段 | sing-box 服务端 |
|---|---|
| `host` | 公网 IP / 域名 |
| `主端口` | `inbounds[hysteria2].listen_port` |
| `密码` | `inbounds[hysteria2].users[0].password` |
| `sni` | `inbounds[hysteria2].tls.server_name` |
| `mport` | 服务端 NAT REDIRECT 的 UDP 区间（不是 inbound 字段） |
| `insecure` | 自签证书时 `1` |

## 3. VLESS + XTLS Vision（REALITY）

```
vless://<UUID>@<host>:<端口>?encryption=none&security=reality&sni=<握手站>&fp=chrome&pbk=<公钥>&sid=<short_id>&type=tcp&flow=xtls-rprx-vision#<备注名>
```

- **REALITY 不靠本机证书**：客户端校验 `pbk` + `sid`，TLS 证书链是 `handshake.server` 那个站的真证书。与 ShadowTLS 共用 `HANDSHAKE_SNI`。
- `sni` 必须 **等于服务端 `reality.handshake.server`**，且该名字在对方证书 SAN 里。
- **不要写 `allowInsecure`**，也**不要写 `alpn`**：REALITY 下两者无意义，带了反而会干扰。
- `security=reality`、`type=tcp`、`encryption=none` 三个都必须有。
- `sid` 是十六进制、**≤ 8 字节（即 16 个字符）**；超长会让 sing-box 直接 panic。
- `fp=chrome` 是 uTLS 指纹，小火箭按需保留。

若不用 REALITY（真域名证书或自签）：

```
vless://<UUID>@<host>:<端口>?encryption=none&security=tls&sni=<sni>&fp=chrome&type=tcp&flow=xtls-rprx-vision&alpn=h2[&allowInsecure=1]#<备注名>
```

`allowInsecure=1` 仅用于自签证书；受信任证书删掉。

字段对应：

| 链接字段 | sing-box 服务端 |
|---|---|
| `UUID` | `inbounds[vless].users[0].uuid` |
| `host:端口` | 公网 IP + `inbounds[vless].listen_port` |
| `sni` | `inbounds[vless].tls.server_name`（REALITY 下同时是 `reality.handshake.server`） |
| `pbk` | `inbounds[vless].tls.reality.public_key`（**客户端才有**，与服务端 `private_key` 成对） |
| `sid` | `inbounds[vless].tls.reality.short_id[0]` |
| `flow` | `inbounds[vless].users[0].flow` |
| `allowInsecure` | 仅普通 TLS + 自签时 `1`；REALITY 不写 |

## 4. 统一生成器

```bash
# 把 scripts/make-qr.py 放到 ${QR_DIR}（remote 模式为 VPS 的 /root，local 模式为任意本地目录）
# 再以 /etc/sing-box/secrets.env 里的密码为参数执行：
python3 /root/make-qr.py \
  --server 203.0.113.10 \
  --ss-sni www.microsoft.com --tls-sni example.com \
  --ss-port 8443 --ss-cipher 2022-blake3-aes-128-gcm \
  --ss-password '<SS_PASSWORD>' --st-password '<ST_PASSWORD>' \
  --hy2-port 2096 --hy2-password '<HY2_PASSWORD>' \
  --hy2-hop-range 20000:30000 \
  --vless-port 8444 --vless-uuid '<UUID>' \
  --vless-sni www.microsoft.com \
  --vless-reality-pbk '<REALITY_PUBLIC_KEY>' --vless-reality-sid '<REALITY_SHORT_ID>' \
  --out-dir /root --name-prefix VPS
```

- 默认 `--protocols ss,hy2,vless`，一次生成三张 PNG 和三份链接（链接文件 600）。
- **传了 `--vless-reality-pbk` + `--vless-reality-sid` 就生成 REALITY 链接**（`security=reality`，无 `allowInsecure`/`alpn`）；不传则回退到普通 TLS 形态。
- REALITY 下 `--vless-sni` 必须等于握手站；不传时默认跟 `--ss-sni`（同一握手站）。
- `--hy2-hop-range` 接受 `20000-30000` 或 `20000:30000`，输出统一为 `-`。
- 普通 TLS 模式下证书自签时 `insecure=1` / `allowInsecure=1`；受信任证书加 `--secure-tls`。REALITY 不受这两者影响。
- `--name-prefix` 只影响链接末尾的 `#备注名`，**不影响文件名**：输出恒为 `ss-qr.png` / `hy2-qr.png` / `vless-qr.png`；且不能含 `#` 或换行。
- `--sni` 可同时充当两种 SNI 的兼底；显式区分时用 `--ss-sni` / `--tls-sni`（优先）。
- 默认不打印含密码的链接；需要文本链接时加 `--show-links`。
- **`--utf8` 是默认交付方式**：在窗口里直接打印三张二维码，用户抬头即可扫；PNG 仍同时落盘。
- `--no-verify` 跳过 `zbarimg` 回读（仅在确认无 zbarimg 时用）。
- 本机快速自检（不生成文件、不需网络）：`python3 scripts/make-qr.py --self-test`。

## 5. 生成与回读二维码

以下命令假定 `--out-dir /root`，即在生成器所在机器上 `cd /root` 后执行（文件名固定，不受 `--name-prefix` 影响）：

```bash
cd /root
qrencode -o ss-qr.png -s 10 -m 3 < ss-link.txt
qrencode -o hy2-qr.png -s 10 -m 3 < hy2-link.txt
qrencode -o vless-qr.png -s 10 -m 3 < vless-link.txt

zbarimg --quiet --raw ss-qr.png
zbarimg --quiet --raw hy2-qr.png
zbarimg --quiet --raw vless-qr.png
```

生成器会自动做这三步并逐字节比对；`zbarimg` 未安装时只警告，不阻断。
**先让用户扫窗口里的二维码**；只有当终端把块字符替换掉（看着正常但扫不出）时，才改用 PNG。

## 6. 必须避免的写法

| ❌ 错误写法 | 症状 |
|---|---|
| SS 的 `"version": 3`（数字） | 能导入，但点进 ShadowTLS 配置直接闪退 |
| SS JSON 多塞 `"address"` / `"port"` | 同上风险；原生导出不写这两个键 |
| SS Base64 保留 `=` 填充 | 可能被当 query 分隔符截断，解码失败 |
| SS userinfo 用明文 | 能扫进去但字段拆错，连接失败 |
| SS 用 `?plugin=shadow-tls;...` / 平铺 query | Shadowrocket 不按这个解析 |
| SS 用 `sing-box://import-remote-profile?...` | Shadowrocket 不认识，完全无反应 |
| HY2 把区间写进 authority | 部分导入器直接拒绝；用数字主端口 + `mport` |
| HY2 漏 `mport` | 只连主端口，端口跳跃不生效 |
| HY2 自签却漏 `insecure=1` | TLS 校验失败 |
| VLESS 漏 `security=reality` / `security=tls` | 扫进去不是 TLS/REALITY，连不上 |
| REALITY 链接带 `allowInsecure` 或 `alpn` | 语义矛盾，部分客户端直接不认 |
| REALITY 漏 `pbk` / `sid` | 无法完成 REALITY 握手 |
| `sni` 不等于握手站 | 证书 SAN 对不上，握手失败 |
| `sid` 超过 16 个十六进制字符 | sing-box 1.14.3 实测 `panic: index out of range` |
| VLESS 把 `flow` 写到服务端 inbound | Vision 不生效 |
| 只生成 SS 一张二维码 | 交付不完整；默认一次生成三种 |
| `SB_MODE=local` 却去 `scp root@VPS:/root/...` | 同机自拷，直接用 `fetch` 或 `cp` |
| 把 `--out-dir` 当相对路径却换了 CWD | 文件落到当前目录；显式用 `/root` 或绝对路径 |

## 7. 自检

```bash
python3 scripts/make-qr.py --self-test
```

自检覆盖：

- SS 链接的 Base64 / ShadowTLS JSON / `version` 字符串类型；
- HY2 的 `mport`、`insecure`、主端口；
- VLESS 的 `security` / `type` / `flow` / `alpn`；
- `20000:30000` → `20000-30000` 的归一化与非法区间拒绝。

部署时再对每张 PNG 做 `zbarimg` 回读；三张全部一致才算通过。
