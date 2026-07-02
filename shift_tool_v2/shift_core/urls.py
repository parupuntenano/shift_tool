from django.urls import path

from . import views


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("import/", views.import_data, name="import_data"),
    path("staff/", views.staff_manage, name="staff_manage"),
    path("staff/<int:pk>/", views.staff_manage, name="staff_edit"),
    path("staff/<int:pk>/delete/", views.staff_delete, name="staff_delete"),
    path("staff/delete-all/", views.staff_delete_all, name="staff_delete_all"),
    path("works/", views.work_manage, name="work_manage"),
    path("works/<int:pk>/", views.work_manage, name="work_edit"),
    path("works/<int:pk>/delete/", views.work_delete, name="work_delete"),
    path("works/<int:pk>/destroy/", views.work_destroy, name="work_destroy"),
    path("skills/", views.skill_map, name="skill_map"),
    path("download/template/", views.download_template, name="download_template"),
    path("download/sample/", views.download_sample, name="download_sample"),
    path("generate/", views.generate_shift, name="generate_shift"),
    path("periods/<int:pk>/", views.shift_detail, name="shift_detail"),
    path("periods/<int:pk>/save/", views.save_shift, name="save_shift"),
    path("periods/<int:pk>/excel/", views.download_shift_excel, name="download_shift_excel"),
    path("periods/<int:pk>/csv/", views.download_shift_csv, name="download_shift_csv"),
]
