import calendar
import json
from collections import defaultdict
from io import BytesIO
from datetime import date
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
from openpyxl import Workbook
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
    ImportJob,
    IndividualConstraint,
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
    ConstraintForm,
    ConstraintTypeForm,
    GenerateForm,
    ImportForm,
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
        "recent_periods": ShiftPeriod.objects.filter(company=request.company).annotate(
            assignment_count=Count("assignments")
        )[:5],
    }
    return render(request, "shifts/manager_dashboard.html", context)


@login_required
@admin_required
def staff_manage(request):
    form = StaffForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
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
    headers = ["社員番号", "氏名", "備考"]
    sample_rows = [
        ["S001", "青木 太郎", "4勤不可;単休不可"],
        ["S002", "田中 花子", "業務Aと業務B交互;業務A連続不可"],
        ["S003", "佐藤 次郎", "業務B禁止"],
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

    for cell in sheet["C"][1:]:
        cell.fill = note_fill
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    for column_index, header in enumerate(headers, start=1):
        width = 14
        if header == "氏名":
            width = 18
        elif header == "備考":
            width = 42
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
    work_headers = ["業務名", "最低必要人数", "有効"]
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
                    "有効" if work.active else "無効",
                ]
            )
    else:
        for name in ("業務A", "業務B", "業務C"):
            work_sheet.append([name, 1, "有効"])

    for column_index, width in enumerate((24, 16, 12), start=1):
        work_sheet.column_dimensions[get_column_letter(column_index)].width = width

    guide = workbook.create_sheet("入力ルール")
    guide_rows = [
        ["項目", "入力内容"],
        ["社員番号", "ログインIDにも使う番号です。必須です。"],
        ["氏名", "スタッフ名です。必須です。"],
        ["備考", "個別制約にしたい条件を書きます。複数ある場合は ; で区切れます。"],
        ["業務マスタ", "業務名・最低必要人数・有効を入力します。取込時に業務管理へ反映します。"],
        ["業務列", "スキル表のD列以降には、業務マスタと同じ業務名を見出しとして追加します。"],
        ["スキル区分", "スキル区分シートの記号・意味・優先度・アサイン可を取込時に自動設定します。"],
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

    return workbook


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
    headers = ["社員番号", "氏名", "備考", "受付", "ロール", "エーカス"]
    sample_rows = [
        ["S001", "青木 太郎", "単休不可", "◎", "○", "△"],
        ["S002", "田中 花子", "ロールとエーカス交互;ロール連続不可", "○", "◎", "◎"],
        ["S003", "佐藤 次郎", "受付禁止", "×", "○", "◎"],
        ["S004", "鈴木 花", "4勤不可", "◎", "△", "○"],
        ["S005", "高橋 健", "エーカス連続不可", "○", "◎", "○"],
        ["S006", "伊藤 美咲", "ロール禁止", "◎", "×", "○"],
        ["S007", "渡辺 翔", "2勤1休", "△", "◎", "○"],
        ["S008", "山本 葵", "単休不可", "○", "○", "◎"],
        ["S009", "中村 優", "受付とロール交互", "◎", "◎", "△"],
        ["S010", "小林 陸", "", "○", "△", "◎"],
    ]

    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row in sample_rows:
        sheet.append(row)

    for cell in sheet["C"][1:]:
        cell.fill = note_fill
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    for row in sheet.iter_rows(min_row=2, min_col=4, max_col=6):
        for cell in row:
            cell.fill = skill_fill
            cell.alignment = Alignment(horizontal="center")

    for column_index, width in enumerate((14, 18, 42, 12, 12, 12), start=1):
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
    work_sheet.append(["業務名", "最低必要人数", "有効"])
    for cell in work_sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for row in (
        ["受付", 2, "有効"],
        ["ロール", 2, "有効"],
        ["エーカス", 1, "有効"],
    ):
        work_sheet.append(row)
    for column_index, width in enumerate((24, 16, 12), start=1):
        work_sheet.column_dimensions[get_column_letter(column_index)].width = width

    guide = workbook.create_sheet("入力ルール")
    guide_rows = [
        ["項目", "入力内容"],
        ["このファイルの目的", "取込テスト用のサンプルです。スタッフ10人・業務3つを登録できます。"],
        ["社員番号", "取込後のログインIDにも使われます。初期パスワードは 0000 です。"],
        ["備考", "個別制約へ自動変換される条件の例を入れています。不要なら空欄で問題ありません。"],
        ["業務マスタ", "業務名・最低必要人数・有効を業務管理へ反映します。"],
        ["スキル表", "D列以降の業務名とセルの記号から、スタッフごとのスキルを登録します。"],
        ["スキル区分", "記号の意味・優先度・アサイン可否を登録します。"],
    ]
    for row in guide_rows:
        guide.append(row)
    for cell in guide[1]:
        cell.fill = header_fill
        cell.font = header_font
    guide.column_dimensions["A"].width = 18
    guide.column_dimensions["B"].width = 78

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


@login_required
@admin_required
def import_skill_map(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
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
            "jobs": ImportJob.objects.filter(company=request.company).order_by(
                "-created_at"
            )[:10],
        },
    )


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
    output = GenerateMonthlyShift(DjangoShiftRepository()).execute(
        request.company.id, month
    )
    messages.success(
        request,
        f"{output.assignment_count}件を配置しました。警告は{output.warning_count}件です。",
    )
    return redirect("shift_detail", pk=output.period_id)


def _period_rows(period):
    # 管理者シフト表専用の行データ。
    # days は表の横軸、rows はスタッフごとの縦軸。
    # row.cells[*].day の class 情報が shift_detail.html 経由で CSS に渡る。
    days = _calendar_days(period.month)
    staff_list = Staff.objects.filter(company=period.company, active=True)
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
    rows = []
    for staff in staff_list:
        cells = [
            {
                "day": day,
                "assignment": assignment_map.get((staff.id, day["number"])),
                "leave_request": leave_request_map.get((staff.id, day["number"])),
                "field_name": f"assignment_{staff.id}_{day['date']:%Y%m%d}",
            }
            for day in days
        ]
        work_count = sum(1 for cell in cells if cell["assignment"])
        rows.append(
            {
                "staff": staff,
                "cells": cells,
                "work_count": work_count,
                "rest_count": len(days) - work_count,
            }
        )
    return days, rows


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
            assignment_issues.append(
                {
                    "level": "warning",
                    "day": assignment.day,
                    "staff": assignment.staff,
                    "message": f"{work_name}のスキルが未設定です。",
                }
            )
        elif not skill.level.assignable:
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
            available=False,
        ).values_list("submission__staff_id", flat=True)
    )
    skill_staff_ids = set(
        StaffSkill.objects.filter(
            staff__company=period.company,
            work_type=leave_request.work_type,
            level__assignable=True,
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
        if staff.id not in skill_staff_ids:
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
    days, rows = _period_rows(period)
    works = list(WorkType.objects.filter(company=request.company, active=True))
    can_edit_shift = period.status == ShiftPeriod.Status.DRAFT
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
    if period.status != ShiftPeriod.Status.DRAFT:
        messages.error(request, "公開済みのシフトは編集できません。")
        return redirect("shift_detail", pk=pk)

    works = {
        str(work.id): work
        for work in WorkType.objects.filter(company=request.company, active=True)
    }
    staff_list = Staff.objects.filter(company=request.company, active=True)
    day_count = calendar.monthrange(period.month.year, period.month.month)[1]
    days = [period.month.replace(day=number) for number in range(1, day_count + 1)]
    changed_count = 0

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

    messages.success(request, f"下書きシフトを保存しました。（変更{changed_count}件）")
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
        for number in range(1, day_count + 1):
            state = request.POST.get(f"day_{number}", "available")
            AvailabilityDay.objects.update_or_create(
                submission=submission,
                day=month.replace(day=number),
                defaults={
                    "available": True,
                    "preferred_off": state == "off",
                },
            )
        submission.status = AvailabilitySubmission.Status.SUBMITTED
        submission.submitted_at = timezone.now()
        submission.save(update_fields=["status", "submitted_at"])
        messages.success(request, f"{month.year}年{month.month}月分を提出しました。")
        return redirect(f"{request.path}?month={month:%Y-%m}")
    saved = {
        item.day.day: ("off" if item.preferred_off else "available")
        for item in submission.days.all()
    }
    suggested_off_days = _suggested_off_days_from_constraints(request.staff, month)
    apply_constraints = request.GET.get("apply_constraints") == "1"

    days = []
    for day in _calendar_days(month):
        suggested_state = "off" if day["number"] in suggested_off_days else "available"
        state = suggested_state if apply_constraints else saved.get(
            day["number"], suggested_state
        )
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
