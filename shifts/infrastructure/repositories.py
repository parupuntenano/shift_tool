import calendar
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from shifts.domain.entities import (
    Availability,
    ConstraintRule,
    GenerationResult,
    PreviousShiftDay,
    SkillRating,
    StaffMember,
    Work,
)
from shifts.domain.rest_patterns import (
    next_pattern_index_from_previous,
    previous_consecutive_work_count,
    previous_work_statuses,
    work_rest_pattern_from_text,
)
from .models import (
    AvailabilityDay,
    AvailabilitySubmission,
    GenerationWarning,
    IndividualConstraint,
    PreviousMonthShiftDay,
    ShiftAssignment,
    ShiftPeriod,
    Staff,
    StaffSkill,
    WorkType,
)


DEFAULT_WORK_REST_PATTERNS = ("1,1", "2,1", "3,1", "4,1", "5,2")


class DjangoShiftRepository:
    def staff_for_generation(self, company_id: int) -> list[StaffMember]:
        limits = {}
        rules = IndividualConstraint.objects.filter(
            Q(kind=IndividualConstraint.Kind.MAX_CONSECUTIVE)
            | Q(rule_type__operator="max_consecutive"),
            company_id=company_id,
            active=True,
        ).exclude(staff=None)
        for rule in rules:
            if rule.strength < 10:
                continue
            limits[rule.staff_id] = int(
                rule.numeric_value or rule.parameters.get("days", 6)
            )
        return [
            StaffMember(row.id, row.name, limits.get(row.id, 6))
            for row in Staff.objects.filter(company_id=company_id, active=True)
        ]

    def works_for_generation(self, company_id: int) -> list[Work]:
        return [
            Work(row.id, row.name, row.required_staff_per_day, row.display_order)
            for row in WorkType.objects.filter(company_id=company_id, active=True)
        ]

    def skills_for_generation(self, company_id: int) -> list[SkillRating]:
        rows = StaffSkill.objects.filter(staff__company_id=company_id).select_related(
            "level"
        )
        return [
            SkillRating(
                row.staff_id,
                row.work_type_id,
                row.level.priority,
                row.level.assignable,
                self._is_instructor_level(row.level),
                self._is_trainee_level(row.level),
            )
            for row in rows
        ]

    @staticmethod
    def _is_instructor_level(level) -> bool:
        text = f"{level.symbol} {level.meaning}".replace(" ", "").replace("　", "")
        return any(word in text for word in ("指導", "教官", "主担当", "リーダー"))

    @staticmethod
    def _is_trainee_level(level) -> bool:
        text = f"{level.symbol} {level.meaning}".replace(" ", "").replace("　", "")
        return any(word in text for word in ("研修", "訓練"))

    def availability_for_generation(
        self, company_id: int, month: date
    ) -> list[Availability]:
        month = month.replace(day=1)
        submitted_rows = list(
            AvailabilityDay.objects.filter(
                submission__staff__company_id=company_id,
                submission__month=month,
                submission__status=AvailabilitySubmission.Status.SUBMITTED,
            ).select_related("submission")
        )
        submitted_staff_ids = {row.submission.staff_id for row in submitted_rows}
        submitted_map = {
            (row.submission.staff_id, row.day): Availability(
                row.submission.staff_id,
                row.day,
                row.available,
                row.preferred_off,
                row.paid_leave,
            )
            for row in submitted_rows
        }

        staff_ids = list(
            Staff.objects.filter(company_id=company_id, active=True).values_list(
                "id", flat=True
            )
        )
        rules = self.rules_for_generation(company_id)
        previous_days = self.previous_shift_days_for_generation(company_id, month)

        result = []
        for staff_id in staff_ids:
            if staff_id in submitted_staff_ids:
                for day in self._days_in_month(month):
                    result.append(
                        submitted_map.get(
                            (staff_id, day),
                            Availability(staff_id, day, True),
                        )
                    )
                continue

            auto_off_days = self._auto_public_holidays_for_unsubmitted_staff(
                staff_id,
                month,
                rules,
                previous_days,
            )
            for day in self._days_in_month(month):
                result.append(
                    Availability(
                        staff_id,
                        day,
                        True,
                        preferred_off=day in auto_off_days,
                    )
                )
        return result

    def _auto_public_holidays_for_unsubmitted_staff(
        self,
        staff_id: int,
        month: date,
        rules: list[ConstraintRule],
        previous_days: list[PreviousShiftDay],
    ) -> set[date]:
        staff_rules = [rule for rule in rules if rule.staff_id == staff_id]
        staff_previous_days = sorted(
            [item for item in previous_days if item.staff_id == staff_id],
            key=lambda item: item.day,
        )
        off_days: set[date] = set()

        pattern_rule = next(
            (
                rule
                for rule in staff_rules
                if rule.operator == "work_rest_pattern" and rule.text_value
            ),
            None,
        )
        if pattern_rule:
            pattern = work_rest_pattern_from_text(pattern_rule.text_value)
            if pattern:
                index = next_pattern_index_from_previous(
                    pattern,
                    previous_work_statuses(staff_previous_days),
                )
                for day in self._days_in_month(month):
                    if not pattern[index % len(pattern)]:
                        off_days.add(day)
                    index += 1

        max_consecutive_candidates = [
            int(rule.numeric_value)
            for rule in staff_rules
            if rule.operator == "max_consecutive" and rule.numeric_value
        ]
        if max_consecutive_candidates:
            max_days = max(1, min(max_consecutive_candidates))
            streak = previous_consecutive_work_count(staff_previous_days)
            for day in self._days_in_month(month):
                if streak >= max_days:
                    off_days.add(day)
                    streak = 0
                else:
                    streak += 1

        has_rest_scheduling_rule = bool(pattern_rule or max_consecutive_candidates)
        if not has_rest_scheduling_rule:
            off_days.update(
                self._weekly_public_holidays_from_previous_or_staff_offset(
                    staff_id,
                    month,
                    staff_previous_days,
                )
            )

        return off_days

    @staticmethod
    def _weekly_public_holidays_from_previous_or_staff_offset(
        staff_id: int,
        month: date,
        previous_days: list[PreviousShiftDay],
    ) -> set[date]:
        day_count = calendar.monthrange(month.year, month.month)[1]
        latest_public_holiday = None
        for item in previous_days:
            if item.status == PreviousMonthShiftDay.Status.PUBLIC_HOLIDAY:
                latest_public_holiday = item.day

        if latest_public_holiday:
            off_days = set()
            current = latest_public_holiday + timedelta(days=7)
            while current < month:
                current += timedelta(days=7)
            while current.month == month.month and current.year == month.year:
                off_days.add(current)
                current += timedelta(days=7)
            return off_days

        first_off_day_number = (staff_id - 1) % 7 + 1
        return {
            month.replace(day=day_number)
            for day_number in range(first_off_day_number, day_count + 1, 7)
        }

    @staticmethod
    def _days_in_month(month: date):
        day_count = calendar.monthrange(month.year, month.month)[1]
        for day_number in range(1, day_count + 1):
            yield month.replace(day=day_number)

    def rules_for_generation(
        self,
        company_id: int,
        *,
        include_default_patterns: bool = False,
    ) -> list[ConstraintRule]:
        rows = IndividualConstraint.objects.filter(
            company_id=company_id, active=True, rule_type__isnull=False
        ).select_related("rule_type", "related_staff", "work_type_a", "work_type_b")
        result = []
        for row in rows:
            operator = row.rule_type.operator
            if operator == "custom":
                continue
            works = [item for item in (row.work_type_a, row.work_type_b) if item]
            result.append(
                ConstraintRule(
                    operator=operator,
                    staff_id=row.staff_id,
                    related_staff_id=row.related_staff_id,
                    work_ids=tuple(work.id for work in works),
                    numeric_value=row.numeric_value,
                    text_value=row.text_value,
                    weekdays=tuple(int(value) for value in row.weekdays),
                    is_hard=row.is_hard,
                    strength=row.strength,
                    name=row.name,
                    rule_type_name=row.rule_type.name,
                    related_staff_name=(
                        row.related_staff.name if row.related_staff else ""
                    ),
                    work_names=tuple(work.name for work in works),
                )
            )
        if include_default_patterns:
            self._append_default_work_rest_pattern_rules(company_id, result)
        return result

    @staticmethod
    def _append_default_work_rest_pattern_rules(
        company_id: int,
        rules: list[ConstraintRule],
    ):
        staff_ids_with_pattern = {
            rule.staff_id
            for rule in rules
            if rule.staff_id and rule.operator == "work_rest_pattern"
        }
        staff_ids = Staff.objects.filter(company_id=company_id, active=True).values_list(
            "id", flat=True
        )
        for staff_id in staff_ids:
            if staff_id in staff_ids_with_pattern:
                continue
            for pattern in DEFAULT_WORK_REST_PATTERNS:
                rules.append(
                    ConstraintRule(
                        operator="work_rest_pattern",
                        staff_id=staff_id,
                        text_value=pattern,
                        is_hard=False,
                        strength=4,
                        name="標準勤務候補",
                        rule_type_name="勤休パターン",
                    )
                )

    def previous_shift_days_for_generation(
        self, company_id: int, month: date
    ) -> list[PreviousShiftDay]:
        previous_month = self._add_months(month.replace(day=1), -1)
        period = (
            ShiftPeriod.objects.filter(
                company_id=company_id,
                month=previous_month,
                assignments__isnull=False,
            )
            .distinct()
            .first()
        )
        if period:
            return self._previous_shift_days_from_period(company_id, period)

        rows = PreviousMonthShiftDay.objects.filter(
            company_id=company_id,
            day__year=previous_month.year,
            day__month=previous_month.month,
        )
        return [
            PreviousShiftDay(
                row.staff_id,
                row.day,
                row.status,
                row.work_type_id,
            )
            for row in rows
        ]

    def _previous_shift_days_from_period(
        self, company_id: int, period: ShiftPeriod
    ) -> list[PreviousShiftDay]:
        last_day_number = calendar.monthrange(period.month.year, period.month.month)[1]
        first_day = period.month.replace(day=last_day_number - 6)
        last_day = period.month.replace(day=last_day_number)
        staff_ids = list(
            Staff.objects.filter(company_id=company_id, active=True).values_list(
                "id", flat=True
            )
        )
        assignment_map = {
            (item.staff_id, item.day): item.work_type_id
            for item in ShiftAssignment.objects.filter(
                period=period,
                day__range=(first_day, last_day),
            )
        }
        paid_leave_days = {
            (item.submission.staff_id, item.day)
            for item in AvailabilityDay.objects.filter(
                submission__staff__company_id=company_id,
                submission__month=period.month,
                submission__status=AvailabilitySubmission.Status.SUBMITTED,
                paid_leave=True,
                day__range=(first_day, last_day),
            ).select_related("submission")
        }
        result = []
        for staff_id in staff_ids:
            for day_number in range(first_day.day, last_day.day + 1):
                day = period.month.replace(day=day_number)
                work_id = assignment_map.get((staff_id, day))
                if work_id:
                    result.append(
                        PreviousShiftDay(
                            staff_id,
                            day,
                            PreviousMonthShiftDay.Status.WORK,
                            work_id,
                        )
                    )
                elif (staff_id, day) in paid_leave_days:
                    result.append(
                        PreviousShiftDay(
                            staff_id,
                            day,
                            PreviousMonthShiftDay.Status.PAID_LEAVE,
                        )
                    )
                else:
                    result.append(
                        PreviousShiftDay(
                            staff_id,
                            day,
                            PreviousMonthShiftDay.Status.PUBLIC_HOLIDAY,
                        )
                    )
        return result

    @staticmethod
    def _add_months(month: date, offset: int) -> date:
        year = month.year + (month.month - 1 + offset) // 12
        month_number = (month.month - 1 + offset) % 12 + 1
        return month.replace(year=year, month=month_number, day=1)

    @transaction.atomic
    def save_generation(
        self, company_id: int, month: date, result: GenerationResult
    ) -> int:
        period, _ = ShiftPeriod.objects.get_or_create(
            company_id=company_id, month=month.replace(day=1)
        )
        period.assignments.all().delete()
        period.warnings.all().delete()
        ShiftAssignment.objects.bulk_create(
            [
                ShiftAssignment(
                    period=period,
                    staff_id=item.staff_id,
                    work_type_id=item.work_id,
                    day=item.day,
                )
                for item in result.assignments
            ]
        )
        GenerationWarning.objects.bulk_create(
            [
                GenerationWarning(
                    period=period,
                    day=item.day,
                    work_type_id=item.work_id,
                    message=item.message,
                )
                for item in result.warnings
            ]
        )
        period.status = ShiftPeriod.Status.DRAFT
        period.warning_count = len(result.warnings)
        period.generated_at = timezone.now()
        period.save(update_fields=["status", "warning_count", "generated_at"])
        return period.id
