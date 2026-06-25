from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path
from . import views

urlpatterns = [
    path(
        "login/",
        LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", views.home, name="home"),
    path("manager/dashboard/", views.manager_dashboard, name="manager_dashboard"),
    path("manager/staff/", views.staff_manage, name="staff_manage"),
    path("manager/staff/<int:pk>/edit/", views.staff_edit, name="staff_edit"),
    path("manager/staff/<int:pk>/delete/", views.staff_delete, name="staff_delete"),
    path("manager/works/", views.work_manage, name="work_manage"),
    path("manager/works/<int:pk>/edit/", views.work_edit, name="work_edit"),
    path("manager/works/<int:pk>/delete/", views.work_delete, name="work_delete"),
    path("manager/skills/", views.skill_manage, name="skill_manage"),
    path("manager/skills/<int:pk>/edit/", views.skill_edit, name="skill_edit"),
    path("manager/skills/<int:pk>/delete/", views.skill_delete, name="skill_delete"),
    path("manager/constraints/", views.constraint_manage, name="constraint_manage"),
    path(
        "manager/constraint-types/",
        views.constraint_type_manage,
        name="constraint_type_manage",
    ),
    path(
        "manager/constraint-types/<int:pk>/edit/",
        views.constraint_type_edit,
        name="constraint_type_edit",
    ),
    path(
        "manager/constraint-types/<int:pk>/delete/",
        views.constraint_type_delete,
        name="constraint_type_delete",
    ),
    path(
        "manager/constraints/<int:pk>/edit/",
        views.constraint_edit,
        name="constraint_edit",
    ),
    path(
        "manager/constraints/<int:pk>/delete/",
        views.constraint_delete,
        name="constraint_delete",
    ),
    path("manager/skill-map/", views.skill_map, name="skill_map"),
    path(
        "manager/skill-map/<int:pk>/delete/",
        views.staff_skill_delete,
        name="staff_skill_delete",
    ),
    path(
        "manager/skill-map/bulk-delete/",
        views.staff_skill_bulk_delete,
        name="staff_skill_bulk_delete",
    ),
    path(
        "manager/import/template/",
        views.download_import_template,
        name="download_import_template",
    ),
    path(
        "manager/import/sample/",
        views.download_import_sample,
        name="download_import_sample",
    ),
    path("manager/import/", views.import_skill_map, name="import_skill_map"),
    path(
        "manager/previous-shifts/",
        views.previous_shift_list,
        name="previous_shift_list",
    ),
    path("manager/shifts/", views.shift_manage, name="shift_manage"),
    path("manager/shifts/generate/", views.generate_shift, name="generate_shift"),
    path("manager/shifts/<int:pk>/", views.shift_detail, name="shift_detail"),
    path(
        "manager/shifts/<int:pk>/download.xlsx/",
        views.download_shift_excel,
        name="download_shift_excel",
    ),
    path(
        "manager/shifts/<int:pk>/download.csv/",
        views.download_shift_csv,
        name="download_shift_csv",
    ),
    path(
        "manager/shifts/<int:pk>/edit/",
        views.update_shift_draft,
        name="update_shift_draft",
    ),
    path(
        "manager/shift-leave-requests/<int:pk>/resolve/",
        views.resolve_leave_request,
        name="resolve_leave_request",
    ),
    path("manager/shifts/<int:pk>/publish/", views.publish_shift, name="publish_shift"),
    path("manager/shifts/<int:pk>/delete/", views.shift_delete, name="shift_delete"),
    path("staff/submit/", views.submit_availability, name="submit_availability"),
    path("staff/shifts/", views.my_shift, name="my_shift"),
    path(
        "staff/shifts/leave-request/",
        views.request_shift_leave,
        name="request_shift_leave",
    ),
    path("staff/password/", views.staff_change_password, name="staff_change_password"),
]
