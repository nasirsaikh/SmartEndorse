from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("policy-plans/", views.policy_plans, name="policy_plans"),
    path("preferences/", views.set_preferences, name="set_preferences"),
    path("notifications/", views.notifications_panel, name="notifications_panel"),
    path("notifications/read/", views.notifications_read, name="notifications_read"),
    path("endorsements/", views.request_list, name="endorsement_list"),
    path("endorsements/recovery/", views.bulk_recovery, name="endorsement_bulk_recovery"),
    path("endorsements/new/", views.create_request, name="endorsement_create"),
    path("endorsements/<int:pk>/", views.request_detail, name="endorsement_detail"),
    path("endorsements/<int:pk>/supplement/", views.supplemental_upload, name="endorsement_supplemental_upload"),
    path("endorsements/<int:pk>/items/<int:item_id>/edit/", views.edit_item, name="endorsement_item_edit"),
    path("endorsements/<int:pk>/items/<int:item_id>/tpa/", views.tpa_update_item, name="endorsement_tpa_item"),
    path("endorsements/<int:pk>/approvals/<int:approval_id>/", views.decide_approval, name="endorsement_approval"),
    path("endorsements/<int:pk>/start/", views.start_processing, name="endorsement_start"),
    path("endorsements/<int:pk>/complete/", views.complete_request, name="endorsement_complete"),
    path("endorsements/<int:pk>/query/", views.raise_query, name="endorsement_query"),
    path("endorsements/<int:pk>/query/<int:query_id>/answer/", views.answer_query, name="endorsement_query_answer"),
]
