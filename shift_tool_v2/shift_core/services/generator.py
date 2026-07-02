from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import transaction

from shift_core.models import (
    PreviousShiftRecord,
    ShiftAssignment,
    ShiftPeriod,
    ShiftWarning,
    Staff,
    StaffSkill,
    WorkType,
)


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
    previous_work_streaks = _previous_work_streaks(staff_list, month)

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
            rest_staff_ids = _choose_public_holidays(
                current,
                staff_list,
                public_holiday_counts,
                assigned_days,
                previous_work_streaks,
            )

            for work in works:
                for _slot in range(work.required_staff_per_day):
                    candidate = _best_candidate(
                        current,
                        work,
                        staff_list,
                        skill_map,
                        assigned_days,
                        rest_staff_ids,
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
                staff_list,
                assigned_days,
                public_holiday_counts,
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
                assignments,
            )

            for staff in staff_list:
                if (staff.id, current) in assigned_days:
                    continue
                if staff.id in final_rest_staff_ids:
                    status = ShiftAssignment.Status.PUBLIC_HOLIDAY
                    public_holiday_counts[staff.id] += 1
                else:
                    status = ShiftAssignment.Status.STANDBY
                assignments.append(
                    ShiftAssignment(period=period, staff=staff, day=current, status=status)
                )

        ShiftAssignment.objects.bulk_create(assignments)
        ShiftWarning.objects.bulk_create(warnings)
        _add_balance_warnings(period, staff_list)
        return period


def _days_in_month(month: date):
    for day in range(1, calendar.monthrange(month.year, month.month)[1] + 1):
        yield month.replace(day=day)


def _choose_public_holidays(current, staff_list, public_holiday_counts, assigned_days, previous_work_streaks):
    week_start = current - timedelta(days=(current.weekday() + 1) % 7)
    candidates = sorted(
        staff_list,
        key=lambda staff: (
            (
                previous_work_streaks[staff.id]
                + _worked_in_week(staff.id, week_start, current, assigned_days)
            )
            < 5,
            public_holiday_counts[staff.id] - staff.public_holiday_count,
            staff.employee_number,
        ),
    )
    return {staff.id for staff in candidates[:_target_daily_rest_count(staff_list)]}


def _final_public_holiday_staff_ids(current, staff_list, assigned_days, public_holiday_counts):
    unassigned_staff = [
        staff for staff in staff_list if (staff.id, current) not in assigned_days
    ]
    candidates = sorted(
        unassigned_staff,
        key=lambda staff: (
            public_holiday_counts[staff.id] - staff.public_holiday_count,
            staff.employee_number,
        ),
    )
    return {staff.id for staff in candidates[:_target_daily_rest_count(staff_list)]}


def _target_daily_rest_count(staff_list):
    return max(1, round(len(staff_list) * 0.2))


def _best_candidate(
    current,
    work,
    staff_list,
    skill_map,
    assigned_days,
    rest_staff_ids,
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
        rest_penalty = 500 if staff.id in rest_staff_ids else 0
        candidates.append(
            Candidate(
                score=(
                    rest_penalty
                    + total_work_counts[staff.id] * 20
                    + work_counts[(staff.id, work.id)] * 12
                    + skill.level.priority * 10
                ),
                staff=staff,
                skill=skill,
            )
        )
    return min(candidates, key=lambda item: (item.score, item.staff.employee_number), default=None)


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


def _best_trainee(current, work, staff_list, skill_map, assigned_days, rest_staff_ids, total_work_counts, work_counts):
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
        candidates.append(
            Candidate(
                score=(500 if staff.id in rest_staff_ids else 0) + total_work_counts[staff.id] * 20 + work_counts[(staff.id, work.id)] * 12,
                staff=staff,
                skill=skill,
            )
        )
    return min(candidates, key=lambda item: (item.score, item.staff.employee_number), default=None)


def _worked_in_week(staff_id, week_start, current, assigned_days):
    return sum(1 for _staff_id, day in assigned_days if _staff_id == staff_id and week_start <= day < current)


def _previous_work_streaks(staff_list, month):
    result = Counter()
    previous_by_staff = defaultdict(list)
    for record in PreviousShiftRecord.objects.filter(day__lt=month).order_by("staff_id", "-day"):
        previous_by_staff[record.staff_id].append(record)
    for staff in staff_list:
        expected_day = month - timedelta(days=1)
        for record in previous_by_staff[staff.id]:
            if record.day != expected_day:
                break
            if record.status not in {
                PreviousShiftRecord.Status.WORK,
                PreviousShiftRecord.Status.STANDBY,
            }:
                break
            result[staff.id] += 1
            expected_day -= timedelta(days=1)
    return result


def _add_balance_warnings(period, staff_list):
    days_in_month = calendar.monthrange(period.month.year, period.month.month)[1]
    rows = period.assignments.values("staff_id", "status")
    counts = defaultdict(Counter)
    for row in rows:
        counts[row["staff_id"]][row["status"]] += 1
    warnings = []
    for staff in staff_list:
        public_holidays = counts[staff.id][ShiftAssignment.Status.PUBLIC_HOLIDAY]
        total = sum(counts[staff.id].values())
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
