#!/usr/bin/env python3
"""Generate and verify Shadowrocket links/QRs for SS+ShadowTLS, Hysteria2, VLESS."""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse as urlparse
import uuid


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode().rstrip("=")


def ss_link(server, port, cipher, ss_password, sni, st_password,
            st_version="3", name=None, udp=False) -> str:
    userinfo = b64(f"{cipher}:{ss_password}@{server}:{port}")
    opts = json.dumps(
        {"version": str(st_version), "host": sni, "password": st_password},
        separators=(",", ":"), ensure_ascii=False,
    )
    link = f"ss://{userinfo}?shadow-tls={b64(opts)}"
    if udp:
        link += "&udp=0"
    if name:
        link += "#" + urlparse.quote(name, safe="")
    return link


def hy2_link(server, port, password, sni, hop_range=None,
             insecure=True, name=None) -> str:
    query = {"sni": sni}
    if insecure:
        query["insecure"] = "1"
    if hop_range:
        query["mport"] = hop_range
    link = (
        f"hysteria2://{urlparse.quote(password, safe='')}"
        f"@{server}:{port}/?{urlparse.urlencode(query)}"
    )
    if name:
        link += "#" + urlparse.quote(name, safe="")
    return link


def vless_link(server, port, user_uuid, sni, insecure=True, name=None,
               reality_pbk=None, reality_sid=None) -> str:
    """VLESS+Vision 链接。传 reality_pbk/reality_sid 则走 REALITY，否则走普通 TLS。

    REALITY 模式不使用 allowInsecure（校验的是握手站的真证书），也不写 alpn。
    """
    if reality_pbk or reality_sid:
        if not (reality_pbk and reality_sid):
            raise ValueError("REALITY 需要同时提供 pbk 与 sid")
        query = {
            "encryption": "none",
            "security": "reality",
            "sni": sni,
            "fp": "chrome",
            "pbk": reality_pbk,
            "sid": reality_sid,
            "type": "tcp",
            "flow": "xtls-rprx-vision",
        }
    else:
        query = {
            "encryption": "none",
            "security": "tls",
            "sni": sni,
            "fp": "chrome",
            "type": "tcp",
            "flow": "xtls-rprx-vision",
            "alpn": "h2",
        }
        if insecure:
            query["allowInsecure"] = "1"
    link = (
        f"vless://{urlparse.quote(user_uuid, safe='')}"
        f"@{server}:{port}?{urlparse.urlencode(query)}"
    )
    if name:
        link += "#" + urlparse.quote(name, safe="")
    return link


def decode_ss(link: str) -> dict:
    content, _, frag = link.partition("#")
    blob, _, q = content[5:].partition("?")
    pad = lambda s: s + "=" * (-len(s) % 4)
    ui = base64.b64decode(pad(blob)).decode()
    cipher, _, rest = ui.partition(":")
    password, _, hostport = rest.rpartition("@")
    host, _, port = hostport.rpartition(":")
    st = json.loads(base64.b64decode(pad(
        re.search(r"[?&]shadow-tls=([^&]+)", "?" + q).group(1))).decode())
    if not isinstance(st.get("version"), str):
        raise ValueError("shadow-tls JSON 的 version 必须是字符串")
    if set(st) != {"version", "host", "password"}:
        raise ValueError("shadow-tls JSON 只能有 version/host/password 三个键")
    return {
        "cipher": cipher,
        "password": password,
        "server": host,
        "port": int(port),
        "sni": st["host"],
        "st_password": st["password"],
        "name": urlparse.unquote(frag),
        "udp": "&udp=0" in q,
    }


def decode_hy2(link: str) -> dict:
    parsed = urlparse.urlsplit(link)
    if parsed.scheme not in {"hysteria2", "hy2"}:
        raise ValueError("不是 hysteria2 链接")
    query = urlparse.parse_qs(parsed.query)
    return {
        "server": parsed.hostname,
        "port": parsed.port,
        "password": urlparse.unquote(parsed.username or ""),
        "sni": query.get("sni", [""])[0],
        "mport": query.get("mport", [None])[0],
        "insecure": query.get("insecure", ["0"])[0] == "1",
        "name": urlparse.unquote(parsed.fragment),
    }


def decode_vless(link: str) -> dict:
    parsed = urlparse.urlsplit(link)
    if parsed.scheme != "vless":
        raise ValueError("不是 vless 链接")
    query = urlparse.parse_qs(parsed.query)
    return {
        "server": parsed.hostname,
        "port": parsed.port,
        "uuid": urlparse.unquote(parsed.username or ""),
        "sni": query.get("sni", [""])[0],
        "security": query.get("security", [""])[0],
        "type": query.get("type", [""])[0],
        "flow": query.get("flow", [""])[0],
        "alpn": query.get("alpn", [""])[0],
        "pbk": query.get("pbk", [""])[0],
        "sid": query.get("sid", [""])[0],
        "insecure": query.get("allowInsecure", ["0"])[0] == "1",
        "name": urlparse.unquote(parsed.fragment),
    }


def normalize_hop_range(value):
    if not value:
        return None
    value = value.strip().replace(":", "-")
    if not re.fullmatch(
        r"\d{1,5}(?:-\d{1,5})?(?:,\d{1,5}(?:-\d{1,5})?)*", value
    ):
        raise ValueError("端口范围格式应为 20000-30000 / 20000:30000 / 443,20000-30000")
    for part in value.split(","):
        start, _, end = part.partition("-")
        for port in (start, end):
            if port and not 1 <= int(port) <= 65535:
                raise ValueError("端口必须在 1-65535")
        if end and int(start) > int(end):
            raise ValueError("端口范围起点不能大于终点")
    return value


def reality_sid_arg(value: str) -> str:
    """REALITY short_id：十六进制，≤ 8 字节（10 个十六进制字符以上会让 sing-box panic）。"""
    value = (value or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{2,16}", value):
        raise argparse.ArgumentTypeError(
            "REALITY short_id 必须是 2-16 个十六进制字符（通常 generate rand 8 --hex）"
        )
    if len(value) % 2:
        raise argparse.ArgumentTypeError("REALITY short_id 长度应为偶数")
    return value


def port_arg(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("端口必须是整数")
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1-65535")
    return port


def protocols_arg(value: str):
    items = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not items:
        raise argparse.ArgumentTypeError("至少选择一个协议")
    unknown = set(items) - {"ss", "hy2", "vless"}
    if unknown:
        raise argparse.ArgumentTypeError("未知协议: " + ",".join(sorted(unknown)))
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def run_self_test() -> int:
    server = "203.0.113.10"
    sni = "www.microsoft.com"
    ss = ss_link(server, 443, "2022-blake3-aes-128-gcm", "c3MtcGFzcw==",
                 sni, "st-pass", name="test-SS")
    assert decode_ss(ss) == {
        "cipher": "2022-blake3-aes-128-gcm",
        "password": "c3MtcGFzcw==",
        "server": server,
        "port": 443,
        "sni": sni,
        "st_password": "st-pass",
        "name": "test-SS",
        "udp": False,
    }
    hy2 = hy2_link(server, 443, "hy2-pass", sni, "20000-30000", True, "test-HY2")
    assert decode_hy2(hy2) == {
        "server": server,
        "port": 443,
        "password": "hy2-pass",
        "sni": sni,
        "mport": "20000-30000",
        "insecure": True,
        "name": "test-HY2",
    }
    assert "mport=20000-30000" in hy2
    vless = vless_link(server, 8443, "11111111-1111-4111-8111-111111111111",
                       sni, True, "test-VLESS")
    assert decode_vless(vless) == {
        "server": server,
        "port": 8443,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "sni": sni,
        "security": "tls",
        "type": "tcp",
        "flow": "xtls-rprx-vision",
        "alpn": "h2",
        "pbk": "",
        "sid": "",
        "insecure": True,
        "name": "test-VLESS",
    }
    # REALITY：借握手站真证书，不带 allowInsecure / alpn
    pbk = "tZ6hlSeyLwd3O1Lu18Nw2_ejG23XVp804U2rqA2cnjo"
    sid = "70df63d777aebd88"
    rv = vless_link(server, 8443, "11111111-1111-4111-8111-111111111111",
                    sni, True, "test-REALITY", reality_pbk=pbk, reality_sid=sid)
    assert decode_vless(rv) == {
        "server": server,
        "port": 8443,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "sni": sni,
        "security": "reality",
        "type": "tcp",
        "flow": "xtls-rprx-vision",
        "alpn": "",
        "pbk": pbk,
        "sid": sid,
        "insecure": False,
        "name": "test-REALITY",
    }
    assert "allowInsecure" not in rv and "alpn=" not in rv
    for bad in ({"reality_pbk": pbk}, {"reality_sid": sid}):
        try:
            vless_link(server, 8443, "11111111-1111-4111-8111-111111111111",
                       sni, True, None, **bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"REALITY 缺参数应报错: {bad}")
    assert reality_sid_arg("70df63d777aebd88") == "70df63d777aebd88"
    for bad in ("", "zz", "0123456789abcdef01"):
        try:
            reality_sid_arg(bad)
        except (ValueError, argparse.ArgumentTypeError):
            pass
        else:
            raise AssertionError(f"应拒绝 short_id: {bad}")
    assert normalize_hop_range("20000:30000") == "20000-30000"
    assert normalize_hop_range("443,20000:30000") == "443,20000-30000"
    for bad in ("abc", "30000-20000", "70000", "20000-"):
        try:
            normalize_hop_range(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝端口范围: {bad}")
    assert protocols_arg("ss,hy2,vless") == ["ss", "hy2", "vless"]
    print("SELF_TEST_OK: ss, hy2, vless(tls+reality) links and hop-range normalization")
    return 0


def build_jobs(args):
    hop_range = normalize_hop_range(args.hy2_hop_range)
    insecure = not args.secure_tls
    ss_sni = args.ss_sni or args.sni
    tls_sni = args.tls_sni or args.sni
    # REALITY 的 sni 必须等于握手站，所以默认跟随 --ss-sni（同一握手站）
    vless_sni = args.vless_sni or (
        (args.ss_sni or args.sni) if args.vless_reality_pbk else tls_sni
    )
    jobs = []
    for protocol in args.protocols:
        if protocol == "ss":
            name = f"{args.name_prefix}-SS"
            link = ss_link(
                args.server, args.ss_port, args.ss_cipher, args.ss_password,
                ss_sni, args.st_password, name=name, udp=args.ss_udp_disable,
            )
            expected = {
                "cipher": args.ss_cipher,
                "password": args.ss_password,
                "server": args.server,
                "port": args.ss_port,
                "sni": ss_sni,
                "st_password": args.st_password,
                "name": name,
                "udp": args.ss_udp_disable,
            }
            if decode_ss(link) != expected:
                sys.exit("SS 链接自检失败")
        elif protocol == "hy2":
            name = f"{args.name_prefix}-HY2"
            link = hy2_link(
                args.server, args.hy2_port, args.hy2_password, tls_sni,
                hop_range, insecure, name,
            )
            expected = {
                "server": args.server,
                "port": args.hy2_port,
                "password": args.hy2_password,
                "sni": tls_sni,
                "mport": hop_range,
                "insecure": insecure,
                "name": name,
            }
            if decode_hy2(link) != expected:
                sys.exit("Hysteria2 链接自检失败")
        else:
            name = f"{args.name_prefix}-VLESS"
            reality = bool(args.vless_reality_pbk)
            link = vless_link(
                args.server, args.vless_port, args.vless_uuid, vless_sni,
                insecure, name,
                reality_pbk=args.vless_reality_pbk if reality else None,
                reality_sid=args.vless_reality_sid if reality else None,
            )
            expected = {
                "server": args.server,
                "port": args.vless_port,
                "uuid": args.vless_uuid,
                "sni": vless_sni,
                "security": "reality" if reality else "tls",
                "type": "tcp",
                "flow": "xtls-rprx-vision",
                "alpn": "" if reality else "h2",
                "pbk": (args.vless_reality_pbk or "") if reality else "",
                "sid": (args.vless_reality_sid or "") if reality else "",
                "insecure": (insecure and not reality),
                "name": name,
            }
            if decode_vless(link) != expected:
                sys.exit("VLESS 链接自检失败")
        jobs.append((protocol, link))
    return jobs


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Shadowrocket QRs for SS+ShadowTLS, Hysteria2, VLESS.",
    )
    parser.add_argument("--server", help="公网 IP 或域名")
    parser.add_argument("--sni", help="SS/TLS 共用 SNI（可被 --ss-sni / --tls-sni 覆盖）")
    parser.add_argument("--ss-sni", help="ShadowTLS 握手 SNI；未指定时用 --sni")
    parser.add_argument("--tls-sni", help="Hysteria2/VLESS SNI；未指定时用 --sni")
    parser.add_argument("--ss-port", type=port_arg, default=8443)
    parser.add_argument("--ss-cipher", default="2022-blake3-aes-128-gcm")
    parser.add_argument("--ss-password")
    parser.add_argument("--st-password")
    parser.add_argument("--ss-udp-disable", action="store_true",
                        help="SS 链接追加 &udp=0（ShadowTLS 仅承载 TCP）")
    parser.add_argument("--hy2-port", type=port_arg, default=2096)
    parser.add_argument("--hy2-password")
    parser.add_argument("--hy2-hop-range",
                        help="端口跳跃范围，如 20000-30000 或 20000:30000")
    parser.add_argument("--vless-port", type=port_arg, default=8444)
    parser.add_argument("--vless-uuid")
    parser.add_argument("--vless-sni",
                        help="VLESS 的 sni；REALITY 模式下应等于握手站（默认跟 --ss-sni）")
    parser.add_argument("--vless-reality-pbk",
                        help="REALITY 公钥（sing-box generate reality-keypair 的 PublicKey）")
    parser.add_argument("--vless-reality-sid", type=reality_sid_arg,
                        help="REALITY short_id（十六进制，≤ 8 字节）")
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--name-prefix", default="VPS")
    parser.add_argument("--protocols", type=protocols_arg,
                        default=["ss", "hy2", "vless"],
                        help="默认 ss,hy2,vless")
    parser.add_argument("--secure-tls", action="store_true",
                        help="证书受信任时省略 insecure / allowInsecure")
    parser.add_argument("--show-links", action="store_true",
                        help="同时打印含密码的链接")
    parser.add_argument("--utf8", action="store_true",
                        help="同时输出终端 UTF8 二维码")
    parser.add_argument("--no-verify", action="store_true",
                        help="跳过 zbarimg 回读校验")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    if not args.server:
        parser.error("--server 必填")
    if "ss" in args.protocols and not (args.sni or args.ss_sni):
        parser.error("--sni 或 --ss-sni 必填")
    if ({"hy2", "vless"} & set(args.protocols)) and not (
            args.sni or args.tls_sni or args.ss_sni):
        parser.error("--sni 或 --tls-sni 必填")
    if not args.name_prefix:
        parser.error("--name-prefix 不能为空")
    if "#" in args.name_prefix or "\n" in args.name_prefix:
        parser.error("--name-prefix 不能包含 # 或换行")
    if "ss" in args.protocols and not (args.ss_password and args.st_password):
        parser.error("SS 需要 --ss-password 与 --st-password")
    if "hy2" in args.protocols and not args.hy2_password:
        parser.error("Hysteria2 需要 --hy2-password")
    if "vless" in args.protocols:
        if not args.vless_uuid:
            parser.error("VLESS 需要 --vless-uuid")
        if bool(args.vless_reality_pbk) != bool(args.vless_reality_sid):
            parser.error("--vless-reality-pbk 与 --vless-reality-sid 必须成对提供")
        if args.vless_reality_pbk and not (args.vless_sni or args.ss_sni or args.sni):
            parser.error("REALITY 需要 --vless-sni 或 --ss-sni（必须等于握手站）")
        try:
            uuid.UUID(args.vless_uuid)
        except ValueError:
            parser.error("--vless-uuid 不是合法 UUID")

    try:
        jobs = build_jobs(args)
    except ValueError as exc:
        parser.error(str(exc))

    if not shutil.which("qrencode"):
        sys.exit("qrencode 未安装：apt-get install -y qrencode / dnf install -y qrencode")
    verifier = shutil.which("zbarimg")
    if not args.no_verify and not verifier:
        print("WARN: zbarimg 未安装，跳过二维码回读校验", file=sys.stderr)

    os.makedirs(args.out_dir, exist_ok=True)
    for protocol, link in jobs:
        png = os.path.join(args.out_dir, f"{protocol}-qr.png")
        link_file = os.path.join(args.out_dir, f"{protocol}-link.txt")
        with open(link_file, "w", encoding="utf-8") as handle:
            handle.write(link + "\n")
        try:
            os.chmod(link_file, 0o600)
        except OSError:
            pass
        subprocess.run(
            ["qrencode", "-o", png, "-s", "10", "-m", "3"],
            input=link.encode(), check=True,
        )
        if verifier and not args.no_verify:
            decoded = subprocess.check_output(
                [verifier, "--quiet", "--raw", png]
            ).decode().strip()
            if decoded != link:
                sys.exit(f"{protocol.upper()} 二维码回读不一致")
        if args.utf8:
            subprocess.run(
                ["qrencode", "-t", "UTF8", "-m", "1"],
                input=link.encode(), check=True,
            )
        print(
            f"{protocol.upper():5} {len(link):3} chars  "
            f"qr={png}  link_file={link_file}"
        )
        if args.show_links:
            print(link)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
