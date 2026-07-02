from django.urls import path

from . import views


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("import/", views.import_data, name="import_data"),
    path("download/template/", views.download_template, name="download_template"),
    path("download/sample/", views.download_sample, name="download_sample"),
    path("generate/", views.generate_shift, name="generate_shift"),
    path("periods/<int:pk>/", views.shift_detail, name="shift_detail"),
    path("periods/<int:pk>/save/", views.save_shift, name="save_shift"),
    path("periods/<int:pk>/excel/", views.download_shift_excel, name="download_shift_excel"),
    path("periods/<int:pk>/csv/", views.download_shift_csv, name="download_shift_csv"),
]
