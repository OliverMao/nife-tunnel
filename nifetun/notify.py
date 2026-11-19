"""发邮件（标准库 smtplib，零第三方依赖）。

密码不写配置文件：优先读环境变量（默认 NIFETUN_SMTP_PASS），
这样配置文件可以随便备份/进仓库而不会泄漏邮箱口令。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate

log = logging.getLogger("nifetun.notify")


def _password(cfg: dict) -> str:
    env = str(cfg.get("password_env") or "NIFETUN_SMTP_PASS")
    pwd = os.environ.get(env, "")
    if not pwd:
        pwd = str(cfg.get("password") or "")  # 兜底：配置里写（不推荐）
    if not pwd:
        raise RuntimeError(f"没拿到邮箱口令：请 export {env}=...")
    return pwd


def _auth_hint(user: str, detail: str) -> str:
    return (
        f"邮箱认证被拒（{detail}），账号 {user}。常见原因："
        "① 该账号没有开启 SMTP 服务（163 企业邮箱：设置 → 客户端设置 → 开启 SMTP）；"
        "② 开启了「客户端授权码」，此时密码必须填授权码而不是登录密码；"
        "③ 用户名要用完整邮箱地址；"
        "④ 管理员后台禁止了该账号的客户端收发"
    )


def send(cfg: dict, subject: str, html: str, to: list[str] | None = None) -> None:
    """同步发信（在 executor 里调用）。"""
    host = str(cfg.get("smtp_host") or "smtphz.qiye.163.com")
    port = int(cfg.get("smtp_port") or 994)
    user = str(cfg.get("username") or cfg.get("from") or "")
    sender = str(cfg.get("from") or user)
    sender_name = str(cfg.get("from_name") or "nifetun")
    recipients = to or list(cfg.get("to") or [user])
    if not user or not recipients:
        raise RuntimeError("email 配置缺少 username / to")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("这是一封 HTML 邮件，请用支持 HTML 的客户端查看。")
    msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    timeout = float(cfg.get("timeout") or 20)
    smtp = None
    try:
        if cfg.get("smtp_ssl", True):
            smtp = smtplib.SMTP_SSL(host, port, context=ctx, timeout=timeout)
        else:
            smtp = smtplib.SMTP(host, port, timeout=timeout)
            smtp.starttls(context=ctx)
        smtp.ehlo()
        pwd = _password(cfg)
        # 手动 AUTH PLAIN：这样能把服务器的 535 原文带出来，方便定位
        token = base64.b64encode(
            b"".join([bytes([0]), user.encode(), bytes([0]), pwd.encode()])
        ).decode()
        code, resp = smtp.docmd("AUTH", "PLAIN " + token)
        if code != 235:
            text = resp.decode("latin-1", "replace") if isinstance(resp, bytes) else str(resp)
            raise RuntimeError(_auth_hint(user, f"{code} {text}"))
        smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        code = getattr(e, "smtp_code", "?")
        detail = e.smtp_error.decode("latin-1", "replace") if isinstance(e.smtp_error, bytes) else e.smtp_error
        raise RuntimeError(
            f"邮箱认证被拒（{code} {detail}）。常见原因：① 账号没开启 SMTP 服务"
            "（163 企业邮箱：设置 → 客户端设置 → 开启 SMTP/POP3/IMAP）；"
            "② 开启了「客户端授权码」，密码要用授权码而不是登录密码；"
            "③ 用户名要用完整邮箱地址"
        ) from e
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass
    log.info("邮件已发送 -> %s (%s)", ", ".join(recipients), subject)


async def send_async(cfg: dict, subject: str, html: str, to: list[str] | None = None) -> bool:
    """异步发信：放到线程池，绝不阻塞事件循环。失败只记日志。"""
    try:
        await asyncio.to_thread(send, cfg, subject, html, to)
        return True
    except Exception as e:  # noqa: BLE001 - 发信失败不能影响接入
        log.error("发信失败: %r", e)
        return False


def enroll_mail(client: str, label: str, peer: str, approve_url: str, deny_url: str,
                expires_min: int, extra: dict | None = None) -> tuple[str, str]:
    """生成审批邮件（主题 + HTML）。按钮是纯链接，点一下就 GET 到 edge。"""
    rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#888">{k}</td>'
        f'<td style="padding:4px 0;font-family:ui-monospace,monospace">{v}</td></tr>'
        for k, v in (extra or {}).items()
    )
    html = f"""<!doctype html><meta charset="utf-8">
<div style="font:15px/1.6 -apple-system,system-ui,sans-serif;color:#222;max-width:520px">
  <h2 style="font-size:18px;margin:0 0 4px">客户端接入申请</h2>
  <p style="color:#666;margin:0 0 16px">
    有客户端请求通过 nifetun 接入内网。批准后它的 token 会直接下发到该客户端，
    不会经过任何人工传递。
  </p>
  <table style="border-collapse:collapse;margin:0 0 20px">
    <tr><td style="padding:4px 12px 4px 0;color:#888">客户端</td>
        <td style="padding:4px 0;font-family:ui-monospace,monospace">{label}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#888">UUID</td>
        <td style="padding:4px 0;font-family:ui-monospace,monospace">{client}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#888">来源 IP</td>
        <td style="padding:4px 0;font-family:ui-monospace,monospace">{peer}</td></tr>
    {rows}
  </table>
  <p style="margin:0 0 20px">
    <a href="{approve_url}"
       style="display:inline-block;background:#0b7a3b;color:#fff;text-decoration:none;
              padding:11px 22px;border-radius:6px;font-weight:600">批准接入</a>
    &nbsp;&nbsp;
    <a href="{deny_url}"
       style="display:inline-block;background:#f2f2f2;color:#a00;text-decoration:none;
              padding:11px 22px;border-radius:6px">拒绝</a>
  </p>
  <p style="color:#999;font-size:13px;margin:0">
    链接 {expires_min} 分钟内有效、只能用一次。如果这不是你发起的，点“拒绝”并检查服务器日志。
  </p>
</div>"""
    return f"[nifetun] 客户端 {label} 请求接入", html
