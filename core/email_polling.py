"""Shared correction-mailbox pipeline for APScheduler and manual polling."""
import logging

from django.utils import timezone

from .email_intake import deliver_reply, process_email
from .mailbox import GraphMailbox, IMAPMailbox
from .models import EmailReply, InboundEmail, MailboxConfiguration


logger = logging.getLogger(__name__)


def poll_correction_mailbox(config, limit=50):
    stats = {"messages": 0, "processed": 0, "replies_sent": 0, "failed": 0}
    transport = None
    try:
        transport = GraphMailbox(config) if config.transport == MailboxConfiguration.Transport.GRAPH else IMAPMailbox(config)
        stats["messages"] = len(transport.poll(limit))
        config.last_sync_at = timezone.now()
        config.last_error = ""
    except Exception as exc:
        config.last_error = str(exc)
        stats["failed"] += 1
        logger.exception("Mailbox %s could not fetch emails.", config.pk)
    finally:
        if transport is not None and config.transport == MailboxConfiguration.Transport.GRAPH:
            try:
                transport.close()
            except Exception:
                logger.exception("Mailbox %s could not close its Graph client.", config.pk)
        config.save(update_fields=["last_sync_at", "last_error", "updated_at"])

    # Fetch failures must not block already stored emails or pending reply delivery.
    pending = InboundEmail.objects.filter(mailbox=config, processing_state=InboundEmail.State.RECEIVED).order_by("pk")
    for email_id in pending.values_list("pk", flat=True)[:limit]:
        try:
            process_email(email_id)
            stats["processed"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception("Email %s could not be processed; it remains pending for retry.", email_id)
    replies = EmailReply.objects.filter(email__mailbox=config, sent_at__isnull=True).order_by("pk")
    for reply_id in replies.values_list("pk", flat=True)[:limit]:
        try:
            if deliver_reply(reply_id):
                stats["replies_sent"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            stats["failed"] += 1
            logger.exception("Reply %s could not be delivered; it remains pending for retry.", reply_id)
    return stats


def poll_correction_mailboxes(*, limit=50, mailbox_id=None):
    result = {"mailboxes": 0, "messages": 0, "processed": 0, "replies_sent": 0, "failed": 0}
    boxes = MailboxConfiguration.objects.filter(is_active=True).order_by("pk")
    if mailbox_id is not None:
        boxes = boxes.filter(pk=mailbox_id)
    for config in boxes:
        result["mailboxes"] += 1
        try:
            stats = poll_correction_mailbox(config, limit=limit)
        except Exception:
            result["failed"] += 1
            logger.exception("Mailbox %s failed; continuing with the other active mailboxes.", config.pk)
            continue
        for key, value in stats.items():
            result[key] += value
    return result
