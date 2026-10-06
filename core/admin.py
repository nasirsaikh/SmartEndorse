from django import forms
from django.contrib import admin

from .models import (
    AIExtractionProfile, AIProviderConfig, AITrainingExample, Attachment, EmailIntakeMailbox, EmailIntakeMessage, EmailIntakeRoute,
    EndorsementApproval, EndorsementItem, EndorsementQuery, EndorsementRequest, IntegrationEndpoint, Organization, PlatformConfiguration,
    Policy, PolicyAccess, PolicyMember, PolicyPlan, PortalNotification, RecoveryUpload, SLAProfile,
    UserProfile, WorkflowEvent,
)


class AIProviderAdminForm(forms.ModelForm):
    class Meta:
        model = AIProviderConfig
        fields = "__all__"
        widgets = {"api_key": forms.PasswordInput(render_value=True)}


class SavedSecretAdminForm(forms.ModelForm):
    """Keep stored secrets on blank submissions without rendering them into HTML."""
    secret_fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.secret_fields:
            if name not in self.fields:
                continue
            self.fields[name].strip = False
            if self.instance.pk and getattr(self.instance, name, ""):
                self.fields[name].help_text += " A value is saved. Leave blank to keep it, enter a replacement, or select Clear saved value."

    def clean(self):
        cleaned = super().clean()
        for name in self.secret_fields:
            if name not in self.fields:
                continue
            if cleaned.get("clear_" + name):
                if cleaned.get(name):
                    self.add_error(name, "Enter a replacement or select Clear saved value, not both.")
                else:
                    cleaned[name] = ""
            elif not cleaned.get(name) and self.instance.pk:
                cleaned[name] = getattr(self.instance, name)
        return cleaned


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "organization_type", "notification_email", "is_active")
    list_filter = ("organization_type", "is_active")
    search_fields = ("code", "name", "notification_email")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "job_title", "portal_theme", "color_mode", "can_override_workflow")
    list_filter = ("role", "organization__organization_type", "portal_theme", "color_mode")
    search_fields = ("user__username", "user__email", "organization__name")


@admin.register(PlatformConfiguration)
class PlatformConfigurationAdmin(admin.ModelAdmin):
    list_display = (
        "name", "auto_stp_enabled", "endorsement_expiry_cutoff_days",
        "tpa_amount_approval_party", "enable_email_notifications", "enable_portal_notifications",
        "enable_sla_escalations",
    )
    fieldsets = (
        ("Processing", {"fields": ("name", "auto_stp_enabled", "default_currency", "maximum_upload_mb", "endorsement_expiry_cutoff_days", "tpa_amount_approval_party")}),
        ("Notifications & SLA", {"fields": ("enable_email_notifications", "enable_portal_notifications", "enable_sla_escalations", "support_email", "weekend_days")}),
        ("AI / advanced", {"fields": ("require_ai_for_unstructured_uploads", "settings_json")}),
    )


@admin.register(AIProviderConfig)
class AIProviderConfigAdmin(admin.ModelAdmin):
    form = AIProviderAdminForm
    list_display = ("name", "provider", "model_name", "priority", "is_active", "supports_vision", "updated_at")
    list_filter = ("provider", "is_active", "supports_vision")
    search_fields = ("name", "model_name")
    fieldsets = (
        ("Provider", {"fields": ("name", "provider", "model_name", "base_url", "secret_reference", "api_key", "inference_provider")}),
        ("Capabilities", {
            "fields": ("is_active", "supports_vision", "priority"),
            "description": (
                "Multiple providers may be active. Lower priority numbers are selected first. SmartEndorse uses a non-vision provider for normal text tasks "
                "when available, and automatically selects an active Supports vision provider for image/scanned-PDF OCR."
            ),
        }),
        ("Runtime", {
            "fields": ("temperature", "timeout_seconds", "options"),
            "description": (
                "Hugging Face uses https://router.huggingface.co/v1 when the base URL is blank; use a model served by your chosen inference provider. A dedicated endpoint may end in /v1 or /v1/chat/completions. request_parameters controls model-specific API options; a null value omits a parameter. response_format may be json_object or json_schema when supported. For Ollama, options may override runtime parameters. If no separate active text provider is configured, "
                "SmartEndorse can auto-discover a local semantic mapper from Ollama /api/tags. "
                'Use {"semantic_model": "qwen2.5:7b"} to force a specific installed mapper. '
                "Generic vision OCR defaults to num_ctx=4096; "
                "GLM-OCR defaults to num_ctx=8192 because its image tokens can exceed 4096. "
                'Example: {"num_gpu": -1, "num_predict": 1024, "keep_alive": "15m"}. '
                "num_gpu=-1 means offload as many layers as fit. GLM-OCR automatically runs as OCR-only and then uses "
                "a separate active text provider for strict JSON mapping. If Ollama hits its known GLM-OCR token-repeat "
                "regression, SmartEndorse will try another active non-GLM vision provider before failing. "
                "Use vision_pipeline=direct_json only if you explicitly want the vision model itself to produce JSON."
            ),
        }),
    )


class AITrainingExampleInline(admin.StackedInline):
    model = AITrainingExample
    extra = 0
    fields = ("name", "input_text", "expected_output", "sort_order", "is_active")
    ordering = ("sort_order", "id")


@admin.register(AIExtractionProfile)
class AIExtractionProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "task", "product_scope", "priority", "example_count", "is_active", "updated_at")
    list_filter = ("task", "product", "is_active")
    search_fields = ("name", "instructions", "system_prompt")
    ordering = ("-priority", "name")
    inlines = [AITrainingExampleInline]
    fieldsets = (
        ("Profile", {"fields": ("name", "task", "product", "priority", "is_active")}),
        ("Prompt training", {
            "fields": ("system_prompt", "instructions", "field_aliases"),
            "description": (
                "These settings control prompt-based training used for OCR/document extraction. "
                "Use Training Examples below for few-shot examples. This does not fine-tune model weights."
            ),
        }),
    )

    @admin.display(description="Scope")
    def product_scope(self, obj):
        return obj.get_product_display() if obj.product else "Global"

    @admin.display(description="Examples")
    def example_count(self, obj):
        return obj.examples.filter(is_active=True).count()


class EmailIntakeMailboxAdminForm(SavedSecretAdminForm):
    secret_fields = ("imap_password", "graph_client_secret")
    clear_imap_password = forms.BooleanField(required=False, label="Clear saved IMAP password")
    clear_graph_client_secret = forms.BooleanField(required=False, label="Clear saved Graph client secret")

    class Meta:
        model = EmailIntakeMailbox
        fields = "__all__"
        widgets = {
            "imap_password": forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
            "graph_client_secret": forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        }


class EmailIntakeRouteInline(admin.TabularInline):
    model = EmailIntakeRoute
    extra = 0
    fields = ("sender_pattern", "organization", "requester", "default_policy", "default_endorsement_type", "priority", "is_active")


@admin.register(EmailIntakeMailbox)
class EmailIntakeMailboxAdmin(admin.ModelAdmin):
    form = EmailIntakeMailboxAdminForm
    list_display = ("name", "email_address", "provider", "is_active", "auto_submit", "last_polled_at")
    list_filter = ("provider", "is_active", "auto_submit", "mark_as_read")
    search_fields = ("name", "email_address")
    inlines = [EmailIntakeRouteInline]
    actions = ("poll_selected_mailboxes",)
    fieldsets = (
        ("Mailbox", {"fields": ("name", "email_address", "provider", "is_active", "folder", "max_messages_per_poll", "poll_interval_seconds")}),
        ("Automation", {"fields": ("auto_submit", "mark_as_read", "default_requester")}),
        ("Microsoft 365 / Graph", {
            "fields": ("graph_tenant_id", "graph_client_id", "graph_client_secret", "clear_graph_client_secret"),
            "description": "Use an Entra ID app with Microsoft Graph application permission Mail.ReadWrite for this mailbox. Admin consent is required.",
        }),
        ("IMAP", {
            "fields": ("imap_host", "imap_port", "imap_username", "imap_password", "clear_imap_password", "imap_use_ssl"),
            "description": "Use IMAP for providers that permit it. For Microsoft 365, Graph is recommended.",
        }),
        ("Runtime", {"fields": ("last_polled_at",)}),
    )
    readonly_fields = ("last_polled_at",)

    @admin.action(description="Poll selected mailbox(es) now")
    def poll_selected_mailboxes(self, request, queryset):
        from .email_intake import EmailIntakeService

        messages_seen = processed = failed = 0
        for mailbox in queryset.filter(is_active=True):
            stats = EmailIntakeService.poll_mailbox(mailbox)
            messages_seen += stats["messages"]
            processed += stats["processed"]
            failed += stats["failed"]
        self.message_user(
            request,
            f"Email intake completed: {messages_seen} message(s), {processed} processed, {failed} failed/review.",
        )


@admin.register(EmailIntakeRoute)
class EmailIntakeRouteAdmin(admin.ModelAdmin):
    list_display = ("mailbox", "sender_pattern", "organization", "requester", "default_policy", "default_endorsement_type", "priority", "is_active")
    list_filter = ("mailbox", "organization", "default_endorsement_type", "is_active")
    search_fields = ("sender_pattern", "organization__name", "organization__code", "requester__username", "requester__email")


@admin.register(EmailIntakeMessage)
class EmailIntakeMessageAdmin(admin.ModelAdmin):
    list_display = ("received_at", "mailbox", "sender_email", "subject", "status", "request", "attachment_count")
    list_filter = ("mailbox", "status")
    search_fields = ("sender_email", "subject", "internet_message_id", "provider_message_id", "request__reference")
    readonly_fields = (
        "mailbox", "provider_message_id", "internet_message_id", "sender_email", "sender_name",
        "subject", "body_text", "received_at", "status", "request", "attachment_count",
        "error", "metadata", "processed_at", "created_at", "updated_at",
    )


@admin.register(SLAProfile)
class SLAProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "stage", "target_hours", "warning_hours", "business_hours_only", "is_active")
    list_filter = ("stage", "is_active")


class PlanInline(admin.TabularInline):
    model = PolicyPlan
    extra = 0


class AccessInline(admin.TabularInline):
    model = PolicyAccess
    extra = 0


@admin.register(Policy)
class PolicyAdmin(admin.ModelAdmin):
    list_display = (
        "policy_number", "policy_name", "product", "client", "tpa", "effective_from", "effective_to",
        "endorsement_expiry_cutoff_days", "rating_method", "auto_stp", "is_active",
    )
    list_filter = ("product", "rating_method", "auto_stp", "is_active", "tpa")
    search_fields = ("policy_number", "policy_name", "client__name", "broker_code", "agent_code", "channel_code")
    inlines = [PlanInline, AccessInline]
    fieldsets = (
        ("Identity", {"fields": ("policy_number", "policy_name", "product", "insurer", "client", "tpa", "is_active")}),
        ("Distribution", {"fields": ("broker_code", "agent_code", "channel_code")}),
        ("Period & pricing", {"fields": ("effective_from", "effective_to", "endorsement_expiry_cutoff_days", "currency", "rating_method", "rating_parameters", "day_count_basis", "allow_backdated_days")}),
        ("Automation rules", {"fields": ("auto_stp", "required_fields_addition", "required_fields_deletion", "mandatory_documents_addition", "mandatory_documents_deletion")}),
        ("SLA", {"fields": ("insurer_sla", "tpa_sla", "client_query_sla")}),
    )


@admin.register(PolicyMember)
class PolicyMemberAdmin(admin.ModelAdmin):
    list_display = ("member_no", "full_name", "policy", "relationship", "plan", "is_active")
    list_filter = ("policy", "relationship", "is_active")
    search_fields = ("member_no", "employee_no", "national_id", "full_name", "policy__policy_number")


class ItemInline(admin.TabularInline):
    model = EndorsementItem
    extra = 0
    readonly_fields = ("validation_status", "annual_premium", "prorata_factor", "premium_impact", "extracted_data", "resolution_data")


class ApprovalInline(admin.TabularInline):
    model = EndorsementApproval
    extra = 0
    readonly_fields = ("approval_type", "assigned_organization", "status", "requested_by", "old_amount", "new_amount", "decided_by", "decided_at")


@admin.register(EndorsementRequest)
class EndorsementRequestAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "policy", "endorsement_type", "status", "requester_organization", "stp_eligible",
        "premium_impact", "current_sla_due_at", "created_at",
    )
    list_filter = ("status", "endorsement_type", "stp_eligible", "policy__product", "policy__tpa")
    search_fields = ("reference", "policy__policy_number", "requester__username", "requester_organization__name", "external_reference")
    readonly_fields = ("reference", "validation_score", "premium_impact", "submitted_at", "completed_at", "created_at", "updated_at")
    inlines = [ItemInline, ApprovalInline]


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_name", "request", "kind", "is_supplemental", "processed", "created_at")
    list_filter = ("kind", "is_supplemental", "processed")
    readonly_fields = ("extracted_payload", "processing_error")


@admin.register(EndorsementApproval)
class EndorsementApprovalAdmin(admin.ModelAdmin):
    list_display = ("request", "approval_type", "item", "assigned_organization", "status", "old_amount", "new_amount", "decided_by", "decided_at")
    list_filter = ("approval_type", "status", "assigned_organization")
    search_fields = ("request__reference", "reason", "decision_comment")


@admin.register(EndorsementQuery)
class EndorsementQueryAdmin(admin.ModelAdmin):
    list_display = ("request", "subject", "raised_by", "assigned_organization", "due_at", "is_closed")
    list_filter = ("is_closed", "assigned_organization")


@admin.register(RecoveryUpload)
class RecoveryUploadAdmin(admin.ModelAdmin):
    list_display = ("reference", "original_name", "organization", "status", "resolved_count", "ambiguous_count", "unmatched_count", "created_at")
    list_filter = ("status", "kind", "organization")
    search_fields = ("reference", "original_name", "uploaded_by__username")
    readonly_fields = ("reference", "resolved_count", "ambiguous_count", "unmatched_count", "processing_error", "extracted_payload", "created_at", "updated_at")



@admin.register(PortalNotification)
class PortalNotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "level", "is_read", "created_at")
    list_filter = ("level", "is_read")
    search_fields = ("user__username", "title", "message")


@admin.register(WorkflowEvent)
class WorkflowEventAdmin(admin.ModelAdmin):
    list_display = ("request", "event_type", "from_status", "to_status", "actor", "created_at")
    list_filter = ("event_type", "to_status")
    search_fields = ("request__reference", "description")
    readonly_fields = ("request", "event_type", "from_status", "to_status", "actor", "description", "payload", "created_at", "updated_at")


@admin.register(PolicyPlan)
class PolicyPlanAdmin(admin.ModelAdmin):
    list_display = ("policy", "code", "name", "annual_rate", "sum_assured", "is_active")
    list_filter = ("policy__product", "is_active")
    search_fields = ("policy__policy_number", "code", "name")


admin.site.register(PolicyAccess)
admin.site.register(IntegrationEndpoint)
admin.site.site_header = "SmartEndorse Administration"
admin.site.site_title = "SmartEndorse Admin"
admin.site.index_title = "Automation & Configuration"


from .models import EmailAuthority, EmailEvidence, EmailReply, InboundEmail, MailboxConfiguration


class MailboxConfigurationAdminForm(SavedSecretAdminForm):
    secret_fields = ("imap_password", "graph_client_secret", "smtp_password")
    clear_imap_password = forms.BooleanField(required=False, label="Clear saved IMAP password / token")
    clear_graph_client_secret = forms.BooleanField(required=False, label="Clear saved Graph client secret")
    clear_smtp_password = forms.BooleanField(required=False, label="Clear saved SMTP password")

    class Meta:
        model = MailboxConfiguration
        fields = "__all__"
        widgets = {
            name: forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"})
            for name in ("imap_password", "graph_client_secret", "smtp_password")
        }


@admin.register(MailboxConfiguration)
class MailboxConfigurationAdmin(admin.ModelAdmin):
    form = MailboxConfigurationAdminForm
    list_display = ("name", "email_address", "transport", "is_active", "auto_submit", "last_sync_at", "last_error")
    list_filter = ("transport", "is_active", "auto_submit")
    readonly_fields = ("cursor", "last_sync_at", "last_error")
    fieldsets = (
        ("Mailbox", {"fields": ("name", "email_address", "transport", "is_active", "folder", "default_policy", "auto_submit")}),
        ("IMAP", {"fields": ("imap_host", "imap_port", "imap_username", "imap_password", "clear_imap_password", "use_oauth")}),
        ("Microsoft 365", {"fields": ("graph_tenant_id", "graph_client_id", "graph_client_secret", "clear_graph_client_secret")}),
        ("IMAP reply delivery", {"fields": ("reply_backend", "smtp_host", "smtp_port", "smtp_username", "smtp_password", "clear_smtp_password", "smtp_use_tls", "smtp_use_ssl", "smtp_timeout")}),
        ("Sender verification", {"fields": ("require_sender_authentication", "trusted_authserv_ids")}),
        ("Polling status", {"fields": ("cursor", "last_sync_at", "last_error")}),
    )


@admin.register(EmailAuthority)
class EmailAuthorityAdmin(admin.ModelAdmin):
    list_display = ("name", "policy", "organization", "email_address", "user", "group", "is_active", "valid_until")
    list_filter = ("is_active", "organization", "policy")
    search_fields = ("name", "email_address", "user__email", "policy__policy_number")
    autocomplete_fields = ("policy", "organization", "user", "processing_user", "group")


class EmailEvidenceInline(admin.TabularInline):
    model = EmailEvidence
    extra = 0
    readonly_fields = ("original_name", "file", "attachment")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(InboundEmail)
class InboundEmailAdmin(admin.ModelAdmin):
    list_display = ("reference", "mailbox", "sender", "subject", "processing_state", "endorsement", "received_at")
    list_filter = ("processing_state", "mailbox")
    search_fields = ("reference", "sender", "subject", "endorsement__reference")
    readonly_fields = tuple(field.name for field in InboundEmail._meta.fields)
    inlines = (EmailEvidenceInline,)
    actions = ("retry_intake",)

    def has_add_permission(self, request):
        return False

    @admin.action(description="Recheck authorization and retry email extraction")
    def retry_intake(self, request, queryset):
        from .email_intake import process_email
        for email in queryset:
            process_email(email.pk, force=True)
        self.message_user(request, "Selected emails rechecked and processed.")


@admin.register(EmailReply)
class EmailReplyAdmin(admin.ModelAdmin):
    list_display = ("email", "subject", "sent_at", "attempts", "last_error")
    readonly_fields = tuple(field.name for field in EmailReply._meta.fields)
    actions = ("retry_delivery",)

    def has_add_permission(self, request):
        return False

    @admin.action(description="Retry unsent authorized replies")
    def retry_delivery(self, request, queryset):
        from .email_intake import deliver_reply
        sent = sum(deliver_reply(reply.pk) for reply in queryset.filter(sent_at=None))
        self.message_user(request, f"Delivered {sent} pending reply(s).")
