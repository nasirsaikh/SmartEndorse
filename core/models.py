import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .credentials import validate_credential_reference


def default_weekend_days():
    return [4, 5]


BOOTSWATCH_THEMES = [
    ("default", "Default"), ("brite", "Brite"), ("cerulean", "Cerulean"),
    ("cosmo", "Cosmo"), ("cyborg", "Cyborg"), ("darkly", "Darkly"),
    ("flatly", "Flatly"), ("journal", "Journal"), ("litera", "Litera"),
    ("lumen", "Lumen"), ("lux", "Lux"), ("materia", "Materia"),
    ("minty", "Minty"), ("morph", "Morph"), ("pulse", "Pulse"),
    ("quartz", "Quartz"), ("sandstone", "Sandstone"), ("simplex", "Simplex"),
    ("sketchy", "Sketchy"), ("slate", "Slate"), ("solar", "Solar"),
    ("spacelab", "Spacelab"), ("superhero", "Superhero"), ("united", "United"),
    ("vapor", "Vapor"), ("yeti", "Yiti / Yeti"),
]


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Organization(TimeStampedModel):
    class Type(models.TextChoices):
        INSURER = "INSURER", "Insurance Company"
        CLIENT = "CLIENT", "Direct Client"
        BROKER = "BROKER", "Broker"
        AGENT = "AGENT", "Agent"
        CHANNEL = "CHANNEL", "Channel Partner"
        TPA = "TPA", "TPA"

    name = models.CharField(max_length=200)
    code = models.CharField(max_length=50, unique=True)
    organization_type = models.CharField(max_length=20, choices=Type.choices)
    notification_email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.code} - {self.name}"


class UserProfile(TimeStampedModel):
    class Role(models.TextChoices):
        SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
        INSURER_ADMIN = "INSURER_ADMIN", "Insurer Admin"
        INSURER_MANAGER = "INSURER_MANAGER", "Insurer Manager"
        INSURER_SUPERVISOR = "INSURER_SUPERVISOR", "Insurer Supervisor"
        INSURER_STAFF = "INSURER_STAFF", "Insurer Staff"
        UNDERWRITER = "UNDERWRITER", "Underwriter"
        CLIENT_ADMIN = "CLIENT_ADMIN", "Client Admin"
        REQUESTER = "REQUESTER", "Client Requester"
        CLIENT_VIEWER = "CLIENT_VIEWER", "Client Viewer"
        BROKER_ADMIN = "BROKER_ADMIN", "Broker Admin"
        BROKER_USER = "BROKER_USER", "Broker User"
        AGENT = "AGENT", "Agent"
        CHANNEL_PARTNER = "CHANNEL_PARTNER", "Channel Partner"
        TPA_ADMIN = "TPA_ADMIN", "TPA Admin"
        TPA_MANAGER = "TPA_MANAGER", "TPA Manager"
        TPA_PROCESSOR = "TPA_PROCESSOR", "TPA Processor"
        TPA_VIEWER = "TPA_VIEWER", "TPA Viewer"
        AUDITOR = "AUDITOR", "Auditor"

    class ColorMode(models.TextChoices):
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"
        AUTO = "auto", "System"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="users")
    role = models.CharField(max_length=30, choices=Role.choices)
    job_title = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    photo = models.ImageField(upload_to="profiles/%Y/%m/", blank=True, null=True)
    can_override_workflow = models.BooleanField(default=False)
    portal_theme = models.CharField(max_length=30, choices=BOOTSWATCH_THEMES, default="default")
    color_mode = models.CharField(max_length=10, choices=ColorMode.choices, default=ColorMode.AUTO)

    def __str__(self):
        return f"{self.user.username} / {self.get_role_display()}"


class PlatformConfiguration(TimeStampedModel):
    class AmountApprovalParty(models.TextChoices):
        INSURER = "INSURER", "Insurance company"
        CLIENT = "CLIENT", "Client"

    name = models.CharField(max_length=80, default="Default", unique=True)
    auto_stp_enabled = models.BooleanField(default=True, verbose_name="Enable straight-through processing")
    default_currency = models.CharField(max_length=3, default="OMR")
    maximum_upload_mb = models.PositiveSmallIntegerField(default=10)
    weekend_days = models.JSONField(default=default_weekend_days, help_text="Python weekday numbers; Oman default Friday/Saturday = 4,5")
    require_ai_for_unstructured_uploads = models.BooleanField(default=False)
    enable_email_notifications = models.BooleanField(default=True)
    enable_sla_escalations = models.BooleanField(default=True)
    enable_portal_notifications = models.BooleanField(default=True)
    endorsement_expiry_cutoff_days = models.PositiveSmallIntegerField(
        default=30,
        help_text="Block new/processing endorsements when policy has fewer than this many days remaining. Policy-level override takes precedence.",
    )
    tpa_amount_approval_party = models.CharField(
        max_length=12, choices=AmountApprovalParty.choices, default=AmountApprovalParty.INSURER,
        help_text="Who must approve a TPA premium/refund amount changed from the system-calculated amount.",
    )
    support_email = models.EmailField(blank=True)
    settings_json = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.name


class AIProviderConfig(TimeStampedModel):
    class Provider(models.TextChoices):
        OLLAMA = "OLLAMA", "Ollama / Llama"
        HUGGINGFACE = "HUGGINGFACE", "Hugging Face Inference Providers / Endpoint"
        OPENAI = "OPENAI", "OpenAI"
        ANTHROPIC = "ANTHROPIC", "Anthropic Claude"
        OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE", "OpenAI-compatible API"

    name = models.CharField(max_length=100)
    provider = models.CharField(max_length=30, choices=Provider.choices)
    model_name = models.CharField(max_length=150)
    base_url = models.URLField(blank=True)
    api_key = models.TextField(blank=True, help_text="For production prefer a secret manager. Admin access to this model should be tightly restricted.")
    secret_reference = models.CharField(max_length=160, blank=True, help_text="Environment variable containing the API token; takes precedence over API key.")
    inference_provider = models.CharField(max_length=80, blank=True, help_text="Hugging Face routing provider or policy (e.g. auto, fastest, cheapest, preferred). Leave blank for automatic routing.")
    priority = models.PositiveSmallIntegerField(default=100, help_text="Lower numbers are selected first. Multiple text and vision providers may be active.")
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.00"))
    timeout_seconds = models.PositiveIntegerField(default=120)
    is_active = models.BooleanField(default=False)
    supports_vision = models.BooleanField(default=False)
    options = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.name} ({self.provider}: {self.model_name})"

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError
        if not isinstance(self.options, dict):
            raise ValidationError({"options": "Options must be a JSON object."})
        if self.provider == self.Provider.OPENAI_COMPATIBLE and not self.base_url:
            raise ValidationError({"base_url": "A base URL is required for compatible/local APIs."})


class SLAProfile(TimeStampedModel):
    class Stage(models.TextChoices):
        CLIENT_RESPONSE = "CLIENT_RESPONSE", "Client response"
        INSURER_REVIEW = "INSURER_REVIEW", "Insurer review"
        TPA_PROCESSING = "TPA_PROCESSING", "TPA processing"
        QUERY_RESPONSE = "QUERY_RESPONSE", "Query response"
        CORE_PROCESSING = "CORE_PROCESSING", "Core-system processing"
        APPROVAL = "APPROVAL", "Approval"

    name = models.CharField(max_length=120)
    stage = models.CharField(max_length=30, choices=Stage.choices)
    target_hours = models.PositiveIntegerField(default=24)
    warning_hours = models.PositiveIntegerField(default=4)
    business_hours_only = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} - {self.get_stage_display()} ({self.target_hours}h)"


class Policy(TimeStampedModel):
    class Product(models.TextChoices):
        GROUP_MEDICAL = "GROUP_MEDICAL", "Group Medical"
        GROUP_LIFE = "GROUP_LIFE", "Group Life"

    class RatingMethod(models.TextChoices):
        FLAT_ANNUAL = "FLAT_ANNUAL", "Flat annual / plan rate"
        PER_MILLE_SUM_ASSURED = "PER_MILLE_SUM_ASSURED", "Per mille of sum assured"
        PERCENT_OF_SALARY = "PERCENT_OF_SALARY", "Percent of annual salary"

    policy_number = models.CharField(max_length=80, unique=True)
    policy_name = models.CharField(max_length=200)
    product = models.CharField(max_length=30, choices=Product.choices)
    insurer = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="insured_policies", limit_choices_to={"organization_type": Organization.Type.INSURER})
    client = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="client_policies", limit_choices_to={"organization_type": Organization.Type.CLIENT})
    tpa = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="tpa_policies", null=True, blank=True, limit_choices_to={"organization_type": Organization.Type.TPA})
    broker_code = models.CharField(max_length=50, blank=True)
    agent_code = models.CharField(max_length=50, blank=True)
    channel_code = models.CharField(max_length=50, blank=True)
    effective_from = models.DateField()
    effective_to = models.DateField()
    currency = models.CharField(max_length=3, default="OMR")
    rating_method = models.CharField(max_length=40, choices=RatingMethod.choices, default=RatingMethod.FLAT_ANNUAL)
    rating_parameters = models.JSONField(default=dict, blank=True, help_text="Examples: rate_per_mille, salary_percent, default_annual_rate")
    required_fields_addition = models.JSONField(default=list, blank=True)
    required_fields_deletion = models.JSONField(default=list, blank=True)
    mandatory_documents_addition = models.JSONField(default=list, blank=True)
    mandatory_documents_deletion = models.JSONField(default=list, blank=True)
    day_count_basis = models.PositiveSmallIntegerField(default=365)
    allow_backdated_days = models.PositiveSmallIntegerField(default=0)
    endorsement_expiry_cutoff_days = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="Optional override of global cutoff. Example 30 or 60. Zero allows processing until policy expiry.",
    )
    auto_stp = models.BooleanField(default=True)
    insurer_sla = models.ForeignKey(SLAProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="insurer_policies")
    tpa_sla = models.ForeignKey(SLAProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="tpa_policies")
    client_query_sla = models.ForeignKey(SLAProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="query_policies")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.policy_number} - {self.policy_name}"


class PolicyPlan(TimeStampedModel):
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="plans")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    annual_rate = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"), validators=[MinValueValidator(Decimal("0"))])
    sum_assured = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Optional plan-level sum assured. When configured, endorsement member sum assured is derived from the selected plan.",
    )
    relationship_rates = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["policy", "code"], name="uq_policy_plan_code")]

    def __str__(self):
        return f"{self.policy.policy_number} / {self.code} - {self.name}"


class AIExtractionProfile(TimeStampedModel):
    class Task(models.TextChoices):
        DOCUMENT_EXTRACTION = "DOCUMENT_EXTRACTION", "Document OCR / extraction"
        STRUCTURED_MAPPING = "STRUCTURED_MAPPING", "Structured header mapping"

    name = models.CharField(max_length=120, unique=True)
    task = models.CharField(max_length=30, choices=Task.choices)
    product = models.CharField(
        max_length=30,
        choices=Policy.Product.choices,
        blank=True,
        help_text="Leave blank for a global profile. Product-specific active profiles override the global profile.",
    )
    system_prompt = models.TextField(
        blank=True,
        help_text="Optional system instruction sent to the LLM before the extraction request.",
    )
    instructions = models.TextField(
        blank=True,
        help_text="Task-specific extraction instructions. Canonical field rules are appended automatically.",
    )
    field_aliases = models.JSONField(
        default=dict,
        blank=True,
        help_text='Optional field guidance, e.g. {"national_id": ["Civil ID", "Resident ID"], "plan_code": ["Plan", "Category"]}.',
    )
    priority = models.PositiveSmallIntegerField(default=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("-priority", "name")

    def __str__(self):
        scope = self.get_product_display() if self.product else "Global"
        return f"{self.name} / {self.get_task_display()} / {scope}"


class AITrainingExample(TimeStampedModel):
    profile = models.ForeignKey(AIExtractionProfile, on_delete=models.CASCADE, related_name="examples")
    name = models.CharField(max_length=120)
    input_text = models.TextField(
        help_text="Representative OCR text, document wording, or structured source row/header.",
    )
    expected_output = models.JSONField(
        default=list,
        help_text="Expected canonical JSON array. This is inserted as a few-shot example for the LLM.",
    )
    sort_order = models.PositiveSmallIntegerField(default=10)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("sort_order", "id")

    def __str__(self):
        return f"{self.profile.name} / {self.name}"


class PolicyAccess(TimeStampedModel):
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="access_grants")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="policy_access")
    can_create = models.BooleanField(default=True)
    can_view_premium = models.BooleanField(default=True)
    can_view_members = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["policy", "organization"], name="uq_policy_org_access")]

    def __str__(self):
        return f"{self.organization.code} -> {self.policy.policy_number}"


class PolicyMember(TimeStampedModel):
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="members")
    member_no = models.CharField(max_length=80)
    employee_no = models.CharField(max_length=80, blank=True)
    national_id = models.CharField(max_length=80, blank=True)
    full_name = models.CharField(max_length=200)
    relationship = models.CharField(max_length=50, default="Employee")
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    plan = models.ForeignKey(PolicyPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name="members")
    annual_salary = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    sum_assured = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    coverage_from = models.DateField(null=True, blank=True)
    coverage_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["policy", "member_no"], name="uq_policy_member_no")]

    def __str__(self):
        return f"{self.member_no} - {self.full_name}"


class IntegrationEndpoint(TimeStampedModel):
    class OwnerType(models.TextChoices):
        INSURER_CORE = "INSURER_CORE", "Insurer core system"
        TPA = "TPA", "TPA"

    class Transport(models.TextChoices):
        API = "API", "REST API"
        EMAIL = "EMAIL", "Email"

    name = models.CharField(max_length=120)
    owner_type = models.CharField(max_length=20, choices=OwnerType.choices)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="integration_endpoints")
    product = models.CharField(max_length=30, choices=Policy.Product.choices, blank=True)
    transport = models.CharField(max_length=20, choices=Transport.choices)
    endpoint_url = models.URLField(blank=True)
    recipient_email = models.EmailField(blank=True)
    auth_headers = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class EndorsementRequest(TimeStampedModel):
    class Type(models.TextChoices):
        ADDITION = "ADDITION", "Addition"
        DELETION = "DELETION", "Deletion"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        VALIDATING = "VALIDATING", "Validating"
        NEEDS_INFO = "NEEDS_INFO", "Needs information"
        SUBMITTED = "SUBMITTED", "Submitted"
        PENDING_INSURER_APPROVAL = "PENDING_INSURER_APPROVAL", "Pending insurer approval"
        PENDING_AMOUNT_APPROVAL = "PENDING_AMOUNT_APPROVAL", "Pending amount approval"
        AUTO_APPROVED = "AUTO_APPROVED", "Auto approved"
        SENT_TO_TPA = "SENT_TO_TPA", "Sent to TPA"
        TPA_IN_PROGRESS = "TPA_IN_PROGRESS", "TPA in progress"
        TPA_QUERY = "TPA_QUERY", "TPA query"
        CORE_DISPATCHED = "CORE_DISPATCHED", "Dispatched to insurer core"
        COMPLETED = "COMPLETED", "Completed"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"
        FAILED = "FAILED", "Automation failed"

    reference = models.CharField(max_length=32, unique=True, editable=False)
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, related_name="endorsements")
    endorsement_type = models.CharField(max_length=20, choices=Type.choices)
    effective_date = models.DateField()
    requester = models.ForeignKey(User, on_delete=models.PROTECT, related_name="endorsement_requests")
    requester_organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="endorsement_requests")
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT)
    stp_eligible = models.BooleanField(default=False)
    validation_score = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    validation_errors = models.JSONField(default=list, blank=True)
    ai_summary = models.TextField(blank=True)
    premium_impact = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0.000"))
    currency = models.CharField(max_length=3, default="OMR")
    current_sla_due_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    external_reference = models.CharField(max_length=120, blank=True)
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_endorsements")
    metadata = models.JSONField(default=dict, blank=True)

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"END-{timezone.now():%Y%m}-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    @property
    def sla_breached(self):
        closed = {self.Status.COMPLETED, self.Status.CANCELLED, self.Status.REJECTED}
        return bool(self.current_sla_due_at and self.status not in closed and timezone.now() > self.current_sla_due_at)

    def __str__(self):
        return self.reference


class EndorsementItem(TimeStampedModel):
    class ValidationStatus(models.TextChoices):
        PENDING = "PENDING", "Pending validation"
        VALID = "VALID", "Correct"
        ERROR = "ERROR", "Error"
        EXISTING = "EXISTING", "Existing record"
        APPROVAL_REQUIRED = "APPROVAL_REQUIRED", "Approval required"

    request = models.ForeignKey(EndorsementRequest, on_delete=models.CASCADE, related_name="items")
    member_no = models.CharField(max_length=80, blank=True)
    employee_no = models.CharField(max_length=80, blank=True)
    national_id = models.CharField(max_length=80, blank=True)
    full_name = models.CharField(max_length=200, blank=True)
    relationship = models.CharField(max_length=50, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    plan = models.ForeignKey(PolicyPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name="endorsement_items")
    annual_salary = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    sum_assured = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    extracted_data = models.JSONField(default=dict, blank=True)
    validation_errors = models.JSONField(default=list, blank=True)
    validation_status = models.CharField(max_length=24, choices=ValidationStatus.choices, default=ValidationStatus.PENDING)
    is_existing_record = models.BooleanField(default=False)
    requires_insurer_approval = models.BooleanField(default=False)
    resolution_data = models.JSONField(default=dict, blank=True)
    annual_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"))
    prorata_factor = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal("0.000000"))
    premium_impact = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"))
    card_number = models.CharField(max_length=100, blank=True)
    tpa_effective_date = models.DateField(null=True, blank=True)
    tpa_premium_amount = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)

    def __str__(self):
        return self.full_name or self.member_no or f"Item {self.pk}"


class Attachment(TimeStampedModel):
    class Kind(models.TextChoices):
        EXCEL = "EXCEL", "Excel / CSV"
        PDF = "PDF", "PDF"
        IMAGE = "IMAGE", "Image"
        OTHER = "OTHER", "Other"

    request = models.ForeignKey(EndorsementRequest, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="endorsements/%Y/%m/")
    original_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    processed = models.BooleanField(default=False)
    processing_error = models.TextField(blank=True)
    extracted_payload = models.JSONField(default=dict, blank=True)
    is_supplemental = models.BooleanField(default=False)

    def __str__(self):
        return self.original_name


class RecoveryUpload(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSED = "PROCESSED", "Processed"
        PARTIAL = "PARTIAL", "Partially matched"
        FAILED = "FAILED", "Failed"

    reference = models.CharField(max_length=32, unique=True, editable=False)
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="recovery_uploads")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="recovery_uploads")
    file = models.FileField(upload_to="recovery/%Y/%m/")
    original_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=20, choices=Attachment.Kind.choices, default=Attachment.Kind.OTHER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    resolved_count = models.PositiveIntegerField(default=0)
    ambiguous_count = models.PositiveIntegerField(default=0)
    unmatched_count = models.PositiveIntegerField(default=0)
    processing_error = models.TextField(blank=True)
    extracted_payload = models.JSONField(default=dict, blank=True)

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"REC-{timezone.now():%Y%m}-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} - {self.original_name}"


class EndorsementQuery(TimeStampedModel):
    request = models.ForeignKey(EndorsementRequest, on_delete=models.CASCADE, related_name="queries")
    raised_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="raised_endorsement_queries")
    assigned_organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="endorsement_queries")
    subject = models.CharField(max_length=200)
    message = models.TextField()
    response = models.TextField(blank=True)
    responded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="answered_endorsement_queries")
    due_at = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    is_closed = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.request.reference}: {self.subject}"


class EndorsementApproval(TimeStampedModel):
    class ApprovalType(models.TextChoices):
        EXISTING_MEMBER = "EXISTING_MEMBER", "Existing member exception"
        INSURER_REVIEW = "INSURER_REVIEW", "Insurer manual review"
        TPA_AMOUNT_CHANGE = "TPA_AMOUNT_CHANGE", "TPA amount change"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    request = models.ForeignKey(EndorsementRequest, on_delete=models.CASCADE, related_name="approvals")
    item = models.ForeignKey(EndorsementItem, on_delete=models.CASCADE, related_name="approvals", null=True, blank=True)
    approval_type = models.CharField(max_length=30, choices=ApprovalType.choices)
    assigned_organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="endorsement_approvals")
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="requested_endorsement_approvals")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    reason = models.TextField(blank=True)
    old_amount = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    new_amount = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="decided_endorsement_approvals")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.TextField(blank=True)

    def __str__(self):
        return f"{self.request.reference} / {self.get_approval_type_display()} / {self.get_status_display()}"



class EmailIntakeMailbox(TimeStampedModel):
    class Provider(models.TextChoices):
        MICROSOFT_GRAPH = "MICROSOFT_GRAPH", "Microsoft 365 / Graph"
        IMAP = "IMAP", "IMAP"

    name = models.CharField(max_length=120, unique=True)
    email_address = models.EmailField()
    provider = models.CharField(max_length=30, choices=Provider.choices, default=Provider.MICROSOFT_GRAPH)
    is_active = models.BooleanField(default=True)
    auto_submit = models.BooleanField(
        default=True,
        help_text="When enabled, successfully extracted email endorsements are immediately validated and routed into the normal workflow.",
    )
    mark_as_read = models.BooleanField(default=True)
    folder = models.CharField(max_length=120, default="Inbox")
    poll_interval_seconds = models.PositiveIntegerField(default=60)
    max_messages_per_poll = models.PositiveSmallIntegerField(default=25)

    default_requester = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="default_email_intake_mailboxes",
        help_text="Fallback requester only when no sender route or matching portal user is available.",
    )

    imap_host = models.CharField(max_length=255, blank=True)
    imap_port = models.PositiveIntegerField(default=993)
    imap_username = models.CharField(max_length=255, blank=True)
    imap_password = models.TextField(blank=True)
    imap_use_ssl = models.BooleanField(default=True)

    graph_tenant_id = models.CharField(max_length=120, blank=True)
    graph_client_id = models.CharField(max_length=120, blank=True)
    graph_client_secret = models.TextField(blank=True)

    last_polled_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} <{self.email_address}>"


class EmailIntakeRoute(TimeStampedModel):
    mailbox = models.ForeignKey(EmailIntakeMailbox, on_delete=models.CASCADE, related_name="routes")
    sender_pattern = models.CharField(
        max_length=255,
        help_text="Exact sender email or domain pattern such as hr@client.com or @client.com.",
    )
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="email_intake_routes")
    requester = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="email_intake_routes",
        help_text="Optional fixed requester. If blank, SmartEndorse first matches the sender to an active portal user in this organization.",
    )
    default_policy = models.ForeignKey(
        Policy, on_delete=models.SET_NULL, null=True, blank=True, related_name="email_intake_routes",
        help_text="Optional fallback policy when the email does not state a policy number.",
    )
    default_endorsement_type = models.CharField(
        max_length=20, choices=[("", "Detect from email"), *EndorsementRequest.Type.choices], blank=True,
    )
    priority = models.PositiveSmallIntegerField(default=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("-priority", "id")

    def __str__(self):
        return f"{self.mailbox.name}: {self.sender_pattern} -> {self.organization.code}"


class EmailIntakeMessage(TimeStampedModel):
    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSING = "PROCESSING", "Processing"
        PROCESSED = "PROCESSED", "Processed"
        NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"
        FAILED = "FAILED", "Failed"
        IGNORED = "IGNORED", "Ignored"

    mailbox = models.ForeignKey(EmailIntakeMailbox, on_delete=models.CASCADE, related_name="messages")
    provider_message_id = models.CharField(max_length=500)
    internet_message_id = models.CharField(max_length=500, blank=True)
    sender_email = models.EmailField(blank=True)
    sender_name = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    body_text = models.TextField(blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    request = models.ForeignKey(
        EndorsementRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="email_intake_messages"
    )
    attachment_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-received_at", "-created_at")
        constraints = [
            models.UniqueConstraint(fields=["mailbox", "provider_message_id"], name="uq_email_intake_provider_message")
        ]

    def __str__(self):
        return self.subject or self.internet_message_id or self.provider_message_id


class PortalNotification(TimeStampedModel):
    class Level(models.TextChoices):
        INFO = "INFO", "Info"
        SUCCESS = "SUCCESS", "Success"
        WARNING = "WARNING", "Warning"
        DANGER = "DANGER", "Danger"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="portal_notifications")
    title = models.CharField(max_length=180)
    message = models.TextField()
    level = models.CharField(max_length=12, choices=Level.choices, default=Level.INFO)
    link = models.CharField(max_length=500, blank=True)
    is_read = models.BooleanField(default=False)
    browser_notified = models.BooleanField(default=False)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username}: {self.title}"


class WorkflowEvent(TimeStampedModel):
    request = models.ForeignKey(EndorsementRequest, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="endorsement_events")
    event_type = models.CharField(max_length=80)
    from_status = models.CharField(max_length=30, blank=True)
    to_status = models.CharField(max_length=30, blank=True)
    description = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.request.reference} / {self.event_type}"


class MailboxConfiguration(TimeStampedModel):
    class Transport(models.TextChoices):
        IMAP = "IMAP", "IMAP / SMTP"
        GRAPH = "GRAPH", "Microsoft 365 / Graph"

    name = models.CharField(max_length=120, unique=True)
    email_address = models.EmailField()
    transport = models.CharField(max_length=12, choices=Transport.choices, default=Transport.IMAP)
    is_active = models.BooleanField(default=False)
    imap_host = models.CharField(max_length=200, blank=True)
    imap_port = models.PositiveIntegerField(default=993)
    imap_username = models.CharField(max_length=200, blank=True)
    credential_reference = models.CharField(max_length=160, blank=True, validators=[validate_credential_reference], help_text="Environment variable name, e.g. ENDORSEMENT_IMAP_PASSWORD. Set its value in .env or the worker environment. Do not enter the password/token here.")
    use_oauth = models.BooleanField(default=False)
    folder = models.CharField(max_length=100, default="INBOX")
    graph_tenant_id = models.CharField(max_length=120, blank=True)
    graph_client_id = models.CharField(max_length=120, blank=True)
    graph_secret_reference = models.CharField(max_length=160, blank=True, validators=[validate_credential_reference], help_text="Environment variable name, e.g. ENDORSEMENT_GRAPH_CLIENT_SECRET. Set its value to the Entra client secret VALUE in .env or the worker environment. Do not enter the secret or its ID here.")
    default_policy = models.ForeignKey(Policy, null=True, blank=True, on_delete=models.SET_NULL)
    auto_submit = models.BooleanField(default=False, help_text="After successful validation, continue through the existing approval/STP workflow.")
    require_sender_authentication = models.BooleanField(default=True, help_text="Require an aligned DMARC pass from a configured trusted Authentication-Results server before applying email data.")
    trusted_authserv_ids = models.JSONField(default=list, blank=True, help_text="Exact authserv-id names of your receiving mail servers. Ignore Authentication-Results from any other server.")
    cursor = models.JSONField(default=dict, blank=True, editable=False)
    last_sync_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True, editable=False)

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError
        errors = {}
        if self.transport == self.Transport.IMAP and not self.imap_host:
            errors["imap_host"] = "IMAP host is required."
        if self.transport == self.Transport.GRAPH and not all((self.graph_tenant_id, self.graph_client_id, self.graph_secret_reference)):
            errors["graph_client_id"] = "Tenant, client ID and client secret reference are required for Graph."
        if not isinstance(self.trusted_authserv_ids, list) or any(not isinstance(x, str) for x in self.trusted_authserv_ids):
            errors["trusted_authserv_ids"] = "Use a JSON array of mail server names."
        elif self.is_active and self.require_sender_authentication and not self.trusted_authserv_ids:
            errors["trusted_authserv_ids"] = "Configure your receiving server IDs before enabling sender authentication."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.name} / {self.email_address}"


class EmailAuthority(TimeStampedModel):
    name = models.CharField(max_length=120)
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="email_authorities")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    email_address = models.EmailField(blank=True, help_text="An exact sender address. Alternatively select a portal user or authorized group.")
    user = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name="email_authorities")
    group = models.ForeignKey("auth.Group", on_delete=models.PROTECT, null=True, blank=True, help_text="Active portal members of this group may submit replies for this policy.")
    processing_user = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name="processed_email_authorities", help_text="Required for external email addresses without a matching portal user. Used for workflow attribution.")
    permitted_types = models.JSONField(default=list, blank=True, help_text='Allowed types: ["ADDITION", "DELETION"]. Empty permits both.')
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        from django.core.exceptions import ValidationError
        if sum(bool(x) for x in (self.email_address, self.user_id, self.group_id)) != 1:
            raise ValidationError("Select exactly one sender identity: email address, portal user or group.")
        if not isinstance(self.permitted_types, list) or any(x not in EndorsementRequest.Type.values for x in self.permitted_types):
            raise ValidationError({"permitted_types": "Use ADDITION and/or DELETION in a JSON array."})
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValidationError({"valid_until": "Must be on or after valid from."})

    def __str__(self):
        return self.name


class InboundEmail(TimeStampedModel):
    class State(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        NEEDS_INFO = "NEEDS_INFO", "Awaiting correction"
        PROCESSED = "PROCESSED", "Processed"
        UNAUTHORIZED = "UNAUTHORIZED", "Unauthorized sender"
        NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"
        IGNORED = "IGNORED", "Ignored automatic reply"

    reference = models.CharField(max_length=32, unique=True, editable=False)
    mailbox = models.ForeignKey(MailboxConfiguration, on_delete=models.PROTECT, related_name="messages")
    message_uid = models.CharField(max_length=512)
    message_id = models.CharField(max_length=512, blank=True)
    in_reply_to = models.CharField(max_length=512, blank=True)
    references_header = models.TextField(blank=True)
    sender = models.EmailField()
    subject = models.CharField(max_length=998)
    body_text = models.TextField(blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    headers = models.JSONField(default=dict, blank=True)
    raw_message = models.FileField(upload_to="inbound/%Y/%m/", blank=True)
    thread = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="corrections")
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, null=True, blank=True)
    endorsement = models.ForeignKey(EndorsementRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="inbound_emails")
    authority = models.ForeignKey(EmailAuthority, on_delete=models.SET_NULL, null=True, blank=True)
    processing_state = models.CharField(max_length=20, choices=State.choices, default=State.RECEIVED)
    processing_error = models.TextField(blank=True)
    extracted_payload = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-received_at", "-pk")
        constraints = [
            models.UniqueConstraint(fields=["mailbox", "message_uid"], name="uq_mailbox_message_uid"),
            models.UniqueConstraint(fields=["mailbox", "message_id"], condition=~models.Q(message_id=""), name="uq_mailbox_message_id"),
        ]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = f"EML-{timezone.now():%Y%m}-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    @property
    def thread_reference(self):
        return self.thread.reference if self.thread_id else self.reference

    def __str__(self):
        return f"{self.reference} / {self.subject}"


class EmailEvidence(TimeStampedModel):
    email = models.ForeignKey(InboundEmail, on_delete=models.CASCADE, related_name="evidence")
    file = models.FileField(upload_to="inbound/evidence/%Y/%m/")
    original_name = models.CharField(max_length=255)
    attachment = models.ForeignKey(Attachment, on_delete=models.SET_NULL, null=True, blank=True)


class EmailReply(TimeStampedModel):
    email = models.OneToOneField(InboundEmail, on_delete=models.PROTECT, related_name="reply")
    subject = models.CharField(max_length=998)
    body_text = models.TextField()
    body_html = models.TextField()
    correction_csv = models.TextField(blank=True)
    message_id = models.CharField(max_length=255)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    graph_draft_id = models.CharField(max_length=512, blank=True)

    def __str__(self):
        return self.subject
