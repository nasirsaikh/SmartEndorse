from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("policy-plans/", views.policy_plans, name="policy_plans"),
    path("endorsements/", views.request_list, name="endorsement_list"),
    path("endorsements/new/", views.create_request, name="endorsement_create"),
    path("endorsements/<int:pk>/", views.request_detail, name="endorsement_detail"),
    path("endorsements/<int:pk>/start/", views.start_processing, name="endorsement_start"),
    path("endorsements/<int:pk>/complete/", views.complete_request, name="endorsement_complete"),
    path("endorsements/<int:pk>/query/", views.raise_query, name="endorsement_query"),
    path(
        "endorsements/<int:pk>/query/<int:query_id>/answer/",
        views.answer_query,
        name="endorsement_query_answer",
    ),
]
