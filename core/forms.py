from django import forms
from django.utils import timezone
from .models import EndorsementQuery, EndorsementRequest, Policy, PolicyPlan


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(item, initial) for item in data]
        return [single(data, initial)] if data else []


class EndorsementCreateForm(forms.ModelForm):
    full_name = forms.CharField(required=False)
    member_no = forms.CharField(required=False, help_text="Required for deletion; optional for addition")
    employee_no = forms.CharField(required=False)
    national_id = forms.CharField(required=False)
    relationship = forms.CharField(required=False, initial="Employee")
    date_of_birth = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    gender = forms.ChoiceField(required=False, choices=[("", "---------"), ("Male", "Male"), ("Female", "Female")])
    plan = forms.ModelChoiceField(required=False, queryset=PolicyPlan.objects.none())
    annual_salary = forms.DecimalField(required=False, max_digits=14, decimal_places=3)
    sum_assured = forms.DecimalField(required=False, max_digits=14, decimal_places=3)
    attachments = MultiFileField(required=False, widget=MultiFileInput(attrs={"accept": ".xlsx,.xls,.csv,.pdf,.png,.jpg,.jpeg"}))

    class Meta:
        model = EndorsementRequest
        fields = ("policy", "endorsement_type", "effective_date")
        widgets = {"effective_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, policies=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["policy"].queryset = policies if policies is not None else Policy.objects.none()
        self.fields["policy"].widget.attrs.update({
            "hx-get": "/policy-plans/",
            "hx-target": "#id_plan",
            "hx-trigger": "change",
        })
        policy_id = self.data.get("policy") or self.initial.get("policy")
        if policy_id:
            self.fields["plan"].queryset = PolicyPlan.objects.filter(policy_id=policy_id, is_active=True)
        self.fields["effective_date"].initial = timezone.localdate()
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif not isinstance(field.widget, (forms.FileInput,)):
                field.widget.attrs["class"] = "form-control"

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
            "sum_assured": self.cleaned_data.get("sum_assured"),
            "effective_date": self.cleaned_data.get("effective_date"),
        }

    def has_manual_item(self):
        p = self.manual_item_payload()
        return any([p["member_no"], p["full_name"], p["employee_no"], p["national_id"]])


class QueryForm(forms.ModelForm):
    class Meta:
        model = EndorsementQuery
        fields = ("subject", "message")
        widgets = {"message": forms.Textarea(attrs={"rows": 3})}
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subject"].widget.attrs["class"] = "form-control"
        self.fields["message"].widget.attrs["class"] = "form-control"


class QueryResponseForm(forms.Form):
    response = forms.CharField(widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}))
