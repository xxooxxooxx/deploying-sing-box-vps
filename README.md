   # deploying-sing-box-vps

   Pi / Agent Skill — 在 VPS 上部署 sing-box 三合一入站（SS+ShadowTLS / Hysteria2 端口跳跃 / VLESS-REALITY），并直接输出 Shadowrocket 二维码。

   ## 安装

   ​```
   git clone https://github.com/xxooxxooxx/deploying-sing-box-vps ~/.agents/skills/deploying-sing-box-vps
   ​```

   或直接对 AI 说：安装这个 skill `https://github.com/xxooxxooxx/deploying-sing-box-vps`

   ## 依赖
   VPS：systemd + iptables/ufw，以及 python3、openssl、qrencode、zbarimg。

   ## 支持本地Ai Agent 
   需要先配置好SSH KEY自动登录
