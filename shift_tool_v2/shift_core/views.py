from __future__ import annotations

import calendar
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import ShiftAssignment, ShiftPeriod, ShiftWarning, Staff, WorkType
from .services.exporters import build_shift_csv, build_shift_workbook, period_matrix
from .services.generator import generate_monthly_shift
from .services.importers import build_template_workbook, import_master_workbook, workbook_response


@login_required
def dashboard(request):
    return render(
        request,
        "shift_core/dashboard.html",
        {
            "staff_count": Staff.objects.filter(is_active=True).count(),
            "work_count": WorkType.objects.filter(active=True).count(),
            "periods": ShiftPeriod.objects.all()[:12],
        },
    )


@login_required
def import_data(request):
    if request.method == "POST":
        uploaded = request.FILES.get("file")
        if not uploaded:
            messages.error(request, "Excelファイルを選択してください。")
            return redirect("import_data")
        try:
            result = import_master_workbook(uploaded)
        except Exception as exc:
            messages.error(request, f"取込に失敗しました：{exc}")
            return redirect("import_data")
        messages.success(
            request,
            f"取込完了：スタッフ{result['staff']}人 / 業務{result['works']}件 / 前月実績{result['previous']}件",
        )
        return redirect("dashboard")
    return render(request, "shift_core/import.html")


@login_required
def download_template(request):
    content, filename = workbook_response(build_template_workbook(sample=False), "shift_tool_v2_template.xlsx")
    return _excel_response(content, filename)


@login_required
def download_sample(request):
    content, filename = workbook_response(build_template_workbook(sample=True), "shift_tool_v2_sample_30.xlsx")
    return _excel_response(content, filename)


@login_required
@require_POST
def generate_shift(request):
    month = _month_from_request(request.POST.get("month"))
    if not month:
        messages.error(request, "対象月を選択してください。")
        return redirect("dashboard")
    period = generate_monthly_shift(month)
    messages.success(request, f"{period.month:%Y年%m月}のシフトを作成しました。")
    return redirect("shift_detail", pk=period.pk)


@login_required
def shift_detail(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk)
    days, rows, works = period_matrix(period)
    return render(
        request,
        "shift_core/shift_detail.html",
        {
            "period": period,
            "days": days,
            "rows": rows,
            "works": works,
            "warnings": period.warnings.select_related("staff", "work_type"),
            "status_options": [
                ("public_holiday", "公休"),
                ("paid_leave", "有休"),
                ("standby", "余剰"),
            ],
        },
    )


@login_required
@require_POST
def save_shift(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk)
    work_ids = {str(work.id): work for work in WorkType.objects.filter(active=True)}
    changed = 0
    for assignment in period.assignments.select_related("work_type"):
        value = request.POST.get(f"assignment_{assignment.id}")
        if value is None:
            continue
        old = (assignment.status, assignment.work_type_id)
        if value in work_ids:
            assignment.status = ShiftAssignment.Status.WORK
            assignment.work_type = work_ids[value]
        elif value == "paid_leave":
            assignment.status = ShiftAssignment.Status.PAID_LEAVE
            assignment.work_type = None
        elif value == "standby":
            assignment.status = ShiftAssignment.Status.STANDBY
            assignment.work_type = None
        else:
            assignment.status = ShiftAssignment.Status.PUBLIC_HOLIDAY
            assignment.work_type = None
        if old != (assignment.status, assignment.work_type_id):
            assignment.source = ShiftAssignment.Source.MANUAL
            assignment.save(update_fields=["status", "work_type", "source"])
            changed += 1
    _recalculate_warnings(period)
    messages.success(request, f"シフトを保存しました。（変更{changed}件）")
    return redirect("shift_detail", pk=period.pk)


@login_required
def download_shift_excel(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk)
    return _excel_response(build_shift_workbook(period), f"shift_{period.month:%Y_%m}.xlsx")


@login_required
def download_shift_csv(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk)
    response = HttpResponse(build_shift_csv(period), content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="shift_{period.month:%Y_%m}.csv"'
    return response


def _recalculate_warnings(period):
    period.warnings.all().delete()
    warnings = []
    days = sorted({item.day for item in period.assignments.all()})
    works = WorkType.objects.filter(active=True)
    for day in days:
        for work in works:
            count = period.assignments.filter(
                day=day,
                status=ShiftAssignment.Status.WORK,
                work_type=work,
            ).exclude(
                staff__skills__work_type=work,
                staff__skills__level__trainee=True,
            ).count()
            if count < work.required_staff_per_day:
                warnings.append(
                    ShiftWarning(
                        period=period,
                        day=day,
                        work_type=work,
                        message=f"{day:%m/%d} {work.name}が必要人数{work.required_staff_per_day}人に対して{count}人です。",
                    )
                )
    day_count = calendar.monthrange(period.month.year, period.month.month)[1]
    for staff in Staff.objects.filter(is_active=True):
        public_holiday_count = period.assignments.filter(staff=staff, status=ShiftAssignment.Status.PUBLIC_HOLIDAY).count()
        total = period.assignments.filter(staff=staff).count()
        if public_holiday_count != staff.public_holiday_count:
            warnings.append(
                ShiftWarning(
                    period=period,
                    staff=staff,
                    message=f"{staff.name}の公休数が設定{staff.public_holiday_count}日に対して{public_holiday_count}日です。",
                )
            )
        if total != day_count:
            warnings.append(
                ShiftWarning(
                    period=period,
                    staff=staff,
                    message=f"{staff.name}の月合計が{total}日です。",
                )
            )
    ShiftWarning.objects.bulk_create(warnings)


def _excel_response(content: bytes, filename: str):
    response = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _month_from_request(value):
    try:
        return datetime.strptime(value or "", "%Y-%m").date().replace(day=1)
    except ValueError:
        return None

# Create your views here.
