# Admin configuration guide

The goal is to let operations teams configure routine behavior without code changes.

## Recommended setup order

1. Create Organizations.
2. Create Users in Django Auth and matching UserProfiles.
3. Create SLA Profiles.
4. Create the Policy.
5. Create Policy Plans where applicable.
6. Create Policy Access grants for client/broker/agent/channel organizations.
7. Load Policy Members or connect the policy census/core integration.
8. Create TPA or insurer-core Integration Endpoints.
9. Configure one active AI Provider.
10. Review Platform Configuration and enable STP.

## Rating parameter examples

Flat annual:
    {"prorata_mode": "fixed_basis", "default_annual_rate": "100.000"}

Per mille sum assured:
    {"rate_per_mille": "1.25", "prorata_mode": "fixed_basis"}

Percent of salary:
    {"salary_percent": "0.35", "prorata_mode": "policy_days"}

Plan relationship rates:
    {"Spouse": "140.000", "Child": "98.000"}

## Mandatory field examples

Medical addition:
    ["full_name", "date_of_birth", "gender", "relationship", "plan"]

Medical deletion:
    ["member_no", "effective_date"]

Life addition:
    ["full_name", "date_of_birth", "gender", "sum_assured"]

## Operational exception philosophy

Do not turn every case into a manual insurer approval. Keep insurer users as an exception/control layer.

Clean case:
requester -> validation -> pricing -> STP -> TPA/core

Exception:
requester -> validation -> needs info or review -> corrected/approved -> re-enter automated path

This preserves zero-touch economics while giving the insurer visibility and override capability.
