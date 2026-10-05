# SmartEndorse

SmartEndorse is an AI-first group insurance endorsement orchestration platform for Group Medical and Group Life. It gives direct clients, brokers, agents, channel partners, insurers and TPAs one role-aware workflow for member additions and deletions.

The design follows a zero-touch operating model: structured or unstructured requests are ingested, fields are extracted, deterministic policy rules validate the request, premium or refund is calculated by an auditable pricing engine, clean cases are straight-through processed, and exceptions are isolated without blocking the rest of the batch.

## What is included

- Django 5.2 LTS backend with Django Admin as the configuration control plane.
- Shared compact GLIS Bootstrap 5.3.2 theme, Bootstrap dialogs/tabs and ApexCharts; no frontend build step.
- Authorized IMAP/Microsoft 365 email intake and reference-based member correction replies.
- HTMX request filtering and dependent plan selection without a SPA framework.
- ApexCharts dashboards for client, insurer and TPA operating views.
- Organization model covering insurer, direct client, broker, agent, channel partner and TPA.
- Role model covering insurer admin/manager/supervisor/staff/underwriter, client roles, broker roles, agent, channel partner, TPA roles and auditor.
- Policy-level access grants so each intermediary sees only mapped policies.
- Group Medical routing to the policy TPA.
- Group Life routing directly to the insurer core integration.
- Addition and deletion endorsements.
- Flat annual / plan pricing, per-mille sum-assured pricing, percent-of-salary pricing and daily pro-rata calculation.
- Deterministic validation for mandatory fields, policy period and active-member checks.
- Excel and CSV deterministic extraction.
- PDF text extraction plus LLM structuring.
- Image extraction using a vision-capable AI provider.
- AI provider switch in Admin: Hugging Face, Ollama/Llama, OpenAI, Anthropic Claude, or an OpenAI-compatible endpoint.
- Straight-through processing controls at platform and policy level.
- Email or REST integration endpoints for TPA and insurer core systems.
- SLA due dates, breach visibility and an automated escalation command.
- TPA / insurer queries back to the requester and requester responses.
- Completion notifications.
- Full workflow event history.
- Realistic sample data and demo accounts.
- Docker + PostgreSQL example deployment and GitHub Actions CI.

## Why the pricing engine is not AI-generated

LLMs are useful for extracting data from inconsistent documents and identifying ambiguous content. They should not be the source of truth for endorsement premium calculations. SmartEndorse therefore keeps financial calculations deterministic and policy-driven. Changing from Llama to OpenAI or Claude does not change the expected premium result.

## End-to-end Group Medical flow

1. Insurer configures policy period, client, broker/agent/channel codes, TPA, plans, rates, mandatory fields, STP setting and SLA.
2. Client/intermediary logs in and sees only authorized policies.
3. User enters one member manually or uploads Excel, CSV, PDF or an image.
4. Spreadsheet data is parsed directly. PDF/image content can be structured by the active AI provider.
5. SmartEndorse validates required fields and policy/member rules.
6. Pricing engine calculates annual premium and pro-rata addition/refund.
7. Clean STP requests auto-approve without insurer handling.
8. Request dispatches automatically to the policy TPA by configured Email or REST endpoint.
9. TPA user sees the request and SLA clock, can start processing, raise a query, or complete it.
10. Requester answers any query in the same case.
11. Completion updates the dashboard, closes the SLA and emails the requester.

## End-to-end Group Life flow

Group Life uses the same intake, validation, pricing, STP and audit model but skips the TPA. Clean requests dispatch to the configured insurer-core endpoint. The demo life policy uses a per-mille sum-assured rate.

## Roles

### Insurance company
- Super Admin: full platform and Django Admin access.
- Insurer Admin: operational configuration role.
- Insurer Manager: portfolio, SLA and exception oversight.
- Insurer Supervisor: team queue and exception handling.
- Insurer Staff: operational case handling when an exception requires intervention.
- Underwriter: exception review for underwriting conditions.
- Auditor: read-only governance and event-trail role.

### Client / intermediary
- Client Admin: client-user governance and portfolio view.
- Requester: create and follow endorsement requests.
- Client Viewer: read-only dashboard.
- Broker Admin / Broker User: policy access based on broker organization grants.
- Agent: policy access based on agent grants.
- Channel Partner: policy access based on channel grants.

### TPA
- TPA Admin: TPA configuration and oversight.
- TPA Manager: queue/SLA oversight.
- TPA Processor: process, query and complete endorsements.
- TPA Viewer: read-only queue.

The initial UI enforces organization/policy visibility and creation grants. For production, add object-level permission tests for every action and map each role to a formal permission matrix.

## Quick start

Windows:

    py -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    copy .env.example .env
    python manage.py migrate
    python manage.py seed_demo
    python manage.py runserver

Linux/macOS:

    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env
    python manage.py migrate
    python manage.py seed_demo
    python manage.py runserver

Open http://127.0.0.1:8000/

Admin is at http://127.0.0.1:8000/admin/

## Demo credentials

All demo accounts use password: Demo@12345

- smartadmin — Super Admin / insurer
- insurer.manager — insurer manager dashboard
- client.requester — direct client requester dashboard
- broker.user — broker dashboard
- agent.user — agent dashboard
- tpa.processor — TPA queue dashboard

Change or delete all demo credentials before any real deployment.

## Demo policies

MED-2026-001 — Acme Group Medical 2026
- TPA: NextCare Demo TPA
- Plans: Silver, Gold, Platinum
- Flat annual / plan rating
- Daily pro-rata
- Client, broker and agent access
- Auto-STP enabled

LIFE-2026-001 — Acme Group Life 2026
- No TPA
- Per-mille sum-assured rating
- Auto-dispatch to insurer-core integration
- Client, broker and agent access

Sample bulk input is available at sample_data/medical_endorsement_upload.csv.

## Admin-driven setup

Almost all operational behavior can be changed without editing code.

### Platform Configuration
Use Admin > Platform configurations for:
- global STP enable/disable
- default currency
- upload limits
- weekend convention
- email and SLA escalation toggles

### AI Provider
Use Admin > AI provider configs. Multiple providers may be active. Lower **Priority** values are selected first, with separate selection for text and vision OCR. Keep **Supports vision** disabled for text-only models. Model IDs are configurable; use a model actually served by the API you choose.

| Provider | Default base URL | Credential reference |
| --- | --- | --- |
| Hugging Face | `https://router.huggingface.co/v1` | e.g. `HF_TOKEN` |
| OpenAI | `https://api.openai.com` | e.g. `OPENAI_API_KEY` |
| Anthropic Claude | `https://api.anthropic.com` | e.g. `ANTHROPIC_API_KEY` |
| Ollama | `http://127.0.0.1:11434` | Leave blank for a local instance |
| OpenAI-compatible / local | Your server URL, normally ending in `/v1` | The variable your server requires |

**Secret reference** is an environment variable name, never the token itself. It takes precedence over the legacy API key field. `.env` is loaded for both the web app and mailbox worker. For Hugging Face, leave **Inference provider** blank/`auto`, choose a routing policy (`fastest`, `cheapest`, `preferred`), or enter a supported provider name. A dedicated compatible endpoint can be configured as a root URL, `/v1`, or `/v1/chat/completions` without duplicating the path.

Optional API settings in **Options**:

```json
{"request_parameters": {"max_tokens": 4096, "temperature": null}, "response_format": "json_object"}
```

A null parameter omits it for models that reject that parameter. `response_format` may be `json_object` or `json_schema` only when supported by the selected model/provider. Member JSON is checked locally; unreadable or malformed results become correction requests. The existing extraction profiles, field aliases and examples continue to apply.

### Email intake and corrections

Configure **Mailbox configurations** and policy-scoped **Email authorities** in Admin, then run:

```bash
python manage.py process_mailbox --watch --interval 60
```

Use `--mailbox ID` for one mailbox or omit `--watch` for an externally scheduled one-shot run. See [email setup and correction examples](docs/EMAIL_CORRECTIONS.md) for IMAP/SMTP, Microsoft 365 permissions, sender/group authorization, and a sample correction reply. SMTP must be configured for IMAP; the default console backend prints messages for development. Microsoft 365 uses Graph replies.

### Policy configuration
Each policy controls:
- medical vs group life
- policy dates
- insurer/client/TPA
- broker, agent and channel codes
- rating method and rating parameters
- addition/deletion mandatory fields
- document requirements
- pro-rata basis
- backdating setting
- policy STP switch
- insurer, TPA and query SLA profiles

### Policy access
Policy Access grants link a policy to a client, broker, agent or channel organization. View and create rights are separate.

### Integration endpoints
Configure TPA and insurer-core integrations as Email or REST API. The demo uses console email so no real messages are sent.

## AI intake behavior

Structured XLSX/CSV input does not require AI.

PDF:
- pypdf extracts text.
- the active LLM converts that text to the standard member JSON schema.
- scanned PDFs without text currently surface as an exception; connect a managed OCR service or PDF-to-image pipeline for production scanned documents.

Images:
- require an AI provider marked as vision-capable.
- OpenAI-compatible, Ollama vision and Anthropic image request formats are implemented.

The model is explicitly instructed not to calculate premium and not to invent missing data.

## Pricing

Supported rating methods:

1. FLAT_ANNUAL
   - plan annual rate or policy default annual rate
   - optional relationship-specific rate in PolicyPlan.relationship_rates

2. PER_MILLE_SUM_ASSURED
   - annual premium = sum assured × rate_per_mille / 1000

3. PERCENT_OF_SALARY
   - annual premium = annual salary × salary_percent / 100

Default fixed-basis pro-rata:

    remaining covered days / policy.day_count_basis

Set rating_parameters.prorata_mode to policy_days to divide by the actual policy period instead.

Addition produces a positive premium impact. Deletion produces a negative refund impact.

## SLA automation

Dashboard SLA clocks update with workflow state.

Schedule this command every 10-15 minutes using cron, systemd timer, Kubernetes CronJob, Windows Task Scheduler, or your enterprise scheduler:

    python manage.py process_automation

It records one SLA_BREACH event per breached due timestamp and sends escalation email to relevant configured organizations when email notifications are enabled.

For high-volume production, move notification delivery and integrations to a durable queue with idempotency and retries.

## Frontend assets

Bootstrap 5.3.2, HTMX, ApexCharts and Bootstrap Icons are bundled locally. `bootstrap-layout.css`, `style.css`, `ui.css` and `portal-compact.css` are shared from GLIS. SmartEndorse-specific wizard, dropzone and responsive page styles are in `static/css/smartendorse.css`. There is no CSS compilation step:

```bash
python manage.py runserver
python manage.py collectstatic --noinput
```

The light/dark toggle updates Bootstrap theme variables, charts and the saved profile preference. Dialogs, navigation, dropdowns and action tabs use Bootstrap lifecycle APIs.

## Docker

After migrations are committed:

    docker compose up --build

The compose example starts PostgreSQL and the Django web process, migrates the database and loads demo data.

## Production controls that should be added before PHI/PII use

This repository is a functional first release, not a compliance certification. Group medical/member data can contain highly sensitive personal information.

Before using real data:

- Enforce HTTPS everywhere.
- Encrypt databases, backups, object storage and integration payloads at rest/in transit.
- Use SSO/OIDC and MFA for insurer, client and TPA users.
- Add explicit object/action permissions per role and organization.
- Use a managed secret store for AI and integration credentials.
- Define retention/deletion rules and data residency requirements.
- Add malware scanning for uploads.
- Add file content-type verification, not only extension checks.
- Add signed URLs and private object storage.
- Add API/webhook signature verification, replay protection and idempotency keys.
- Add duplicate-member and overlapping-coverage controls.
- Add dependent age/newborn/marriage/divorce document rules where applicable.
- Add configurable backdating/refund cutoff and cancellation rules.
- Add maker-checker only for exception classes that legally or contractually require it.
- Add field-level PII masking in dashboards/logs.
- Add audit export and immutable log retention.
- Obtain the required contractual/privacy approval before sending health data to any external AI provider.

## Recommended next functional modules

- TPA completion API/webhook so completed endorsements close automatically without a TPA user clicking Complete.
- Core-policy-system member synchronization and reconciliation.
- Policy census import and nightly delta sync.
- Bulk mixed-batch processing where clean rows STP and only failing rows become exceptions.
- Re-submission workflow for corrected rows without duplicating clean rows.
- Coverage/benefit eligibility rules by plan and member relationship.
- Automated endorsement schedule/certificate generation.
- Premium invoice/credit-note generation and finance reconciliation.
- SLA working calendar with public holidays and stage-specific business hours.
- Notification templates editable in Admin.
- Broker/client monthly service reports.
- API-first intake for large corporate clients.
- SFTP integration for TPAs that cannot expose REST APIs.
- Operational reconciliation between requested, TPA-completed and core-system member records.

## Technology decision

Django 5.2.17 LTS is intentionally used for the first enterprise baseline even though newer feature releases exist. It has extended support through April 2028, while the code remains straightforward to upgrade later.

## Project status

This commit establishes the working architecture, workflow engine, dashboards, AI abstraction, file intake, pricing, access model, admin configuration, sample data, tests and deployment scaffolding. Production integrations and organization-specific underwriting/endorsement rules should be configured and tested against actual policy wordings and TPA/core specifications before live use.
