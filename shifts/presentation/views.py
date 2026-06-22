import calendar
from datetime import date

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.decorators import login_required
from django.db.models.deletion import ProtectedError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from shifts.application.use_cases import GenerateMonthlyShift, ImportSkillMap
from shifts.infrastructure.importers import SkillMapFileReader, SkillMapReadError
from shifts.infrastructure.master_repository import DjangoMasterRepository
from shifts.infrastructure.models import (
    AvailabilityDay, AvailabilitySubmission, CompanyMembership, ConstraintType, ImportJob, IndividualConstraint,
    ShiftPeriod, SkillLevel, Staff, StaffSkill, WorkType,
)
from shifts.infrastructure.repositories import DjangoShiftRepository
from .company import admin_required, current_membership, staff_required
from .forms import ConstraintForm, ConstraintTypeForm, GenerateForm, ImportForm, SkillLevelForm, StaffForm, StaffSkillForm, WorkTypeForm


def _month(value=None):
    if value:
        try: return date.fromisoformat(f"{value[:7]}-01")
        except ValueError: pass
    today = timezone.localdate()
    return today.replace(day=1)


def _delete_confirmation(request, obj, label, cancel_url, success_url, on_deleted=None):
    if request.method == "POST":
        try:
            obj.delete()
        except ProtectedError:
            messages.error(request, f"{label}はシフトやスキル設定で使用中のため削除できません。先に関連データを削除するか、無効にしてください。")
        else:
            if on_deleted:
                on_deleted()
            messages.success(request, f"{label}を削除しました。")
        return redirect(success_url)
    return render(request, "shifts/confirm_delete.html", {"target": obj, "label": label, "cancel_url": cancel_url})


@login_required
def home(request):
    membership = current_membership(request)
    if not membership:
        return render(request, "shifts/no_company.html")
    return redirect("manager_dashboard" if membership.role == CompanyMembership.Role.ADMIN else "submit_availability")


@login_required
@admin_required
def manager_dashboard(request):
    month = _month(request.GET.get("month"))
    active_staff = Staff.objects.filter(company=request.company, active=True)
    submitted = AvailabilitySubmission.objects.filter(
        staff__company=request.company, month=month, status=AvailabilitySubmission.Status.SUBMITTED
    ).count()
    staff_count = active_staff.count()
    rate = round(submitted / staff_count * 100) if staff_count else 0
    latest_period = ShiftPeriod.objects.filter(company=request.company, month=month).first()
    context = {
        "month": month, "staff_count": staff_count, "submitted_count": submitted,
        "submission_rate": rate, "missing_count": max(staff_count - submitted, 0),
        "warning_count": latest_period.warning_count if latest_period else 0,
        "shift_status": latest_period.get_status_display() if latest_period else "未生成",
        "latest_period": latest_period,
        "recent_periods": ShiftPeriod.objects.filter(company=request.company).annotate(assignment_count=Count("assignments"))[:5],
    }
    return render(request, "shifts/manager_dashboard.html", context)


@login_required
@admin_required
def staff_manage(request):
    form = StaffForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        staff = form.save_for_company(request.company)
        if staff.user:
            CompanyMembership.objects.update_or_create(company=request.company, user=staff.user, defaults={"role": CompanyMembership.Role.STAFF})
        messages.success(request, "スタッフを登録しました。")
        return redirect("staff_manage")
    return render(request, "shifts/staff_manage.html", {"form": form, "items": Staff.objects.filter(company=request.company).select_related("user")})


@login_required
@admin_required
def staff_edit(request, pk):
    item = get_object_or_404(Staff, pk=pk, company=request.company)
    form = StaffForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        staff = form.save_for_company(request.company)
        if staff.user:
            CompanyMembership.objects.update_or_create(company=request.company, user=staff.user, defaults={"role": CompanyMembership.Role.STAFF})
        messages.success(request, "スタッフ情報を更新しました。")
        return redirect("staff_manage")
    return render(request, "shifts/staff_manage.html", {"form": form, "items": Staff.objects.filter(company=request.company).select_related("user"), "editing_item": item})


@login_required
@admin_required
def staff_delete(request, pk):
    item = get_object_or_404(Staff, pk=pk, company=request.company)
    user_id = item.user_id
    return _delete_confirmation(
        request, item, f"スタッフ「{item.name}」", reverse("staff_manage"), "staff_manage",
        (lambda: CompanyMembership.objects.filter(company=request.company, user_id=user_id, role=CompanyMembership.Role.STAFF).delete()) if user_id else None,
    )


@login_required
@admin_required
def work_manage(request):
    form = WorkTypeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company); messages.success(request, "業務を登録しました。"); return redirect("work_manage")
    return render(request, "shifts/work_manage.html", {"form": form, "items": WorkType.objects.filter(company=request.company)})


@login_required
@admin_required
def work_edit(request, pk):
    item = get_object_or_404(WorkType, pk=pk, company=request.company)
    form = WorkTypeForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company); messages.success(request, "業務を更新しました。"); return redirect("work_manage")
    return render(request, "shifts/work_manage.html", {"form": form, "items": WorkType.objects.filter(company=request.company), "editing_item": item})


@login_required
@admin_required
def work_delete(request, pk):
    item = get_object_or_404(WorkType, pk=pk, company=request.company)
    return _delete_confirmation(request, item, f"業務「{item.name}」", reverse("work_manage"), "work_manage")


@login_required
@admin_required
def skill_manage(request):
    form = SkillLevelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company); messages.success(request, "スキル区分を登録しました。"); return redirect("skill_manage")
    return render(request, "shifts/skill_manage.html", {"form": form, "items": SkillLevel.objects.filter(company=request.company)})


@login_required
@admin_required
def skill_edit(request, pk):
    item = get_object_or_404(SkillLevel, pk=pk, company=request.company)
    form = SkillLevelForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company); messages.success(request, "スキル区分を更新しました。"); return redirect("skill_manage")
    return render(request, "shifts/skill_manage.html", {"form": form, "items": SkillLevel.objects.filter(company=request.company), "editing_item": item})


@login_required
@admin_required
def skill_delete(request, pk):
    item = get_object_or_404(SkillLevel, pk=pk, company=request.company)
    return _delete_confirmation(request, item, f"スキル区分「{item.symbol}」", reverse("skill_manage"), "skill_manage")


@login_required
@admin_required
def constraint_manage(request):
    form = ConstraintForm(request.POST or None, company=request.company)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company); messages.success(request, "制約条件を登録しました。"); return redirect("constraint_manage")
    items = IndividualConstraint.objects.filter(company=request.company).select_related("staff", "related_staff", "rule_type", "work_type_a", "work_type_b")
    return render(request, "shifts/constraint_manage.html", {"form": form, "items": items, "has_rule_types": ConstraintType.objects.filter(company=request.company, active=True).exists()})


@login_required
@admin_required
def constraint_edit(request, pk):
    item = get_object_or_404(IndividualConstraint, pk=pk, company=request.company)
    form = ConstraintForm(request.POST or None, instance=item, company=request.company)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company); messages.success(request, "制約条件を更新しました。"); return redirect("constraint_manage")
    items = IndividualConstraint.objects.filter(company=request.company).select_related("staff", "related_staff", "rule_type", "work_type_a", "work_type_b")
    return render(request, "shifts/constraint_manage.html", {"form": form, "items": items, "editing_item": item, "has_rule_types": ConstraintType.objects.filter(company=request.company, active=True).exists()})


@login_required
@admin_required
def constraint_delete(request, pk):
    item = get_object_or_404(IndividualConstraint, pk=pk, company=request.company)
    return _delete_confirmation(request, item, f"制約条件「{item.name}」", reverse("constraint_manage"), "constraint_manage")


@login_required
@admin_required
def constraint_type_manage(request):
    form = ConstraintTypeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company); messages.success(request, "条件種別を登録しました。"); return redirect("constraint_type_manage")
    return render(request, "shifts/constraint_type_manage.html", {"form": form, "items": ConstraintType.objects.filter(company=request.company)})


@login_required
@admin_required
def constraint_type_edit(request, pk):
    item = get_object_or_404(ConstraintType, pk=pk, company=request.company)
    form = ConstraintTypeForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save_for_company(request.company); messages.success(request, "条件種別を更新しました。"); return redirect("constraint_type_manage")
    return render(request, "shifts/constraint_type_manage.html", {"form": form, "items": ConstraintType.objects.filter(company=request.company), "editing_item": item})


@login_required
@admin_required
def constraint_type_delete(request, pk):
    item = get_object_or_404(ConstraintType, pk=pk, company=request.company)
    return _delete_confirmation(request, item, f"条件種別「{item.name}」", reverse("constraint_type_manage"), "constraint_type_manage")


@login_required
@admin_required
def skill_map(request):
    form = StaffSkillForm(request.POST or None, company=request.company)
    if request.method == "POST" and form.is_valid():
        StaffSkill.objects.update_or_create(
            staff=form.cleaned_data["staff"], work_type=form.cleaned_data["work_type"], defaults={"level": form.cleaned_data["level"]}
        )
        messages.success(request, "スタッフスキルを更新しました。"); return redirect("skill_map")
    works = list(WorkType.objects.filter(company=request.company, active=True))
    staff_rows = []
    for staff in Staff.objects.filter(company=request.company, active=True):
        levels = {item.work_type_id: item.level for item in staff.work_skills.select_related("level")}
        staff_rows.append({"staff": staff, "cells": [levels.get(work.id) for work in works]})
    query = request.GET.get("q", "").strip()
    work_filter = request.GET.get("work", "")
    level_filter = request.GET.get("level", "")
    skill_entries = StaffSkill.objects.filter(staff__company=request.company).select_related("staff", "work_type", "level")
    if query:
        skill_entries = skill_entries.filter(
            Q(staff__employee_number__icontains=query) | Q(staff__name__icontains=query)
            | Q(work_type__name__icontains=query) | Q(level__symbol__icontains=query)
            | Q(level__meaning__icontains=query)
        )
    if work_filter.isdigit():
        skill_entries = skill_entries.filter(work_type_id=int(work_filter))
    if level_filter.isdigit():
        skill_entries = skill_entries.filter(level_id=int(level_filter))
    skill_entries = skill_entries.order_by("staff__employee_number", "work_type__display_order", "work_type__id")
    return render(request, "shifts/skill_map.html", {
        "form": form, "works": works, "staff_rows": staff_rows, "skill_entries": skill_entries,
        "query": query, "work_filter": work_filter, "level_filter": level_filter,
        "skill_levels": SkillLevel.objects.filter(company=request.company),
    })


@login_required
@admin_required
def staff_skill_delete(request, pk):
    item = get_object_or_404(StaffSkill.objects.select_related("staff", "work_type"), pk=pk, staff__company=request.company)
    return _delete_confirmation(request, item, f"{item.staff.name}の「{item.work_type.name}」スキル", reverse("skill_map"), "skill_map")


@login_required
@admin_required
@require_POST
def staff_skill_bulk_delete(request):
    selected_ids = request.POST.getlist("skill_ids")
    selected_ids = [int(value) for value in selected_ids if value.isdigit()]
    items = StaffSkill.objects.filter(
        pk__in=selected_ids, staff__company=request.company
    ).select_related("staff", "work_type", "level").order_by("staff__employee_number", "work_type__display_order")
    if not items.exists():
        messages.error(request, "削除するスキルを選択してください。")
        return redirect("skill_map")
    if request.POST.get("confirmed") == "1":
        count = items.count()
        items.delete()
        messages.success(request, f"スタッフスキルを{count}件削除しました。")
        return redirect("skill_map")
    return render(request, "shifts/bulk_delete_confirm.html", {"items": items, "selected_ids": selected_ids})


@login_required
@admin_required
def import_skill_map(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        file = form.cleaned_data["file"]
        try:
            result = ImportSkillMap(SkillMapFileReader(), DjangoMasterRepository()).execute(request.company.id, file.name, file)
        except (SkillMapReadError, ValueError) as exc:
            form.add_error("file", str(exc))
        else:
            ImportJob.objects.create(company=request.company, uploaded_by=request.user, filename=file.name, result=result)
            messages.success(
                request,
                f"スタッフ{result['staff']}件、スキル{result['skills']}件を取り込みました。"
                f"新規ログインアカウントは{result.get('accounts', 0)}件です。",
            )
            return redirect("skill_map")
    return render(request, "shifts/import.html", {"form": form, "jobs": ImportJob.objects.filter(company=request.company).order_by("-created_at")[:10]})


@login_required
@admin_required
def shift_manage(request):
    return render(request, "shifts/shift_manage.html", {
        "form": GenerateForm(initial={"month": _month()}),
        "periods": ShiftPeriod.objects.filter(company=request.company).annotate(assignment_count=Count("assignments")),
    })


@login_required
@admin_required
@require_POST
def generate_shift(request):
    form = GenerateForm(request.POST)
    if not form.is_valid():
        messages.error(request, "対象月を確認してください。"); return redirect("shift_manage")
    month = form.cleaned_data["month"].replace(day=1)
    output = GenerateMonthlyShift(DjangoShiftRepository()).execute(request.company.id, month)
    messages.success(request, f"{output.assignment_count}件を配置しました。警告は{output.warning_count}件です。")
    return redirect("shift_detail", pk=output.period_id)


def _period_rows(period):
    day_count = calendar.monthrange(period.month.year, period.month.month)[1]
    staff_list = Staff.objects.filter(company=period.company, active=True)
    assignment_map = {(item.staff_id, item.day.day): item for item in period.assignments.select_related("work_type")}
    return list(range(1, day_count + 1)), [
        {"staff": staff, "cells": [assignment_map.get((staff.id, day)) for day in range(1, day_count + 1)]}
        for staff in staff_list
    ]


@login_required
@admin_required
def shift_detail(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk, company=request.company)
    days, rows = _period_rows(period)
    return render(request, "shifts/shift_detail.html", {"period": period, "days": days, "rows": rows, "warnings": period.warnings.select_related("work_type")})


@login_required
@admin_required
@require_POST
def publish_shift(request, pk):
    period = get_object_or_404(ShiftPeriod, pk=pk, company=request.company)
    period.status = ShiftPeriod.Status.PUBLISHED; period.published_at = timezone.now()
    period.save(update_fields=["status", "published_at"])
    messages.success(request, "シフトを公開しました。"); return redirect("shift_detail", pk=pk)


@login_required
@admin_required
def shift_delete(request, pk):
    item = get_object_or_404(ShiftPeriod, pk=pk, company=request.company)
    return _delete_confirmation(request, item, f"{item.month:%Y年%m月}のシフト", reverse("shift_detail", kwargs={"pk": pk}), "shift_manage")


@login_required
@staff_required
def submit_availability(request):
    month = _month(request.POST.get("month") or request.GET.get("month"))
    submission, _ = AvailabilitySubmission.objects.get_or_create(staff=request.staff, month=month)
    day_count = calendar.monthrange(month.year, month.month)[1]
    if request.method == "POST":
        for number in range(1, day_count + 1):
            state = request.POST.get(f"day_{number}", "available")
            AvailabilityDay.objects.update_or_create(
                submission=submission, day=month.replace(day=number),
                defaults={"available": state != "unavailable", "preferred_off": state == "off"},
            )
        submission.status = AvailabilitySubmission.Status.SUBMITTED; submission.submitted_at = timezone.now()
        submission.save(update_fields=["status", "submitted_at"])
        messages.success(request, f"{month.year}年{month.month}月分を提出しました。"); return redirect(f"{request.path}?month={month:%Y-%m}")
    saved = {item.day.day: ("off" if item.preferred_off else "available" if item.available else "unavailable") for item in submission.days.all()}
    days = [{"number": number, "date": month.replace(day=number), "state": saved.get(number, "available")} for number in range(1, day_count + 1)]
    return render(request, "shifts/submit.html", {"month": month, "submission": submission, "days": days})


@login_required
@staff_required
def my_shift(request):
    month = _month(request.GET.get("month"))
    period = ShiftPeriod.objects.filter(company=request.company, month=month, status=ShiftPeriod.Status.PUBLISHED).first()
    assignments = period.assignments.filter(staff=request.staff).select_related("work_type") if period else []
    assignment_map = {item.day.day: item for item in assignments}
    day_count = calendar.monthrange(month.year, month.month)[1]
    days = [{"date": month.replace(day=number), "assignment": assignment_map.get(number)} for number in range(1, day_count + 1)]
    return render(request, "shifts/my_shift.html", {"month": month, "period": period, "days": days})


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
