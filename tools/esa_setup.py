#!/usr/bin/env python3
"""ESA + 云解析 一次性配置工具（幂等）。

把 `nife-tunnel.yoobit.cn` 配成"ESA 回源到你服务器 8443"的入口，共三步：
  1. 云解析加 CNAME：nife-tunnel → nife-tunnel.yoobit.cn.a1.initah.com
  2. ESA 加解析记录：nife-tunnel.yoobit.cn A/AAAA → 你的服务器 IP（Proxied 开）
  3. ESA 加回源规则：http.host eq "nife-tunnel.yoobit.cn" → HTTP:8443

密钥只从环境变量读，不接受命令行参数（避免进 shell 历史）：
    export ALIYUN_AK_ID=...
    export ALIYUN_AK_SECRET=...

用法:
    uv run --group setup python tools/esa_setup.py check
    uv run --group setup python tools/esa_setup.py setup --origin-ip 1.2.3.4 --dry-run
    uv run --group setup python tools/esa_setup.py setup --origin-ip 1.2.3.4
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from alibabacloud_alidns20150109 import models as dns_models
from alibabacloud_alidns20150109.client import Client as DnsClient
from alibabacloud_esa20240910 import models as esa_models
from alibabacloud_esa20240910.client import Client as EsaClient
from alibabacloud_tea_openapi import models as open_api_models

DEFAULT_SITE = "yoobit.cn"
DEFAULT_HOST = "nife-tunnel.yoobit.cn"


def clients():
    ak = os.environ.get("ALIYUN_AK_ID") or os.environ.get("AK")
    sk = os.environ.get("ALIYUN_AK_SECRET") or os.environ.get("SK")
    if not ak or not sk:
        raise SystemExit(
            "缺少密钥：请先 export ALIYUN_AK_ID=... ALIYUN_AK_SECRET=...\n"
            "（工具故意不接受命令行传密钥，避免留在 shell 历史里）"
        )
    region = os.environ.get("ALIYUN_REGION", "cn-hangzhou")
    esa = EsaClient(
        open_api_models.Config(
            access_key_id=ak, access_key_secret=sk,
            region_id=region, endpoint=f"esa.{region}.aliyuncs.com",
        )
    )
    dns = DnsClient(
        open_api_models.Config(
            access_key_id=ak, access_key_secret=sk,
            region_id=region, endpoint=f"alidns.{region}.aliyuncs.com",
        )
    )
    return esa, dns


# ---------- 查询 ----------
def find_site(esa: EsaClient, name: str) -> dict:
    r = esa.list_sites(esa_models.ListSitesRequest(page_number=1, page_size=100))
    for s in (r.body.sites or []):
        if s.site_name == name:
            return {
                "site_id": s.site_id, "site_name": s.site_name,
                "status": s.status, "access_type": s.access_type,
            }
    raise SystemExit(f"ESA 里找不到站点 {name}（先用控制台接入）")


def get_cname_zone(esa: EsaClient, site_id: int) -> str:
    r = esa.get_site(esa_models.GetSiteRequest(site_id=site_id))
    zone = r.body.site_model.cname_zone
    if not zone:
        raise SystemExit("该站点不是 CNAME 接入或拿不到 CnameZone，请用控制台配置")
    return zone


def find_zone(dns: DnsClient, host: str) -> tuple[str, str]:
    """返回 (apex, rr)。取匹配到的最长域名后缀。"""
    r = dns.describe_domains(dns_models.DescribeDomainsRequest(page_number=1, page_size=100))
    zones = [d.domain_name for d in (getattr(getattr(r.body, "domains", None), "domain", None) or [])]
    best = ""
    for z in zones:
        if (host == z or host.endswith("." + z)) and len(z) > len(best):
            best = z
    if not best:
        raise SystemExit(f"云解析里没有能覆盖 {host} 的域名")
    rr = "" if host == best else host[: -len(best) - 1]
    return best, "@" if rr == "" else rr


def get_cname_record(dns: DnsClient, apex: str, rr: str):
    r = dns.describe_domain_records(
        dns_models.DescribeDomainRecordsRequest(
            domain_name=apex, rrkey_word=rr, page_number=1, page_size=100
        )
    )
    recs = getattr(getattr(r.body, "domain_records", None), "record", None) or []
    for x in recs:
        if x.rr == rr and str(x.type).upper() == "CNAME":
            return x
    return None


def get_esa_record(esa: EsaClient, site_id: int, host: str):
    r = esa.list_records(
        esa_models.ListRecordsRequest(
            site_id=site_id, page_number=1, page_size=100, record_name=host
        )
    )
    for x in (r.body.records or []):
        if x.record_name == host:
            return x
    return None


def get_origin_rule(esa: EsaClient, site_id: int, host: str):
    r = esa.list_origin_rules(
        esa_models.ListOriginRulesRequest(site_id=site_id, page_number=1, page_size=100)
    )
    needle = f'http.host eq "{host}"'
    for x in (r.body.configs or []):
        if (x.rule or "").replace(" ", "").find(needle.replace(" ", "")) >= 0:
            return x
    return None


# ---------- 变更 ----------
def ensure_dns(dns: DnsClient, host: str, cname_zone: str, dry: bool, ttl: int) -> str:
    apex, rr = find_zone(dns, host)
    target = f"{host}.{cname_zone}"
    cur = get_cname_record(dns, apex, rr)
    if cur is not None:
        if str(cur.value).lower().rstrip(".") == target.lower():
            return f"跳过（已存在）{rr} CNAME {cur.value}"
        if dry:
            return f"[dry-run] 会更新 {rr} CNAME {cur.value} -> {target}"
        dns.update_domain_record(
            dns_models.UpdateDomainRecordRequest(
                record_id=cur.record_id, rr=rr, type="CNAME", value=target, ttl=ttl
            )
        )
        return f"已更新 {rr} CNAME {cur.value} -> {target}"
    if dry:
        return f"[dry-run] 会新增 {rr} CNAME {target} (ttl={ttl})"
    dns.add_domain_record(
        dns_models.AddDomainRecordRequest(
            domain_name=apex, rr=rr, type="CNAME", value=target, ttl=ttl
        )
    )
    return f"已新增 {rr} CNAME {target} (ttl={ttl})"


def ensure_esa_record(esa: EsaClient, site_id: int, host: str, ip: str, dry: bool) -> str:
    cur = get_esa_record(esa, site_id, host)
    if cur is not None:
        val = getattr(cur.data, "value", None) if cur.data else None
        return f"跳过（已存在）{host} -> {val} proxied={cur.proxied}"
    if dry:
        return f"[dry-run] 会在 ESA 新增记录 {host} -> {ip} (Proxied)"
    esa.create_record(
        esa_models.CreateRecordRequest(
            site_id=site_id,
            record_name=host,
            type="A/AAAA",
            proxied=True,
            ttl=1,
            biz_name="web",
            comment="nifetun",
            data=esa_models.CreateRecordRequestData(value=ip),
        )
    )
    return f"已在 ESA 新增记录 {host} -> {ip} (Proxied)"


def ensure_origin_rule(esa: EsaClient, site_id: int, host: str, scheme: str,
                       port: int, dry: bool) -> str:
    cur = get_origin_rule(esa, site_id, host)
    if cur is not None:
        return (f"跳过（已存在）回源规则 #{cur.config_id} "
                f"{cur.origin_scheme}://...:{cur.origin_http_port or cur.origin_https_port}")
    if dry:
        return f"[dry-run] 会新增回源规则 host={host} -> {scheme}:{port}"
    esa.create_origin_rule(
        esa_models.CreateOriginRuleRequest(
            site_id=site_id,
            rule_name=f"nifetun-{host}",
            rule=f'(http.host eq "{host}")',
            rule_enable="on",
            origin_scheme=scheme,
            origin_http_port=str(port) if scheme == "http" else None,
            origin_https_port=str(port) if scheme == "https" else None,
        )
    )
    return f"已新增回源规则 host={host} -> {scheme}:{port}"


# ---------- 命令 ----------
def cmd_check(args) -> int:
    esa, dns = clients()
    site = find_site(esa, args.site)
    print("站点:", json.dumps(site, ensure_ascii=False))
    zone = get_cname_zone(esa, site["site_id"])
    print("CnameZone:", zone)
    apex, rr = find_zone(dns, args.host)
    print(f"云解析: apex={apex} rr={rr}")
    cur = get_cname_record(dns, apex, rr)
    print("  现有 CNAME:", f"{cur.value} ttl={cur.ttl}" if cur else "无")
    rec = get_esa_record(esa, site["site_id"], args.host)
    if rec:
        val = getattr(rec.data, "value", None) if rec.data else None
        print(f"  ESA 记录: -> {val} proxied={rec.proxied} type={rec.record_type}")
    else:
        print("  ESA 记录: 无")
    rule = get_origin_rule(esa, site["site_id"], args.host)
    print("  回源规则:", f"#{rule.config_id} {rule.origin_scheme} "
          f"{rule.origin_http_port or rule.origin_https_port}" if rule else "无")
    print(f"\n结论：访问 https://{args.host}{args.prefix}/<隧道ID>/")
    return 0


def cmd_setup(args) -> int:
    esa, dns = clients()
    site = find_site(esa, args.site)
    sid = site["site_id"]
    zone = get_cname_zone(esa, sid)
    print(f"站点 {site['site_name']} (SiteId={sid}, {site['access_type']}, {site['status']})")
    print("1)", ensure_dns(dns, args.host, zone, args.dry_run, args.ttl))
    print("2)", ensure_esa_record(esa, sid, args.host, args.origin_ip, args.dry_run))
    print("3)", ensure_origin_rule(esa, sid, args.host, args.scheme, args.port, args.dry_run))
    print(f"\n完成。入口： https://{args.host}{args.prefix}/<隧道ID>/")
    if not args.dry_run:
        print("提示：DNS/ESA 生效通常 1~5 分钟；edge 上用 prefix 前缀路由即可。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="nifetun 的 ESA/云解析 配置工具")
    ap.add_argument("--site", default=DEFAULT_SITE, help=f"ESA 站点名（默认 {DEFAULT_SITE}）")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"入口域名（默认 {DEFAULT_HOST}）")
    ap.add_argument("--prefix", default="/t", help="edge 的路径前缀（默认 /t）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="只读：检查当前配置状态")

    st = sub.add_parser("setup", help="幂等地配好三步")
    st.add_argument("--origin-ip", required=True, help="你的服务器公网 IP")
    st.add_argument("--port", type=int, default=8443, help="回源端口（默认 8443）")
    st.add_argument("--scheme", default="http", choices=["http", "https"])
    st.add_argument("--ttl", type=int, default=600, help="云解析 TTL（免费版最低 600）")
    st.add_argument("--dry-run", action="store_true", help="只打印将要做什么")

    args = ap.parse_args(argv)
    return cmd_check(args) if args.cmd == "check" else cmd_setup(args)


if __name__ == "__main__":
    sys.exit(main())
