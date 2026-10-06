"""Authorized, idempotent email intake and correction of endorsement member rows."""
import csv
import io
import json
import re
from email import policy as email_policy
from email.parser import BytesParser
from email.utils import getaddresses, make_msgid, parsedate_to_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .email_authorization import authority_decision, resolve_authority
from .ai import CANONICAL_FIELDS
from .models import (
    Attachment, EmailAuthority, EmailEvidence, EmailReply, EndorsementItem,
    EndorsementRequest, InboundEmail, Policy, WorkflowEvent,
)
from .services import FileIntakeService, PricingEngine, ValidationService, WorkflowService, json_safe, platform_config

REFERENCE_PATTERN = re.compile(r"\b(?:EML|END)-\d{6}-[A-F0-9]{8}\b", re.I)
ROW_FIELDS = ["row_reference", *CANONICAL_FIELDS]
EDITABLE_FIELDS = ["member_no", "employee_no", "national_id", "full_name", "relationship", "date_of_birth", "gender", "plan", "annual_salary", "sum_assured", "effective_date"]
NULLABLE_FIELDS = {"plan", "date_of_birth", "effective_date", "annual_salary", "sum_assured"}
ROW_METADATA = {"row_reference", "member_ref", "member_reference", "errors", "status", "source_raw"}


class EmailHTMLText(HTMLParser):
    """Preserve editable table cells while discarding common quoted HTML replies."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.quote_depth = 0
        self.quote_tag = ""
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        quoted = tag in {"blockquote", "script", "style"} or any(x in values.get("class", "") for x in ("gmail_quote", "yahoo_quoted")) or values.get("id") in {"divRplyFwdMsg", "appendonsend"}
        if self.quote_depth:
            if tag == self.quote_tag:
                self.quote_depth += 1
            return
        if quoted:
            self.quote_depth = 1
            self.quote_tag = tag
        elif tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []
        elif tag in {"br", "p", "div", "table", "h1", "h2", "h3", "h4"}:
            (self.cell if self.cell is not None else self.chunks).append(" " if self.cell is not None else "\n")

    def handle_endtag(self, tag):
        if self.quote_depth:
            if tag == self.quote_tag:
                self.quote_depth -= 1
            return
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            out = io.StringIO()
            csv.writer(out).writerow(self.row)
            self.chunks.append(out.getvalue())
            self.row = None
        elif tag in {"p", "div", "table", "h1", "h2", "h3", "h4"}:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self.quote_depth:
            return
        if self.cell is None and not data.strip():
            return
        (self.cell if self.cell is not None else self.chunks).append(data)


def current_reply_text(text):
    lines = []
    for line in (text or "").splitlines():
        if re.match(r"^\s*(?:On .+wrote:|-----Original Message-----|From:\s|_{5,})", line, re.I):
            break
        if not line.lstrip().startswith(">"):
            lines.append(line)
    return "\n".join(lines).strip()


def ingest_message(mailbox, raw, uid):
    message = BytesParser(policy=email_policy.default).parsebytes(raw)
    senders = getaddresses(message.get_all("From", []))
    sender = senders[0][1].strip().lower() if len(senders) == 1 else ""
    message_id = str(message.get("Message-ID", "")).strip()[:512]
    existing = InboundEmail.objects.filter(mailbox=mailbox).filter(Q(message_uid=str(uid)) | (Q(message_id=message_id) if message_id else Q(pk=None))).first()
    if existing:
        return existing
    body = message.get_body(preferencelist=("plain", "html"))
    text = ""
    if body:
        try:
            text = body.get_content()
        except (UnicodeError, LookupError):
            text = body.get_payload(decode=True).decode("utf-8", errors="replace")
        if body.get_content_type() == "text/html":
            parser = EmailHTMLText()
            parser.feed(text)
            text = "".join(parser.chunks)
    try:
        received = parsedate_to_datetime(str(message.get("Date", "")))
        if timezone.is_naive(received):
            received = timezone.make_aware(received)
    except (TypeError, ValueError, OverflowError):
        received = timezone.now()
    # MIME/header dates are untrusted. Authorization is checked at processing time.
    headers = {key.lower(): message.get_all(key, []) for key in message.keys()}
    with transaction.atomic():
        obj, created = InboundEmail.objects.get_or_create(mailbox=mailbox, message_uid=str(uid), defaults={
            "message_id": message_id, "sender": sender, "subject": str(message.get("Subject", ""))[:998],
            "in_reply_to": str(message.get("In-Reply-To", ""))[:512],
            "references_header": str(message.get("References", "")), "body_text": current_reply_text(text),
            "received_at": received, "headers": headers,
        })
        if not created:
            return obj
        obj.raw_message.save(f"{obj.reference}.eml", ContentFile(raw), save=True)
        maximum = platform_config().maximum_upload_mb * 1024 * 1024
        for part in message.iter_attachments():
            name = Path((part.get_filename() or "attachment").replace("\\", "/")).name[:255]
            content = part.get_payload(decode=True) or b""
            if not content and part.get_content_type() == "message/rfc822":
                content = part.as_bytes()
            if len(content) > maximum:
                obj.processing_error = f"Attachment {name} exceeds the configured upload limit."
                obj.save(update_fields=["processing_error", "updated_at"])
                continue
            evidence = EmailEvidence(email=obj, original_name=name)
            evidence.file.save(name, ContentFile(content), save=True)
    return obj


def _authentication_clauses(header):
    """Split Authentication-Results without treating comments/quoted text as results."""
    clauses, chars = [], []
    depth, quoted, escaped = 0, False, False
    for char in str(header):
        if escaped:
            if not depth:
                chars.append(char)
            escaped = False
        elif char == "\\" and (depth or quoted):
            if not depth:
                chars.append(char)
            escaped = True
        elif depth:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
        elif char == '"':
            quoted = not quoted
            chars.append(char)
        elif char == "(" and not quoted:
            depth = 1
            chars.append(" ")
        elif char == ";" and not quoted:
            clauses.append("".join(chars).strip())
            chars = []
        else:
            chars.append(char)
    return [] if depth or quoted or escaped else [*clauses, "".join(chars).strip()]


def sender_authentication_error(email):
    if "@" not in email.sender:
        return "Sender verification failed: the email must contain exactly one valid From address."
    mailbox = email.mailbox
    if not mailbox.require_sender_authentication:
        return ""
    trusted = {str(x).strip().casefold().rstrip(".") for x in mailbox.trusted_authserv_ids if str(x).strip()}
    settings_hint = "Review Sender verification in Admin > Mailbox configurations. Email authorities control policy permission separately."
    if not trusted:
        return f"Sender verification failed: Trusted authserv IDs is empty. {settings_hint}"
    results = email.headers.get("authentication-results", [])
    if not results:
        return ("Sender verification failed: this email has no Authentication-Results header. "
                "Uncheck Require sender authentication in Admin > Mailbox configurations > Sender verification "
                "only if your receiving mail provider already verifies senders. Email authorities and policy access remain required.")
    domain = email.sender.rsplit("@", 1)[1].strip().casefold().rstrip(".")
    trusted_found, observed, unidentified = False, set(), False
    for result in results:
        clauses = _authentication_clauses(result)
        if not clauses:
            continue
        identifier = re.fullmatch(r'(?:"([^"\\]+)"|([^\s=;]+))(?:\s+\d+)?', clauses[0])
        if not identifier:
            unidentified = True
            continue
        authserv = (identifier.group(1) or identifier.group(2)).casefold().rstrip(".")
        observed.add(authserv)
        if authserv not in trusted:
            continue
        trusted_found = True
        for clause in clauses[1:]:
            if not re.match(r"^dmarc(?:/\d+)?\s*=\s*pass(?:\s|$)", clause, re.I):
                continue
            # Consume entire quoted values so a reason="header.from=..." cannot
            # be confused with the actual property of a successful DMARC check.
            properties = {match.group(1).casefold(): match.group(2).strip('"').casefold().rstrip(".") for match in re.finditer(
                r'(?:^|\s)([\w.\-]+)\s*=\s*("(?:\\.|[^"\\])*"|[^\s]+)', clause,
            )}
            if properties.get("header.from") == domain:
                return ""
    if trusted_found:
        return f"Sender verification failed: the trusted receiving server did not report dmarc=pass aligned with From domain {domain}. {settings_hint}"
    if unidentified:
        return ("Sender verification failed: Authentication-Results has no receiving server ID (as in Microsoft 365 headers). "
                "This mailbox requires an identified trusted DMARC result. Configure your gateway to supply that result, "
                "or disable Require sender authentication in Admin only when your mail gateway already verifies senders. "
                "An active Email authority is still required.")
    return f"Sender verification failed: no Authentication-Results server matches Trusted authserv IDs. Observed: {', '.join(sorted(observed)) or 'none'}. {settings_hint}"


def authenticated_sender(email):
    return not sender_authentication_error(email)


def _structured_body(text):
    if not text.strip():
        return []
    block = re.search(r"BEGIN MEMBERS\s*(.*?)\s*END MEMBERS", text, re.S | re.I)
    data = block.group(1) if block else text
    data = data.strip().removeprefix("```json").removeprefix("```csv").removesuffix("```").strip()
    try:
        parsed = json.loads(data)
        rows = parsed if isinstance(parsed, list) else parsed.get("members", parsed.get("items", [])) if isinstance(parsed, dict) else []
        if rows:
            if not all(isinstance(row, dict) for row in rows):
                raise ValueError("Each member must be a JSON object.")
            return rows
    except json.JSONDecodeError:
        pass
    lines = data.splitlines()
    rows = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        delimiter = next((char for char in (",", "\t", "|", ";") if char in line), None)
        if not delimiter:
            continue
        headers = next(csv.reader([line], delimiter=delimiter))
        keys = [FileIntakeService._map_header(key.strip()) for key in headers]
        if not {"full_name", "member_no", "row_reference", "member_ref", "member_reference"}.intersection(keys):
            continue
        body_lines = []
        for body_line in lines[index:]:
            if not body_line.strip() or delimiter not in body_line:
                break
            body_lines.append(body_line)
            index += 1
        rows.extend(csv.DictReader([line, *body_lines], delimiter=delimiter))
    return rows


def _row_reference(row):
    raw = row.get("_source_raw") if isinstance(row.get("_source_raw"), dict) else row
    for data in (row, raw):
        for key, value in data.items():
            if FileIntakeService._map_header(key) in {"row_reference", "member_ref", "member_reference"}:
                return str(value or "").strip().upper()
    return ""


def _match_row(request, row, is_correction=False):
    ref = _row_reference(row)
    if ref.startswith("ITEM-"):
        try:
            item = request.items.get(pk=int(ref[5:]))
        except (ValueError, EndorsementItem.DoesNotExist):
            raise ValueError(f"Member reference {ref} does not belong to this endorsement.")
        return item, "row_reference"
    matches = []
    for field in ("national_id", "member_no"):
        value = str(row.get(field) or "").strip()
        if value:
            found = list(request.items.filter(**{field + "__iexact": value})[:2])
            if len(found) > 1:
                raise ValueError(f"Ambiguous {field}: {value}. Use the ITEM member reference.")
            matches.extend(found)
    distinct = {item.pk: item for item in matches}
    if len(distinct) > 1:
        raise ValueError("Member identifiers match different rows. Use the ITEM member reference and correct the identifiers.")
    if distinct:
        return next(iter(distinct.values())), "identifier"
    name, dob = str(row.get("full_name") or "").strip(), FileIntakeService._date(row.get("date_of_birth"))
    if name and dob:
        found = list(request.items.filter(full_name__iexact=name, date_of_birth=dob)[:2])
        if len(found) == 1:
            return found[0], "name+dob"
        if len(found) > 1:
            raise ValueError("Name and date of birth are ambiguous. Use the ITEM member reference.")
    employee_no = str(row.get("employee_no") or "").strip()
    if employee_no:
        candidates = request.items.filter(employee_no__iexact=employee_no)
        # Dependents can share an employee number; distinct people must retain separate rows.
        if name:
            candidates = candidates.filter(full_name__iexact=name)
        if dob:
            candidates = candidates.filter(Q(date_of_birth=dob) | Q(date_of_birth=None))
        if row.get("relationship"):
            candidates = candidates.filter(relationship__iexact=row["relationship"])
        found = list(candidates[:2])
        if len(found) == 1:
            return found[0], "employee_no"
        if len(found) > 1:
            raise ValueError("Employee number is ambiguous. Use the ITEM member reference.")
    if ref and not (ref == "NEW" or re.fullmatch(r"DOC-\d+", ref)):
        raise ValueError(f"Unknown member reference {ref}.")
    if request.items.exists() and not ref and is_correction:
        raise ValueError("This row could not be matched. Keep its ITEM reference, or use NEW to explicitly add a member.")
    return None, "new"


def merge_rows(request, rows, email, actor):
    changed, created, unchanged, errors, replaced_documents = 0, 0, 0, [], set()
    # Helper columns must not trigger an LLM that fills intentionally blank correction fields.
    payload_rows = [{key: value for key, value in row.items() if FileIntakeService._map_header(key) not in ROW_METADATA} for row in rows]
    normalized, _ = FileIntakeService._normalize_structured(payload_rows, FileIntakeService._ai_for_request(request))
    for source, row in zip(rows, normalized):
        # Normalization must preserve stable row references, including when an identifier changes.
        row["_source_raw"] = source.get("_source_raw", source)
        try:
            item, matched_by = _match_row(request, row, is_correction=bool(email.thread_id))
            ref = _row_reference(row)
            if ref.startswith("DOC-"):
                document_id = int(ref[4:])
                if not request.attachments.filter(pk=document_id, processing_error__gt="").exists():
                    raise ValueError(f"Document reference {ref} is not a failed document on this endorsement.")
            if not any(row.get(key) not in (None, "") for key in CANONICAL_FIELDS):
                continue
            for key in CANONICAL_FIELDS:
                if isinstance(row.get(key), (dict, list, bool)):
                    raise ValueError(f"Invalid member field {key}: use a scalar value.")
            for date_field in ("date_of_birth", "effective_date"):
                if row.get(date_field) not in (None, "", "[CLEAR]") and FileIntakeService._date(row[date_field]) is None:
                    raise ValueError(f"Invalid {date_field}; use YYYY-MM-DD.")
            for amount_field in ("annual_salary", "sum_assured"):
                if row.get(amount_field) not in (None, "", "[CLEAR]") and FileIntakeService._decimal(row[amount_field]) is None:
                    raise ValueError(f"Invalid {amount_field}; use a number.")
            values = FileIntakeService._to_item_values(request, row)
            if not item:
                item = EndorsementItem(request=request, extracted_data={"normalized": json_safe(row), "source": "email", "email_id": email.pk}, **values)
                item.full_clean()
                item.save()
                created += 1
                if ref.startswith("DOC-"):
                    replaced_documents.add(int(ref[4:]))
                WorkflowEvent.objects.create(request=request, actor=actor, event_type="EMAIL_MEMBER_CREATED", description=f"Member {item.pk} added from {email.reference}.", payload={"email_id": email.pk, "item_id": item.pk})
                continue
            selected_plan = values["plan"] if row.get("plan_code") not in (None, "") else item.plan
            if selected_plan and selected_plan.sum_assured is not None:
                values["sum_assured"] = selected_plan.sum_assured
            before, changes = {}, {}
            for field in EDITABLE_FIELDS:
                canonical = "plan_code" if field == "plan" else field
                if row.get(canonical) in (None, ""):
                    continue  # A partial reply preserves omitted accepted values.
                raw_value = str(row.get(canonical)).strip()
                value = values[field]
                if raw_value.upper() == "[CLEAR]":
                    value = None if field in NULLABLE_FIELDS else ""
                elif field == "date_of_birth" and value is None:
                    raise ValueError("Date of birth is invalid; use YYYY-MM-DD.")
                old = getattr(item, field)
                old_compare = old.pk if field == "plan" and old else old
                new_compare = value.pk if field == "plan" and value else value
                if old_compare != new_compare:
                    before[field] = json_safe(old_compare)
                    changes[field] = json_safe(new_compare)
            if "plan" in changes and values["plan"] and item.sum_assured != values["sum_assured"]:
                before["sum_assured"] = json_safe(item.sum_assured)
                changes["sum_assured"] = json_safe(values["sum_assured"])
            was_correct = item.validation_status in {EndorsementItem.ValidationStatus.VALID, EndorsementItem.ValidationStatus.EXISTING, EndorsementItem.ValidationStatus.APPROVAL_REQUIRED}
            if changes:
                for field in changes:
                    value = values[field]
                    if str(row.get("plan_code" if field == "plan" else field)).strip().upper() == "[CLEAR]":
                        value = None if field in NULLABLE_FIELDS else ""
                    setattr(item, field, value)
                item.extracted_data = {**(item.extracted_data or {}), "email_correction": json_safe(row)}
                item.resolution_data = {**(item.resolution_data or {}), "last_email_id": email.pk}
                item.full_clean()
                item.save()
                changed += 1
                WorkflowEvent.objects.create(request=request, actor=actor, event_type="EMAIL_CORRECT_MEMBER_CHANGED" if was_correct else "EMAIL_MEMBER_CORRECTED", description=f"Member {item.pk} updated from {email.reference} and will be revalidated.", payload={"email_id": email.pk, "item_id": item.pk, "matched_by": matched_by, "before": before, "after": changes})
            else:
                unchanged += 1
            if ref.startswith("DOC-"):
                replaced_documents.add(int(ref[4:]))
        except (ValueError, TypeError, ValidationError) as exc:
            errors.append(str(exc))
    return {"changed": changed, "created": created, "unchanged": unchanged, "errors": errors, "replaced_documents": sorted(replaced_documents)}


def _identity_details(email, root, request):
    if request:
        return request.policy, request.endorsement_type, request.effective_date
    text = email.subject + "\n" + email.body_text
    policies = list(Policy.objects.filter(is_active=True))
    matches = [p for p in policies if re.search(r"(?<![\w/])" + re.escape(p.policy_number) + r"(?![\w/])", text, re.I)]
    if len(matches) > 1:
        raise ValueError("More than one policy number was found. Specify exactly one policy.")
    policy = matches[0] if matches else (root.policy if root and root.policy_id else email.mailbox.default_policy)
    deletion = bool(re.search(r"\b(delet(?:e|ion)|remov(?:e|al)|terminat(?:e|ion))\b", text, re.I))
    addition = bool(re.search(r"\b(add(?:ition)?|enroll(?:ment)?)\b", text, re.I))
    if deletion and addition:
        raise ValueError("Both addition and deletion were requested. Send them as separate endorsements.")
    endorsement_type = EndorsementRequest.Type.DELETION if deletion else EndorsementRequest.Type.ADDITION if addition else ""
    if not endorsement_type and root:
        endorsement_type = root.extracted_payload.get("endorsement_type", "")
    date_match = re.search(r"(?:effective[_ ]date|effective from|with effect from)\s*[:=]?\s*(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2}[/-]\d{4})", text, re.I)
    effective = FileIntakeService._date(date_match.group(1)) if date_match else None
    if not effective and root:
        effective = FileIntakeService._date(root.extracted_payload.get("effective_date"))
    return policy, endorsement_type, effective


@transaction.atomic
def process_email(email_id, force=False, recheck_authorization=False):
    email = InboundEmail.objects.select_for_update().select_related("mailbox", "thread", "endorsement__policy", "policy").get(pk=email_id)
    if email.processing_state != InboundEmail.State.RECEIVED and not force and not (recheck_authorization and email.processing_state == InboundEmail.State.UNAUTHORIZED):
        return email
    if any(value.lower() not in {"no", ""} for value in email.headers.get("auto-submitted", [])) or email.headers.get("x-autoreply") or any("bulk" in value.lower() for value in email.headers.get("precedence", [])) or email.sender.casefold() == email.mailbox.email_address.casefold():
        email.processing_state = InboundEmail.State.IGNORED
        email.save(update_fields=["processing_state", "updated_at"])
        return email
    try:
        references = {ref.upper() for ref in REFERENCE_PATTERN.findall(email.subject)}
        root = email.thread or email
        request = email.endorsement
        if len(references) > 1:
            raise ValueError("The subject has multiple references. Keep only the original email reference.")
        if references:
            reference = next(iter(references))
            if reference.startswith("EML-"):
                matched = InboundEmail.objects.select_for_update().filter(reference=reference, mailbox=email.mailbox).first()
                if not matched:
                    raise ValueError("The subject reference was not found in this mailbox.")
                root = matched.thread or matched
                if root.pk == email.pk:
                    request = email.endorsement
                else:
                    email.thread = root
                    request = root.endorsement
            else:
                request = EndorsementRequest.objects.select_for_update().filter(reference=reference).first()
                if not request:
                    raise ValueError("The endorsement reference was not found.")
                first = request.inbound_emails.filter(mailbox=email.mailbox, thread=None).order_by("pk").first()
                if not first:
                    raise ValueError("This endorsement has no original email in this mailbox.")
                root = first
                email.thread = root
        if request:
            request = EndorsementRequest.objects.select_for_update().select_related("policy").get(pk=request.pk)
        policy, endorsement_type, effective = _identity_details(email, root, request)
        authentication_error = sender_authentication_error(email)
        if authentication_error:
            email.processing_state = InboundEmail.State.UNAUTHORIZED
            email.processing_error = authentication_error
            email.save()
            return email
        # An unknown policy may be resolved only within an active, explicitly granted scope.
        if not policy:
            permitted = {a.policy_id for a in EmailAuthority.objects.filter(is_active=True) if resolve_authority(email.sender, a.policy, endorsement_type)[0]}
            if len(permitted) == 1:
                policy = Policy.objects.get(pk=next(iter(permitted)))
        decision = authority_decision(email.sender, policy, endorsement_type)
        if not decision.authority:
            email.processing_state = InboundEmail.State.UNAUTHORIZED
            email.processing_error = "Sender authorization failed: " + decision.reason
            email.save()
            return email
        authority, actor = decision.authority, decision.actor
        if request and authority.organization_id not in {request.requester_organization_id, request.policy.insurer_id}:
            email.processing_state = InboundEmail.State.UNAUTHORIZED
            email.processing_error = "Sender organization cannot correct this request."
            email.save()
            return email
        email.policy, email.authority = policy, authority
        email.endorsement = request
        email.extracted_payload = {"endorsement_type": endorsement_type, "effective_date": effective.isoformat() if effective else None}
        if request and root.corrections.filter(pk__gt=email.pk, processed_at__isnull=False, endorsement=request).exists():
            raise ValueError("A newer email correction has already been processed. Reprocess the latest reply instead.")
        if request and request.status not in {EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.NEEDS_INFO}:
            raise ValueError("This endorsement has already left intake/correction. Staff must reopen it before applying a reply.")
        missing = [label for label, value in (("endorsement type (Addition or Deletion)", endorsement_type), ("effective date (YYYY-MM-DD)", effective)) if not value]
        if missing:
            email.processing_state = InboundEmail.State.NEEDS_INFO
            email.processing_error = "Please supply: " + ", ".join(missing) + "."
            email.save()
            queue_reply(email)
            return email
        if not request:
            request = EndorsementRequest.objects.create(policy=policy, endorsement_type=endorsement_type, effective_date=effective, requester=actor, requester_organization=authority.organization, currency=policy.currency, metadata={"email_reference": root.reference})
            root.endorsement = request
            root.policy = policy
            root.save(update_fields=["endorsement", "policy", "updated_at"])
        email.endorsement = request
        email.save()
        rows = _structured_body(email.body_text)
        extraction_errors = []
        if not rows and re.search(r"\b(?:member[_ ]?(?:ref|no|name)|full[_ ]name|date[_ ]of[_ ]birth|\bdob\b|employee[_ ](?:id|no))\b", email.body_text, re.I):
            try:
                ai = FileIntakeService._ai_for_request(request)
                rows = ai.extract_text_rows(email.body_text)
                if not rows:
                    raise ValueError("No member rows were extracted from the email body.")
            except Exception as exc:
                extraction_errors.append("Email body extraction failed: " + str(exc))
        for evidence in email.evidence.all():
            attachment = evidence.attachment
            if not attachment:
                attachment = Attachment(request=request, original_name=evidence.original_name, kind=FileIntakeService.kind_for_name(evidence.original_name), is_supplemental=bool(email.thread_id))
                with evidence.file.open("rb") as content:
                    attachment.file.save(evidence.original_name, ContentFile(content.read()), save=True)
                evidence.attachment = attachment
                evidence.save(update_fields=["attachment", "updated_at"])
            try:
                raw, normalized, metadata = FileIntakeService._extract(attachment)
                for raw_row, normalized_row in zip(raw, normalized):
                    normalized_row["_source_raw"] = raw_row
                rows.extend(normalized)
                attachment.processed = True
                attachment.processing_error = ""
                attachment.extracted_payload = json_safe({"raw_rows": raw, "normalized_rows": normalized, "email_id": email.pk, **metadata})
                previous = request.attachments.filter(original_name=attachment.original_name).exclude(pk=attachment.pk).exclude(processing_error="")
                for old in previous:
                    old.extracted_payload = {**(old.extracted_payload or {}), "superseded_by_email_id": email.pk}
                    old.save(update_fields=["extracted_payload", "updated_at"])
            except Exception as exc:
                attachment.processing_error = str(exc)
                attachment.processed = False
                extraction_errors.append(f"{evidence.original_name}: {exc}")
            attachment.save()
        summary = merge_rows(request, rows, email, actor)
        for document_id in summary.pop("replaced_documents"):
            attachment = request.attachments.get(pk=document_id)
            attachment.extracted_payload = {**(attachment.extracted_payload or {}), "superseded_by_email_id": email.pk}
            attachment.save(update_fields=["extracted_payload", "updated_at"])
        errors, score = ValidationService.validate(request)
        all_errors = list(dict.fromkeys([*errors, *summary["errors"], *extraction_errors]))
        if not request.items.exists():
            all_errors.append("No member details were extracted. Fill a NEW member row or resend readable documents.")
        if email.processing_error and "upload limit" in email.processing_error:
            all_errors.append(email.processing_error)
        request.validation_errors, request.validation_score = all_errors, score
        request.save(update_fields=["validation_errors", "validation_score", "updated_at"])
        email.extracted_payload = {**email.extracted_payload, "summary": summary, "errors": all_errors}
        email.processing_error = "\n".join(all_errors)
        email.processing_state = InboundEmail.State.NEEDS_INFO if all_errors else InboundEmail.State.PROCESSED
        email.processed_at = timezone.now()
        email.save()
        WorkflowService.transition(request, EndorsementRequest.Status.NEEDS_INFO if all_errors else EndorsementRequest.Status.DRAFT, actor, "Email correction requires more information." if all_errors else "Email members extracted/corrected and revalidated.", {"email_id": email.pk, **summary}, notify=False)
        if not all_errors:
            PricingEngine.calculate_request(request)
            if email.mailbox.auto_submit:
                WorkflowService.submit(request, actor)
        queue_reply(email)
    except (ValueError, RuntimeError) as exc:
        email.processing_error = str(exc)
        email.processing_state = InboundEmail.State.NEEDS_REVIEW
        email.save()
        if email.authority_id and email.policy_id:
            queue_reply(email)
    return email


def _member_row(item):
    return {
        "row_reference": f"ITEM-{item.pk}", "member_no": item.member_no, "employee_no": item.employee_no,
        "national_id": item.national_id, "full_name": item.full_name, "relationship": item.relationship,
        "date_of_birth": item.date_of_birth.isoformat() if item.date_of_birth else "", "gender": item.gender,
        "plan_code": item.plan.code if item.plan_id else "", "annual_salary": str(item.annual_salary) if item.annual_salary is not None else "", "sum_assured": str(item.sum_assured) if item.sum_assured is not None else "",
        "effective_date": item.effective_date.isoformat() if item.effective_date else "",
    }


def queue_reply(email):
    if not email.authority_id:
        return None
    request = email.endorsement
    items = list(request.items.select_related("plan").order_by("pk")) if request else []
    correct = [item for item in items if item.validation_status != EndorsementItem.ValidationStatus.ERROR]
    incorrect = [item for item in items if item.validation_status == EndorsementItem.ValidationStatus.ERROR]
    reference = email.thread_reference
    subject = re.sub(r"^(?:\s*re:\s*)+", "", email.subject, flags=re.I)
    subject = REFERENCE_PATTERN.sub("", subject)
    subject = re.sub(r"\[SE:\s*\]", "", subject).strip()
    subject = f"Re: [SE: {reference}] {subject}"[:998]
    lines = [f"SmartEndorse reference: {reference}", f"Endorsement: {request.reference}" if request else "Endorsement pending required details.", f"Policy: {email.policy.policy_number}" if email.policy_id else "", "", "Keep the reference in this subject when replying. Use the Member Ref to identify each row, including accepted members you change.", "Correct members are preserved. Changed accepted members are updated and revalidated. Blank fields preserve existing values; use [CLEAR] to clear a value.", "", f"CORRECT MEMBERS ({len(correct)})"]
    html_parts = [f"<p><strong>Reference: {escape(reference)}</strong></p><p>{escape(lines[1])}</p>", "<p>Reply to this email, keep the reference in the subject, and correct the error members below or in the attached CSV. Keep each Member Ref. Correct members are preserved; changes to them are revalidated. Blank fields preserve values; [CLEAR] explicitly clears a field.</p>"]
    columns = ["Member Ref", "Member No", "Employee No", "National ID", "Full Name", "DOB", "Gender", "Relationship", "Plan", "Annual Salary", "Sum Assured", "Effective Date", "Errors"]
    for title, group in (("Correct members", correct), ("Error members / needs correction", incorrect)):
        if title.startswith("Error"):
            lines.extend(["", f"ERROR MEMBERS / NEEDS CORRECTION ({len(incorrect)})"])
        html_parts.append(f"<h3>{escape(title)} ({len(group)})</h3><table border=\"1\" cellpadding=\"6\" cellspacing=\"0\" style=\"border-collapse:collapse;font:13px Arial\"><thead><tr>" + "".join(f"<th>{escape(c)}</th>" for c in columns) + "</tr></thead><tbody>")
        for item in group:
            row = _member_row(item)
            values = [row[key] for key in ("row_reference", "member_no", "employee_no", "national_id", "full_name", "date_of_birth", "gender", "relationship", "plan_code", "annual_salary", "sum_assured", "effective_date")]
            values.append("; ".join(item.validation_errors))
            lines.append(" | ".join(values))
            html_parts.append("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in values) + "</tr>")
        if not group:
            lines.append("None.")
            html_parts.append(f"<tr><td colspan=\"{len(columns)}\">None</td></tr>")
        html_parts.append("</tbody></table>")
    failed = list(request.attachments.exclude(processing_error="").order_by("pk")) if request else []
    failed = [a for a in failed if not (a.extracted_payload or {}).get("superseded_by_email_id")]
    for attachment in failed:
        description = f"DOC-{attachment.pk}: {attachment.original_name} — {attachment.processing_error}"
        lines.extend(["", description, "Resend this file with the same filename, or fill its DOC reference row with the complete member details."])
        html_parts.append(f"<p><strong>{escape(description)}</strong><br>Resend with the same filename or fill the DOC reference row in the CSV. Repeat that DOC row for multiple members in the failed document.</p>")
    if email.processing_error:
        lines.extend(["", "REQUEST / DOCUMENT ERRORS", email.processing_error])
        html_parts.append("<h3>Request / document errors</h3><p>" + escape(email.processing_error).replace("\n", "<br>") + "</p>")
    if email.processing_state == InboundEmail.State.PROCESSED:
        lines.extend(["", "All member rows passed validation. " + ("The configured workflow has continued." if email.mailbox.auto_submit else "The endorsement is ready for submission in the portal.")])
    lines.extend(["", "For email-body corrections use BEGIN MEMBERS and END MEMBERS around CSV or a JSON members array. Include NEW in row_reference when adding a new member. Mandatory documents must still be supplied. Sum assured follows the selected policy plan."])
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=ROW_FIELDS)
    writer.writeheader()
    for item in items:
        writer.writerow(_member_row(item))
    for attachment in failed:
        writer.writerow({"row_reference": f"DOC-{attachment.pk}"})
    if not items and not failed:
        writer.writerow({"row_reference": "NEW"})
    content = {"subject": subject, "body_text": "\n".join(lines), "body_html": "".join(html_parts), "correction_csv": output.getvalue()}
    reply, created = EmailReply.objects.get_or_create(email=email, defaults={**content, "message_id": make_msgid(domain=email.mailbox.email_address.rsplit("@", 1)[-1])})
    if not created and not reply.sent_at:
        for key, value in content.items():
            setattr(reply, key, value)
        reply.save(update_fields=[*content, "updated_at"])
    return reply


def reply_message(reply):
    """Build the same threaded MIME message for SMTP and Graph delivery."""
    email = reply.email
    headers = {"Message-ID": reply.message_id, "X-SmartEndorse-Reference": email.thread_reference, "Auto-Submitted": "auto-replied"}
    if email.message_id:
        headers["In-Reply-To"] = email.message_id
        headers["References"] = " ".join(dict.fromkeys((email.references_header + " " + email.message_id).split()))
    message = EmailMultiAlternatives(subject=reply.subject, body=reply.body_text, from_email=email.mailbox.email_address, to=[email.sender], reply_to=[email.mailbox.email_address], headers=headers)
    message.attach_alternative(reply.body_html, "text/html")
    if reply.correction_csv:
        message.attach(f"corrections-{email.thread_reference}.csv", reply.correction_csv, "text/csv")
    return message


def deliver_reply(reply_id):
    with transaction.atomic():
        reply = EmailReply.objects.select_for_update().select_related("email__mailbox", "email__policy", "email__authority").get(pk=reply_id)
        if reply.sent_at or not platform_config().enable_email_notifications:
            return bool(reply.sent_at)
        email = reply.email
        decision = authority_decision(email.sender, email.policy, email.extracted_payload.get("endorsement_type", ""))
        authentication_error = sender_authentication_error(email)
        if not decision.authority or authentication_error:
            reply.last_error = "Reply withheld. " + (authentication_error or decision.reason)
            reply.save(update_fields=["last_error", "updated_at"])
            return False
        reply.attempts += 1
        try:
            if email.mailbox.transport == "GRAPH":
                from .mailbox import GraphMailbox
                client = GraphMailbox(email.mailbox)
                try:
                    client.send_reply(reply)
                finally:
                    client.close()
            else:
                from .mailbox import smtp_connection
                message = reply_message(reply)
                connection = smtp_connection(email.mailbox)
                message.connection = connection
                try:
                    if message.send(fail_silently=False) != 1:
                        raise RuntimeError("Mail backend did not confirm delivery.")
                finally:
                    connection.close()
            reply.sent_at = timezone.now()
            reply.last_error = ""
        except Exception as exc:
            reply.last_error = str(exc)
        reply.save()
        return bool(reply.sent_at)


# Preserve the service import used by automatic polling and admin integrations.
from .email_automation import EmailIntakeService
