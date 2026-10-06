# Automated endorsement email corrections

After pulling this change, install requirements and run `python manage.py migrate` and `python manage.py collectstatic --noinput` before restarting the web process.

## Configure AI

In **Admin > AI provider configs**, add your text provider and a vision provider for image/scanned-PDF OCR. Select Hugging Face, OpenAI, Anthropic Claude, Ollama or a compatible/local API. Use **Secret reference** for an environment variable such as `HF_TOKEN`; set its value in `.env` or the worker environment. Lower **Priority** numbers win within text/vision selection. Configure model IDs that your chosen service actually supports; this repository does not enable paid models or set a token for you.

Hugging Face defaults to `https://router.huggingface.co/v1`. The optional **Inference provider** chooses a provider/routing policy. Keep **Supports vision** off for a text-only model. Existing extraction profiles and training examples still control field mapping.

API references: [Hugging Face routing](https://huggingface.co/docs/inference-providers/en/index), [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create), [Claude Messages](https://platform.claude.com/docs/en/api/http/messages).

## Configure a mailbox

Create a **Mailbox configuration** in Admin. Its sender address must be a mailbox your sending service may send as. All mailbox passwords, client secrets and IMAP reply delivery settings are saved directly in this Admin page.

For **IMAP / SMTP**, set the TLS IMAP host/port, username, folder and actual **IMAP password / OAuth token**. Select OAuth when entering an access token. Under **IMAP reply delivery**, select **SMTP delivery** and enter the SMTP host, port, username, password, timeout and either STARTTLS or implicit TLS/SSL. An anonymous SMTP relay may leave username/password blank. Select **Console (development only)** to print replies during development.

For **Microsoft 365 / Graph**, enter the tenant ID, client ID and full **Graph client secret** Value from Entra App registrations > your application > Certificates & secrets. Use application `Mail.ReadWrite` and `Mail.Send` permissions with tenant admin consent, scoped by your Exchange application access policy to the chosen mailbox. Inbound messages are fetched as MIME; outgoing emails are native replies with the reference appended to the subject. The worker retains Graph reply drafts during retry. See [Graph createReply](https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0) and [message delta](https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0).

Secrets are stored with the mailbox in the database. Password inputs are blank when reopening the form and never contain the saved value in the HTML. Leave them blank to preserve existing credentials, enter a replacement to update them, or select **Clear saved value** to remove them. Deactivate the mailbox before clearing required credentials. The existing route-based Email intake mailbox forms follow the same preservation behaviour.

Save the mailbox and the scheduler reads the updated credentials on its next poll. IMAP correction replies use that mailbox's saved SMTP settings. Configuration errors name the missing field without printing secret values.

After upgrading, run migrations. The former reference fields are renamed to the direct credential fields and their stored values are preserved. If you previously saved an environment variable name, replace it with the actual password/client secret Value in Admin. Previously pasted secret values remain available without copying them into another file.

Sender authentication is enabled by default. Configure **Trusted authserv IDs** with the exact names in Authentication-Results headers produced by your receiving mail servers, e.g. `["mx.company.example"]`. The worker accepts a DMARC pass for the sender's From domain only from these servers. Your gateway must strip forged headers using its own authserv ID. Disable the check only where an upstream verified identity mechanism is enforced; exact policy authorization remains required either way. Unauthorized messages receive no member-containing reply.

If the message reports **no Authentication-Results header**, and your receiving mail provider already verifies senders, open **Admin > Mailbox configurations > your mailbox > Sender verification**, uncheck **Require sender authentication**, and save. This applies to existing mailboxes and does not need an environment setting or worker restart. Active mailboxes automatically recheck stored blocked emails on subsequent polls.

You can make the same explicit choice from the mailbox list: select only the intended mailbox(es), choose **Trust receiving provider for sender verification (keep Email authority checks)**, and click **Go**. The action requires mailbox change permission, records the change in Admin history, and preserves all mailbox credentials. The list and change page show the selected verification mode. **Email authorities**, active identities, group membership and policy creation permissions are checked in both modes; this action does not create or widen a sender grant. The default DMARC requirement is unchanged until you explicitly change a mailbox.

A **Default policy** is optional. If the email omits a policy number, a single active policy grant may disambiguate it. More than one policy remains a review item. Effective dates are never guessed: use `Effective date: YYYY-MM-DD` in the body. Select **Auto submit** only if valid emails should continue into existing pricing, STP, insurer approval and TPA/core dispatch. Otherwise they remain ready for explicit portal submission.

## Authorize exact addresses, users or groups

Create **Email authorities** with one identity per grant:

| Identity | Requirement |
| --- | --- |
| Exact email address | Case-insensitive exact match; surrounding whitespace is ignored. Set an eligible processing user if there is no unique matching portal user. An explicitly selected processing user takes precedence over the sender's own portal account. |
| Portal user | The active user's email is the sender identity. |
| Django group | The sender must uniquely match an active portal user who is currently in the selected group. |

Each grant names a policy, organization, allowed endorsement types and optional validity dates. The processing user must be active, belong to the grant organization and have endorsement creation access for that policy. Add a **Policy access** grant for client/broker organizations. A deactivated user, inactive organization or expired/revoked grant cannot process or receive sensitive replies. Validity is evaluated on the current processing date, not an untrusted email Date header. The original requester organization and insurer organization may correct the same request when authorized; another client's grant cannot change it.

Admin checks the fixed workflow user before saving an active email/user grant. Group grants use each sender's own active portal identity and permissions; a processing user does not delegate access to group members. **Grant configuration** on the Email authorities list/change page identifies incomplete or ineligible existing grants. Inactive grants can be saved as drafts.

### An authorized address is still blocked

Open **Admin > Inbound emails > the message > Processing error** (or its portal detail page) for the actual reason. The list also shows a short reason and distinguishes **Sender verification failed** from **Unauthorized sender**.

| Reason | Admin setting to check |
| --- | --- |
| No Email authority matches From address / policy | Match the actual From address, the portal user's Email or current group membership; select the same policy and enable Active. Reply-To addresses and aliases are not inferred. |
| Workflow user is missing, inactive or has an ineligible role | Select an active **Processing user** for an exact-address grant, or update the sender's **User profile** for user/group grants. |
| Workflow user belongs to another organization / has no policy creation access | Match the grant organization; add **Policy access** with **Can create** for client/broker organizations. |
| Grant is expired, not yet valid or does not permit the transaction | Check **Valid from**, **Valid until**, **Active** and **Permitted types**. |
| Sender verification failed | Check **Mailbox configurations > Sender verification**. A policy grant does not supply a DMARC pass or make an untrusted header trustworthy. |

Microsoft 365 commonly reports `Authentication-Results` without an authserv-id ([Microsoft header examples](https://learn.microsoft.com/en-us/defender-office-365/message-headers-eop-mdo)). Such a result cannot be matched against **Trusted authserv IDs**; Graph API access itself does not prove the sender's identity. For the strict DMARC option, configure a trusted receiving gateway to provide an identified, aligned result and strip forged headers. Where the receiving mail gateway already verifies senders, you can uncheck **Require sender authentication** in this mailbox's Admin settings; policy-scoped address/user/group grants and workflow permissions remain enforced. The application does not automatically disable this check.

After you add or fix a grant, active mailboxes automatically recheck previously blocked emails on scheduled polls. Rechecks also pick up changes to user/group membership, policy access and mailbox verification settings. Checks are bounded and rotate through older blocked messages; new mail is processed independently. Already processed messages are not replayed by this retry stage. Use **Recheck authorization and retry email extraction** in Admin, or **Recheck and process** on a permitted portal detail page, for an immediate check. Retry messages report the resulting status rather than claiming that a still-blocked email was processed successfully.

## Start the worker

APScheduler starts automatically with local DEBUG/runserver. Set `EMAIL_INTAKE_AUTOSTART=1` explicitly to enable it and `EMAIL_INTAKE_POLL_SECONDS=30` to control the interval. The first poll runs immediately; subsequent cycles pull new emails, process stored pending messages, recheck blocked senders and retry unsent replies. `EMAIL_INTAKE_BATCH_SIZE=50` limits each stage per correction mailbox. Poll statistics include `rechecked` for blocked emails examined again, separately from newly pending emails processed.

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
