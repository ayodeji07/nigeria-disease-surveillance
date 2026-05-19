"""
src/etl/notify.py
────────────────────────────────────────────────────────────────
Pipeline completion notifications.

Sends a plain-text email when the ETL pipeline finishes —
whether it succeeded, partially succeeded, or failed.

Why email rather than Slack/Teams?
  Email requires no webhook setup, works with any notification
  target, and is the standard for automated system alerts.
  SendGrid is used because it has a generous free tier (100
  emails/day) and a straightforward REST API.

The notifier is entirely optional — if SENDGRID_API_KEY and
NOTIFY_EMAIL are not set in .env, notifications are skipped
silently. The pipeline never fails because of a notification
error.

Usage:
    from src.etl.notify import send_pipeline_notification
    send_pipeline_notification(pipeline_result)
────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

import requests

from src.utils.config import settings
from src.utils.logger import get_logger

if TYPE_CHECKING:
    # Avoid circular import at runtime — PipelineResult is only
    # used as a type hint here, not instantiated.
    from src.etl.pipeline import PipelineResult

logger = get_logger(__name__)

# SendGrid messages API endpoint
_SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"

# Sender address — SendGrid requires a verified sender
# For a portfolio project, use your own verified email here
_SENDER_EMAIL = "noreply@nigeria-health-surveillance.dev"
_SENDER_NAME  = "Nigeria Surveillance Pipeline"


def send_pipeline_notification(result: "PipelineResult") -> bool:
    """
    Send an email notification summarising the pipeline run outcome.

    The email is formatted as plain text to ensure it renders
    correctly in all email clients without needing HTML templates.

    Parameters
    ----------
    result : PipelineResult
        The completed pipeline result object.

    Returns
    -------
    bool
        True if the notification was sent successfully, False otherwise.
        Callers should not treat False as a pipeline failure.
    """
    if not settings.notifications_enabled:
        logger.debug(
            "Notifications disabled — NOTIFY_EMAIL or SENDGRID_API_KEY not set"
        )
        return False

    subject = _build_subject(result)
    body    = _build_body(result)

    return _send_via_sendgrid(
        to_email=settings.notify_email,
        subject=subject,
        body=body,
    )


def _build_subject(result: "PipelineResult") -> str:
    """
    Build a descriptive email subject line.

    The status is always at the front so it is visible in
    email preview panes without opening the message.

    Parameters
    ----------
    result : PipelineResult

    Returns
    -------
    str
    """
    status_emoji = {
        "SUCCESS": "✅",
        "PARTIAL": "⚠️",
        "FAILED":  "❌",
    }.get(result.status, "ℹ️")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    return (
        f"{status_emoji} [{result.status}] "
        f"Nigeria Surveillance ETL — {timestamp}"
    )


def _build_body(result: "PipelineResult") -> str:
    """
    Build a structured plain-text email body.

    Includes run statistics, stage timings, any warnings, and
    the error message if the pipeline failed.

    Parameters
    ----------
    result : PipelineResult

    Returns
    -------
    str
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    separator = "─" * 50

    lines = [
        "Nigeria Disease Surveillance — ETL Pipeline Report",
        separator,
        f"Status      : {result.status}",
        f"Timestamp   : {timestamp}",
        f"Duration    : {result.duration_seconds:.1f} seconds",
        "",
        "Record counts:",
        f"  Extracted : {result.records_extracted:,}",
        f"  Loaded    : {result.records_loaded:,}",
        f"  Failed    : {result.records_failed:,}",
        "",
    ]

    # Stage timings — useful for spotting which stage is slow
    if result.stage_timings:
        lines.append("Stage timings:")
        for stage, secs in result.stage_timings.items():
            lines.append(f"  {stage:<20} {secs:.1f}s")
        lines.append("")

    # Warnings
    if result.warnings:
        lines.append(f"Warnings ({len(result.warnings)}):")
        for warning in result.warnings[:10]:  # Cap at 10 for readability
            lines.append(f"  • {warning}")
        if len(result.warnings) > 10:
            lines.append(f"  ... and {len(result.warnings) - 10} more")
        lines.append("")

    # Error details
    if result.error_message:
        lines += [
            "Error:",
            separator,
            result.error_message,
            separator,
            "",
        ]

    lines += [
        "──",
        "This is an automated message from the Nigeria Disease",
        "Surveillance ETL pipeline. Do not reply to this email.",
        f"Environment: {settings.app_env}",
    ]

    return "\n".join(lines)


def _send_via_sendgrid(
    to_email: str,
    subject: str,
    body: str,
) -> bool:
    """
    Deliver an email via the SendGrid v3 Mail Send API.

    Uses the REST API directly rather than the sendgrid Python
    library to avoid an extra dependency. The v3 API is stable
    and well-documented.

    Parameters
    ----------
    to_email : str
        Recipient email address.
    subject : str
        Email subject line.
    body : str
        Plain-text email body.

    Returns
    -------
    bool
        True on successful delivery (HTTP 202), False otherwise.
    """
    payload = {
        "personalizations": [
            {
                "to": [{"email": to_email}],
            }
        ],
        "from": {
            "email": _SENDER_EMAIL,
            "name":  _SENDER_NAME,
        },
        "subject": subject,
        "content": [
            {
                "type":  "text/plain",
                "value": body,
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {settings.sendgrid_api_key}",
        "Content-Type":  "application/json",
    }

    try:
        response = requests.post(
            _SENDGRID_API_URL,
            headers=headers,
            data=json.dumps(payload),
            timeout=15,
        )

        # SendGrid returns 202 Accepted on success — not 200
        if response.status_code == 202:
            logger.info(
                "Pipeline notification sent to %s (status=202)",
                to_email,
            )
            return True

        logger.warning(
            "SendGrid returned unexpected status %d: %s",
            response.status_code,
            response.text[:200],
        )
        return False

    except requests.exceptions.Timeout:
        logger.warning("SendGrid request timed out — notification not sent")
        return False
    except requests.exceptions.RequestException as exc:
        logger.warning("SendGrid request failed: %s", exc)
        return False
    except Exception as exc:
        # Catch-all so a notification bug never bubbles up to the pipeline
        logger.warning("Unexpected notification error: %s", exc)
        return False
