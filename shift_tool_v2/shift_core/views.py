from __future__ import annotations

import calendar
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import (
    ShiftAssignment,
    ShiftPeriod,
    ShiftRequest,
    ShiftWarning,
    SkillLevel,
    Staff,
    StaffSkill,
    WorkType,
)
from .services.exporters import build_shift_csv, build_shift_workbook, period_matrix
from .services.generator import (
    MIN_PUBLIC_HOLIDAYS_PER_WEEK,
    build_request_warnings,
    build_required_staff_warnings,
    generate_monthly_shift,
    previous_weekly_public_holiday_counts,
    week_ranges_in_month,
    week_start,
)
from .services.importers import build_template_workbook, import_master_workbook, workbook_response


@login_required
def dashboard(request):
    return render(
        request,
        "shift_core/dashboard.html",
        {
            "staff_count": Staff.objects.filter(is_active=True).count(),
            "work_count": WorkType.objects.filter(active=True).count(),
            "request_count": ShiftRequest.objects.count(),
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
            (
                f"取込完了：スタッフ{result['staff']}人 / 業務{result['works']}件 / "
                f"前月実績{result['previous']}件 / シフト提出{result.get('requests', 0)}件"
            ),
        )
        return redirect("dashboard")
    return render(request, "shift_core/import.html")


@login_required
def staff_manage(request, pk=None):
    editing_item = get_object_or_404(Staff, pk=pk) if pk else None
    if request.method == "POST":
        target = editing_item or Staff()
        employee_number = request.POST.get("employee_number", "").strip()
        name = request.POST.get("name", "").strip()
        duplicate = Staff.objects.filter(employee_number=employee_number).exclude(
            pk=target.pk
        )
        if not employee_number or not name:
            messages.error(request, "社員番号と氏名を入力してください。")
            return redirect("staff_manage")
        if duplicate.exists():
            messages.error(request, "同じ社員番号のスタッフが既に登録されています。")
            return redirect("staff_manage")
        target.employee_number = employee_number
        target.name = name
        target.public_holiday_count = max(
            0,
            _int(request.POST.get("public_holiday_count"), 8),
        )
        target.is_active = request.POST.get("is_active") == "on"
        target.save()
        messages.success(request, "スタッフ情報を保存しました。")
        return redirect("staff_manage")

    query = request.GET.get("q", "").strip()
    items = Staff.objects.all().order_by("employee_number")
    if query:
        items = (
            items.filter(employee_number__icontains=query)
            | Staff.objects.filter(name__icontains=query)
        ).distinct().order_by("employee_number")
    return render(
        request,
        "shift_core/staff_manage.html",
        {
            "items": items,
            "editing_item": editing_item,
            "query": query,
        },
    )


@login_required
@require_POST
def staff_delete(request, pk):
    staff = get_object_or_404(Staff, pk=pk)
    staff_name = staff.name
    staff.delete()
    messages.success(request, f"{staff_name}を削除しました。")
    return redirect("staff_manage")


@login_required
def staff_delete_all(request):
    staff_count = Staff.objects.count()
    if request.method == "POST":
        ShiftPeriod.objects.all().delete()
        deleted_count, _details = Staff.objects.all().delete()
        messages.success(
            request,
            f"スタッフ情報を全削除しました。（関連データを含む削除件数：{deleted_count}件）",
        )
        return redirect("staff_manage")
    return render(
        request,
        "shift_core/staff_delete_all.html",
        {
            "staff_count": staff_count,
            "period_count": ShiftPeriod.objects.count(),
        },
    )


@login_required
def work_manage(request, pk=None):
    editing_item = get_object_or_404(WorkType, pk=pk) if pk else None
    if request.method == "POST":
        target = editing_item or WorkType()
        target.name = request.POST.get("name", "").strip()
        target.required_staff_per_day = max(1, _int(request.POST.get("required_staff_per_day"), 1))
        target.color = request.POST.get("color", "").strip()
        target.display_order = max(0, _int(request.POST.get("display_order"), 0))
        target.active = request.POST.get("active") == "on"
        if not target.name:
            messages.error(request, "業務名を入力してください。")
            return redirect("work_manage")
        target.save()
        messages.success(request, "業務を保存しました。")
        return redirect("work_manage")
    return render(
        request,
        "shift_core/work_manage.html",
        {
            "items": WorkType.objects.all(),
            "editing_item": editing_item,
        },
    )


@login_required
@require_POST
def work_delete(request, pk):
    work = get_object_or_404(WorkType, pk=pk)
    work.active = False
    work.save(update_fields=["active"])
    messages.success(request, f"{work.name}を無効にしました。")
    return redirect("work_manage")


@login_required
def work_destroy(request, pk):
    work = get_object_or_404(WorkType, pk=pk)
    if request.method == "POST":
        work_name = work.name
        work.delete()
        messages.success(request, f"{work_name}を削除しました。")
        return redirect("work_manage")
    return render(
        request,
        "shift_core/work_destroy.html",
        {
            "work": work,
            "skill_count": work.skills.count(),
            "assignment_count": ShiftAssignment.objects.filter(work_type=work).count(),
            "previous_count": work.previousshiftrecord_set.count(),
        },
    )


@login_required
def skill_map(request):
    if request.method == "POST":
        levels = {str(level.id): level for level in SkillLevel.objects.all()}
        changed = 0
        for staff in Staff.objects.filter(is_active=True):
            for work in WorkType.objects.filter(active=True):
                field_name = f"skill_{staff.id}_{work.id}"
                if field_name not in request.POST:
                    continue
                level_id = request.POST.get(field_name, "")
                current = StaffSkill.objects.filter(staff=staff, work_type=work).first()
                if not level_id:
                    if current:
                        current.delete()
                        changed += 1
                    continue
                level = levels.get(level_id)
                if not level:
                    continue
                if not current or current.level_id != level.id:
                    StaffSkill.objects.update_or_create(
                        staff=staff,
                        work_type=work,
                        defaults={"level": level},
                    )
                    changed += 1
        messages.success(request, f"スキルマップを保存しました。（変更{changed}件）")
        return redirect("skill_map")

    query = request.GET.get("q", "").strip()
    staff_qs = Staff.objects.filter(is_active=True).order_by("employee_number")
    if query:
        staff_qs = staff_qs.filter(employee_number__icontains=query) | Staff.objects.filter(
            is_active=True,
            name__icontains=query,
        )
        staff_qs = staff_qs.distinct().order_by("employee_number")
    works = list(WorkType.objects.filter(active=True).order_by("display_order", "name"))
    skill_map_data = {
        (skill.staff_id, skill.work_type_id): skill.level
        for skill in StaffSkill.objects.select_related("level").filter(
            staff__in=staff_qs,
            work_type__in=works,
        )
    }
    staff_rows = []
    for staff in staff_qs:
        cells = []
        for work in works:
            cells.append(
                {
                    "work": work,
                    "field_name": f"skill_{staff.id}_{work.id}",
                    "level": skill_map_data.get((staff.id, work.id)),
                }
            )
        staff_rows.append({"staff": staff, "cells": cells})
    return render(
        request,
        "shift_core/skill_map.html",
        {
            "works": works,
            "skill_levels": SkillLevel.objects.all(),
            "staff_rows": staff_rows,
            "query": query,
        },
    )


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
    days, rows, works, previous_days, daily_work_stats = period_matrix(period)
    return render(
        request,
        "shift_core/shift_detail.html",
        {
            "period": period,
            "days": days,
            "previous_days": previous_days,
            "rows": rows,
            "works": works,
            "daily_work_stats": daily_work_stats,
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
    warnings = build_required_staff_warnings(period)
    warnings.extend(build_request_warnings(period))
    staff_list = list(Staff.objects.filter(is_active=True))
    weekly_public_holiday_counts = previous_weekly_public_holiday_counts(staff_list, period.month)
    for item in period.assignments.filter(status=ShiftAssignment.Status.PUBLIC_HOLIDAY).values("staff_id", "day"):
        weekly_public_holiday_counts[(week_start(item["day"]), item["staff_id"])] += 1

    day_count = calendar.monthrange(period.month.year, period.month.month)[1]
    for staff in staff_list:
        public_holiday_count = period.assignments.filter(staff=staff, status=ShiftAssignment.Status.PUBLIC_HOLIDAY).count()
        total = period.assignments.filter(staff=staff).count()
        for current_week_start, week_end in week_ranges_in_month(period.month):
            week_public_holidays = weekly_public_holiday_counts[(current_week_start, staff.id)]
            if week_public_holidays < MIN_PUBLIC_HOLIDAYS_PER_WEEK:
                warnings.append(
                    ShiftWarning(
                        period=period,
                        staff=staff,
                        message=(
                            f"{staff.name}の週公休が{week_public_holidays}日です。"
                            f"（{current_week_start:%m/%d}〜{week_end:%m/%d}、最低{MIN_PUBLIC_HOLIDAYS_PER_WEEK}日）"
                        ),
                    )
                )
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


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
