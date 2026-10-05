# SmartEndorse Email Intake

SmartEndorse can turn inbound emails into endorsement requests using the same validation, pricing, approval, TPA and audit workflow used by the portal.

## Supported sources

The intake service reads:

- Email subject and body
- XLSX / XLS / CSV
- PDF
- PNG / JPG / JPEG / WEBP
- Multiple PDF/image files as one evidence bundle, including separate front/back ID files

The system tries to determine:

1. Sender / requester organization
2. Policy
3. Addition vs deletion
4. Effective date
5. Member data
6. Supporting evidence

The effective date may come from the email body or extracted attachment data. If the email does not contain a date, SmartEndorse temporarily uses the email received date (clamped to the policy period) and replaces it when a supported attachment provides an explicit effective date.

## Admin setup

Run migrations first:

```powershell
python manage.py migrate
```

Open **Admin -> Email intake mailboxes** and create a mailbox.

### Microsoft 365 / Graph

Recommended for Microsoft 365.

Configure an Entra ID application with Microsoft Graph **Application** permission:

- `Mail.ReadWrite`

Grant tenant admin consent, then enter:

- Mailbox email address
- Tenant ID
- Client ID
- Client secret
- Folder: `inbox`

### IMAP

For mail providers that allow IMAP, configure:

- Host
- Port
- Username
- Password / app password
- SSL

## Sender routes

Under the mailbox add one or more Email Intake Routes.

A sender pattern can be:

- `hr@client.com` for an exact sender
- `@client.com` for the whole domain
- `*` as a catch-all

Each route maps the sender to:

- Organization
- Requester (recommended)
- Optional default policy
- Optional default endorsement type

If no default policy is configured, SmartEndorse looks for the policy number in the email. If exactly one accessible active policy exists for that organization, it can use that policy automatically.

## Automatic polling

During local DEBUG/runserver, email intake starts automatically by default.

Environment options:

```env
EMAIL_INTAKE_AUTOSTART=1
EMAIL_INTAKE_POLL_SECONDS=30
```

For production, a dedicated worker is also supported:

```powershell
python manage.py process_email_intake --loop --interval 30
```

One-time/manual poll:

```powershell
python manage.py process_email_intake --force
```

You can also select a mailbox in Django admin and run **Poll selected mailbox(es) now**.

## Processing behaviour

If **Auto submit** is enabled on the mailbox:

```text
Email received
  -> identify sender / policy / type
  -> extract body + attachments
  -> create endorsement
  -> validate
  -> price
  -> STP or insurer approval
  -> TPA / core processing
  -> completion
```

If extraction is incomplete, the request remains available for human correction and the Email Intake Message is marked **Needs review**.

Every inbound message is stored with its provider message ID. The same provider email cannot create the endorsement twice.
