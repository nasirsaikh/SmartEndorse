# Automated endorsement email corrections

After pulling this change, install requirements and run `python manage.py migrate` and `python manage.py collectstatic --noinput` before restarting the web process.

## Configure AI

In **Admin > AI provider configs**, add your text provider and a vision provider for image/scanned-PDF OCR. Select Hugging Face, OpenAI, Anthropic Claude, Ollama or a compatible/local API. Use **Secret reference** for an environment variable such as `HF_TOKEN`; set its value in `.env` or the worker environment. Lower **Priority** numbers win within text/vision selection. Configure model IDs that your chosen service actually supports; this repository does not enable paid models or set a token for you.

Hugging Face defaults to `https://router.huggingface.co/v1`. The optional **Inference provider** chooses a provider/routing policy. Keep **Supports vision** off for a text-only model. Existing extraction profiles and training examples still control field mapping.

API references: [Hugging Face routing](https://huggingface.co/docs/inference-providers/en/index), [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create), [Claude Messages](https://platform.claude.com/docs/en/api/http/messages).

## Configure a mailbox

Create a **Mailbox configuration** in Admin. Its sender address must be a mailbox your sending service may send as. The worker reads `.env` just like the web app.

For **IMAP / SMTP**, set the TLS IMAP host/port, username, folder, and a credential environment reference, e.g. `ENDORSEMENT_IMAP_PASSWORD`. Select OAuth when the variable contains an access token. SMTP uses the Django `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and `EMAIL_USE_TLS` settings. Use `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend` for delivery; the default console backend is for development only.

For **Microsoft 365 / Graph**, configure tenant ID, client ID, and a client-secret environment reference, e.g. `ENDORSEMENT_GRAPH_CLIENT_SECRET`. Use application `Mail.ReadWrite` and `Mail.Send` permissions with tenant admin consent, scoped by your Exchange application access policy to the chosen mailbox. Inbound messages are fetched as MIME; outgoing emails are native replies with the reference appended to the subject. The worker retains Graph reply drafts during retry. See [Graph createReply](https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0) and [message delta](https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0).

The **Graph secret reference** field contains the variable name, and `.env` next to `manage.py` contains the actual client secret:

```env
ENDORSEMENT_GRAPH_CLIENT_SECRET="your-full-client-secret-value"
```

Set **Graph secret reference** to `ENDORSEMENT_GRAPH_CLIENT_SECRET`. Use the client secret **Value** from Entra App registrations > your application > Certificates & secrets, not the Secret ID. Save the mailbox and restart Django and any dedicated scheduler after changing `.env`; running processes do not reload credentials from that file. Reference fields accept variable names using letters, digits and underscores, starting with a letter or underscore.

If polling reports that the referenced environment variable is missing or empty, verify that `.env` is in the project root and the variable name matches the reference field. The app logs a configuration warning without echoing the submitted reference or secret. It continues processing stored messages and retries credential resolution on subsequent polls. A mailbox worker in Docker receives its secrets through the Compose `.env` configuration and must be recreated after a credential change.

Sender authentication is enabled by default. Configure **Trusted authserv IDs** with the exact names in Authentication-Results headers produced by your receiving mail servers, e.g. `["mx.company.example"]`. The worker accepts a DMARC pass for the sender's From domain only from these servers. Your gateway must strip forged headers using its own authserv ID. Disable the check only where an upstream verified identity mechanism is enforced; exact policy authorization remains required either way. Unauthorized messages receive no member-containing reply.

A **Default policy** is optional. If the email omits a policy number, a single active policy grant may disambiguate it. More than one policy remains a review item. Effective dates are never guessed: use `Effective date: YYYY-MM-DD` in the body. Select **Auto submit** only if valid emails should continue into existing pricing, STP, insurer approval and TPA/core dispatch. Otherwise they remain ready for explicit portal submission.

## Authorize exact addresses, users or groups

Create **Email authorities** with one identity per grant:

| Identity | Requirement |
| --- | --- |
| Exact email address | Case-insensitive exact match; no domain wildcards. Set a processing user if there is no matching portal user. |
| Portal user | The active user's email is the sender identity. |
| Django group | The sender must uniquely match an active portal user who is currently in the selected group. |

Each grant names a policy, organization, allowed endorsement types and optional validity dates. The processing user must be active, belong to the grant organization and have endorsement creation access for that policy. Add a **Policy access** grant for client/broker organizations. A deactivated user, inactive organization or expired/revoked grant cannot process or receive sensitive replies. Validity is evaluated on the current processing date, not an untrusted email Date header. The original requester organization and insurer organization may correct the same request when authorized; another client's grant cannot change it.

## Start the worker

APScheduler starts automatically with local DEBUG/runserver. Set `EMAIL_INTAKE_AUTOSTART=1` explicitly to enable it and `EMAIL_INTAKE_POLL_SECONDS=30` to control the interval. The first poll runs immediately; subsequent cycles pull new emails, process stored pending messages and retry unsent replies. `EMAIL_INTAKE_BATCH_SIZE=50` limits each stage per correction mailbox.

For production, set `EMAIL_INTAKE_AUTOSTART=0` on web processes and run the dedicated scheduler as a separate service:

```bash
python manage.py run_email_scheduler --interval 30 --limit 50
```

For a manual one-time run, use `python manage.py process_mailbox`; `--mailbox 1` selects a mailbox and `--limit 50` bounds each stage. The existing `--watch` command is retained for compatibility. For Docker, `docker compose --profile email up -d --build` starts the APScheduler worker.

The scheduler uses a single job instance, coalesces missed ticks, and takes an OS file lock to prevent overlapping scheduled polls. Its default lock file is `MEDIA_ROOT/email-intake-poll.lock`, shared by the Docker web and worker volume. Override `EMAIL_INTAKE_LOCK_FILE` when processes need a different shared path. On multiple hosts, use a single dedicated scheduler or a shared filesystem with reliable file locks. See [automatic polling](EMAIL_INTAKE.md#automatic-polling) for the route-based intake path and its mailbox-level interval settings.

The worker persists messages before OCR and commits its mailbox cursor only after ingestion. Processing and delivery use stored state. Failed SMTP/Graph delivery remains pending for retries. No live credentials are included. Incoming automatic replies and messages from the intake mailbox itself are ignored to prevent loops.

## What the sender receives

An original email creates one `EML-YYYYMM-XXXXXXXX` reference. Replies retain it in their subject, e.g. `Re: [SE: EML-202610-A1B2C3D4] Addition endorsement`. Both SMTP and Graph MIME replies include `X-SmartEndorse-Reference`, `In-Reply-To` and `References`. The body includes the associated endorsement reference, policy, a **Correct members** table and a separate **Error members / needs correction** table with the error on each row.

The attached CSV includes each stable `ITEM-123` member reference and member fields. The sender can fill missing data and return the CSV, edit the email's table, or send CSV/JSON between `BEGIN MEMBERS` and `END MEMBERS`:

```text
Effective date: 2026-10-05
BEGIN MEMBERS
row_reference,employee_no,full_name,date_of_birth,gender,relationship,plan_code
ITEM-123,E1001,Corrected Name,1990-01-01,Male,Employee,GOLD
ITEM-124,E1002,Second Member,1992-02-03,Female,Employee,SILVER
END MEMBERS
```

Keep the reference in the subject. Omit unchanged members or leave their fields blank to preserve accepted values. Use `[CLEAR]` explicitly to clear a field. Changing an accepted member is supported and audited, including changes to identifiers when its ITEM reference is kept. All resulting rows are revalidated; plan sum assured is derived from the selected policy plan.

Use `NEW` as the row reference to add an additional member in a correction. If matching identifiers point to different rows or are ambiguous, the system asks for the stable ITEM reference and does not guess. A foreign ITEM reference cannot update another endorsement. Omitted members are never deleted.

Failed OCR documents include a `DOC-123` row in the CSV. Resend a readable file with the same filename, or fill that DOC row with the complete member details it contains. Repeat the DOC row for multiple members. The original OCR error/evidence remains in the audit record and is marked superseded by the authorized correction. An unrelated correct member change does not silently dismiss the failed document. Mandatory documents still need to be supplied.

Once a request leaves intake/correction, a late email is held for staff review so it cannot overwrite an already dispatched or completed endorsement. Replaying an older received email after a newer correction has been applied also requires review. Sent reply snapshots remain unchanged. Review messages, source files, merge results and outbound delivery status in **Inbound emails**. Admin may recheck extraction or retry an unsent reply. Automatic live API/SMTP/Graph behavior requires your configured credentials; the automated tests mock these services.
