# SmartEndorse architecture

## Operating principle

Use AI where uncertainty exists and deterministic code where contractual or financial reproducibility is required.

AI responsibilities:
- document classification
- field extraction
- normalization assistance
- ambiguity and missing-data explanation

Deterministic responsibilities:
- policy access
- mandatory-field validation
- member existence checks
- premium/refund calculation
- STP eligibility gates
- routing
- SLA timestamps
- status transitions
- audit events

## Seven-stage pipeline

1. Intake
2. Extract
3. Validate
4. Price
5. Decide STP vs exception
6. Dispatch to TPA or insurer core
7. Fulfil, reconcile and close

## Core entities

Organization
Represents insurer, direct client, broker, agent, channel partner or TPA.

UserProfile
Maps Django user to organization and business role.

Policy
Contains product, parties, distribution codes, dates, rating settings, validation settings and SLA references.

PolicyAccess
Authorizes intermediary/client organizations to view/create against specific policies.

PolicyPlan
Contains plan-level annual and relationship rates.

PolicyMember
Current census/member record used for deletion checks and prefill.

AIProviderConfig
Runtime-selectable LLM/provider configuration.

SLAProfile
Stage-level service target.

IntegrationEndpoint
Email/REST routing destination for TPA or insurer core.

EndorsementRequest
Workflow header and SLA/premium state.

EndorsementItem
Member-level requested change and calculated premium impact.

Attachment
Source file plus extraction status.

EndorsementQuery
Structured query/response loop.

WorkflowEvent
Append-only operational history from application workflows.

## Recommended production topology

Browser
  -> reverse proxy / WAF
  -> Django application
  -> PostgreSQL
  -> private object storage
  -> durable job queue
  -> AI gateway/provider
  -> TPA API/email/SFTP
  -> insurer policy administration/core API
  -> email/SMS notification service

Use a dedicated AI gateway if multiple providers are allowed. The gateway can enforce redaction, model allowlists, data residency and per-tenant policy.

## Multi-tenancy

The first release uses application-level row scoping by Organization and PolicyAccess. If multiple unrelated insurers will share one deployment, consider stronger tenant isolation:
- tenant_id on every business table
- PostgreSQL row-level security
- per-tenant encryption keys
- separate object-storage prefixes/buckets
- isolated integration credentials
- optional separate database/schema for regulated tenants

## Straight-through processing

STP requires both:
- PlatformConfiguration.auto_stp_enabled
- Policy.auto_stp

A request still needs zero validation errors. This permits a global kill switch and a policy-specific switch.

A production rule engine should add explicit STP blockers such as:
- backdated request exceeds permitted days
- member already active on addition
- deletion member not active
- dependent age limit exceeded
- plan/relationship not allowed
- required proof missing
- sum assured above automatic limit
- medically-underwritten life class
- duplicate file/member row
- retroactive refund exceeds threshold
- policy suspended or expired

## Integration reliability

REST endpoints should become asynchronous outbox messages in production. Recommended pattern:
- write endorsement and outbox event in one DB transaction
- worker sends outbound request
- store idempotency key = SmartEndorse reference + event type
- retry transient failures with exponential backoff
- dead-letter terminal failures
- verify callback signatures
- reconcile external status daily

This avoids losing a TPA/core request if the remote service is temporarily unavailable.


## Reference-based email intake

`process_mailbox` persists MIME messages and attachments before extraction. IMAP UID/UIDVALIDITY or Microsoft Graph delta cursors prevent lost messages on restart. `InboundEmail` carries an immutable email reference; corrections link to the original email and the same endorsement. `EmailAuthority` checks policy, organization, identity/group, date range and transaction type, while trusted receiving-server Authentication-Results establishes sender authentication.

`email_intake.process_email` extracts body/attachments, merges using stable member references or unambiguous identifiers, records changes to accepted/error rows, reruns deterministic validation and pricing, and optionally enters the existing STP/approval workflow. A stored `EmailReply` contains separate correct/error tables and an editable CSV. SMTP retains In-Reply-To/References; Graph creates a native reply draft, changes its subject/body and sends it. The subject carries the original reference. Failed delivery remains pending and is retried without rebuilding an already sent reply.
