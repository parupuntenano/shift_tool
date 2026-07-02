from __future__ import annotations

import calendar
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import transaction

from shift_core.models import (
    PreviousShiftRecord,
    ShiftAssignment,
    ShiftPeriod,
    ShiftRequest,
    ShiftWarning,
    Staff,
    StaffSkill,
    WorkType,
)


WEEK_START_WEEKDAY = 5  # 土曜始まり版：土曜日から金曜日までを1週間として扱う。
MIN_PUBLIC_HOLIDAYS_PER_WEEK = 2


@dataclass
class Candidate:
    score: int
    staff: Staff
    skill: StaffSkill


def generate_monthly_shift(month: date) -> ShiftPeriod:
    month = month.replace(day=1)
    staff_list = list(Staff.objects.filter(is_active=True).order_by("employee_number"))
    works = list(WorkType.objects.filter(active=True).order_by("display_order", "name"))
    skill_map = {
        (skill.staff_id, skill.work_type_id): skill
        for skill in StaffSkill.objects.select_related("level", "staff", "work_type")
    }
    weekly_public_holiday_counts = previous_weekly_public_holiday_counts(staff_list, month)
    month_last_day = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    request_map = {
        (request.staff_id, request.day): request
        for request in ShiftRequest.objects.filter(
            staff__in=staff_list,
            day__range=(month, month_last_day),
        )
    }

    with transaction.atomic():
        period, _created = ShiftPeriod.objects.update_or_create(
            month=month,
            defaults={"status": ShiftPeriod.Status.DRAFT},
        )
        period.assignments.all().delete()
        period.warnings.all().delete()

        total_work_counts = Counter()
        work_counts = Counter()
        public_holiday_counts = Counter()
        assigned_days: set[tuple[int, date]] = set()
        assignments: list[ShiftAssignment] = []
        warnings: list[ShiftWarning] = []

        for current in _days_in_month(month):
            daily_work_staff: dict[int, list[int]] = defaultdict(list)
            must_rest_staff_ids = _must_rest_staff_ids(
                current,
                month,
                staff_list,
                weekly_public_holiday_counts,
            )

            for work in works:
                for _slot in range(work.required_staff_per_day):
                    candidate = _best_candidate(
                        current,
                        work,
                        staff_list,
                        skill_map,
                        assigned_days,
                        must_rest_staff_ids,
                        request_map,
                        total_work_counts,
                        work_counts,
                    )
                    if not candidate:
                        warnings.append(
                            ShiftWarning(
                                period=period,
                                day=current,
                                work_type=work,
                                message=f"{work.name}の必要人数を満たせませんでした。",
                            )
                        )
                        continue
                    assignments.append(
                        ShiftAssignment(
                            period=period,
                            staff=candidate.staff,
                            day=current,
                            status=ShiftAssignment.Status.WORK,
                            work_type=work,
                        )
                    )
                    assigned_days.add((candidate.staff.id, current))
                    daily_work_staff[work.id].append(candidate.staff.id)
                    total_work_counts[candidate.staff.id] += 1
                    work_counts[(candidate.staff.id, work.id)] += 1

            final_rest_staff_ids = _final_public_holiday_staff_ids(
                current,
                month,
                staff_list,
                assigned_days,
                public_holiday_counts,
                weekly_public_holiday_counts,
                must_rest_staff_ids,
                request_map,
            )

            _assign_trainees(
                period,
                current,
                staff_list,
                works,
                skill_map,
                assigned_days,
                final_rest_staff_ids,
                daily_work_staff,
                total_work_counts,
                work_counts,
                request_map,
                assignments,
            )

            for staff in staff_list:
                if (staff.id, current) in assigned_days:
                    continue
                request = request_map.get((staff.id, current))
                if request and request.kind == ShiftRequest.Kind.PAID_LEAVE:
                    status = ShiftAssignment.Status.PAID_LEAVE
                elif staff.id in final_rest_staff_ids:
                    status = ShiftAssignment.Status.PUBLIC_HOLIDAY
                    public_holiday_counts[staff.id] += 1
                    weekly_public_holiday_counts[(week_start(current), staff.id)] += 1
                else:
                    status = ShiftAssignment.Status.STANDBY
                assignments.append(
                    ShiftAssignment(period=period, staff=staff, day=current, status=status)
                )

        ShiftAssignment.objects.bulk_create(assignments)
        ShiftWarning.objects.bulk_create(warnings)
        _add_balance_warnings(period, staff_list)
        _add_request_warnings(period)
        return period


def _days_in_month(month: date):
    for day in range(1, calendar.monthrange(month.year, month.month)[1] + 1):
        yield month.replace(day=day)


def week_start(day: date) -> date:
    return day - timedelta(days=(day.weekday() - WEEK_START_WEEKDAY) % 7)


def week_ranges_in_month(month: date):
    current = week_start(month)
    last_day = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    while current <= last_day:
        yield current, current + timedelta(days=6)
        current += timedelta(days=7)


def _remaining_days_in_week(current: date, month: date) -> int:
    last_day = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    week_end = min(week_start(current) + timedelta(days=6), last_day)
    return (week_end - current).days + 1


def _must_rest_staff_ids(current, month, staff_list, weekly_public_holiday_counts):
    current_week_start = week_start(current)
    remaining_days = _remaining_days_in_week(current, month)
    result = set()
    for staff in staff_list:
        current_count = weekly_public_holiday_counts[(current_week_start, staff.id)]
        missing_count = max(0, MIN_PUBLIC_HOLIDAYS_PER_WEEK - current_count)
        if missing_count and missing_count >= remaining_days:
            result.add(staff.id)
    return result


def _final_public_holiday_staff_ids(
    current,
    month,
    staff_list,
    assigned_days,
    public_holiday_counts,
    weekly_public_holiday_counts,
    must_rest_staff_ids,
    request_map,
):
    current_week_start = week_start(current)
    unassigned_staff = [
        staff for staff in staff_list if (staff.id, current) not in assigned_days
    ]
    candidates = sorted(
        unassigned_staff,
        key=lambda staff: (
            _request_rest_rank(request_map.get((staff.id, current))),
            staff.id not in must_rest_staff_ids,
            weekly_public_holiday_counts[(current_week_start, staff.id)] >= MIN_PUBLIC_HOLIDAYS_PER_WEEK,
            public_holiday_counts[staff.id] - staff.public_holiday_count,
            staff.employee_number,
        ),
    )
    mandatory_count = sum(1 for staff in unassigned_staff if staff.id in must_rest_staff_ids)
    rest_count = max(_target_daily_rest_count(staff_list), mandatory_count)
    return {staff.id for staff in candidates[:rest_count]}


def _target_daily_rest_count(staff_list):
    weekly_required_rest_count = len(staff_list) * MIN_PUBLIC_HOLIDAYS_PER_WEEK
    return max(1, math.ceil(weekly_required_rest_count / 7))


def _request_rest_rank(request):
    if not request:
        return 2
    if request.kind == ShiftRequest.Kind.PUBLIC_HOLIDAY:
        return 0
    if request.kind in {ShiftRequest.Kind.PAID_LEAVE, ShiftRequest.Kind.UNAVAILABLE}:
        return 3
    return 2


def _best_candidate(
    current,
    work,
    staff_list,
    skill_map,
    assigned_days,
    must_rest_staff_ids,
    request_map,
    total_work_counts,
    work_counts,
):
    candidates = []
    for staff in staff_list:
        skill = skill_map.get((staff.id, work.id))
        if (
            not skill
            or not skill.level.assignable
            or skill.level.trainee
            or (staff.id, current) in assigned_days
        ):
            continue
        request = request_map.get((staff.id, current))
        if request and request.kind in {
            ShiftRequest.Kind.PAID_LEAVE,
            ShiftRequest.Kind.UNAVAILABLE,
        }:
            continue
        rest_penalty = 500 if staff.id in must_rest_staff_ids else 0
        request_penalty = (
            700
            if request and request.kind == ShiftRequest.Kind.PUBLIC_HOLIDAY
            else 0
        )
        candidates.append(
            Candidate(
                score=(
                    rest_penalty
                    + request_penalty
                    + total_work_counts[staff.id] * 20
                    + work_counts[(staff.id, work.id)] * 12
                    + skill.level.priority * 10
                ),
                staff=staff,
                skill=skill,
            )
        )
    preferred_candidates = [
        candidate for candidate in candidates if candidate.staff.id not in must_rest_staff_ids
    ]
    pool = preferred_candidates or candidates
    return min(pool, key=lambda item: (item.score, item.staff.employee_number), default=None)


def _assign_trainees(
    period,
    current,
    staff_list,
    works,
    skill_map,
    assigned_days,
    rest_staff_ids,
    daily_work_staff,
    total_work_counts,
    work_counts,
    request_map,
    assignments,
):
    for work in works:
        instructor_count = sum(
            1
            for staff_id in daily_work_staff[work.id]
            if skill_map.get((staff_id, work.id)) and skill_map[(staff_id, work.id)].level.instructor
        )
        trainee_slots = instructor_count
        while trainee_slots > 0:
            trainee = _best_trainee(
                current,
                work,
                staff_list,
                skill_map,
                assigned_days,
                rest_staff_ids,
                request_map,
                total_work_counts,
                work_counts,
            )
            if not trainee:
                break
            assignments.append(
                ShiftAssignment(
                    period=period,
                    staff=trainee.staff,
                    day=current,
                    status=ShiftAssignment.Status.WORK,
                    work_type=work,
                )
            )
            assigned_days.add((trainee.staff.id, current))
            total_work_counts[trainee.staff.id] += 1
            work_counts[(trainee.staff.id, work.id)] += 1
            trainee_slots -= 1


def _best_trainee(
    current,
    work,
    staff_list,
    skill_map,
    assigned_days,
    rest_staff_ids,
    request_map,
    total_work_counts,
    work_counts,
):
    candidates = []
    for staff in staff_list:
        skill = skill_map.get((staff.id, work.id))
        if (
            not skill
            or not skill.level.assignable
            or not skill.level.trainee
            or staff.id in rest_staff_ids
            or (staff.id, current) in assigned_days
        ):
            continue
        request = request_map.get((staff.id, current))
        if request and request.kind in {
            ShiftRequest.Kind.PUBLIC_HOLIDAY,
            ShiftRequest.Kind.PAID_LEAVE,
            ShiftRequest.Kind.UNAVAILABLE,
        }:
            continue
        candidates.append(
            Candidate(
                score=(500 if staff.id in rest_staff_ids else 0) + total_work_counts[staff.id] * 20 + work_counts[(staff.id, work.id)] * 12,
                staff=staff,
                skill=skill,
            )
        )
    return min(candidates, key=lambda item: (item.score, item.staff.employee_number), default=None)


def previous_weekly_public_holiday_counts(staff_list, month):
    counts = Counter()
    staff_ids = [staff.id for staff in staff_list]
    if not staff_ids:
        return counts
    for record in PreviousShiftRecord.objects.filter(
        staff_id__in=staff_ids,
        day__gte=week_start(month),
        day__lt=month,
        status=PreviousShiftRecord.Status.PUBLIC_HOLIDAY,
    ):
        counts[(week_start(record.day), record.staff_id)] += 1
    return counts


def build_required_staff_warnings(period):
    works = list(WorkType.objects.filter(active=True))
    skill_map = {
        (skill.staff_id, skill.work_type_id): skill
        for skill in StaffSkill.objects.select_related("level").filter(work_type__in=works)
    }
    work_counts = Counter()
    for assignment in period.assignments.filter(
        status=ShiftAssignment.Status.WORK,
        work_type__in=works,
    ).values("day", "staff_id", "work_type_id"):
        skill = skill_map.get((assignment["staff_id"], assignment["work_type_id"]))
        if skill and skill.level.trainee:
            continue
        work_counts[(assignment["day"], assignment["work_type_id"])] += 1

    warnings = []
    for day in _days_in_month(period.month):
        for work in works:
            count = work_counts[(day, work.id)]
            if count < work.required_staff_per_day:
                warnings.append(
                    ShiftWarning(
                        period=period,
                        day=day,
                        work_type=work,
                        message=f"{day:%m/%d} {work.name}が必要人数{work.required_staff_per_day}人に対して{count}人です。",
                    )
                )
    return warnings


def build_request_warnings(period):
    request_map = {
        (request.staff_id, request.day): request
        for request in ShiftRequest.objects.filter(
            day__gte=period.month,
            day__lte=period.month.replace(
                day=calendar.monthrange(period.month.year, period.month.month)[1]
            ),
        )
    }
    warnings = []
    for assignment in period.assignments.select_related("staff", "work_type"):
        request = request_map.get((assignment.staff_id, assignment.day))
        if not request:
            continue
        if (
            request.kind == ShiftRequest.Kind.PUBLIC_HOLIDAY
            and assignment.status != ShiftAssignment.Status.PUBLIC_HOLIDAY
        ):
            warnings.append(
                ShiftWarning(
                    period=period,
                    day=assignment.day,
                    staff=assignment.staff,
                    work_type=assignment.work_type,
                    message=f"{assignment.staff.name}の公休希望日に{_assignment_label(assignment)}が入っています。",
                )
            )
        elif (
            request.kind == ShiftRequest.Kind.PAID_LEAVE
            and assignment.status != ShiftAssignment.Status.PAID_LEAVE
        ):
            warnings.append(
                ShiftWarning(
                    period=period,
                    day=assignment.day,
                    staff=assignment.staff,
                    work_type=assignment.work_type,
                    message=f"{assignment.staff.name}の有休希望日に{_assignment_label(assignment)}が入っています。",
                )
            )
        elif (
            request.kind == ShiftRequest.Kind.UNAVAILABLE
            and assignment.status == ShiftAssignment.Status.WORK
        ):
            warnings.append(
                ShiftWarning(
                    period=period,
                    day=assignment.day,
                    staff=assignment.staff,
                    work_type=assignment.work_type,
                    message=f"{assignment.staff.name}の勤務不可日に{_assignment_label(assignment)}が入っています。",
                )
            )
    return warnings


def _add_request_warnings(period):
    ShiftWarning.objects.bulk_create(build_request_warnings(period))


def _assignment_label(assignment):
    if assignment.status == ShiftAssignment.Status.WORK:
        return assignment.work_type.name if assignment.work_type else "勤務"
    return assignment.get_status_display()


def _add_balance_warnings(period, staff_list):
    days_in_month = calendar.monthrange(period.month.year, period.month.month)[1]
    rows = period.assignments.values("staff_id", "status", "day")
    counts = defaultdict(Counter)
    weekly_public_holiday_counts = previous_weekly_public_holiday_counts(staff_list, period.month)
    for row in rows:
        counts[row["staff_id"]][row["status"]] += 1
        if row["status"] == ShiftAssignment.Status.PUBLIC_HOLIDAY:
            weekly_public_holiday_counts[(week_start(row["day"]), row["staff_id"])] += 1
    warnings = build_required_staff_warnings(period)
    for staff in staff_list:
        public_holidays = counts[staff.id][ShiftAssignment.Status.PUBLIC_HOLIDAY]
        total = sum(counts[staff.id].values())
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
        if public_holidays != staff.public_holiday_count:
            warnings.append(
                ShiftWarning(
                    period=period,
                    staff=staff,
                    message=f"{staff.name}の公休数が設定{staff.public_holiday_count}日に対して{public_holidays}日です。",
                )
            )
        if total != days_in_month:
            warnings.append(
                ShiftWarning(
                    period=period,
                    staff=staff,
                    message=f"{staff.name}の月合計が{total}日です。月日数{days_in_month}日と一致しません。",
                )
            )
    ShiftWarning.objects.bulk_create(warnings)
