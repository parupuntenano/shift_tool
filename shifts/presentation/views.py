import calendar
import csv
import json
import re
from collections import defaultdict
from io import BytesIO, StringIO
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import HttpResponse
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from shifts.application.use_cases import GenerateMonthlyShift, ImportSkillMap
from shifts.infrastructure.importers import SkillMapFileReader, SkillMapReadError
from shifts.infrastructure.master_repository import DjangoMasterRepository
from shifts.infrastructure.models import (
    AvailabilityDay,
    AvailabilitySubmission,
    CompanyMembership,
    ConstraintType,
    GenerationWarning,
    ImportJob,
    IndividualConstraint,
    PreviousMonthShiftDay,
    ShiftAssignment,
    ShiftLeaveRequest,
    ShiftPeriod,
    SkillLevel,
    Staff,
    StaffSkill,
    WorkType,
)
from shifts.infrastructure.repositories import DjangoShiftRepository
from .company import admin_required, current_membership, staff_required
from .forms import (
    BulkDesiredOffLimitForm,
    ConstraintForm,
    ConstraintTypeForm,
    GenerateForm,
    ImportForm,
    PreviousShiftImportForm,
    SkillLevelForm,
    StaffForm,
    WorkTypeForm,
)


def _month(value=None) -> date:
    if value:
        try:
            return date.fromisoformat(f"{value[:7]}-01")
        except ValueError:
            pass
    today = timezone.localdate()
    return today.replace(day=1)


def _add_months(month: date, offset: int) -> date:
    year = month.year + (month.month - 1 + offset) // 12
    month_number = (month.month - 1 + offset) % 12 + 1
    return month.replace(year=year, month=month_number, day=1)


def _next_month() -> date:
    return _add_months(timezone.localdate().replace(day=1), 1)


def _koyomi_api_url(month: date) -> str:
    # 祝日名は暦APIから取得する。
    # 取得した祝日名は _calendar_days() の holiday に入り、
    # submit.html / my_shift.html / shift_detail.html で表示される。
    day_count = calendar.monthrange(month.year, month.month)[1]
    params = {
        "mode": "d",
        "cnt": day_count,
        "targetyyyy": month.year,
        "targetmm": f"{month.month:02d}",
        "targetdd": "01",
    }
    return "https://koyomi.zingsystem.com/api/?" + urlencode(params)


def _holidays_for_month(month: date) -> dict[date, str]:
    # API通信に失敗してもシフト画面は表示したいので、失敗時は祝日なしで続行する。
    try:
        with urlopen(_koyomi_api_url(month), timeout=5) as response:
            data = json.load(response)
    except Exception:
        return {}

    holidays = {}
    for day_text, info in data.get("datelist", {}).items():
        holiday_name = info.get("holiday", "")
        if holiday_name:
            holidays[date.fromisoformat(day_text)] = holiday_name
    return holidays


def _calendar_days(month: date) -> list[dict]:
    # 3つの画面で共通利用する「日付表示用データ」をここで作る。
    #
    # - submit.html: スタッフのシフト希望提出カード
    # - my_shift.html: スタッフのシフト確認カード
    # - shift_detail.html: 管理者の月間シフト表ヘッダー/セル
    #
    # is_non_workday / is_saturday はテンプレートで class 名になり、
    # static/shifts/app.css の .non-workday / .saturday に干渉する。
    day_count = calendar.monthrange(month.year, month.month)[1]
    holidays = _holidays_for_month(month)
    days = []
    for number in range(1, day_count + 1):
        current = month.replace(day=number)
        holiday_name = holidays.get(current, "")
        days.append(
            {
                "number": number,
                "date": current,
                "holiday": holiday_name,
                "is_holiday": bool(holiday_name),
                "is_saturday": current.weekday() == 5,
                "is_weekend": current.weekday() >= 5,
                "is_non_workday": current.weekday() >= 5 or bool(holiday_name),
            }
        )
    return days


def _delete_confirmation(request, obj, label, cancel_url, success_url, on_deleted=None):
    if request.method == "POST":
        try:
            obj.delete()
        except ProtectedError:
            messages.error(
                request,
                f"{label}はシフトやスキル設定で使用中のため削除できません。先に関連データを削除するか、無効にしてください。",
            )
        else:
            if on_deleted:
                on_deleted()
            messages.success(request, f"{label}を削除しました。")
        return redirect(success_url)
    return render(
        request,
        "shifts/confirm_delete.html",
        {"target": obj, "label": label, "cancel_url": cancel_url},
    )


@login_required
def home(request):
    membership = current_membership(request)
    if not membership:
        return render(request, "shifts/no_company.html")
    return redirect(
        "manager_dashboard"
        if membership.role == CompanyMembership.Role.ADMIN
        else "submit_availability"
    )


@login_required
@admin_required
def manager_dashboard(request):
    # ダッシュボードは ?month=YYYY-MM で表示月を切り替える。
    # templates/shifts/manager_dashboard.html の「翌月のダッシュボードへ」ボタンが
    # next_month を使ってこのviewに戻ってくる。
    month = _month(request.GET.get("month"))
    current_month = timezone.localdate().replace(day=1)
    prev_month = _add_months(month, -1)
    next_month = _add_months(month, 1)
    active_staff = Staff.objects.filter(company=request.company, active=True)
    submitted = AvailabilitySubmission.objects.filter(
        staff__company=request.company,
        month=month,
        status=AvailabilitySubmission.Status.SUBMITTED,
    ).count()
    staff_count = active_staff.count()
    rate = round(submitted / staff_count * 100) if staff_count else 0
    latest_period = ShiftPeriod.objects.filter(
        company=request.company, month=month
    ).first()
    pending_leave_requests = ShiftLeaveRequest.objects.filter(
        period__company=request.company,
        status=ShiftLeaveRequest.Status.PENDING,
    ).select_related("period", "staff", "work_type")
    context = {
        "month": month,
        "current_month": current_month,
        "prev_month": prev_month,
        "next_month": next_month,
        "staff_count": staff_count,
        "submitted_count": submitted,
        "submission_rate": rate,
        "missing_count": max(staff_count - submitted, 0),
        "warning_count": latest_period.warning_count if latest_period else 0,
        "shift_status": (
            latest_period.get_status_display() if latest_period else "未生成"
        ),
        "latest_period": latest_period,
        "pending_leave_requests": pending_leave_requests[:8],
        "pending_leave_request_count": pending_leave_requests.count(),
        "recent_periods": ShiftPeriod.objects.filter(company=request.company).annotate(
            assignment_count=Count("assignments")
        )[:5],
    }
    return render(request, "shifts/manager_dashboard.html", context)


@login_required
@admin_required
def staff_manage(request):
    form = StaffForm()
    bulk_limit_form = BulkDesiredOffLimitForm(
        initial={"desired_off_limit": request.company.default_desired_off_limit}
    )
    if request.method == "POST" and request.POST.get("action") == "bulk_limit":
        bulk_limit_form = BulkDesiredOffLimitForm(request.POST)
        if bulk_limit_form.is_valid():
            limit = bulk_limit_form.cleaned_data["desired_off_limit"]
            request.company.default_desired_off_limit = limit
            request.company.save(update_fields=["default_desired_off_limit"])
            count = Staff.objects.filter(company=request.company).update(
                desired_off_limit=limit
            )
            messages.success(
                request,
                f"公有給希望上限を{limit}日に変更しました。（{count}名へ反映）",
            )
            return redirect("staff_manage")
    elif request.method == "POST":
        form = StaffForm(request.POST)
        if form.is_valid():
            staff = form.save_for_company(request.company)
            if staff.user:
                CompanyMembership.objects.update_or_create(
                    company=request.company,
                    user=staff.user,
                    defaults={"role": CompanyMembership.Role.STAFF},
                )
            messages.success(request, "スタッフを登録しました。")
            return redirect("staff_manage")
    return render(
        request,
        "shifts/staff_manage.html",
        {
            "form": form,
            "bulk_limit_form": bulk_limit_form,
            "items": Staff.objects.filter(company=request.company).select_related(
                "user"
            ),
        },
    )


@login_required
@admin_required
def staff_edit(request, pk):
    item = get_object_or_404(Staff, pk=pk, company=request.company)
    form = StaffForm(request.POST or None, instance=item)
    bulk_limit_form = BulkDesiredOffLimitForm(
        initial={"desired_off_limit": request.company.default_desired_off_limit}
    )
    if request.method == "POST" and form.is_valid():
        staff = form.save_for_company(request.company)
        if staff.user:
            CompanyMembership.objects.update_or_create(
                company=request.company,
                user=staff.user,
                defaults={"role": CompanyMembership.Role.STAFF},
            )
        messages.success(request, "スタッフ情報を更新しました。")
        return redirect("staff_manage")
    return render(
        request,
        "shifts/staff_manage.html",
        {
            "form": form,
            "bulk_limit_form": bulk_limit_form,
            "items": Staff.objects.filter(company=request.company).select_related(
                "user"
            ),
            "editing_item": item,
        },
    )


@login_required
@admin_required
def staff_delete(request, pk):
    item = get_object_or_404(Staff, pk=pk, company=request.company)
    user_id = item.user_id

    def delete_staff_membership():
        CompanyMembership.objects.filter(
            company=request.company,
            user_id=user_id,
            role=CompanyMembership.Role.STAFF,
        ).delete()

    return _delete_confirmation(
        request,
        item,
        f"スタッフ「{item.name}」",
        reverse("staff_manage"),
        "staff_manage",
        delete_staff_membership if user_id else None,
    )


@login_required
@admin_required
def work_manage(request):
    form = WorkTypeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company)
        messages.success(request, "業務を登録しました。")
        return redirect("work_manage")
    return render(
        request,
        "shifts/work_manage.html",
        {"form": form, "items": WorkType.objects.filter(company=request.company)},
    )


@login_required
@admin_required
def work_edit(request, pk):
    item = get_object_or_404(WorkType, pk=pk, company=request.company)
    form = WorkTypeForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company)
        messages.success(request, "業務を更新しました。")
        return redirect("work_manage")
    return render(
        request,
        "shifts/work_manage.html",
        {
            "form": form,
            "items": WorkType.objects.filter(company=request.company),
            "editing_item": item,
        },
    )


@login_required
@admin_required
def work_delete(request, pk):
    item = get_object_or_404(WorkType, pk=pk, company=request.company)
    return _delete_confirmation(
        request, item, f"業務「{item.name}」", reverse("work_manage"), "work_manage"
    )


@login_required
@admin_required
def skill_manage(request):
    form = SkillLevelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company)
        messages.success(request, "スキル区分を登録しました。")
        return redirect("skill_manage")
    return render(
        request,
        "shifts/skill_manage.html",
        {"form": form, "items": SkillLevel.objects.filter(company=request.company)},
    )


@login_required
@admin_required
def skill_edit(request, pk):
    item = get_object_or_404(SkillLevel, pk=pk, company=request.company)
    form = SkillLevelForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company)
        messages.success(request, "スキル区分を更新しました。")
        return redirect("skill_manage")
    return render(
        request,
        "shifts/skill_manage.html",
        {
            "form": form,
            "items": SkillLevel.objects.filter(company=request.company),
            "editing_item": item,
        },
    )


@login_required
@admin_required
def skill_delete(request, pk):
    item = get_object_or_404(SkillLevel, pk=pk, company=request.company)
    return _delete_confirmation(
        request,
        item,
        f"スキル区分「{item.symbol}」",
        reverse("skill_manage"),
        "skill_manage",
    )


def _constraint_items(company, query=""):
    # 個別制約の検索は、編集したいスタッフをすぐ見つけるために
    # 名前・社員番号だけを対象にする。
    items = IndividualConstraint.objects.filter(company=company).select_related(
        "staff", "related_staff", "rule_type", "work_type_a", "work_type_b"
    )
    if query:
        items = items.filter(
            Q(staff__name__icontains=query)
            | Q(staff__employee_number__icontains=query)
        )
    return items


@login_required
@admin_required
def constraint_manage(request):
    form = ConstraintForm(request.POST or None, company=request.company)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company)
        messages.success(request, "制約条件を登録しました。")
        return redirect("constraint_manage")
    query = request.GET.get("q", "").strip()
    items = _constraint_items(request.company, query)
    return render(
        request,
        "shifts/constraint_manage.html",
        {
            "form": form,
            "items": items,
            "query": query,
            "has_rule_types": ConstraintType.objects.filter(
                company=request.company, active=True
            ).exists(),
        },
    )


@login_required
@admin_required
def constraint_edit(request, pk):
    item = get_object_or_404(IndividualConstraint, pk=pk, company=request.company)
    form = ConstraintForm(request.POST or None, instance=item, company=request.company)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company)
        messages.success(request, "制約条件を更新しました。")
        return redirect("constraint_manage")
    query = request.GET.get("q", "").strip()
    items = _constraint_items(request.company, query)
    return render(
        request,
        "shifts/constraint_manage.html",
        {
            "form": form,
            "items": items,
            "editing_item": item,
            "query": query,
            "has_rule_types": ConstraintType.objects.filter(
                company=request.company, active=True
            ).exists(),
        },
    )


@login_required
@admin_required
def constraint_delete(request, pk):
    item = get_object_or_404(IndividualConstraint, pk=pk, company=request.company)
    return _delete_confirmation(
        request,
        item,
        f"制約条件「{item.name}」",
        reverse("constraint_manage"),
        "constraint_manage",
    )


@login_required
@admin_required
def constraint_type_manage(request):
    form = ConstraintTypeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company)
        messages.success(request, "条件種別を登録しました。")
        return redirect("constraint_type_manage")
    return render(
        request,
        "shifts/constraint_type_manage.html",
        {"form": form, "items": ConstraintType.objects.filter(company=request.company)},
    )


@login_required
@admin_required
def constraint_type_edit(request, pk):
    item = get_object_or_404(ConstraintType, pk=pk, company=request.company)
    form = ConstraintTypeForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company)
        messages.success(request, "条件種別を更新しました。")
        return redirect("constraint_type_manage")
    return render(
        request,
        "shifts/constraint_type_manage.html",
        {
            "form": form,
            "items": ConstraintType.objects.filter(company=request.company),
            "editing_item": item,
        },
    )


@login_required
@admin_required
def constraint_type_delete(request, pk):
    item = get_object_or_404(ConstraintType, pk=pk, company=request.company)
    return _delete_confirmation(
        request,
        item,
        f"条件種別「{item.name}」",
        reverse("constraint_type_manage"),
        "constraint_type_manage",
    )


@login_required
@admin_required
def skill_map(request):
    works = list(WorkType.objects.filter(company=request.company, active=True))
    skill_levels = list(SkillLevel.objects.filter(company=request.company))
    matrix_query = request.GET.get("matrix_q", "").strip()
    delete_query = request.GET.get("delete_q", "").strip()
    work_filter = request.GET.get("work", "")
    level_filter = request.GET.get("level", "")

    if request.method == "POST" and request.POST.get("action") == "update_matrix":
        staff_ids = {
            staff_id
            for staff_id in Staff.objects.filter(
                company=request.company, active=True
            ).values_list("id", flat=True)
        }
        work_ids = {work.id for work in works}
        level_ids = {level.id for level in skill_levels}
        updated_count = 0
        deleted_count = 0

        for staff_id in staff_ids:
            for work_id in work_ids:
                field_name = f"skill_{staff_id}_{work_id}"
                if field_name not in request.POST:
                    continue

                level_id = request.POST.get(field_name)
                current_skill = StaffSkill.objects.filter(
                    staff_id=staff_id,
                    staff__company=request.company,
                    work_type_id=work_id,
                )

                if not level_id:
                    deleted_count += current_skill.count()
                    current_skill.delete()
                    continue

                if not level_id.isdigit() or int(level_id) not in level_ids:
                    continue

                StaffSkill.objects.update_or_create(
                    staff_id=staff_id,
                    work_type_id=work_id,
                    defaults={"level_id": int(level_id)},
                )
                updated_count += 1

        messages.success(
            request,
            f"スキルマップを更新しました。（更新{updated_count}件・未設定{deleted_count}件）",
        )
        redirect_url = reverse("skill_map")
        if request.GET:
            redirect_url = f"{redirect_url}?{request.GET.urlencode()}"
        return redirect(redirect_url)

    staff_qs = Staff.objects.filter(company=request.company, active=True)
    if matrix_query:
        staff_qs = staff_qs.filter(
            Q(employee_number__icontains=matrix_query)
            | Q(name__icontains=matrix_query)
        )

    staff_rows = []
    for staff in staff_qs:
        levels = {
            item.work_type_id: item.level
            for item in staff.work_skills.select_related("level")
        }
        staff_rows.append(
            {
                "staff": staff,
                "cells": [
                    {
                        "field_name": f"skill_{staff.id}_{work.id}",
                        "level": levels.get(work.id),
                        "work": work,
                    }
                    for work in works
                ],
            }
        )
    skill_entries = StaffSkill.objects.filter(
        staff__company=request.company
    ).select_related("staff", "work_type", "level")
    if delete_query:
        skill_entries = skill_entries.filter(
            Q(staff__employee_number__icontains=delete_query)
            | Q(staff__name__icontains=delete_query)
            | Q(work_type__name__icontains=delete_query)
            | Q(level__symbol__icontains=delete_query)
            | Q(level__meaning__icontains=delete_query)
        )
    if work_filter.isdigit():
        skill_entries = skill_entries.filter(work_type_id=int(work_filter))
    if level_filter.isdigit():
        skill_entries = skill_entries.filter(level_id=int(level_filter))
    skill_entries = skill_entries.order_by(
        "staff__employee_number", "work_type__display_order", "work_type__id"
    )
    matrix_clear_params = {
        key: value
        for key, value in {
            "delete_q": delete_query,
            "work": work_filter,
            "level": level_filter,
        }.items()
        if value
    }
    delete_clear_params = {
        "matrix_q": matrix_query,
    } if matrix_query else {}
    return render(
        request,
        "shifts/skill_map.html",
        {
            "works": works,
            "staff_rows": staff_rows,
            "skill_entries": skill_entries,
            "matrix_query": matrix_query,
            "delete_query": delete_query,
            "work_filter": work_filter,
            "level_filter": level_filter,
            "matrix_clear_url": (
                f"{reverse('skill_map')}?{urlencode(matrix_clear_params)}"
                if matrix_clear_params
                else reverse("skill_map")
            ),
            "delete_clear_url": (
                f"{reverse('skill_map')}?{urlencode(delete_clear_params)}"
                if delete_clear_params
                else reverse("skill_map")
            ),
            "skill_levels": skill_levels,
        },
    )


@login_required
@admin_required
def staff_skill_delete(request, pk):
    item = get_object_or_404(
        StaffSkill.objects.select_related("staff", "work_type"),
        pk=pk,
        staff__company=request.company,
    )
    return _delete_confirmation(
        request,
        item,
        f"{item.staff.name}の「{item.work_type.name}」スキル",
        reverse("skill_map"),
        "skill_map",
    )


@login_required
@admin_required
@require_POST
def staff_skill_bulk_delete(request):
    selected_ids = request.POST.getlist("skill_ids")
    selected_ids = [int(value) for value in selected_ids if value.isdigit()]
    items = (
        StaffSkill.objects.filter(pk__in=selected_ids, staff__company=request.company)
        .select_related("staff", "work_type", "level")
        .order_by("staff__employee_number", "work_type__display_order")
    )
    if not items.exists():
        messages.error(request, "削除するスキルを選択してください。")
        return redirect("skill_map")
    if request.POST.get("confirmed") == "1":
        count = items.count()
        items.delete()
        messages.success(request, f"スタッフスキルを{count}件削除しました。")
        return redirect("skill_map")
    return render(
        request,
        "shifts/bulk_delete_confirm.html",
        {"items": items, "selected_ids": selected_ids},
    )


def _skill_import_template_workbook(company):
    works = list(WorkType.objects.filter(company=company, active=True))
    skill_levels = list(SkillLevel.objects.filter(company=company))
    if not skill_levels:
        skill_levels = [
            {"symbol": "◎", "meaning": "リーダー", "priority": 1, "assignable": True},
            {"symbol": "○", "meaning": "対応可能", "priority": 2, "assignable": True},
            {"symbol": "△", "meaning": "訓練中", "priority": 3, "assignable": True},
            {"symbol": "×", "meaning": "対応不可", "priority": 99, "assignable": False},
        ]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "スキル表"
    work_names = [work.name for work in works] or ["業務A", "業務B", "業務C"]
    example_work_names = work_names[:3]
    headers = ["社員番号", "氏名", "公休数", "備考", *example_work_names]
    sample_rows = [
        ["S001", "青木 太郎", 8, "4勤不可;単休不可", "◎", "○", "×"][: len(headers)],
        ["S002", "田中 花子", 8, "業務Aと業務B交互;業務A連続不可", "○", "◎", "△"][: len(headers)],
        ["S003", "佐藤 次郎", 9, "業務B禁止", "△", "○", "◎"][: len(headers)],
    ]

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    note_fill = PatternFill("solid", fgColor="FFF2CC")

    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for sample in sample_rows:
        sheet.append(sample)

    for cell in sheet["D"][1:]:
        cell.fill = note_fill
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    if len(headers) > 4:
        for row in sheet.iter_rows(min_row=2, min_col=5, max_col=len(headers)):
            for cell in row:
                cell.fill = PatternFill("solid", fgColor="EAF4FF")
                cell.alignment = Alignment(horizontal="center")

    for column_index, header in enumerate(headers, start=1):
        width = 14
        if header == "氏名":
            width = 18
        elif header == "公休数":
            width = 12
        elif header == "備考":
            width = 42
        elif column_index >= 5:
            width = 14
        sheet.column_dimensions[get_column_letter(column_index)].width = width
    sheet.freeze_panes = "A2"

    level_sheet = workbook.create_sheet("スキル区分")
    level_headers = ["記号", "意味", "優先度", "アサイン可"]
    level_sheet.append(level_headers)
    for cell in level_sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for index, level in enumerate(skill_levels, start=1):
        if isinstance(level, dict):
            symbol = level["symbol"]
            meaning = level["meaning"]
            priority = level["priority"]
            assignable = level["assignable"]
        else:
            symbol = level.symbol
            meaning = level.meaning
            priority = level.priority
            assignable = level.assignable
        level_sheet.append([symbol, meaning, priority, "可" if assignable else "不可"])
    for column_index, width in enumerate((12, 28, 12, 14), start=1):
        level_sheet.column_dimensions[get_column_letter(column_index)].width = width

    work_sheet = workbook.create_sheet("業務マスタ")
    work_headers = ["業務名", "必要人数", "色", "有効"]
    work_sheet.append(work_headers)
    for cell in work_sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    if works:
        for work in works:
            work_sheet.append(
                [
                    work.name,
                    work.required_staff_per_day,
                    work.color,
                    "有効" if work.active else "無効",
                ]
            )
    else:
        for name, color in (("業務A", "#2563eb"), ("業務B", "#16a34a"), ("業務C", "#f97316")):
            work_sheet.append([name, 1, color, "有効"])

    for column_index, width in enumerate((24, 16, 14, 12), start=1):
        work_sheet.column_dimensions[get_column_letter(column_index)].width = width

    _add_skill_entry_guide_sheet(
        workbook,
        header_fill,
        header_font,
        [
            ["1", "業務マスタ", "業務名に「受付」「ロール」「エーカス」のような業務名を書きます。"],
            ["2", "スキル区分", "◎・○・△・×など、会社で使うマークと意味を書きます。"],
            ["3", "スキル表", "備考の右側に、業務マスタと同じ業務名を列見出しとして書きます。"],
            ["4", "スキル表", "スタッフごとに、その業務へ入れるレベルのマークをセルへ書きます。"],
        ],
        [
            ["社員番号", "氏名", "公休数", "備考", "業務A", "業務B", "業務C"],
            ["S001", "青木 太郎", 8, "2勤1休", "◎", "○", "×"],
            ["S002", "田中 花子", 8, "業務Aと業務B交互", "○", "◎", "△"],
        ],
    )
    _add_previous_shift_example_sheet(
        workbook,
        header_fill,
        header_font,
        [
            ["S001", "青木 太郎", "業務A", "業務B", "公休", "業務A", "有給", "業務B", "公休"],
            ["S002", "田中 花子", "公休", "業務A", "業務B", "公休", "業務A", "業務B", "有給"],
            ["S003", "佐藤 次郎", "業務C", "公休", "業務A", "業務B", "公休", "業務C", "業務A"],
        ],
    )

    guide = workbook.create_sheet("入力ルール")
    guide_rows = [
        ["項目", "入力内容"],
        ["社員番号", "ログインIDにも使う番号です。必須です。"],
        ["氏名", "スタッフ名です。必須です。"],
        ["公休数", "スタッフごとの月公休数です。スタッフ管理へ反映します。"],
        ["希望上限", "Excelには記入不要です。スタッフ管理の一括変更で全スタッフへ反映します。"],
        ["備考", "個別制約にしたい条件を書きます。複数ある場合は ; で区切れます。"],
        ["業務マスタ", "業務名・必要人数・色・有効を入力します。取込時に業務管理へ反映します。"],
        ["業務列", "スキル表の公休数・備考の後ろには、業務マスタと同じ業務名を見出しとして追加します。"],
        ["スキル区分", "スキル区分シートの記号・意味・優先度・アサイン可を取込時に自動設定します。"],
        ["先月シフト実績", "マスタ取込後にこのシートを使って、前月の月末7日分の勤務・公休・有給を取り込めます。"],
        ["備考例", "2勤1休 / 4勤不可 / 単休不可"],
        ["備考例", "業務Aと業務B交互 / 業務A連続不可 / 業務B禁止"],
    ]
    for row in guide_rows:
        guide.append(row)
    for cell in guide[1]:
        cell.fill = header_fill
        cell.font = header_font
    guide.column_dimensions["A"].width = 16
    guide.column_dimensions["B"].width = 72

    _add_skill_sheet_comments(sheet)
    _add_work_sheet_comments(work_sheet)
    _add_level_sheet_comments(level_sheet)

    return workbook


def _add_skill_entry_guide_sheet(workbook, header_fill, header_font, steps, examples):
    sheet = workbook.create_sheet("業務スキル記入例")
    sheet.append(["手順", "書く場所", "内容"])
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for row in steps:
        sheet.append(row)

    start_row = len(steps) + 4
    sheet.cell(row=start_row, column=1, value="スキル表の書き方例")
    sheet.cell(row=start_row, column=1).font = Font(bold=True)
    for row_index, row in enumerate(examples, start=start_row + 1):
        for column_index, value in enumerate(row, start=1):
            cell = sheet.cell(row=row_index, column=column_index, value=value)
            if row_index == start_row + 1:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")
            elif column_index >= 5:
                cell.alignment = Alignment(horizontal="center")

    notes_start = start_row + len(examples) + 3
    notes = [
        "業務列の見出しは、必ず「業務マスタ」シートの業務名と同じ文字にしてください。",
        "セルに書くマークは、必ず「スキル区分」シートの記号から選んでください。",
        "空欄はスキル未設定として扱います。対応不可にしたい場合は、×などの不可マークを書いてください。",
    ]
    sheet.cell(row=notes_start, column=1, value="注意")
    sheet.cell(row=notes_start, column=1).font = Font(bold=True)
    for offset, note in enumerate(notes, start=1):
        sheet.cell(row=notes_start + offset, column=1, value=f"・{note}")
        sheet.merge_cells(
            start_row=notes_start + offset,
            start_column=1,
            end_row=notes_start + offset,
            end_column=7,
        )

    widths = (10, 18, 72, 16, 14, 14, 14)
    for column_index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column_index)].width = width


def _add_skill_sheet_comments(sheet):
    comments = {
        "A1": "スタッフを照合するキーです。ログインIDにも使います。",
        "B1": "スタッフ名です。",
        "C1": "スタッフごとの月公休数です。",
        "D1": "個別制約にしたい条件を書きます。例：2勤1休、単休不可、ロールとエーカス交互",
        "E1": "ここから右側が業務スキル欄です。業務マスタと同じ業務名を見出しにしてください。",
    }
    for address, text in comments.items():
        if sheet[address].value is not None:
            sheet[address].comment = Comment(text, "ShiftFlow")


def _add_work_sheet_comments(sheet):
    comments = {
        "A1": "スキル表の業務列・先月実績の勤務名と同じ名前にしてください。",
        "B1": "1日に必要な人数です。",
        "C1": "シフト表で使う色です。例：#2563eb",
        "D1": "有効/無効を書きます。",
    }
    for address, text in comments.items():
        sheet[address].comment = Comment(text, "ShiftFlow")


def _add_level_sheet_comments(sheet):
    comments = {
        "A1": "スキル表のセルに書くマークです。例：◎、○、△、×",
        "B1": "マークの意味です。",
        "C1": "数字が小さいほど優先されます。",
        "D1": "このマークのスタッフをアサイン可能にする場合は可、不可にする場合は不可。",
    }
    for address, text in comments.items():
        sheet[address].comment = Comment(text, "ShiftFlow")


def _add_previous_shift_example_sheet(workbook, header_fill, header_font, rows):
    sheet = workbook.create_sheet("先月シフト実績")
    headers = [
        "社員番号",
        "氏名",
        "2026/6/24",
        "2026/6/25",
        "2026/6/26",
        "2026/6/27",
        "2026/6/28",
        "2026/6/29",
        "2026/6/30",
    ]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for row in rows:
        sheet.append(row)
    for row in sheet.iter_rows(min_row=2, min_col=3, max_col=9):
        for cell in row:
            cell.alignment = Alignment(horizontal="center")
    for column_index, width in enumerate((14, 18, 12, 12, 12, 12, 12, 12, 12), start=1):
        sheet.column_dimensions[get_column_letter(column_index)].width = width
    sheet.freeze_panes = "C2"


def _skill_import_sample_workbook():
    # 実運用前にそのまま取り込んで試せるサンプル。
    # 対応画面: templates/shifts/import.html の「サンプルデータをダウンロード」
    workbook = Workbook()
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    note_fill = PatternFill("solid", fgColor="FFF2CC")
    skill_fill = PatternFill("solid", fgColor="EAF4FF")

    sheet = workbook.active
    sheet.title = "スキル表"
    headers = ["社員番号", "氏名", "公休数", "備考", "受付", "ロール", "エーカス"]
    sample_rows = [
        ["S001", "青木 太郎", 8, "単休不可", "◎", "○", "△"],
        ["S002", "田中 花子", 8, "ロールとエーカス交互;ロール連続不可", "○", "◎", "◎"],
        ["S003", "佐藤 次郎", 8, "受付禁止", "×", "○", "◎"],
        ["S004", "鈴木 花", 9, "4勤不可", "◎", "△", "○"],
        ["S005", "高橋 健", 8, "エーカス連続不可", "○", "◎", "○"],
        ["S006", "伊藤 美咲", 8, "ロール禁止", "◎", "×", "○"],
        ["S007", "渡辺 翔", 10, "2勤1休", "△", "◎", "○"],
        ["S008", "山本 葵", 8, "単休不可", "○", "○", "◎"],
        ["S009", "中村 優", 8, "受付とロール交互", "◎", "◎", "△"],
        ["S010", "小林 陸", 8, "", "○", "△", "◎"],
    ]

    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row in sample_rows:
        sheet.append(row)

    for cell in sheet["D"][1:]:
        cell.fill = note_fill
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    for row in sheet.iter_rows(min_row=2, min_col=5, max_col=7):
        for cell in row:
            cell.fill = skill_fill
            cell.alignment = Alignment(horizontal="center")

    for column_index, width in enumerate((14, 18, 12, 42, 12, 12, 12), start=1):
        sheet.column_dimensions[get_column_letter(column_index)].width = width
    sheet.freeze_panes = "A2"

    level_sheet = workbook.create_sheet("スキル区分")
    level_sheet.append(["記号", "意味", "優先度", "アサイン可"])
    for cell in level_sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for row in (
        ["◎", "主担当・指導可", 1, "可"],
        ["○", "対応可能", 2, "可"],
        ["△", "補助・訓練中", 3, "可"],
        ["×", "対応不可", 99, "不可"],
    ):
        level_sheet.append(row)
    for column_index, width in enumerate((12, 28, 12, 14), start=1):
        level_sheet.column_dimensions[get_column_letter(column_index)].width = width

    work_sheet = workbook.create_sheet("業務マスタ")
    work_sheet.append(["業務名", "必要人数", "色", "有効"])
    for cell in work_sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for row in (
        ["受付", 2, "#2563eb", "有効"],
        ["ロール", 2, "#16a34a", "有効"],
        ["エーカス", 1, "#f97316", "有効"],
    ):
        work_sheet.append(row)
    for column_index, width in enumerate((24, 16, 14, 12), start=1):
        work_sheet.column_dimensions[get_column_letter(column_index)].width = width

    _add_skill_entry_guide_sheet(
        workbook,
        header_fill,
        header_font,
        [
            ["1", "業務マスタ", "業務名に「受付」「ロール」「エーカス」を登録しています。"],
            ["2", "スキル区分", "◎・○・△・×の意味と、アサイン可否を登録しています。"],
            ["3", "スキル表", "備考の右側にある「受付」「ロール」「エーカス」が業務列です。"],
            ["4", "スキル表", "各スタッフのセルに、入れる業務レベルのマークを書いています。"],
        ],
        [
            ["社員番号", "氏名", "公休数", "備考", "受付", "ロール", "エーカス"],
            ["S001", "青木 太郎", 8, "単休不可", "◎", "○", "△"],
            ["S003", "佐藤 次郎", 8, "受付禁止", "×", "○", "◎"],
        ],
    )
    _add_previous_shift_example_sheet(
        workbook,
        header_fill,
        header_font,
        [
            ["S001", "青木 太郎", "受付", "ロール", "公休", "エーカス", "有給", "受付", "公休"],
            ["S002", "田中 花子", "ロール", "公休", "エーカス", "受付", "公休", "ロール", "有給"],
            ["S003", "佐藤 次郎", "公休", "受付", "ロール", "公休", "エーカス", "受付", "ロール"],
            ["S004", "鈴木 花", "受付", "公休", "受付", "ロール", "エーカス", "公休", "受付"],
            ["S005", "高橋 健", "エーカス", "受付", "公休", "ロール", "受付", "有給", "エーカス"],
            ["S006", "伊藤 美咲", "公休", "ロール", "受付", "公休", "ロール", "受付", "エーカス"],
            ["S007", "渡辺 翔", "ロール", "エーカス", "公休", "受付", "ロール", "公休", "受付"],
            ["S008", "山本 葵", "受付", "公休", "エーカス", "受付", "公休", "ロール", "エーカス"],
            ["S009", "中村 優", "エーカス", "受付", "ロール", "有給", "受付", "ロール", "公休"],
            ["S010", "小林 陸", "公休", "エーカス", "受付", "ロール", "公休", "受付", "ロール"],
        ],
    )

    guide = workbook.create_sheet("入力ルール")
    guide_rows = [
        ["項目", "入力内容"],
        ["このファイルの目的", "取込テスト用のサンプルです。スタッフ10人・業務3つを登録できます。"],
        ["社員番号", "取込後のログインIDにも使われます。初期パスワードは 0000 です。"],
        ["公休数", "スタッフごとの月公休数です。スタッフ管理へ反映します。"],
        ["希望上限", "Excelには記入不要です。スタッフ管理の一括変更で全スタッフへ反映します。"],
        ["備考", "個別制約へ自動変換される条件の例を入れています。不要なら空欄で問題ありません。"],
        ["業務マスタ", "業務名・必要人数・色・有効を業務管理へ反映します。"],
        ["スキル表", "公休数・備考の後ろの業務名とセルの記号から、スタッフごとのスキルを登録します。"],
        ["スキル区分", "記号の意味・優先度・アサイン可否を登録します。"],
        ["先月シフト実績", "マスタ取込後にこのシートを使って、前月の月末7日分の勤務・公休・有給を取り込めます。"],
    ]
    for row in guide_rows:
        guide.append(row)
    for cell in guide[1]:
        cell.fill = header_fill
        cell.font = header_font
    guide.column_dimensions["A"].width = 18
    guide.column_dimensions["B"].width = 78

    _add_skill_sheet_comments(sheet)
    _add_work_sheet_comments(work_sheet)
    _add_level_sheet_comments(level_sheet)

    return workbook


@login_required
@admin_required
def download_import_template(request):
    workbook = _skill_import_template_workbook(request.company)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        'attachment; filename="shift_import_template.xlsx"'
    )
    return response


@login_required
@admin_required
def download_import_sample(request):
    workbook = _skill_import_sample_workbook()
    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="shift_import_sample.xlsx"'
    return response


PUBLIC_HOLIDAY_TOKENS = {"休", "公休", "公", "休日", "公休日"}
PAID_LEAVE_TOKENS = {"有休", "有給", "有", "年休"}
BLANK_SHIFT_TOKENS = {"", "-", "ー", "－", "未入力"}


def _previous_shift_day_from_header(header, month):
    if isinstance(header, datetime):
        return header.date()
    if isinstance(header, date):
        return header
    if isinstance(header, int) or (isinstance(header, float) and header.is_integer()):
        try:
            return month.replace(day=int(header))
        except ValueError:
            return None
    text = str(header or "").strip()
    if not text:
        return None
    if text.isdigit() or (text.endswith(".0") and text[:-2].isdigit()):
        try:
            return month.replace(day=int(float(text)))
        except ValueError:
            return None

    numbers = [int(value) for value in re.findall(r"\d+", text)]
    if len(numbers) == 1:
        try:
            return month.replace(day=numbers[0])
        except ValueError:
            return None
    if len(numbers) == 2:
        try:
            return date(month.year, numbers[0], numbers[1])
        except ValueError:
            return None
    if len(numbers) >= 3:
        try:
            return date(numbers[0], numbers[1], numbers[2])
        except ValueError:
            return None
    return None


def _previous_shift_rows_from_file(file_obj):
    suffix = Path(file_obj.name).suffix.lower()
    file_obj.seek(0)
    if suffix == ".csv":
        text = file_obj.read().decode("utf-8-sig")
        return list(csv.reader(StringIO(text)))
    if suffix == ".xlsx":
        workbook = load_workbook(file_obj, data_only=True)
        if "先月シフト実績" not in workbook.sheetnames:
            raise ValueError("Excel内に「先月シフト実績」シートが必要です。")
        sheet = workbook["先月シフト実績"]
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    raise ValueError("先月シフト実績は .xlsx または .csv で取り込んでください。")


def _classify_previous_shift_cell(raw_value, work_map):
    value = str(raw_value or "").strip()
    compact = value.replace(" ", "").replace("　", "")
    if compact in BLANK_SHIFT_TOKENS:
        return PreviousMonthShiftDay.Status.BLANK, None
    if compact in PUBLIC_HOLIDAY_TOKENS:
        return PreviousMonthShiftDay.Status.PUBLIC_HOLIDAY, None
    if compact in PAID_LEAVE_TOKENS:
        return PreviousMonthShiftDay.Status.PAID_LEAVE, None
    work = work_map.get(value) or work_map.get(compact)
    if work:
        return PreviousMonthShiftDay.Status.WORK, work
    raise ValueError(f"業務名「{value}」が業務管理に登録されていません。")


def _import_previous_shift_days(company, month, file_obj):
    rows = _previous_shift_rows_from_file(file_obj)
    if not rows:
        raise ValueError("取込対象の行がありません。")

    last_day_number = calendar.monthrange(month.year, month.month)[1]
    import_from = month.replace(day=last_day_number - 6)
    import_to = month.replace(day=last_day_number)
    headers = [str(value or "").strip() for value in rows[0]]
    if "社員番号" not in headers:
        raise ValueError("見出しに「社員番号」が必要です。")
    employee_index = headers.index("社員番号")
    day_columns = [
        (index, day)
        for index, header in enumerate(rows[0])
        if index != employee_index
        for day in [_previous_shift_day_from_header(header, month)]
        if day and import_from <= day <= import_to
    ]
    if not day_columns:
        raise ValueError(
            f"月末7日分の日付列が見つかりません。例：{month.month}/{import_from.day}〜{month.month}/{import_to.day}"
        )

    staff_map = {
        staff.employee_number: staff
        for staff in Staff.objects.filter(company=company, active=True)
    }
    work_map = {}
    for work in WorkType.objects.filter(company=company, active=True):
        work_map[work.name] = work
        work_map[work.name.replace(" ", "").replace("　", "")] = work

    imported_items = []
    skipped = 0
    for raw_row in rows[1:]:
        row = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
        employee_number = str(row[employee_index] or "").strip()
        if not employee_number:
            skipped += 1
            continue
        staff = staff_map.get(employee_number)
        if not staff:
            raise ValueError(f"社員番号「{employee_number}」のスタッフが登録されていません。")
        for index, day in day_columns:
            raw_value = row[index] if index < len(row) else ""
            status, work = _classify_previous_shift_cell(raw_value, work_map)
            imported_items.append(
                PreviousMonthShiftDay(
                    company=company,
                    staff=staff,
                    day=day,
                    status=status,
                    work_type=work,
                    raw_value=str(raw_value or "").strip(),
                )
            )

    PreviousMonthShiftDay.objects.filter(
        company=company, day__range=(import_from, import_to)
    ).delete()
    PreviousMonthShiftDay.objects.bulk_create(imported_items)
    return {
        "days": len(imported_items),
        "staff": len({item.staff_id for item in imported_items}),
        "skipped": skipped,
        "from": import_from.isoformat(),
        "to": import_to.isoformat(),
    }


@login_required
@admin_required
def import_skill_map(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    previous_shift_form = PreviousShiftImportForm()
    if request.method == "POST" and request.POST.get("action") == "previous_shift":
        previous_shift_form = PreviousShiftImportForm(request.POST, request.FILES)
        if previous_shift_form.is_valid():
            file = previous_shift_form.cleaned_data["file"]
            month = previous_shift_form.cleaned_data["month"].replace(day=1)
            try:
                result = _import_previous_shift_days(request.company, month, file)
            except ValueError as exc:
                previous_shift_form.add_error("file", str(exc))
            else:
                ImportJob.objects.create(
                    company=request.company,
                    uploaded_by=request.user,
                    filename=f"先月実績: {file.name}",
                    result=result,
                )
                messages.success(
                    request,
                    f"{month.year}年{month.month}月の月末7日分の先月シフト実績を{result['days']}件取り込みました。",
                )
                return redirect("import_skill_map")
    elif request.method == "POST" and form.is_valid():
        file = form.cleaned_data["file"]
        try:
            result = ImportSkillMap(
                SkillMapFileReader(), DjangoMasterRepository()
            ).execute(request.company.id, file.name, file)
        except (SkillMapReadError, ValueError) as exc:
            form.add_error("file", str(exc))
        else:
            ImportJob.objects.create(
                company=request.company,
                uploaded_by=request.user,
                filename=file.name,
                result=result,
            )
            messages.success(
                request,
                f"スタッフ{result['staff']}件、スキル{result['skills']}件を取り込みました。"
                f"スキル区分{result.get('levels', 0)}件を設定しました。"
                f"備考から個別制約{result.get('constraints', 0)}件を反映しました。"
                f"新規ログインアカウントは{result.get('accounts', 0)}件です。",
            )
            return redirect("skill_map")
    return render(
        request,
        "shifts/import.html",
        {
            "form": form,
            "previous_shift_form": previous_shift_form,
            "jobs": ImportJob.objects.filter(company=request.company).order_by(
                "-created_at"
            )[:10],
        },
    )


@login_required
@admin_required
def previous_shift_list(request):
    if request.GET.get("month"):
        month = _month(request.GET.get("month"))
    else:
        month = _latest_previous_shift_month(request.company) or _add_months(
            timezone.localdate().replace(day=1), -1
        )

    first_day, last_day, days = _previous_shift_month_end_days(month)
    period = (
        ShiftPeriod.objects.filter(
            company=request.company,
            month=month,
            assignments__isnull=False,
        )
        .distinct()
        .first()
    )
    if period:
        display = _previous_shift_display_from_period(
            request.company, period, first_day, last_day, days
        )
    else:
        display = _previous_shift_display_from_import(
            request.company, first_day, last_day, days
        )

    return render(
        request,
        "shifts/previous_shift_list.html",
        {
            "month": month,
            "prev_month": _add_months(month, -1),
            "next_month": _add_months(month, 1),
            "first_day": first_day,
            "last_day": last_day,
            "days": days,
            **display,
        },
    )


def _latest_previous_shift_month(company):
    internal_month = (
        ShiftPeriod.objects.filter(company=company, assignments__isnull=False)
        .distinct()
        .order_by("-month")
        .values_list("month", flat=True)
        .first()
    )
    imported_day = (
        PreviousMonthShiftDay.objects.filter(company=company)
        .order_by("-day")
        .values_list("day", flat=True)
        .first()
    )
    imported_month = imported_day.replace(day=1) if imported_day else None
    months = [month for month in (internal_month, imported_month) if month]
    return max(months) if months else None


def _previous_shift_month_end_days(month):
    last_day_number = calendar.monthrange(month.year, month.month)[1]
    first_day = month.replace(day=last_day_number - 6)
    last_day = month.replace(day=last_day_number)
    days = [
        month.replace(day=number)
        for number in range(first_day.day, last_day.day + 1)
    ]
    return first_day, last_day, days


def _previous_shift_display_from_period(company, period, first_day, last_day, days):
    staff_list = list(
        Staff.objects.filter(company=company, active=True).order_by("employee_number")
    )
    assignment_map = {
        (item.staff_id, item.day): item
        for item in ShiftAssignment.objects.filter(
            period=period,
            day__range=(first_day, last_day),
        ).select_related("work_type")
    }
    paid_leave_days = {
        (item.submission.staff_id, item.day)
        for item in AvailabilityDay.objects.filter(
            submission__staff__company=company,
            submission__month=period.month,
            submission__status=AvailabilitySubmission.Status.SUBMITTED,
            paid_leave=True,
            day__range=(first_day, last_day),
        ).select_related("submission")
    }
    rows = []
    totals = defaultdict(int)
    for staff in staff_list:
        cells = []
        for day in days:
            assignment = assignment_map.get((staff.id, day))
            if assignment and assignment.work_type:
                status = PreviousMonthShiftDay.Status.WORK
                label = assignment.work_type.name
                raw_value = "作成済みシフト"
            elif (staff.id, day) in paid_leave_days:
                status = PreviousMonthShiftDay.Status.PAID_LEAVE
                label = "有給"
                raw_value = "作成済みシフト"
            else:
                status = PreviousMonthShiftDay.Status.PUBLIC_HOLIDAY
                label = "公休"
                raw_value = "作成済みシフト"
            totals[status] += 1
            cells.append(
                {
                    "day": day,
                    "label": label,
                    "status": status,
                    "raw_value": raw_value,
                }
            )
        rows.append({"staff": staff, "cells": cells})

    return {
        "source": "internal",
        "source_label": "作成済みシフト表",
        "source_note": "この月のシフト表がアプリ内にあるため、Excel取込より優先して表示しています。",
        "item_count": len(staff_list) * len(days),
        "staff_count": len(staff_list),
        "latest_imported_at": None,
        "rows": rows,
        "totals": {
            "work": totals[PreviousMonthShiftDay.Status.WORK],
            "public_holiday": totals[PreviousMonthShiftDay.Status.PUBLIC_HOLIDAY],
            "paid_leave": totals[PreviousMonthShiftDay.Status.PAID_LEAVE],
            "blank": 0,
        },
    }


def _previous_shift_display_from_import(company, first_day, last_day, days):
    items = list(
        PreviousMonthShiftDay.objects.filter(
            company=company,
            day__range=(first_day, last_day),
        )
        .select_related("staff", "work_type")
        .order_by("staff__employee_number", "day")
    )
    items_by_staff_day = {(item.staff_id, item.day): item for item in items}
    staff_list = []
    seen_staff_ids = set()
    for item in items:
        if item.staff_id in seen_staff_ids:
            continue
        seen_staff_ids.add(item.staff_id)
        staff_list.append(item.staff)

    rows = []
    totals = defaultdict(int)
    for staff in staff_list:
        cells = []
        for day in days:
            item = items_by_staff_day.get((staff.id, day))
            if not item:
                cells.append(
                    {
                        "day": day,
                        "label": "未取込",
                        "status": "missing",
                        "raw_value": "",
                    }
                )
                continue
            label = item.get_status_display()
            if item.status == PreviousMonthShiftDay.Status.WORK and item.work_type:
                label = item.work_type.name
            cells.append(
                {
                    "day": day,
                    "label": label,
                    "status": item.status,
                    "raw_value": item.raw_value,
                }
            )
            totals[item.status] += 1
        rows.append({"staff": staff, "cells": cells})

    latest_imported_at = max((item.imported_at for item in items), default=None)
    return {
        "source": "excel",
        "source_label": "Excel取込データ",
        "source_note": "作成済みシフト表がないため、Excelから取り込んだ先月実績を表示しています。",
        "item_count": len(items),
        "staff_count": len(staff_list),
        "latest_imported_at": latest_imported_at,
        "rows": rows,
        "totals": {
            "work": totals[PreviousMonthShiftDay.Status.WORK],
            "public_holiday": totals[PreviousMonthShiftDay.Status.PUBLIC_HOLIDAY],
            "paid_leave": totals[PreviousMonthShiftDay.Status.PAID_LEAVE],
            "blank": totals[PreviousMonthShiftDay.Status.BLANK],
        },
    }


@login_required
@admin_required
def shift_manage(request):
    return render(
        request,
        "shifts/shift_manage.html",
        {
            "form": GenerateForm(initial={"month": _month()}),
            "periods": ShiftPeriod.objects.filter(company=request.company).annotate(
                assignment_count=Count("assignments")
            ),
        },
    )


@login_required
@admin_required
@require_POST
def generate_shift(request):
    form = GenerateForm(request.POST)
    if not form.is_valid():
        messages.error(request, "対象月を確認してください。")
        return redirect("shift_manage")
    month = form.cleaned_data["month"].replace(day=1)
    previous_month = _add_months(month, -1)
    previous_period = (
        ShiftPeriod.objects.filter(
            company=request.company,
            month=previous_month,
            assignments__isnull=False,
        )
        .distinct()
        .first()
    )
    if previous_period:
        previous_shift_count = previous_period.assignments.count()
        carryover_message = (
            f"作成済みの前月シフト{previous_shift_count}件を加味しました。"
        )
    else:
        previous_shift_count = PreviousMonthShiftDay.objects.filter(
            company=request.company,
            day__year=previous_month.year,
            day__month=previous_month.month,
        ).count()
        carryover_message = (
            f"Excel取込の先月実績{previous_shift_count}件を加味しました。"
            if previous_shift_count
            else "先月実績が未取込のため、当月データのみで作成しました。"
        )
    output = GenerateMonthlyShift(DjangoShiftRepository()).execute(
        request.company.id, month
    )
    period = ShiftPeriod.objects.get(pk=output.period_id, company=request.company)
    _refresh_period_constraint_warnings(period)
    period.refresh_from_db()
    messages.success(
        request,
        f"{output.assignment_count}件を配置しました。警告は{period.warning_count}件です。"
        f"{carryover_message}",
    )
    return redirect("shift_detail", pk=output.period_id)


def _period_rows(period, works=None):
    # 管理者シフト表専用の行データ。
    # days は表の横軸、rows はスタッフごとの縦軸。
    # row.cells[*].day の class 情報が shift_detail.html 経由で CSS に渡る。
    days = _calendar_days(period.month)
    works = list(works or WorkType.objects.filter(company=period.company, active=True))
    staff_list = list(Staff.objects.filter(company=period.company, active=True))
    work_options_by_staff = _assignable_work_options_by_staff(
        period.company, staff_list, works
    )
    assignment_map = {
        (item.staff_id, item.day.day): item
        for item in period.assignments.select_related("work_type")
    }
    leave_request_map = {
        (item.staff_id, item.day.day): item
        for item in period.leave_requests.exclude(
            status=ShiftLeaveRequest.Status.REJECTED
        ).select_related("work_type")
    }
    paid_leave_days = defaultdict(set)
    for item in AvailabilityDay.objects.filter(
        submission__staff__company=period.company,
        submission__month=period.month,
        paid_leave=True,
    ):
        paid_leave_days[item.submission.staff_id].add(item.day)
    requested_public_holiday_days = defaultdict(set)
    for item in AvailabilityDay.objects.filter(
        submission__staff__company=period.company,
        submission__month=period.month,
        preferred_off=True,
    ):
        requested_public_holiday_days[item.submission.staff_id].add(item.day)
    day_count = len(days)
    rows = []
    for staff in staff_list:
        work_options = work_options_by_staff[staff.id]
        work_option_ids = {work.id for work in work_options}
        staff_paid_leave_days = paid_leave_days[staff.id]
        staff_requested_public_holiday_days = requested_public_holiday_days[staff.id]
        cells = []
        for day in days:
            assignment = assignment_map.get((staff.id, day["number"]))
            day_date = day["date"]
            rest_kind = ""
            rest_label = ""
            if not assignment:
                if day_date in staff_paid_leave_days:
                    rest_kind = "paid-leave"
                    rest_label = "有給"
                elif day_date in staff_requested_public_holiday_days:
                    rest_kind = "requested-public-holiday"
                    rest_label = "申請公休"
                else:
                    rest_kind = "inserted-public-holiday"
                    rest_label = "公休"
            cells.append(
                {
                    "day": day,
                    "assignment": assignment,
                    "leave_request": leave_request_map.get((staff.id, day["number"])),
                    "field_name": f"assignment_{staff.id}_{day_date:%Y%m%d}",
                    "rest_kind": rest_kind,
                    "rest_label": rest_label,
                    "assignment_work_is_option": (
                        not assignment or assignment.work_type_id in work_option_ids
                    ),
                }
            )
        work_count = sum(1 for cell in cells if cell["assignment"])
        rest_count = day_count - work_count
        paid_leave_count = sum(
            1
            for cell in cells
            if not cell["assignment"] and cell["day"]["date"] in staff_paid_leave_days
        )
        public_holiday_count = max(rest_count - paid_leave_count, 0)
        public_holiday_target = staff.monthly_public_holidays
        expected_total = work_count + public_holiday_count + paid_leave_count
        public_holiday_status = (
            "ok"
            if public_holiday_count == public_holiday_target
            else "under"
            if public_holiday_count < public_holiday_target
            else "over"
        )
        expected_total_status = (
            "ok"
            if expected_total == day_count
            else "under"
            if expected_total < day_count
            else "over"
        )
        rows.append(
            {
                "staff": staff,
                "work_options": work_options,
                "cells": cells,
                "work_count": work_count,
                "rest_count": rest_count,
                "public_holiday_count": public_holiday_count,
                "public_holiday_target": public_holiday_target,
                "public_holiday_status": public_holiday_status,
                "public_holiday_status_label": (
                    "不足" if public_holiday_status == "under" else "超過"
                    if public_holiday_status == "over"
                    else ""
                ),
                "paid_leave_count": paid_leave_count,
                "expected_total": expected_total,
                "expected_total_status": expected_total_status,
                "expected_total_status_label": (
                    "不足" if expected_total_status == "under" else "超過"
                    if expected_total_status == "over"
                    else ""
                ),
            }
        )
    return days, rows


def _assignable_work_options_by_staff(company, staff_list, works):
    disallowed_work_ids = defaultdict(set)
    for skill in StaffSkill.objects.filter(
        staff__company=company,
        staff__in=staff_list,
        work_type__in=works,
        level__assignable=False,
    ).select_related("level"):
        disallowed_work_ids[skill.staff_id].add(skill.work_type_id)

    return {
        staff.id: [
            work
            for work in works
            if work.id not in disallowed_work_ids[staff.id]
        ]
        for staff in staff_list
    }


def _attach_shift_statistics(rows, days, works):
    # シフト表の右側・下部に出す集計値を作る。
    # 右側: スタッフごとの業務別回数
    # 下部: 日ごとの業務別人数
    daily_stats = [
        {
            "work": work,
            "counts": [0 for _day in days],
            "total": 0,
        }
        for work in works
    ]
    daily_stat_map = {item["work"].id: item for item in daily_stats}

    for row in rows:
        counts = {work.id: 0 for work in works}
        for index, cell in enumerate(row["cells"]):
            assignment = cell["assignment"]
            if not assignment or not assignment.work_type_id:
                continue
            if assignment.work_type_id not in counts:
                continue
            counts[assignment.work_type_id] += 1
            daily_stat_map[assignment.work_type_id]["counts"][index] += 1
            daily_stat_map[assignment.work_type_id]["total"] += 1
        row["work_summary"] = [
            {
                "work": work,
                "count": counts[work.id],
            }
            for work in works
        ]

    return daily_stats


def _shift_export_day_label(day):
    # ダウンロード用の日付見出し。
    # 集計値は含めず、画面のシフト本体だけをファイル化する。
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    label = f"{day['number']}日({weekdays[day['date'].weekday()]})"
    if day["holiday"]:
        label = f"{label} {day['holiday']}"
    return label


def _shift_export_cell_label(staff, cell):
    assignment = cell["assignment"]
    if assignment and assignment.work_type:
        return assignment.work_type.name
    if staff.is_employee and cell["rest_kind"] != "paid-leave":
        return ""
    return cell["rest_label"] or "休"


def _shift_export_rows(period):
    days, rows = _period_rows(period)
    header = ["社員番号", "氏名"] + [_shift_export_day_label(day) for day in days]
    body = []
    for row in rows:
        staff = row["staff"]
        body.append(
            [
                staff.employee_number,
                staff.name,
                *[_shift_export_cell_label(staff, cell) for cell in row["cells"]],
            ]
        )
    return days, rows, header, body


def _excel_color(value):
    if not value:
        return None
    color = str(value).strip().replace("#", "")
    if len(color) == 6 and re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        return color.upper()
    return None


def _apply_shift_export_styles(sheet, period, days, rows):
    title_fill = PatternFill("solid", fgColor="E0F2FE")
    header_fill = PatternFill("solid", fgColor="F1F5F9")
    saturday_fill = PatternFill("solid", fgColor="DBEAFE")
    non_workday_fill = PatternFill("solid", fgColor="FEE2E2")
    requested_public_holiday_fill = PatternFill("solid", fgColor="FEE2E2")
    inserted_public_holiday_fill = PatternFill("solid", fgColor="F8FAFC")
    paid_leave_fill = PatternFill("solid", fgColor="EDE9FE")
    borderless_center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    max_column = sheet.max_column
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max_column)
    title_cell = sheet.cell(row=1, column=1)
    title_cell.font = Font(size=14, bold=True)
    title_cell.fill = title_fill
    title_cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 26

    for cell in sheet[2]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = borderless_center

    for index, day in enumerate(days, start=3):
        header_cell = sheet.cell(row=2, column=index)
        if day["is_saturday"]:
            header_cell.fill = saturday_fill
        elif day["is_non_workday"]:
            header_cell.fill = non_workday_fill

    for row_index, row in enumerate(rows, start=3):
        staff = row["staff"]
        for column_index in (1, 2):
            sheet.cell(row=row_index, column=column_index).alignment = Alignment(
                vertical="center"
            )
        for day_index, cell_data in enumerate(row["cells"], start=3):
            cell = sheet.cell(row=row_index, column=day_index)
            cell.alignment = borderless_center
            assignment = cell_data["assignment"]
            if assignment and assignment.work_type:
                work_color = _excel_color(assignment.work_type.color)
                if work_color:
                    cell.fill = PatternFill("solid", fgColor=work_color)
                    cell.font = Font(color="FFFFFF", bold=True)
                continue

            if staff.is_employee and cell_data["rest_kind"] != "paid-leave":
                continue

            if cell_data["rest_kind"] == "requested-public-holiday":
                cell.fill = requested_public_holiday_fill
                cell.font = Font(color="B4233A", bold=True)
            elif cell_data["rest_kind"] == "paid-leave":
                cell.fill = paid_leave_fill
                cell.font = Font(color="6D28D9", bold=True)
            elif cell_data["rest_kind"] == "inserted-public-holiday":
                cell.fill = inserted_public_holiday_fill

    sheet.freeze_panes = "C3"
    sheet.column_dimensions["A"].width = 14
    sheet.column_dimensions["B"].width = 18
    for column_index in range(3, max_column + 1):
        sheet.column_dimensions[get_column_letter(column_index)].width = 13


@login_required
@admin_required
def download_shift_csv(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk, company=request.company)
    _days, _rows, header, body = _shift_export_rows(period)

    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = (
        f'attachment; filename="shift_{period.month:%Y_%m}.csv"'
    )
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow([f"{period.month.year}年{period.month.month}月 シフト表"])
    writer.writerow(header)
    writer.writerows(body)
    return response


@login_required
@admin_required
def download_shift_excel(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk, company=request.company)
    days, rows, header, body = _shift_export_rows(period)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "シフト表"
    sheet.append([f"{period.month.year}年{period.month.month}月 シフト表"])
    sheet.append(header)
    for line in body:
        sheet.append(line)
    _apply_shift_export_styles(sheet, period, days, rows)

    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)

    response = HttpResponse(
        stream.getvalue(),
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = (
        f'attachment; filename="shift_{period.month:%Y_%m}.xlsx"'
    )
    return response


def _shift_edit_support(period, days, rows, works):
    # 下書き編集で崩れやすいポイントを検知する。
    # 対応HTML: templates/shifts/shift_detail.html の「下書き編集サポート」
    assignments = list(
        period.assignments.select_related("staff", "work_type").order_by(
            "day", "work_type__display_order", "staff__employee_number"
        )
    )
    assignment_counts = defaultdict(int)
    for assignment in assignments:
        assignment_counts[(assignment.day, assignment.work_type_id)] += 1

    daily_shortages = []
    for day in days:
        for work in works:
            count = assignment_counts[(day["date"], work.id)]
            shortage = max(0, work.required_staff_per_day - count)
            if shortage:
                daily_shortages.append(
                    {
                        "day": day,
                        "work": work,
                        "count": count,
                        "required": work.required_staff_per_day,
                        "shortage": shortage,
                    }
                )

    availability = {
        (item.submission.staff_id, item.day): item
        for item in AvailabilityDay.objects.filter(
            submission__staff__company=period.company,
            submission__month=period.month,
        ).select_related("submission")
    }
    skills = {
        (item.staff_id, item.work_type_id): item
        for item in StaffSkill.objects.filter(
            staff__company=period.company,
            work_type__company=period.company,
        ).select_related("level")
    }
    pending_leave_map = {
        (item.staff_id, item.day): item
        for item in ShiftLeaveRequest.objects.filter(
            period=period,
            status=ShiftLeaveRequest.Status.PENDING,
        )
    }

    assignment_issues = []
    for assignment in assignments:
        work_name = assignment.work_type.name if assignment.work_type else "業務未設定"
        pending_leave = pending_leave_map.get((assignment.staff_id, assignment.day))
        if pending_leave:
            assignment_issues.append(
                {
                    "level": "danger",
                    "day": assignment.day,
                    "staff": assignment.staff,
                    "message": f"{work_name}に急な休み申請が出ています。",
                }
            )

        request_day = availability.get((assignment.staff_id, assignment.day))
        if request_day and not request_day.available:
            assignment_issues.append(
                {
                    "level": "danger",
                    "day": assignment.day,
                    "staff": assignment.staff,
                    "message": f"{work_name}に入っていますが、勤務不可で提出されています。",
                }
            )
        elif request_day and request_day.paid_leave:
            assignment_issues.append(
                {
                    "level": "danger",
                    "day": assignment.day,
                    "staff": assignment.staff,
                    "message": f"{work_name}に入っていますが、有給希望の日です。",
                }
            )
        elif request_day and request_day.preferred_off:
            assignment_issues.append(
                {
                    "level": "warning",
                    "day": assignment.day,
                    "staff": assignment.staff,
                    "message": f"{work_name}に入っていますが、休み希望の日です。",
                }
            )

        skill = skills.get((assignment.staff_id, assignment.work_type_id))
        if not skill:
            if not assignment.staff.is_employee:
                assignment_issues.append(
                    {
                        "level": "warning",
                        "day": assignment.day,
                        "staff": assignment.staff,
                        "message": f"{work_name}のスキルが未設定です。",
                    }
                )
            continue
        if not skill.level.assignable:
            assignment_issues.append(
                {
                    "level": "danger",
                    "day": assignment.day,
                    "staff": assignment.staff,
                    "message": f"{work_name}はスキル区分「{skill.level.symbol}」のためアサイン不可です。",
                }
            )

    work_counts = [row["work_count"] for row in rows]
    average = round(sum(work_counts) / len(work_counts), 1) if work_counts else 0
    high_threshold = average + 2
    low_threshold = max(0, average - 2)
    balance_rows = []
    for row in rows:
        status = "normal"
        if row["work_count"] > high_threshold:
            status = "high"
        elif row["work_count"] < low_threshold:
            status = "low"
        balance_rows.append(
            {
                "staff": row["staff"],
                "work_count": row["work_count"],
                "rest_count": row["rest_count"],
                "status": status,
            }
        )

    return {
        "daily_shortages": daily_shortages,
        "assignment_issues": assignment_issues,
        "balance_rows": balance_rows,
        "average_work_count": average,
        "has_issues": bool(daily_shortages or assignment_issues),
    }


def _refresh_period_constraint_warnings(period):
    GenerationWarning.objects.filter(
        period=period,
        message__startswith="制約違反：",
    ).delete()
    warnings = _constraint_violation_warnings(period)
    GenerationWarning.objects.bulk_create(
        [
            GenerationWarning(
                period=period,
                day=item["day"],
                work_type=item.get("work"),
                message=item["message"],
            )
            for item in warnings
        ]
    )
    period.warning_count = period.warnings.count()
    period.save(update_fields=["warning_count"])
    return len(warnings)


def _constraint_violation_warnings(period):
    assignments = list(
        period.assignments.select_related("staff", "work_type").order_by(
            "day", "staff__employee_number", "work_type__display_order"
        )
    )
    if not assignments:
        return []

    previous_work_days = [
        item
        for item in DjangoShiftRepository().previous_shift_days_for_generation(
            period.company_id, period.month
        )
        if item.status == PreviousMonthShiftDay.Status.WORK and item.work_id
    ]
    previous_assignments = [
        {
            "staff_id": item.staff_id,
            "day": item.day,
            "work_id": item.work_id,
            "staff": None,
            "work": None,
        }
        for item in previous_work_days
    ]
    current_assignments = [
        {
            "staff_id": item.staff_id,
            "day": item.day,
            "work_id": item.work_type_id,
            "staff": item.staff,
            "work": item.work_type,
        }
        for item in assignments
        if item.work_type_id
    ]
    all_assignments = sorted(
        [*previous_assignments, *current_assignments],
        key=lambda item: (item["day"], item["staff_id"]),
    )
    current_by_staff = defaultdict(list)
    all_by_staff = defaultdict(list)
    current_by_day_work = defaultdict(list)
    for item in all_assignments:
        all_by_staff[item["staff_id"]].append(item)
        if item["day"].year == period.month.year and item["day"].month == period.month.month:
            current_by_staff[item["staff_id"]].append(item)
            current_by_day_work[(item["day"], item["work_id"])].append(item)

    rules = DjangoShiftRepository().rules_for_generation(period.company_id)
    warnings = []
    seen = set()

    def add_warning(day, staff, work, message):
        key = (day, staff.id if staff else None, work.id if work else None, message)
        if key in seen:
            return
        seen.add(key)
        warnings.append(
            {
                "day": day,
                "work": work,
                "message": f"制約違反：{staff.name if staff else ''}{message}",
            }
        )

    for rule in rules:
        if rule.operator == "incompatible_same_work":
            _append_incompatible_same_work_warnings(
                rule, current_by_day_work, add_warning
            )
            continue
        if rule.staff_id is None:
            continue
        staff_assignments = all_by_staff[rule.staff_id]
        current_staff_assignments = current_by_staff[rule.staff_id]
        if not current_staff_assignments:
            continue
        if rule.operator == "max_consecutive":
            _append_max_consecutive_warnings(rule, staff_assignments, add_warning)
        elif rule.operator == "work_alternation":
            _append_work_alternation_warnings(rule, staff_assignments, add_warning)
        elif rule.operator == "avoid_same_work":
            _append_avoid_same_work_warnings(rule, staff_assignments, add_warning)
        elif rule.operator == "avoid_specific_work":
            _append_avoid_specific_work_warnings(rule, staff_assignments, add_warning)
        elif rule.operator == "forbid_specific_work":
            _append_forbid_specific_work_warnings(
                rule, current_staff_assignments, add_warning
            )
        elif rule.operator == "forbid_works_on_weekdays":
            _append_forbid_works_on_weekdays_warnings(
                rule, current_staff_assignments, add_warning
            )
        elif rule.operator == "no_single_rest":
            _append_no_single_rest_warnings(rule, current_staff_assignments, add_warning)
        elif rule.operator == "work_rest_pattern":
            _append_work_rest_pattern_warnings(
                rule, current_staff_assignments, period.month, add_warning
            )
    return warnings


def _append_incompatible_same_work_warnings(rule, current_by_day_work, add_warning):
    pair = {rule.staff_id, rule.related_staff_id}
    if None in pair:
        return
    for (day, work_id), items in current_by_day_work.items():
        if rule.work_ids and work_id not in rule.work_ids:
            continue
        staff_ids = {item["staff_id"] for item in items}
        if pair.issubset(staff_ids):
            target = next(item for item in items if item["staff_id"] == rule.staff_id)
            add_warning(day, target["staff"], target["work"], "さんが同時配置禁止の相手と同じ業務に入っています。")


def _append_max_consecutive_warnings(rule, staff_assignments, add_warning):
    limit = rule.numeric_value or 6
    streak = []
    previous_day = None
    for item in staff_assignments:
        if previous_day and item["day"] == previous_day + timedelta(days=1):
            streak.append(item)
        else:
            streak = [item]
        previous_day = item["day"]
        if len(streak) > limit and item["staff"]:
            add_warning(item["day"], item["staff"], item["work"], f"さんが最大連続勤務{limit}日を超えています。")


def _append_work_alternation_warnings(rule, staff_assignments, add_warning):
    filtered = [item for item in staff_assignments if item["work_id"] in rule.work_ids]
    for previous, current in zip(filtered, filtered[1:]):
        if current["work_id"] == previous["work_id"] and current["staff"]:
            add_warning(current["day"], current["staff"], current["work"], "さんの交互配置が崩れています。")


def _append_avoid_same_work_warnings(rule, staff_assignments, add_warning):
    for previous, current in zip(staff_assignments, staff_assignments[1:]):
        if current["work_id"] == previous["work_id"] and current["staff"]:
            add_warning(current["day"], current["staff"], current["work"], "さんが同じ業務に連続で入っています。")


def _append_avoid_specific_work_warnings(rule, staff_assignments, add_warning):
    for previous, current in zip(staff_assignments, staff_assignments[1:]):
        if (
            current["work_id"] == previous["work_id"]
            and current["work_id"] in rule.work_ids
            and current["staff"]
        ):
            add_warning(current["day"], current["staff"], current["work"], "さんが連続回避対象の業務に続けて入っています。")


def _append_forbid_specific_work_warnings(rule, current_staff_assignments, add_warning):
    for item in current_staff_assignments:
        if item["work_id"] in rule.work_ids and item["staff"]:
            add_warning(item["day"], item["staff"], item["work"], "さんが禁止業務に入っています。")


def _append_forbid_works_on_weekdays_warnings(rule, current_staff_assignments, add_warning):
    for item in current_staff_assignments:
        if item["day"].weekday() not in rule.weekdays:
            continue
        if rule.work_ids and item["work_id"] not in rule.work_ids:
            continue
        if item["staff"]:
            add_warning(item["day"], item["staff"], item["work"], "さんが禁止曜日の業務に入っています。")


def _append_no_single_rest_warnings(rule, current_staff_assignments, add_warning):
    worked_days = {item["day"] for item in current_staff_assignments}
    staff_item = next((item for item in current_staff_assignments if item["staff"]), None)
    if not staff_item:
        return
    start = min(worked_days)
    end = max(worked_days)
    current = start + timedelta(days=1)
    while current < end:
        if current not in worked_days and current - timedelta(days=1) in worked_days and current + timedelta(days=1) in worked_days:
            add_warning(current, staff_item["staff"], None, "さんが単休になっています。")
        current += timedelta(days=1)


def _append_work_rest_pattern_warnings(rule, current_staff_assignments, month, add_warning):
    pattern = _work_rest_pattern(rule.text_value)
    if not pattern:
        return
    for item in current_staff_assignments:
        index = (item["day"].day - 1) % len(pattern)
        if not pattern[index] and item["staff"]:
            add_warning(item["day"], item["staff"], item["work"], "さんが勤務・休みパターンの休み日に入っています。")


def _work_rest_pattern(value):
    try:
        counts = [int(part.strip()) for part in value.replace("、", ",").split(",") if part.strip()]
    except ValueError:
        return ()
    pattern = []
    working = True
    for count in counts:
        if count < 1:
            return ()
        pattern.extend([working] * count)
        working = not working
    return tuple(pattern)


def _replacement_candidates(period, leave_request):
    # 急な休み申請の代替候補。
    # すでに同日に勤務が入っている人、勤務不可の人、対象業務にアサイン不可の人は候補から外す。
    if not leave_request.work_type:
        return []

    assigned_staff_ids = set(
        ShiftAssignment.objects.filter(period=period, day=leave_request.day).values_list(
            "staff_id", flat=True
        )
    )
    unavailable_staff_ids = set(
        AvailabilityDay.objects.filter(
            submission__staff__company=period.company,
            submission__month=period.month,
            day=leave_request.day,
        ).filter(
            Q(available=False) | Q(preferred_off=True) | Q(paid_leave=True)
        ).values_list("submission__staff_id", flat=True)
    )
    assignable_skill_staff_ids = set(
        StaffSkill.objects.filter(
            staff__company=period.company,
            work_type=leave_request.work_type,
            level__assignable=True,
        ).values_list("staff_id", flat=True)
    )
    unassignable_skill_staff_ids = set(
        StaffSkill.objects.filter(
            staff__company=period.company,
            work_type=leave_request.work_type,
            level__assignable=False,
        ).values_list("staff_id", flat=True)
    )
    monthly_work_counts = dict(
        ShiftAssignment.objects.filter(period=period)
        .values_list("staff_id")
        .annotate(count=Count("id"))
    )
    candidates = []
    for staff in Staff.objects.filter(company=period.company, active=True):
        if staff.id == leave_request.staff_id:
            continue
        if staff.id in assigned_staff_ids:
            continue
        if staff.id in unavailable_staff_ids:
            continue
        if staff.id in unassignable_skill_staff_ids:
            continue
        if staff.id not in assignable_skill_staff_ids and not staff.is_employee:
            continue
        candidates.append(
            {
                "staff": staff,
                "work_count": monthly_work_counts.get(staff.id, 0),
            }
        )
    return sorted(candidates, key=lambda item: (item["work_count"], item["staff"].employee_number))


def _leave_request_context(period):
    requests = list(
        period.leave_requests.select_related("staff", "work_type").order_by(
            "status", "day", "staff__employee_number"
        )
    )
    pending = []
    for item in requests:
        if item.status == ShiftLeaveRequest.Status.PENDING:
            pending.append(
                {
                    "request": item,
                    "candidates": _replacement_candidates(period, item),
                }
            )
    return {
        "leave_requests": requests,
        "pending_leave_requests": pending,
    }


@login_required
@admin_required
def shift_detail(request, pk):
    # 管理者の「シフト表」画面。
    # templates/shifts/shift_detail.html に days / rows を渡す。
    period = get_object_or_404(ShiftPeriod, pk=pk, company=request.company)
    works = list(WorkType.objects.filter(company=request.company, active=True))
    days, rows = _period_rows(period, works)
    daily_work_stats = _attach_shift_statistics(rows, days, works)
    can_edit_shift = period.status in {
        ShiftPeriod.Status.DRAFT,
        ShiftPeriod.Status.PUBLISHED,
    }
    leave_context = _leave_request_context(period)
    show_shift_support = can_edit_shift or bool(leave_context["leave_requests"])
    return render(
        request,
        "shifts/shift_detail.html",
        {
            "period": period,
            "days": days,
            "rows": rows,
            "works": works,
            "daily_work_stats": daily_work_stats,
            "can_edit_shift": can_edit_shift,
            "show_shift_support": show_shift_support,
            "edit_support": _shift_edit_support(period, days, rows, works),
            **leave_context,
            "warnings": period.warnings.select_related("work_type"),
        },
    )


@login_required
@admin_required
@require_POST
def update_shift_draft(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk, company=request.company)
    if period.status not in {
        ShiftPeriod.Status.DRAFT,
        ShiftPeriod.Status.PUBLISHED,
    }:
        messages.error(request, "まだ作成されていないシフトは編集できません。")
        return redirect("shift_detail", pk=pk)

    works = {
        str(work.id): work
        for work in WorkType.objects.filter(company=request.company, active=True)
    }
    staff_list = list(Staff.objects.filter(company=request.company, active=True))
    disallowed_work_ids = defaultdict(set)
    for skill in StaffSkill.objects.filter(
        staff__company=request.company,
        staff__in=staff_list,
        work_type_id__in=[work.id for work in works.values()],
        level__assignable=False,
    ).select_related("level"):
        disallowed_work_ids[skill.staff_id].add(str(skill.work_type_id))
    day_count = calendar.monthrange(period.month.year, period.month.month)[1]
    days = [period.month.replace(day=number) for number in range(1, day_count + 1)]
    changed_count = 0
    skipped_count = 0

    for staff in staff_list:
        for current in days:
            field_name = f"assignment_{staff.id}_{current:%Y%m%d}"
            if field_name not in request.POST:
                continue

            selected_work_id = request.POST.get(field_name, "")
            assignment = ShiftAssignment.objects.filter(
                period=period,
                staff=staff,
                day=current,
            ).first()

            if not selected_work_id:
                if assignment:
                    assignment.delete()
                    changed_count += 1
                continue

            work = works.get(selected_work_id)
            if not work:
                continue
            if selected_work_id in disallowed_work_ids[staff.id]:
                skipped_count += 1
                continue

            if assignment:
                if (
                    assignment.work_type_id != work.id
                    or not assignment.manually_edited
                ):
                    assignment.work_type = work
                    assignment.manually_edited = True
                    assignment.save(update_fields=["work_type", "manually_edited"])
                    changed_count += 1
            else:
                ShiftAssignment.objects.create(
                    period=period,
                    staff=staff,
                    day=current,
                    work_type=work,
                    manually_edited=True,
                )
                changed_count += 1

    constraint_warning_count = _refresh_period_constraint_warnings(period)
    message = f"シフトを保存しました。（変更{changed_count}件）"
    if skipped_count:
        message += f" 不可スキルの業務{skipped_count}件は反映しませんでした。"
    if constraint_warning_count:
        message += f" 制約違反の警告{constraint_warning_count}件を確認してください。"
    messages.success(request, message)
    return redirect("shift_detail", pk=pk)


@login_required
@admin_required
@require_POST
def resolve_leave_request(request, pk):
    leave_request = get_object_or_404(
        ShiftLeaveRequest.objects.select_related("period", "staff", "work_type"),
        pk=pk,
        period__company=request.company,
    )
    period = leave_request.period
    action = request.POST.get("action")

    if leave_request.status != ShiftLeaveRequest.Status.PENDING:
        messages.error(request, "この休み申請はすでに対応済みです。")
        return redirect("shift_detail", pk=period.pk)

    if action == "reject":
        leave_request.status = ShiftLeaveRequest.Status.REJECTED
        leave_request.admin_note = request.POST.get("admin_note", "")
        leave_request.resolved_at = timezone.now()
        leave_request.resolved_by = request.user
        leave_request.save(
            update_fields=["status", "admin_note", "resolved_at", "resolved_by"]
        )
        messages.success(request, "急な休み申請を却下しました。")
        return redirect("shift_detail", pk=period.pk)

    if action != "approve":
        messages.error(request, "対応内容を選択してください。")
        return redirect("shift_detail", pk=period.pk)

    assignment = ShiftAssignment.objects.filter(
        period=period,
        staff=leave_request.staff,
        day=leave_request.day,
    ).first()
    work_type = leave_request.work_type or (assignment.work_type if assignment else None)
    replacement_staff_id = request.POST.get("replacement_staff")
    replacement_staff = None
    if replacement_staff_id:
        replacement_staff = Staff.objects.filter(
            pk=replacement_staff_id,
            company=request.company,
            active=True,
        ).first()
        if not replacement_staff or replacement_staff == leave_request.staff:
            messages.error(request, "代替スタッフを確認してください。")
            return redirect("shift_detail", pk=period.pk)
        if ShiftAssignment.objects.filter(
            period=period,
            staff=replacement_staff,
            day=leave_request.day,
        ).exists():
            messages.error(request, "選択した代替スタッフは同日にすでに勤務があります。")
            return redirect("shift_detail", pk=period.pk)
        valid_candidate_ids = {
            item["staff"].id for item in _replacement_candidates(period, leave_request)
        }
        if replacement_staff.id not in valid_candidate_ids:
            messages.error(request, "選択した代替スタッフは条件に合いません。")
            return redirect("shift_detail", pk=period.pk)

    if assignment:
        assignment.delete()

    if replacement_staff and work_type:
        ShiftAssignment.objects.create(
            period=period,
            staff=replacement_staff,
            day=leave_request.day,
            work_type=work_type,
            note=f"{leave_request.staff.name}さん急休の代替",
            manually_edited=True,
        )

    leave_request.status = ShiftLeaveRequest.Status.APPROVED
    leave_request.admin_note = request.POST.get("admin_note", "")
    leave_request.resolved_at = timezone.now()
    leave_request.resolved_by = request.user
    leave_request.save(
        update_fields=["status", "admin_note", "resolved_at", "resolved_by"]
    )
    if replacement_staff:
        messages.success(
            request,
            f"急な休み申請を承認し、{replacement_staff.name}さんを代替配置しました。",
        )
    else:
        messages.warning(request, "急な休み申請を承認しました。代替は未配置です。")
    return redirect("shift_detail", pk=period.pk)


@login_required
@admin_required
@require_POST
def publish_shift(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk, company=request.company)
    period.status = ShiftPeriod.Status.PUBLISHED
    period.published_at = timezone.now()
    period.save(update_fields=["status", "published_at"])
    messages.success(request, "シフトを公開しました。")
    return redirect("shift_detail", pk=pk)


@login_required
@admin_required
def shift_delete(request, pk):
    item = get_object_or_404(ShiftPeriod, pk=pk, company=request.company)
    return _delete_confirmation(
        request,
        item,
        f"{item.month:%Y年%m月}のシフト",
        reverse("shift_detail", kwargs={"pk": pk}),
        "shift_manage",
    )


def _parse_work_rest_pattern(value):
    text = (value or "").replace("、", ",")
    try:
        counts = [int(part.strip()) for part in text.split(",") if part.strip()]
    except ValueError:
        return []
    if not counts or len(counts) % 2 or any(count < 1 for count in counts):
        return []
    return counts


def _suggested_off_days_from_constraints(staff, month):
    # スタッフの個別制約から、提出画面の休み希望候補を作る。
    # 対応HTML: templates/shifts/submit.html の「制約から休み希望を自動入力」
    day_count = calendar.monthrange(month.year, month.month)[1]
    suggested = set()
    rules = IndividualConstraint.objects.filter(
        company=staff.company,
        staff=staff,
        active=True,
    ).select_related("rule_type")

    pattern_rule = next(
        (
            rule
            for rule in rules
            if rule.rule_type
            and rule.rule_type.operator == ConstraintType.Operator.WORK_REST_PATTERN
        ),
        None,
    )
    if pattern_rule:
        counts = _parse_work_rest_pattern(pattern_rule.text_value)
        if counts:
            cursor = 1
            index = 0
            while cursor <= day_count:
                work_days = counts[index % len(counts)]
                rest_days = counts[(index + 1) % len(counts)]
                cursor += work_days
                for _ in range(rest_days):
                    if cursor <= day_count:
                        suggested.add(cursor)
                    cursor += 1
                index += 2

    max_days_candidates = [
        int(rule.numeric_value)
        for rule in rules
        if rule.rule_type
        and rule.rule_type.operator == ConstraintType.Operator.MAX_CONSECUTIVE
        and rule.numeric_value
    ]
    if max_days_candidates:
        max_days = max(1, min(max_days_candidates))
        suggested.update(range(max_days + 1, day_count + 1, max_days + 1))

    return suggested


def _staff_rest_constraint_notes(staff):
    # シフト提出時にスタッフ本人へ見せる「休み方」だけの個別制約。
    # 業務交互・特定業務禁止など、管理者側の割当判断に近い制約はここでは出さない。
    rest_operators = {
        ConstraintType.Operator.WORK_REST_PATTERN,
        ConstraintType.Operator.NO_SINGLE_REST,
        ConstraintType.Operator.MAX_CONSECUTIVE,
    }
    rest_kinds = {
        IndividualConstraint.Kind.NO_SINGLE_REST,
        IndividualConstraint.Kind.MAX_CONSECUTIVE,
    }
    rules = IndividualConstraint.objects.filter(
        company=staff.company,
        staff=staff,
        active=True,
    ).select_related("rule_type")

    notes = []
    for rule in rules:
        operator = rule.rule_type.operator if rule.rule_type else ""
        if operator not in rest_operators and rule.kind not in rest_kinds:
            continue

        label = rule.name.split("：")[-1].split(":")[-1].strip()
        detail = ""
        if operator == ConstraintType.Operator.WORK_REST_PATTERN:
            counts = _parse_work_rest_pattern(rule.text_value)
            if counts:
                pairs = [
                    f"{counts[index]}勤{counts[index + 1]}休"
                    for index in range(0, len(counts), 2)
                ]
                label = "・".join(pairs)
                detail = "この流れを希望"
        elif operator == ConstraintType.Operator.NO_SINGLE_REST:
            label = "単休禁止"
            detail = "休みを1日だけにせず、できれば連休にする"
        elif operator == ConstraintType.Operator.MAX_CONSECUTIVE and rule.numeric_value:
            label = f"{rule.numeric_value}勤超過不可"
            detail = f"{rule.numeric_value}勤を超えないようにする"

        notes.append(
            {
                "label": label,
                "detail": detail,
                "strength": rule.strength,
                "strength_label": rule.strength_label,
                "strength_class": rule.strength_class,
            }
        )
    return notes


@login_required
@staff_required
def submit_availability(request):
    # スタッフの「シフト提出」画面。
    # _calendar_days() の日付情報に、提出済み状態(state)を足して submit.html に渡す。
    requested_month = request.POST.get("month") or request.GET.get("month")
    month = _month(requested_month) if requested_month else _next_month()
    submission, _ = AvailabilitySubmission.objects.get_or_create(
        staff=request.staff, month=month
    )
    day_count = calendar.monthrange(month.year, month.month)[1]
    if request.method == "POST":
        requested_off_count = sum(
            1
            for number in range(1, day_count + 1)
            if request.POST.get(f"day_{number}", "available") in {"off", "paid"}
        )
        if requested_off_count > request.staff.desired_off_limit:
            messages.error(
                request,
                f"公休希望と有給希望は合計{request.staff.desired_off_limit}日までです。",
            )
            return redirect(f"{request.path}?month={month:%Y-%m}")
        for number in range(1, day_count + 1):
            state = request.POST.get(f"day_{number}", "available")
            AvailabilityDay.objects.update_or_create(
                submission=submission,
                day=month.replace(day=number),
                defaults={
                    "available": True,
                    "preferred_off": state == "off",
                    "paid_leave": state == "paid",
                },
            )
        submission.status = AvailabilitySubmission.Status.SUBMITTED
        submission.submitted_at = timezone.now()
        submission.save(update_fields=["status", "submitted_at"])
        messages.success(request, f"{month.year}年{month.month}月分を提出しました。")
        return redirect(f"{request.path}?month={month:%Y-%m}")
    saved = {
        item.day.day: (
            "paid" if item.paid_leave else "off" if item.preferred_off else "available"
        )
        for item in submission.days.all()
    }
    suggested_off_days = _suggested_off_days_from_constraints(request.staff, month)
    apply_constraints = request.GET.get("apply_constraints") == "1"

    days = []
    requested_off_count = 0
    for day in _calendar_days(month):
        suggested_state = "off" if day["number"] in suggested_off_days else "available"
        state = suggested_state if apply_constraints else saved.get(
            day["number"], suggested_state
        )
        if state in {"off", "paid"}:
            requested_off_count += 1
        days.append(
            {
                **day,
                "suggested_off": day["number"] in suggested_off_days,
                "state": state,
            }
        )
    return render(
        request,
        "shifts/submit.html",
        {
            "month": month,
            "submission": submission,
            "days": days,
            "suggested_off_count": len(suggested_off_days),
            "desired_off_limit": request.staff.desired_off_limit,
            "requested_off_count": requested_off_count,
            "rest_constraint_notes": _staff_rest_constraint_notes(request.staff),
        },
    )


@login_required
@staff_required
def my_shift(request):
    # スタッフの「シフト表確認」画面。
    # _calendar_days() の日付情報に、公開済みの割当(assignment)を足して my_shift.html に渡す。
    requested_month = request.GET.get("month")
    today_month = timezone.localdate().replace(day=1)
    min_month = _add_months(today_month, -1)
    max_month = _add_months(today_month, 1)
    month = _month(requested_month) if requested_month else max_month
    if month < min_month:
        month = min_month
    elif month > max_month:
        month = max_month
    prev_month = _add_months(month, -1) if month > min_month else None
    next_month = _add_months(month, 1) if month < max_month else None
    period = ShiftPeriod.objects.filter(
        company=request.company, month=month, status=ShiftPeriod.Status.PUBLISHED
    ).first()
    assignments = (
        period.assignments.filter(staff=request.staff).select_related("work_type")
        if period
        else []
    )
    assignment_map = {item.day.day: item for item in assignments}
    leave_request_map = {
        item.day.day: item
        for item in (
            period.leave_requests.filter(staff=request.staff)
            if period
            else ShiftLeaveRequest.objects.none()
        )
    }
    days = [
        {
            **day,
            "assignment": assignment_map.get(day["number"]),
            "leave_request": leave_request_map.get(day["number"]),
        }
        for day in _calendar_days(month)
    ]
    return render(
        request,
        "shifts/my_shift.html",
        {
            "month": month,
            "period": period,
            "days": days,
            "min_month": min_month,
            "max_month": max_month,
            "prev_month": prev_month,
            "next_month": next_month,
        },
    )


@login_required
@staff_required
@require_POST
def request_shift_leave(request):
    assignment = get_object_or_404(
        ShiftAssignment.objects.select_related("period", "work_type"),
        pk=request.POST.get("assignment_id"),
        staff=request.staff,
        period__company=request.company,
        period__status=ShiftPeriod.Status.PUBLISHED,
    )
    reason = request.POST.get("reason", "").strip()
    leave_request, created = ShiftLeaveRequest.objects.update_or_create(
        period=assignment.period,
        staff=request.staff,
        day=assignment.day,
        defaults={
            "assignment": assignment,
            "work_type": assignment.work_type,
            "reason": reason,
            "status": ShiftLeaveRequest.Status.PENDING,
            "admin_note": "",
            "resolved_at": None,
            "resolved_by": None,
        },
    )
    if created:
        messages.success(request, "急な休み申請を送信しました。")
    else:
        messages.success(request, "急な休み申請を更新して再送信しました。")
    return redirect(f"{reverse('my_shift')}?month={assignment.period.month:%Y-%m}")


@login_required
@staff_required
def staff_change_password(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        messages.success(request, "パスワードを変更しました。")
        return redirect("staff_change_password")
    return render(request, "shifts/change_password.html", {"form": form})
