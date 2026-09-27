import base64
import email
import imaplib
import re
from datetime import datetime
from email.header import decode_header, make_header
from email.policy import default as email_policy
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from pathlib import Path

import httpx
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.mail import EmailMultiAlternatives
from django.db.models import Q
from django.utils import timezone

from .ai import AIService
from .models import (
    Attachment,
    EmailIntakeMailbox,
    EmailIntakeMessage,
    EmailIntakeRoute,
    EndorsementItem,
    EndorsementRequest,
    Policy,
    PortalNotification,
    UserProfile,
    WorkflowEvent,
)
from .services import FileIntakeService, NotificationService, WorkflowService, json_safe


SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}


class EmailIntakeService:
    """Read inbound endorsement emails and route them through the normal SmartEndorse workflow."""

    @classmethod
    def poll_all(cls):
        result = {"mailboxes": 0, "messages": 0, "processed": 0, "failed": 0}
        for mailbox in EmailIntakeMailbox.objects.filter(is_active=True).order_by("id"):
            result["mailboxes"] += 1
            stats = cls.poll_mailbox(mailbox)
            for key in ("messages", "processed", "failed"):
                result[key] += stats[key]
        return result

    @classmethod
    def poll_mailbox(cls, mailbox):
        stats = {"messages": 0, "processed": 0, "failed": 0}
        try:
            messages = (
                cls._graph_messages(mailbox)
                if mailbox.provider == EmailIntakeMailbox.Provider.MICROSOFT_GRAPH
                else cls._imap_messages(mailbox)
            )
            for source in messages:
                stats["messages"] += 1
                record, created = EmailIntakeMessage.objects.get_or_create(
                    mailbox=mailbox,
                    provider_message_id=source["provider_message_id"],
                    defaults={
                        "internet_message_id": source.get("internet_message_id", ""),
                        "sender_email": source.get("sender_email", ""),
                        "sender_name": source.get("sender_name", ""),
                        "subject": source.get("subject", "")[:500],
                        "body_text": source.get("body_text", ""),
                        "received_at": source.get("received_at"),
                        "attachment_count": len(source.get("attachments", [])),
                        "metadata": {"provider": mailbox.provider},
                    },
                )
                if not created and record.status in {
                    EmailIntakeMessage.Status.PROCESSED,
                    EmailIntakeMessage.Status.NEEDS_REVIEW,
                    EmailIntakeMessage.Status.IGNORED,
                }:
                    cls._mark_source_read(mailbox, source)
                    continue
                if not created:
                    record.internet_message_id = source.get("internet_message_id", "")
                    record.sender_email = source.get("sender_email", "")
                    record.sender_name = source.get("sender_name", "")
                    record.subject = source.get("subject", "")[:500]
                    record.body_text = source.get("body_text", "")
                    record.received_at = source.get("received_at")
                    record.attachment_count = len(source.get("attachments", []))
                    record.error = ""
                    record.save()

                try:
                    cls.process_message(record, source)
                    stats["processed"] += 1
                    cls._mark_source_read(mailbox, source)
                except Exception as exc:
                    record.status = EmailIntakeMessage.Status.FAILED
                    record.error = str(exc)
                    record.processed_at = timezone.now()
                    record.save(update_fields=["status", "error", "processed_at", "updated_at"])
                    stats["failed"] += 1
            mailbox.last_polled_at = timezone.now()
            mailbox.save(update_fields=["last_polled_at", "updated_at"])
        except Exception:
            mailbox.last_polled_at = timezone.now()
            mailbox.save(update_fields=["last_polled_at", "updated_at"])
            raise
        return stats

    @classmethod
    def process_message(cls, record, source):
        mailbox = record.mailbox
        record.status = EmailIntakeMessage.Status.PROCESSING
        record.error = ""
        record.save(update_fields=["status", "error", "updated_at"])

        route, requester, organization = cls._resolve_sender(mailbox, record.sender_email)
        text_context = "\n".join(
            [
                record.subject or "",
                record.body_text or "",
                " ".join(att.get("name", "") for att in source.get("attachments", [])),
            ]
        )
        policy = cls._resolve_policy(text_context, organization, route)
        endorsement_type = cls._resolve_type(text_context, route)
        explicit_date = cls._find_effective_date(text_context)
        provisional_date = explicit_date or cls._provisional_date(record.received_at, policy)

        request_obj = EndorsementRequest.objects.create(
            policy=policy,
            endorsement_type=endorsement_type,
            effective_date=provisional_date,
            requester=requester,
            requester_organization=organization,
            currency=policy.currency,
            metadata={
                "intake_channel": "EMAIL",
                "email_intake_message_id": record.pk,
                "email_provider_message_id": record.provider_message_id,
                "email_internet_message_id": record.internet_message_id,
                "email_sender": record.sender_email,
                "email_subject": record.subject,
                "effective_date_source": "email" if explicit_date else "provisional",
            },
        )
        record.request = request_obj
        record.save(update_fields=["request", "updated_at"])

        WorkflowEvent.objects.create(
            request=request_obj,
            actor=requester,
            event_type="EMAIL_INTAKE_RECEIVED",
            description=f"Endorsement received by email from {record.sender_email}.",
            payload=json_safe({
                "email_message_id": record.pk,
                "subject": record.subject,
                "sender": record.sender_email,
                "body": (record.body_text or "")[:12000],
                "attachment_count": len(source.get("attachments", [])),
            }),
        )

        cls._extract_body_items(request_obj, record.body_text)
        created_attachments = cls._save_attachments(request_obj, source.get("attachments", []))
        cls._process_attachments(request_obj, created_attachments)
        cls._apply_extracted_effective_date(request_obj)

        if not request_obj.items.exists():
            request_obj.validation_errors = [
                "Email received, but no member rows could be identified from the email body or supported attachments."
            ]
            request_obj.save(update_fields=["validation_errors", "updated_at"])
            record.status = EmailIntakeMessage.Status.NEEDS_REVIEW
            record.error = request_obj.validation_errors[0]
        elif mailbox.auto_submit:
            WorkflowService.submit(request_obj, requester)
            request_obj.refresh_from_db()
            if request_obj.status in {
                EndorsementRequest.Status.NEEDS_INFO,
                EndorsementRequest.Status.FAILED,
            }:
                record.status = EmailIntakeMessage.Status.NEEDS_REVIEW
                record.error = "; ".join(request_obj.validation_errors or [])
            else:
                record.status = EmailIntakeMessage.Status.PROCESSED
        else:
            record.status = EmailIntakeMessage.Status.PROCESSED

        record.processed_at = timezone.now()
        record.metadata = {
            **(record.metadata or {}),
            "request_reference": request_obj.reference,
            "policy_number": policy.policy_number,
            "endorsement_type": endorsement_type,
            "item_count": request_obj.items.count(),
            "final_status": request_obj.status,
        }
        record.save(update_fields=["status", "error", "processed_at", "metadata", "updated_at"])

        NotificationService.create_portal(
            [requester],
            f"Email intake · {request_obj.reference}",
            f"Email '{record.subject or '(no subject)'} was converted into endorsement {request_obj.reference}.",
            request_obj,
            PortalNotification.Level.SUCCESS if record.status == EmailIntakeMessage.Status.PROCESSED else PortalNotification.Level.WARNING,
            {"email_intake_message_id": record.pk},
        )
        cls._send_acknowledgement(record, request_obj)
        return request_obj

    @classmethod
    def _resolve_sender(cls, mailbox, sender_email):
        sender = (sender_email or "").strip().lower()
        routes = list(
            mailbox.routes.filter(is_active=True)
            .select_related("organization", "requester", "default_policy")
            .order_by("-priority", "id")
        )
        route = next((item for item in routes if cls._sender_matches(item.sender_pattern, sender)), None)

        user = (
            User.objects.filter(email__iexact=sender, is_active=True)
            .select_related("profile__organization")
            .first()
        )
        if route:
            organization = route.organization
            requester = route.requester
            if not requester and user and hasattr(user, "profile") and user.profile.organization_id == organization.pk:
                requester = user
            if not requester:
                requester = (
                    User.objects.filter(
                        is_active=True,
                        profile__organization=organization,
                        profile__role__in=[
                            UserProfile.Role.CLIENT_ADMIN,
                            UserProfile.Role.REQUESTER,
                            UserProfile.Role.BROKER_ADMIN,
                            UserProfile.Role.BROKER_USER,
                            UserProfile.Role.AGENT,
                            UserProfile.Role.CHANNEL_PARTNER,
                        ],
                    )
                    .order_by("id")
                    .first()
                )
            if not requester:
                raise ValueError(
                    f"Sender route {route.sender_pattern} has no usable requester. Configure a requester on the Email Intake Route."
                )
            return route, requester, organization

        if user and hasattr(user, "profile"):
            return None, user, user.profile.organization

        requester = mailbox.default_requester
        if requester and hasattr(requester, "profile"):
            return None, requester, requester.profile.organization

        raise ValueError(
            f"No Email Intake Route or active portal user matches sender {sender_email}. Configure the sender in Django admin."
        )

    @staticmethod
    def _sender_matches(pattern, sender):
        pattern = (pattern or "").strip().lower()
        if not pattern:
            return False
        if pattern == "*":
            return True
        if pattern.startswith("@"):
            return sender.endswith(pattern)
        if pattern.startswith("*."):
            return sender.endswith("@" + pattern[2:]) or sender.endswith(pattern[1:])
        return sender == pattern

    @classmethod
    def _resolve_policy(cls, text, organization, route=None):
        qs = Policy.objects.filter(is_active=True).filter(
            Q(client=organization)
            | Q(access_grants__organization=organization, access_grants__can_create=True)
        ).distinct()
        candidates = list(qs.select_related("client", "insurer", "tpa"))
        normalized = (text or "").lower()

        direct = [
            policy for policy in candidates
            if policy.policy_number and policy.policy_number.lower() in normalized
        ]
        if len(direct) == 1:
            return direct[0]
        if len(direct) > 1:
            direct.sort(key=lambda p: len(p.policy_number), reverse=True)
            if len(direct[0].policy_number) > len(direct[1].policy_number):
                return direct[0]
            raise ValueError("More than one policy number was found in the email. Manual review is required.")

        if route and route.default_policy_id:
            if route.default_policy in candidates:
                return route.default_policy
            raise ValueError("The Email Intake Route default policy is not accessible to the sender organization.")

        if len(candidates) == 1:
            return candidates[0]

        raise ValueError(
            "Policy could not be determined from the email. Include the policy number in the subject/body or configure a default policy on the Email Intake Route."
        )

    @staticmethod
    def _resolve_type(text, route=None):
        if route and route.default_endorsement_type:
            return route.default_endorsement_type

        subject, _, body = (text or "").partition("\n")
        def detect(value):
            value = value.lower()
            deletion = re.search(r"\b(delete|deletion|remove|removal|cancel|cancellation|terminate|termination|exit)\b", value)
            addition = re.search(r"\b(add|addition|include|inclusion|enrol|enroll|enrollment|joiner|new member)\b", value)
            if deletion and not addition:
                return EndorsementRequest.Type.DELETION
            if addition and not deletion:
                return EndorsementRequest.Type.ADDITION
            return None

        return detect(subject) or detect(body) or (_ for _ in ()).throw(
            ValueError(
                "Endorsement type could not be determined. Use Addition/Add or Deletion/Delete in the email subject/body, or configure a default type on the sender route."
            )
        )

    @classmethod
    def _find_effective_date(cls, text):
        source = str(text or "")
        label = r"(?:effective\s*date|addition\s*date|deletion\s*date|endorsement\s*date|date\s*of\s*joining|doj|effective\s*from)"
        date_token = (
            r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
            r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|"
            r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|"
            r"\d{1,2}[-][A-Za-z]{3,9}[-]\d{2,4})"
        )
        match = re.search(rf"(?i){label}\s*(?::|=|-|–|—)?\s*{date_token}", source)
        if not match:
            return None
        return cls._parse_date(match.group(1))

    @staticmethod
    def _parse_date(value):
        value = str(value or "").strip()
        for fmt in (
            "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
            "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y",
        ):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _provisional_date(received_at, policy):
        value = (received_at.date() if received_at else timezone.localdate())
        if value < policy.effective_from:
            return policy.effective_from
        if value > policy.effective_to:
            return policy.effective_to
        return value

    @classmethod
    def _extract_body_items(cls, request_obj, body_text):
        text = str(body_text or "").strip()
        if len(text) < 8:
            return 0
        ai = FileIntakeService._ai_for_request(request_obj)
        rows = []
        if ai.available:
            try:
                rows = [FileIntakeService._normalize_ai_row(row) for row in ai.extract_text_rows(text)]
            except Exception:
                rows = []
        if not rows:
            try:
                row = ai.extract_form_fields_from_text(text)
                rows = [FileIntakeService._normalize_ai_row(row)]
            except Exception:
                rows = []

        created = 0
        for row in rows:
            identity = any(row.get(field) not in (None, "") for field in ("full_name", "member_no", "employee_no", "national_id"))
            populated = sum(row.get(field) not in (None, "") for field in (
                "full_name", "member_no", "employee_no", "national_id", "relationship",
                "date_of_birth", "gender", "plan_code", "effective_date",
            ))
            if not identity or populated < 2:
                continue
            values = FileIntakeService._to_item_values(request_obj, row)
            EndorsementItem.objects.create(
                request=request_obj,
                extracted_data=json_safe({
                    "normalized": row,
                    "source_raw": row.get("_source_raw", {}),
                    "source": "email_body",
                }),
                **values,
            )
            created += 1
        return created

    @classmethod
    def _save_attachments(cls, request_obj, attachments):
        created = []
        for part in attachments:
            name = Path(part.get("name") or "attachment.bin").name
            content = part.get("content") or b""
            if not content:
                continue
            suffix = Path(name).suffix.lower()
            attachment = Attachment.objects.create(
                request=request_obj,
                file=ContentFile(content, name=name),
                original_name=name,
                kind=FileIntakeService.kind_for_name(name),
                is_supplemental=request_obj.items.exists(),
            )
            if suffix not in SUPPORTED_EXTENSIONS:
                attachment.processed = False
                attachment.processing_error = "Retained from email, but this file type is not supported for automatic extraction."
                attachment.save(update_fields=["processed", "processing_error", "updated_at"])
            created.append(attachment)
        return created

    @classmethod
    def _process_attachments(cls, request_obj, attachments):
        supported = [a for a in attachments if Path(a.original_name).suffix.lower() in SUPPORTED_EXTENSIONS]
        structured = [a for a in supported if Path(a.original_name).suffix.lower() in {".xlsx", ".xls", ".csv"}]
        evidence = [a for a in supported if Path(a.original_name).suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}]

        for attachment in structured:
            attachment.is_supplemental = request_obj.items.exists()
            attachment.save(update_fields=["is_supplemental", "updated_at"])
            FileIntakeService.process(attachment)

        if evidence:
            for attachment in evidence:
                attachment.is_supplemental = request_obj.items.exists()
                attachment.save(update_fields=["is_supplemental", "updated_at"])
            if request_obj.items.exists():
                FileIntakeService.process_supplemental_evidence_bundle(evidence)
            else:
                FileIntakeService.process_initial_evidence_bundle(evidence)

    @classmethod
    def _apply_extracted_effective_date(cls, request_obj):
        explicit_dates = []
        items = list(request_obj.items.all())
        for item in items:
            data = item.extracted_data if isinstance(item.extracted_data, dict) else {}
            normalized = data.get("normalized") if isinstance(data.get("normalized"), dict) else {}
            raw_value = normalized.get("effective_date")
            parsed = FileIntakeService._date(raw_value)
            if parsed:
                explicit_dates.append(parsed)

        for attachment in request_obj.attachments.filter(processed=True):
            payload = attachment.extracted_payload if isinstance(attachment.extracted_payload, dict) else {}
            rows = payload.get("normalized_rows") if isinstance(payload.get("normalized_rows"), list) else []
            for row in rows:
                if isinstance(row, dict):
                    parsed = FileIntakeService._date(row.get("effective_date"))
                    if parsed:
                        explicit_dates.append(parsed)

        if not explicit_dates:
            return
        selected = min(explicit_dates)
        old = request_obj.effective_date
        if selected != old:
            request_obj.effective_date = selected
            metadata = request_obj.metadata or {}
            metadata["effective_date_source"] = "attachment_or_email_extraction"
            request_obj.metadata = metadata
            request_obj.save(update_fields=["effective_date", "metadata", "updated_at"])
            for item in items:
                data = item.extracted_data if isinstance(item.extracted_data, dict) else {}
                normalized = data.get("normalized") if isinstance(data.get("normalized"), dict) else {}
                if not FileIntakeService._date(normalized.get("effective_date")):
                    item.effective_date = selected
                    item.save(update_fields=["effective_date", "updated_at"])

    @staticmethod
    def _strip_html(value):
        value = re.sub(r"(?i)<\s*br\s*/?>", "\n", value or "")
        value = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6])\s*>", "\n", value)
        value = re.sub(r"<[^>]+>", " ", value)
        value = unescape(value)
        return re.sub(r"[ \t]+", " ", value).replace("\r", "").strip()

    @classmethod
    def _graph_token(cls, mailbox):
        url = f"https://login.microsoftonline.com/{mailbox.graph_tenant_id}/oauth2/v2.0/token"
        response = httpx.post(
            url,
            data={
                "client_id": mailbox.graph_client_id,
                "client_secret": mailbox.graph_client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    @classmethod
    def _graph_messages(cls, mailbox):
        if not all([mailbox.graph_tenant_id, mailbox.graph_client_id, mailbox.graph_client_secret, mailbox.email_address]):
            raise ValueError("Microsoft Graph mailbox requires tenant ID, client ID, client secret and mailbox email.")
        token = cls._graph_token(mailbox)
        headers = {"Authorization": f"Bearer {token}"}
        user = mailbox.email_address
        folder = mailbox.folder or "Inbox"
        url = (
            f"https://graph.microsoft.com/v1.0/users/{user}/mailFolders/{folder}/messages"
            f"?$filter=isRead eq false&$top={mailbox.max_messages_per_poll}"
            "&$select=id,internetMessageId,subject,receivedDateTime,from,body,hasAttachments"
            "&$orderby=receivedDateTime asc"
        )
        response = httpx.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        result = []
        for item in response.json().get("value", []):
            attachments = []
            if item.get("hasAttachments"):
                att_response = httpx.get(
                    f"https://graph.microsoft.com/v1.0/users/{user}/messages/{item['id']}/attachments",
                    headers=headers,
                    timeout=30,
                )
                att_response.raise_for_status()
                for attachment in att_response.json().get("value", []):
                    if attachment.get("@odata.type", "").endswith("fileAttachment") and attachment.get("contentBytes"):
                        attachments.append({
                            "name": attachment.get("name") or "attachment.bin",
                            "content": base64.b64decode(attachment["contentBytes"]),
                            "content_type": attachment.get("contentType", ""),
                        })
            sender = ((item.get("from") or {}).get("emailAddress") or {})
            body = item.get("body") or {}
            received = item.get("receivedDateTime")
            try:
                received_at = datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None
            except ValueError:
                received_at = None
            result.append({
                "provider_message_id": item["id"],
                "internet_message_id": item.get("internetMessageId") or "",
                "sender_email": sender.get("address") or "",
                "sender_name": sender.get("name") or "",
                "subject": item.get("subject") or "",
                "body_text": cls._strip_html(body.get("content") or "") if str(body.get("contentType") or "").lower() == "html" else (body.get("content") or ""),
                "received_at": received_at,
                "attachments": attachments,
                "_graph_token": token,
            })
        return result

    @classmethod
    def _imap_messages(cls, mailbox):
        if not mailbox.imap_host:
            raise ValueError("IMAP mailbox requires a host.")
        client = (
            imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port)
            if mailbox.imap_use_ssl
            else imaplib.IMAP4(mailbox.imap_host, mailbox.imap_port)
        )
        try:
            if not mailbox.imap_use_ssl:
                client.starttls()
            client.login(mailbox.imap_username or mailbox.email_address, mailbox.imap_password)
            client.select(mailbox.folder or "INBOX")
            status, data = client.uid("search", None, "UNSEEN")
            if status != "OK":
                raise RuntimeError("IMAP search failed.")
            uids = (data[0].split() if data and data[0] else [])[: mailbox.max_messages_per_poll]
            result = []
            for uid in uids:
                status, payload = client.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not payload:
                    continue
                raw = next((part[1] for part in payload if isinstance(part, tuple) and len(part) > 1), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw, policy=email_policy)
                sender_name, sender_email = parseaddr(msg.get("From", ""))
                subject = str(make_header(decode_header(msg.get("Subject", ""))))
                plain_parts, html_parts, attachments = [], [], []
                for part in msg.walk():
                    disposition = (part.get_content_disposition() or "").lower()
                    filename = part.get_filename()
                    if filename or disposition == "attachment":
                        name = str(make_header(decode_header(filename or "attachment.bin")))
                        attachments.append({
                            "name": name,
                            "content": part.get_payload(decode=True) or b"",
                            "content_type": part.get_content_type(),
                        })
                        continue
                    if part.get_content_type() == "text/plain":
                        try:
                            plain_parts.append(part.get_content())
                        except Exception:
                            pass
                    elif part.get_content_type() == "text/html":
                        try:
                            html_parts.append(part.get_content())
                        except Exception:
                            pass
                body = "\n".join(plain_parts).strip() or cls._strip_html("\n".join(html_parts))
                try:
                    received_at = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
                except Exception:
                    received_at = None
                result.append({
                    "provider_message_id": uid.decode(),
                    "internet_message_id": msg.get("Message-ID", ""),
                    "sender_email": sender_email,
                    "sender_name": sender_name,
                    "subject": subject,
                    "body_text": body,
                    "received_at": received_at,
                    "attachments": attachments,
                    "_imap_uid": uid.decode(),
                })
            return result
        finally:
            try:
                client.logout()
            except Exception:
                pass

    @classmethod
    def _mark_source_read(cls, mailbox, source):
        if not mailbox.mark_as_read:
            return
        if mailbox.provider == EmailIntakeMailbox.Provider.MICROSOFT_GRAPH:
            try:
                token = source.get("_graph_token") or cls._graph_token(mailbox)
                httpx.patch(
                    f"https://graph.microsoft.com/v1.0/users/{mailbox.email_address}/messages/{source['provider_message_id']}",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"isRead": True},
                    timeout=20,
                ).raise_for_status()
            except Exception:
                pass
            return

        try:
            client = (
                imaplib.IMAP4_SSL(mailbox.imap_host, mailbox.imap_port)
                if mailbox.imap_use_ssl
                else imaplib.IMAP4(mailbox.imap_host, mailbox.imap_port)
            )
            if not mailbox.imap_use_ssl:
                client.starttls()
            client.login(mailbox.imap_username or mailbox.email_address, mailbox.imap_password)
            client.select(mailbox.folder or "INBOX")
            client.uid("store", str(source.get("_imap_uid") or source["provider_message_id"]), "+FLAGS", "(\\Seen)")
            client.logout()
        except Exception:
            pass

    @staticmethod
    def _send_acknowledgement(record, request_obj):
        if not record.sender_email:
            return
        subject = f"SmartEndorse {request_obj.reference} · {request_obj.get_status_display()}"
        body = (
            f"Your email has been received and converted to endorsement {request_obj.reference}.\n\n"
            f"Policy: {request_obj.policy.policy_number}\n"
            f"Type: {request_obj.get_endorsement_type_display()}\n"
            f"Status: {request_obj.get_status_display()}\n"
            f"Members identified: {request_obj.items.count()}\n\n"
            "If additional information is required, SmartEndorse will send a follow-up notification."
        )
        EmailMultiAlternatives(
            subject=subject,
            body=body,
            to=[record.sender_email],
        ).send(fail_silently=True)
