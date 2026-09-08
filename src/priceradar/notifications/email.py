"""E-posta bildirimi.

Şifre asla yapılandırmada tutulmaz; ortam değişkeninden okunur. Gmail
kullanıyorsan normal hesap şifresi çalışmaz, uygulama şifresi gerekir.

Tasarım notu: gönderim `asyncio.to_thread` içinde çalışıyor. `smtplib`
senkron ve bloklayıcı; doğrudan çağırmak async olay döngüsünü kilitler ve
kazıma dururdu.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from ..alerts import TriggeredAlert
from ..config import Settings

logger = logging.getLogger(__name__)


class EmailError(RuntimeError):
    """E-posta gönderilemedi."""


def _format_price(value, currency: str | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} {currency or ''}".strip()


def render_alert_email(alerts: list[TriggeredAlert]) -> tuple[str, str, str]:
    """(konu, düz metin, HTML) döndürür."""
    count = len(alerts)
    subject = (
        f"{alerts[0].product_title[:50]} ucuzladı"
        if count == 1
        else f"{count} üründe fiyat düşüşü"
    )

    lines = []
    rows = []
    for alert in alerts:
        old = _format_price(alert.old_price, alert.currency)
        new = _format_price(alert.new_price, alert.currency)
        percent = f"{alert.percent:.1f}%" if alert.percent is not None else "—"

        lines.append(
            f"- {alert.product_title} ({alert.site_slug}): {old} → {new} ({percent})\n"
            f"  {alert.reason}\n  {alert.url}"
        )
        rows.append(
            f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #c9d2db">
                <a href="{alert.url}" style="color:#16212e;text-decoration:none">
                  {alert.product_title}</a>
                <div style="color:#5a6a7a;font-size:13px">{alert.site_slug} · {alert.reason}</div>
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #c9d2db;
                         text-align:right;color:#5a6a7a;text-decoration:line-through">{old}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #c9d2db;
                         text-align:right;font-weight:600;color:#0b6b5b">{new}</td>
              <td style="padding:10px 12px;border-bottom:1px solid #c9d2db;
                         text-align:right;color:#0b6b5b">{percent}</td>
            </tr>"""
        )

    text = "Takip ettiğin ürünlerde fiyat düştü:\n\n" + "\n\n".join(lines)

    html = f"""<!doctype html>
<html lang="tr"><body style="margin:0;background:#e9edf1;font-family:
  'IBM Plex Sans',system-ui,-apple-system,sans-serif;color:#16212e">
  <div style="max-width:640px;margin:0 auto;padding:24px">
    <h1 style="font-size:20px;margin:0 0 4px">Fiyat düşüşü</h1>
    <p style="color:#5a6a7a;margin:0 0 20px;font-size:14px">
      Takip ettiğin {count} üründe fiyat düştü.</p>
    <table style="width:100%;border-collapse:collapse;background:#fff;
                  border:1px solid #c9d2db;border-radius:3px">
      <thead>
        <tr style="background:#f4f6f8">
          <th style="text-align:left;padding:8px 12px;font-size:13px;
                     color:#5a6a7a;font-weight:500">Ürün</th>
          <th style="text-align:right;padding:8px 12px;font-size:13px;
                     color:#5a6a7a;font-weight:500">Önce</th>
          <th style="text-align:right;padding:8px 12px;font-size:13px;
                     color:#5a6a7a;font-weight:500">Sonra</th>
          <th style="text-align:right;padding:8px 12px;font-size:13px;
                     color:#5a6a7a;font-weight:500">Değişim</th>
        </tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <p style="color:#5a6a7a;font-size:12px;margin-top:20px">
      price-radar tarafından gönderildi.</p>
  </div>
</body></html>"""

    return subject, text, html


def _build_message(
    settings: Settings, recipient: str, subject: str, text: str, html: str
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.alert_sender or settings.smtp_user
    message["To"] = recipient
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def _send_sync(settings: Settings, messages: list[EmailMessage]) -> None:
    """Bloklayan SMTP işi. Tek bağlantıdan tüm mesajlar gönderilir."""
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_password)
        for message in messages:
            server.send_message(message)


async def send_alert_emails(
    settings: Settings, alerts: list[TriggeredAlert]
) -> dict[str, int]:
    """Tetiklenen alarmları alıcıya göre gruplayıp e-posta gönderir.

    Gruplama önemli: bir kullanıcının beş ürünü aynı anda ucuzladıysa
    beş ayrı e-posta yerine tek e-posta gider.
    """
    if not alerts:
        return {"sent": 0, "recipients": 0}

    if not settings.smtp_user or not settings.smtp_password:
        raise EmailError(
            "SMTP kimlik bilgileri eksik. PRICERADAR_SMTP_USER ve "
            "PRICERADAR_SMTP_PASSWORD ortam değişkenlerini tanımla."
        )

    by_recipient: dict[str, list[TriggeredAlert]] = {}
    for alert in alerts:
        by_recipient.setdefault(alert.email, []).append(alert)

    messages = []
    for recipient, recipient_alerts in by_recipient.items():
        subject, text, html = render_alert_email(recipient_alerts)
        messages.append(_build_message(settings, recipient, subject, text, html))

    try:
        # smtplib senkron: olay döngüsünü bloklamasın diye ayrı iş parçacığında
        await asyncio.to_thread(_send_sync, settings, messages)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(
            "SMTP girişi reddedildi. Gmail kullanıyorsan uygulama şifresi gerekir."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"E-posta gönderilemedi: {exc}") from exc

    logger.info("%d alıcıya bildirim gönderildi", len(by_recipient))
    return {"sent": len(messages), "recipients": len(by_recipient)}
