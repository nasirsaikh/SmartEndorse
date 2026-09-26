from django.db import migrations, models
import django.db.models.deletion


def seed_ai_extraction_profiles(apps, schema_editor):
    Profile = apps.get_model("core", "AIExtractionProfile")
    Example = apps.get_model("core", "AITrainingExample")

    document, _ = Profile.objects.update_or_create(
        name="Default Insurance Document OCR",
        defaults={
            "task": "DOCUMENT_EXTRACTION",
            "product": "",
            "system_prompt": (
                "You are an insurance document extraction engine. Read only what is present in the supplied "
                "document or image. Never guess, infer, calculate, or fabricate missing member data. "
                "Return strict JSON only."
            ),
            "instructions": (
                "Extract group medical or group life endorsement member data. "
                "Treat each distinct insured/member/dependent as one output row. "
                "Keep identifiers exactly as printed except surrounding whitespace. "
                "Normalize dates to YYYY-MM-DD only when the date is legible and unambiguous. "
                "Normalize gender to Male or Female when clearly stated. "
                "Normalize relationship to Employee, Spouse, or Child only when the document supports it. "
                "For plan_code, copy the visible plan/category/class code or name; do not substitute a person name. "
                "For sum_assured, copy only an explicitly printed coverage/sum-assured value. "
                "Unknown or unreadable values must be null. "
                "Ignore logos, addresses, page numbers, signatures, stamps, and unrelated policy wording."
            ),
            "field_aliases": {
                "member_no": ["Member No", "Member ID", "Membership No", "Card No"],
                "employee_no": ["Employee No", "Employee ID", "Emp No", "Staff No"],
                "national_id": ["Civil ID", "National ID", "Resident ID", "ID No"],
                "full_name": ["Member Name", "Insured Name", "Employee Name", "Name"],
                "relationship": ["Relationship", "Relation", "Dependent Type"],
                "date_of_birth": ["DOB", "Date of Birth", "Birth Date"],
                "gender": ["Gender", "Sex"],
                "plan_code": ["Plan", "Plan Code", "Category", "Class", "Benefit Class"],
                "sum_assured": ["Sum Assured", "Sum Insured", "Coverage Amount"],
                "effective_date": ["Effective Date", "Addition Date", "Deletion Date", "Endorsement Date"],
            },
            "priority": 100,
            "is_active": True,
        },
    )
    Example.objects.update_or_create(
        profile=document,
        name="Medical dependent example",
        defaults={
            "input_text": (
                "Employee No: E1101-1\nCivil ID: 12345678\nMember Name: Sara Al Hinai\n"
                "Relationship: Daughter\nDOB: 22/09/2018\nSex: F\nPlan: GOLD\nEffective Date: 15/09/2026"
            ),
            "expected_output": [{
                "member_no": None,
                "employee_no": "E1101-1",
                "national_id": "12345678",
                "full_name": "Sara Al Hinai",
                "relationship": "Child",
                "date_of_birth": "2018-09-22",
                "gender": "Female",
                "plan_code": "GOLD",
                "annual_salary": None,
                "sum_assured": None,
                "effective_date": "2026-09-15",
                "_source_raw": {},
            }],
            "sort_order": 10,
            "is_active": True,
        },
    )
    Example.objects.update_or_create(
        profile=document,
        name="Life coverage example",
        defaults={
            "input_text": (
                "Employee ID: E2001\nInsured Name: Ahmed Said\nDOB 01-Jan-1985\n"
                "Gender: Male\nBenefit Class: LIFE50\nSum Assured: 50,000 OMR"
            ),
            "expected_output": [{
                "member_no": None,
                "employee_no": "E2001",
                "national_id": None,
                "full_name": "Ahmed Said",
                "relationship": "Employee",
                "date_of_birth": "1985-01-01",
                "gender": "Male",
                "plan_code": "LIFE50",
                "annual_salary": None,
                "sum_assured": "50000",
                "effective_date": None,
                "_source_raw": {},
            }],
            "sort_order": 20,
            "is_active": True,
        },
    )

    structured, _ = Profile.objects.update_or_create(
        name="Default Structured Header Mapping",
        defaults={
            "task": "STRUCTURED_MAPPING",
            "product": "",
            "system_prompt": "You normalize insurance spreadsheet rows. Return strict JSON only and never invent values.",
            "instructions": (
                "Map source spreadsheet/CSV headers and values to the canonical insurance member fields. "
                "Preserve the original input object in _source_raw. "
                "Use source values exactly when present, normalize dates where unambiguous, and leave unknown values null."
            ),
            "field_aliases": {
                "national_id": ["Civil Number", "Civil ID", "Resident ID"],
                "employee_no": ["Staff ID", "Employee Number", "Emp ID"],
                "full_name": ["Employee Name", "Insured", "Member"],
                "plan_code": ["Benefit Plan", "Medical Class", "Category"],
            },
            "priority": 100,
            "is_active": True,
        },
    )
    Example.objects.update_or_create(
        profile=structured,
        name="Alternate spreadsheet headings",
        defaults={
            "input_text": '{"Staff ID":"E3001","Civil Number":"99887766","Employee Name":"Maya Ali","Medical Class":"PLATINUM"}',
            "expected_output": [{
                "employee_no": "E3001",
                "national_id": "99887766",
                "full_name": "Maya Ali",
                "plan_code": "PLATINUM",
                "_source_raw": {
                    "Staff ID": "E3001",
                    "Civil Number": "99887766",
                    "Employee Name": "Maya Ali",
                    "Medical Class": "PLATINUM",
                },
            }],
            "sort_order": 10,
            "is_active": True,
        },
    )


def reverse_seed(apps, schema_editor):
    Profile = apps.get_model("core", "AIExtractionProfile")
    Profile.objects.filter(name__in=[
        "Default Insurance Document OCR",
        "Default Structured Header Mapping",
    ]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_policyplan_sum_assured"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIExtractionProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120, unique=True)),
                ("task", models.CharField(choices=[("DOCUMENT_EXTRACTION", "Document OCR / extraction"), ("STRUCTURED_MAPPING", "Structured header mapping")], max_length=30)),
                ("product", models.CharField(blank=True, choices=[("GROUP_MEDICAL", "Group Medical"), ("GROUP_LIFE", "Group Life")], help_text="Leave blank for a global profile. Product-specific active profiles override the global profile.", max_length=30)),
                ("system_prompt", models.TextField(blank=True, help_text="Optional system instruction sent to the LLM before the extraction request.")),
                ("instructions", models.TextField(blank=True, help_text="Task-specific extraction instructions. Canonical field rules are appended automatically.")),
                ("field_aliases", models.JSONField(blank=True, default=dict, help_text='Optional field guidance, e.g. {"national_id": ["Civil ID", "Resident ID"], "plan_code": ["Plan", "Category"]}.')),
                ("priority", models.PositiveSmallIntegerField(default=100)),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={"ordering": ("-priority", "name")},
        ),
        migrations.CreateModel(
            name="AITrainingExample",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120)),
                ("input_text", models.TextField(help_text="Representative OCR text, document wording, or structured source row/header.")),
                ("expected_output", models.JSONField(default=list, help_text="Expected canonical JSON array. This is inserted as a few-shot example for the LLM.")),
                ("sort_order", models.PositiveSmallIntegerField(default=10)),
                ("is_active", models.BooleanField(default=True)),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="examples", to="core.aiextractionprofile")),
            ],
            options={"ordering": ("sort_order", "id")},
        ),
        migrations.RunPython(seed_ai_extraction_profiles, reverse_seed),
    ]
