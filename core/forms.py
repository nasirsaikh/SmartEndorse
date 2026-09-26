from django import forms
from django.contrib.auth.models import User
from django.utils import timezone

from .models import EndorsementItem, EndorsementQuery, EndorsementRequest, Policy, PolicyPlan, UserProfile
from .services import platform_config


ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}
OCR_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
RELATIONSHIP_CHOICES = [
    ("", "Select relationship"),
    ("Employee", "Employee"),
    ("Spouse", "Spouse"),
    ("Child", "Child"),
]
GENDER_CHOICES = [
    ("", "Select gender"),
    ("Male", "Male"),
    ("Female", "Female"),
]


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    def clean(self, data, initial=None):
        single = super().clean
        items = list(data) if isinstance(data, (list, tuple)) else ([data] if data else [])
        cleaned = [single(item, initial) for item in items]
        cfg = platform_config()
        max_bytes = cfg.maximum_upload_mb * 1024 * 1024
        for upload in cleaned:
            if upload.size > max_bytes:
                raise forms.ValidationError(f"{upload.name} exceeds the {cfg.maximum_upload_mb} MB upload limit.")
            from pathlib import Path
            if Path(upload.name).suffix.lower() not in ALLOWED_EXTENSIONS:
                raise forms.ValidationError(f"Unsupported file type: {upload.name}")
        return cleaned


class StyledFormMixin:
    def apply_bootstrap(self):
        for field in self.fields.values():
            existing = field.widget.attrs.get("class", "")
            classes = [x for x in existing.split() if x not in {
                "form-control", "form-select", "form-check-input", "vTextField",
                "input", "select", "textarea", "input-bordered", "select-bordered", "textarea-bordered",
            }]
            if isinstance(field.widget, forms.Select):
                classes.extend(["select", "select-bordered", "w-full"])
            elif isinstance(field.widget, forms.CheckboxInput):
                classes.extend(["checkbox", "checkbox-sm"])
            elif isinstance(field.widget, forms.Textarea):
                classes.extend(["textarea", "textarea-bordered", "w-full"])
            elif not isinstance(field.widget, forms.FileInput):
                classes.extend(["input", "input-bordered", "w-full"])
            field.widget.attrs["class"] = " ".join(dict.fromkeys(classes))


class EndorsementCreateForm(StyledFormMixin, forms.ModelForm):
    full_name = forms.CharField(required=False)
    member_no = forms.CharField(required=False, help_text="Required for deletion; optional for addition")
    employee_no = forms.CharField(required=False)
    national_id = forms.CharField(required=False)
    relationship = forms.ChoiceField(required=False, choices=RELATIONSHIP_CHOICES, initial="Employee")
    date_of_birth = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    gender = forms.ChoiceField(required=False, choices=GENDER_CHOICES)
    plan = forms.ModelChoiceField(required=False, queryset=PolicyPlan.objects.none(), empty_label="Select plan")
    annual_salary = forms.DecimalField(required=False, max_digits=14, decimal_places=3)
    sum_assured = forms.DecimalField(required=False, max_digits=14, decimal_places=3, disabled=True)
    attachments = MultiFileField(required=False, widget=MultiFileInput(attrs={
        "accept": ".xlsx,.xls,.csv,.pdf,.png,.jpg,.jpeg,.webp",
        "class": "portal-file-input",
        "data-drop-input": "true",
        "multiple": True,
    }))

    class Meta:
        model = EndorsementRequest
        fields = ("policy", "endorsement_type", "effective_date")
        widgets = {"effective_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, policies=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["policy"].queryset = policies if policies is not None else Policy.objects.none()
        self.fields["policy"].empty_label = "Select policy"
        self.fields["policy"].widget.attrs.update({
            "hx-get": "/policy-plans/", "hx-target": "#id_plan", "hx-trigger": "change",
        })
        policy_id = self.data.get("policy") or self.initial.get("policy")
        if policy_id:
            self.fields["plan"].queryset = PolicyPlan.objects.filter(policy_id=policy_id, is_active=True).order_by("code")
        self.fields["plan"].widget.attrs.update({
            "data-plan-sum-assured": "true",
            "data-plan-sum-url": "/policy-plans/sum-assured/",
        })
        self.fields["sum_assured"].widget.attrs.update({
            "readonly": True,
            "data-plan-sum-target": "true",
            "placeholder": "Derived from selected plan",
        })
        self.fields["effective_date"].initial = timezone.localdate()
        self.apply_bootstrap()

    def manual_item_payload(self):
        return {
            "member_no": self.cleaned_data.get("member_no", ""),
            "employee_no": self.cleaned_data.get("employee_no", ""),
            "national_id": self.cleaned_data.get("national_id", ""),
            "full_name": self.cleaned_data.get("full_name", ""),
            "relationship": self.cleaned_data.get("relationship", ""),
            "date_of_birth": self.cleaned_data.get("date_of_birth"),
            "gender": self.cleaned_data.get("gender", ""),
            "plan": self.cleaned_data.get("plan"),
            "annual_salary": self.cleaned_data.get("annual_salary"),
            "sum_assured": self.cleaned_data.get("plan").sum_assured if self.cleaned_data.get("plan") and self.cleaned_data.get("plan").sum_assured is not None else None,
            "effective_date": self.cleaned_data.get("effective_date"),
            "extracted_data": {"source": "manual_entry"},
        }

    def has_manual_item(self):
        p = self.manual_item_payload()
        return any([p["member_no"], p["full_name"], p["employee_no"], p["national_id"]])


class BulkRecoveryForm(forms.Form):
    attachments = MultiFileField(required=True, widget=MultiFileInput(attrs={
        "accept": ".xlsx,.xls,.csv,.pdf,.png,.jpg,.jpeg,.webp",
        "class": "portal-file-input", "data-drop-input": "true", "multiple": True,
    }))



class SupplementalUploadForm(forms.Form):
    attachments = MultiFileField(required=True, widget=MultiFileInput(attrs={
        "accept": ".xlsx,.xls,.csv,.pdf,.png,.jpg,.jpeg,.webp",
        "class": "portal-file-input", "data-drop-input": "true", "multiple": True,
    }))


class ItemOCRFillForm(forms.Form):
    ocr_files = MultiFileField(
        required=True,
        widget=MultiFileInput(attrs={
            "accept": ".pdf,.png,.jpg,.jpeg,.webp",
            "class": "portal-file-input",
            "data-drop-input": "true",
            "multiple": True,
        }),
    )

    def clean_ocr_files(self):
        uploads = self.cleaned_data["ocr_files"]
        for upload in uploads:
            from pathlib import Path
            ext = Path(upload.name).suffix.lower()
            if ext not in OCR_EXTENSIONS:
                raise forms.ValidationError("Use PDF, PNG, JPG, JPEG or WEBP only.")
        return uploads


class EndorsementItemCorrectionForm(StyledFormMixin, forms.ModelForm):
    relationship = forms.ChoiceField(required=False, choices=RELATIONSHIP_CHOICES)
    gender = forms.ChoiceField(required=False, choices=GENDER_CHOICES)
    sum_assured = forms.DecimalField(required=False, max_digits=14, decimal_places=3, disabled=True)

    class Meta:
        model = EndorsementItem
        fields = (
            "member_no", "employee_no", "national_id", "full_name", "relationship",
            "date_of_birth", "gender", "plan", "sum_assured", "effective_date",
        )
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "effective_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.request_id:
            policy = self.instance.request.policy
            self.fields["plan"].queryset = PolicyPlan.objects.filter(policy=policy, is_active=True).order_by("code")
            self.fields["plan"].empty_label = "Select policy plan"
            self.fields["plan"].widget.attrs.update({
                "data-plan-sum-assured": "true",
                "data-plan-sum-url": "/policy-plans/sum-assured/",
            })

            plan_value = self.data.get("plan") if self.is_bound else self.initial.get("plan") or self.instance.plan_id
            try:
                selected_plan = self.fields["plan"].queryset.filter(pk=getattr(plan_value, "pk", plan_value)).first() if plan_value else None
            except (TypeError, ValueError):
                selected_plan = None
            self.initial["sum_assured"] = selected_plan.sum_assured if selected_plan else None

        self.fields["sum_assured"].widget.attrs.update({
            "readonly": True,
            "data-plan-sum-target": "true",
            "placeholder": "Derived from selected plan",
        })
        self.apply_bootstrap()

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.sum_assured = instance.plan.sum_assured if instance.plan_id and instance.plan else None
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class TPAItemProcessingForm(StyledFormMixin, forms.Form):
    card_number = forms.CharField(required=True, max_length=100)
    amount = forms.DecimalField(required=True, max_digits=14, decimal_places=3)

    def __init__(self, *args, item=None, **kwargs):
        super().__init__(*args, **kwargs)
        if item and not self.is_bound:
            self.initial["card_number"] = item.card_number
            self.initial["amount"] = item.tpa_premium_amount if item.tpa_premium_amount is not None else item.premium_impact
        self.apply_bootstrap()


class ApprovalDecisionForm(StyledFormMixin, forms.Form):
    decision = forms.ChoiceField(choices=[("approve", "Approve"), ("reject", "Reject")])
    comment = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_bootstrap()


class QueryForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = EndorsementQuery
        fields = ("subject", "message")
        widgets = {"message": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subject"].widget.attrs["placeholder"] = "Example: Missing Civil ID"
        self.fields["message"].widget.attrs["placeholder"] = "Explain what is missing or incorrect and exactly what the requester should provide."
        self.apply_bootstrap()


class QueryResponseForm(StyledFormMixin, forms.Form):
    response = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_bootstrap()
