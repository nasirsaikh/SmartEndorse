from django import forms
from django.contrib import admin
from .models import (
    AIProviderConfig, Attachment, EndorsementItem, EndorsementQuery, EndorsementRequest,
    IntegrationEndpoint, Organization, PlatformConfiguration, Policy, PolicyAccess,
    PolicyMember, PolicyPlan, SLAProfile, UserProfile, WorkflowEvent,
)


class AIProviderAdminForm(forms.ModelForm):
    class Meta:
        model = AIProviderConfig
        fields = "__all__"
        widgets = {"api_key": forms.PasswordInput(render_value=True)}


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "organization_type", "notification_email", "is_active")
    list_filter = ("organization_type", "is_active")
    search_fields = ("code", "name", "notification_email")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "job_title", "can_override_workflow")
    list_filter = ("role", "organization__organization_type")
    search_fields = ("user__username", "user__email", "organization__name")


@admin.register(PlatformConfiguration)
class PlatformConfigurationAdmin(admin.ModelAdmin):
    list_display = ("name", "auto_stp_enabled", "default_currency", "enable_email_notifications", "enable_sla_escalations")


@admin.register(AIProviderConfig)
class AIProviderConfigAdmin(admin.ModelAdmin):
    form = AIProviderAdminForm
    list_display = ("name", "provider", "model_name", "is_active", "supports_vision", "updated_at")
    list_filter = ("provider", "is_active", "supports_vision")
    search_fields = ("name", "model_name")
    def save_model(self, request, obj, form, change):
        if obj.is_active:
            AIProviderConfig.objects.exclude(pk=obj.pk).update(is_active=False)
        super().save_model(request, obj, form, change)


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
    list_display = ("policy_number", "policy_name", "product", "client", "tpa", "effective_from", "effective_to", "rating_method", "auto_stp", "is_active")
    list_filter = ("product", "rating_method", "auto_stp", "is_active", "tpa")
    search_fields = ("policy_number", "policy_name", "client__name", "broker_code", "agent_code", "channel_code")
    inlines = [PlanInline, AccessInline]
    fieldsets = (
        ("Identity", {"fields": ("policy_number", "policy_name", "product", "insurer", "client", "tpa", "is_active")}),
        ("Distribution", {"fields": ("broker_code", "agent_code", "channel_code")}),
        ("Period & pricing", {"fields": ("effective_from", "effective_to", "currency", "rating_method", "rating_parameters", "day_count_basis", "allow_backdated_days")}),
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
    readonly_fields = ("annual_premium", "prorata_factor", "premium_impact")


@admin.register(EndorsementRequest)
class EndorsementRequestAdmin(admin.ModelAdmin):
    list_display = ("reference", "policy", "endorsement_type", "status", "requester_organization", "stp_eligible", "premium_impact", "current_sla_due_at", "created_at")
    list_filter = ("status", "endorsement_type", "stp_eligible", "policy__product", "policy__tpa")
    search_fields = ("reference", "policy__policy_number", "requester__username", "requester_organization__name", "external_reference")
    readonly_fields = ("reference", "validation_score", "premium_impact", "submitted_at", "completed_at", "created_at", "updated_at")
    inlines = [ItemInline]


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_name", "request", "kind", "processed", "created_at")
    list_filter = ("kind", "processed")


@admin.register(EndorsementQuery)
class EndorsementQueryAdmin(admin.ModelAdmin):
    list_display = ("request", "subject", "raised_by", "assigned_organization", "due_at", "is_closed")
    list_filter = ("is_closed", "assigned_organization")


@admin.register(WorkflowEvent)
class WorkflowEventAdmin(admin.ModelAdmin):
    list_display = ("request", "event_type", "from_status", "to_status", "actor", "created_at")
    list_filter = ("event_type", "to_status")
    search_fields = ("request__reference", "description")
    readonly_fields = ("request", "event_type", "from_status", "to_status", "actor", "description", "payload", "created_at", "updated_at")


admin.site.register(PolicyPlan)
admin.site.register(PolicyAccess)
admin.site.register(IntegrationEndpoint)
admin.site.site_header = "SmartEndorse Administration"
admin.site.site_title = "SmartEndorse Admin"
admin.site.index_title = "Automation & Configuration"
