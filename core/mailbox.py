"""Mailbox transports used by APScheduler and the manual process_mailbox command."""
import base64
import imaplib
from urllib.parse import quote, urlsplit

import httpx
from django.core.mail import get_connection

from .email_intake import ingest_message, reply_message
from .credentials import MailboxCredentialError, credential
from .models import InboundEmail


class IMAPMailbox:
    def __init__(self, config):
        self.config = config

    def poll(self, limit=50):
        config = self.config
        result = []
        secret = credential(config.imap_password, label="IMAP password / OAuth token")
        # Always use TLS; credentials never leave an encrypted connection.
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=30) as client:
            username = config.imap_username or config.email_address
            if config.use_oauth:
                value = f"user={username}\x01auth=Bearer {secret}\x01\x01".encode()
                client.authenticate("XOAUTH2", lambda _: value)
            else:
                client.login(username, secret)
            status, _ = client.select(config.folder, readonly=True)
            if status != "OK":
                raise RuntimeError("Cannot open configured IMAP folder.")
            response = client.response("UIDVALIDITY")
            if not response or not response[1] or not response[1][0]:
                raise RuntimeError("IMAP server did not return UIDVALIDITY; cursor was not advanced.")
            validity = response[1][0].decode()
            cursor = config.cursor or {}
            last_uid = int(cursor.get("last_uid", 0)) if cursor.get("uidvalidity") == validity else 0
            status, data = client.uid("search", None, "UID", f"{last_uid + 1}:*")
            if status != "OK":
                raise RuntimeError("IMAP UID search failed.")
            ids = sorted(int(uid) for uid in (data[0] or b"").split() if int(uid) > last_uid)[:limit]
            for uid in ids:
                status, content = client.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = next((item[1] for item in content if isinstance(item, tuple)), None) if status == "OK" else None
                if raw is None:
                    raise RuntimeError(f"Cannot fetch IMAP UID {uid}; cursor retained for retry.")
                message = ingest_message(config, raw, f"{validity}:{uid}")
                result.append(message.pk)
                config.cursor = {"uidvalidity": validity, "last_uid": uid}
                config.save(update_fields=["cursor", "updated_at"])
        return result


class GraphMailbox:
    ROOT = "https://graph.microsoft.com/v1.0"

    def __init__(self, config):
        self.config = config
        secret = credential(config.graph_client_secret, label="Graph client secret")
        self.client = httpx.Client(timeout=60)
        try:
            response = self.client.post(
                f"https://login.microsoftonline.com/{quote(config.graph_tenant_id, safe='')}/oauth2/v2.0/token",
                data={"client_id": config.graph_client_id, "client_secret": secret, "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
            ).raise_for_status().json()
        except Exception:
            self.client.close()
            raise
        self.client.headers["Authorization"] = "Bearer " + response["access_token"]
        self.client.headers["Prefer"] = 'IdType="ImmutableId"'
        self.user_url = self.ROOT + "/users/" + quote(config.email_address, safe="")

    def close(self):
        self.client.close()

    def _checked_link(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or not parsed.path.startswith("/v1.0/"):
            raise ValueError("Graph returned an invalid delta link.")
        return url

    def poll(self, limit=50):
        config = self.config
        result = []
        cursor = dict(config.cursor or {})
        # Pending IDs are a saved page snapshot, so a restart never skips newly reordered messages.
        while len(result) < limit:
            pending = list(cursor.get("pending_ids", []))
            if not pending:
                url = cursor.get("next_link") or cursor.get("delta_link") or (
                    self.user_url + "/mailFolders/" + quote(config.folder, safe="") + f"/messages/delta?$select=id&$top={min(limit, 50)}"
                )
                page = self.client.get(self._checked_link(url)).raise_for_status().json()
                pending = [item["id"] for item in page.get("value", []) if "@removed" not in item]
                next_url = page.get("@odata.nextLink")
                cursor = {"next_link": self._checked_link(next_url)} if next_url else {"delta_link": self._checked_link(page["@odata.deltaLink"])}
                cursor["pending_ids"] = pending
                config.cursor = cursor
                config.save(update_fields=["cursor", "updated_at"])
                if not pending:
                    if next_url:
                        continue
                    break
            while pending and len(result) < limit:
                uid = pending[0]
                existing = InboundEmail.objects.filter(mailbox=config, message_uid=uid).first()
                if not existing:
                    raw = self.client.get(self.user_url + "/messages/" + quote(uid, safe="") + "/$value").raise_for_status().content
                    existing = ingest_message(config, raw, uid)
                result.append(existing.pk)
                pending.pop(0)
                cursor["pending_ids"] = pending
                config.cursor = dict(cursor)
                config.save(update_fields=["cursor", "updated_at"])
            if not pending and not cursor.get("next_link"):
                break
        return result

    def send_reply(self, reply):
        email = reply.email
        message_url = self.user_url + "/messages/" + quote(email.message_uid, safe="")
        if not reply.graph_draft_id:
            mime = reply_message(reply).message().as_bytes()
            draft = self.client.post(message_url + "/createReply", content=base64.b64encode(mime).decode(), headers={"Content-Type": "text/plain"}).raise_for_status().json()
            reply.graph_draft_id = draft["id"]
            reply.save(update_fields=["graph_draft_id", "updated_at"])
        draft_url = self.user_url + "/messages/" + quote(reply.graph_draft_id, safe="")
        state = self.client.get(draft_url + "?$select=isDraft").raise_for_status().json()
        if state.get("isDraft") is False:
            return  # A previous send succeeded but its response/DB acknowledgment was lost.
        self.client.patch(draft_url, json={
            "subject": reply.subject,
            "body": {"contentType": "HTML", "content": reply.body_html},
            "toRecipients": [{"emailAddress": {"address": email.sender}}],
            "ccRecipients": [], "bccRecipients": [],
        }).raise_for_status()
        if reply.correction_csv:
            attachments = self.client.get(draft_url + "/attachments").raise_for_status().json().get("value", [])
            name = f"corrections-{email.thread_reference}.csv"
            if not any(item.get("name") == name for item in attachments):
                self.client.post(draft_url + "/attachments", json={
                    "@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": "text/csv",
                    "contentBytes": base64.b64encode(reply.correction_csv.encode()).decode(),
                }).raise_for_status()
        self.client.post(draft_url + "/send", json={}).raise_for_status()


def smtp_connection(config):
    """Build reply delivery entirely from the mailbox's saved Admin settings."""
    if config.reply_backend == "CONSOLE":
        return get_connection(backend="django.core.mail.backends.console.EmailBackend")
    if not config.smtp_host:
        raise MailboxCredentialError("SMTP host is missing. Configure reply delivery in Admin > Mailbox configurations.")
    if config.smtp_use_tls and config.smtp_use_ssl:
        raise MailboxCredentialError("Choose either SMTP STARTTLS or implicit TLS/SSL in Admin > Mailbox configurations.")
    if config.smtp_username:
        credential(config.smtp_password, label="SMTP password")
    return get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=config.smtp_host,
        port=config.smtp_port,
        username=config.smtp_username,
        password=config.smtp_password,
        use_tls=config.smtp_use_tls,
        use_ssl=config.smtp_use_ssl,
        timeout=config.smtp_timeout,
        ssl_keyfile="",
        ssl_certfile="",
    )
