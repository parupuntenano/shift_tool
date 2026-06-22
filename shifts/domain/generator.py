import calendar
from collections import Counter
from datetime import date, timedelta
from typing import Iterable

from .entities import Assignment, Availability, ConstraintRule, GenerationResult, GenerationWarningData, SkillRating, StaffMember, Work


class MonthlyShiftGenerator:
    """設定データだけを受け取り、Django/DBなしで月間シフトを生成する。"""

    def generate(
        self,
        month: date,
        staff: Iterable[StaffMember],
        works: Iterable[Work],
        skills: Iterable[SkillRating],
        availability: Iterable[Availability],
        constraints: Iterable[ConstraintRule] = (),
    ) -> GenerationResult:
        month = month.replace(day=1)
        staff_list = list(staff)
        work_list = sorted(works, key=lambda item: (item.display_order, item.id))
        skill_map = {(item.staff_id, item.work_id): item for item in skills}
        availability_map = {(item.staff_id, item.day): item for item in availability}
        rules = list(constraints)
        total_assignments: Counter[int] = Counter()
        last_work: dict[int, int] = {}
        assigned_by_day: set[tuple[int, date]] = set()
        assignments: list[Assignment] = []
        warnings: list[GenerationWarningData] = []

        for day_number in range(1, calendar.monthrange(month.year, month.month)[1] + 1):
            current = month.replace(day=day_number)
            for work in work_list:
                selected_count = 0
                for _slot in range(work.required_staff_per_day):
                    candidates = []
                    assigned_same_work = {item.staff_id for item in assignments if item.day == current and item.work_id == work.id}
                    for member in staff_list:
                        available = availability_map.get((member.id, current))
                        rating = skill_map.get((member.id, work.id))
                        if not available or not available.available or available.preferred_off:
                            continue
                        if not rating or not rating.assignable or (member.id, current) in assigned_by_day:
                            continue
                        if self._consecutive_days(member.id, current, assignments) >= member.max_consecutive_days:
                            continue
                        allowed, rule_penalty = self._evaluate_rules(member.id, work.id, current, assigned_same_work, assignments, rules)
                        if not allowed:
                            continue
                        repeat_penalty = 1 if last_work.get(member.id) == work.id else 0
                        candidates.append((rating.priority, rule_penalty + repeat_penalty, total_assignments[member.id], member.id, member))
                    candidates.sort(key=lambda item: item[:4])
                    if not candidates:
                        break
                    member = candidates[0][4]
                    assignments.append(Assignment(member.id, work.id, current))
                    assigned_by_day.add((member.id, current))
                    total_assignments[member.id] += 1
                    last_work[member.id] = work.id
                    selected_count += 1
                missing = work.required_staff_per_day - selected_count
                if missing:
                    warnings.append(GenerationWarningData(
                        current, work.id, f"{work.name}の配置が{missing}名不足しています。"
                    ))
        return GenerationResult(tuple(assignments), tuple(warnings))

    @classmethod
    def _evaluate_rules(cls, staff_id, work_id, current, assigned_same_work, assignments, rules):
        penalty = 0
        for rule in rules:
            violated = False
            if rule.operator == "incompatible_same_work":
                pair = {rule.staff_id, rule.related_staff_id}
                if staff_id not in pair:
                    continue
                if rule.work_ids and work_id not in rule.work_ids:
                    continue
                other_staff = rule.related_staff_id if staff_id == rule.staff_id else rule.staff_id
                violated = other_staff in assigned_same_work
            elif rule.staff_id is not None and rule.staff_id != staff_id:
                continue
            elif rule.operator == "work_alternation" and work_id in rule.work_ids:
                previous = cls._last_work_in(staff_id, rule.work_ids, assignments)
                violated = previous == work_id
            elif rule.operator == "avoid_same_work":
                previous = cls._last_work_in(staff_id, (), assignments)
                violated = previous == work_id
            elif rule.operator == "avoid_specific_work" and work_id in rule.work_ids:
                previous = cls._last_work_in(staff_id, (), assignments)
                violated = previous == work_id
            elif rule.operator == "forbid_specific_work":
                violated = work_id in rule.work_ids
            elif rule.operator == "forbid_works_on_weekdays":
                violated = current.weekday() in rule.weekdays and (not rule.work_ids or work_id in rule.work_ids)
            elif rule.operator == "no_single_rest":
                worked = {item.day for item in assignments if item.staff_id == staff_id}
                violated = current - timedelta(days=1) not in worked and current - timedelta(days=2) in worked
            elif rule.operator == "work_rest_pattern":
                pattern = cls._work_rest_pattern(rule.text_value)
                if pattern:
                    violated = not pattern[(current.day - 1) % len(pattern)]
            if violated and rule.is_hard:
                return False, penalty
            if violated:
                penalty += 100
        return True, penalty

    @staticmethod
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

    @staticmethod
    def _last_work_in(staff_id, work_ids, assignments):
        for item in reversed(assignments):
            if item.staff_id == staff_id and (not work_ids or item.work_id in work_ids):
                return item.work_id
        return None

    @staticmethod
    def _consecutive_days(staff_id: int, current: date, assignments: list[Assignment]) -> int:
        worked = {item.day for item in assignments if item.staff_id == staff_id}
        count, target = 0, current - timedelta(days=1)
        while target in worked:
            count += 1
            target -= timedelta(days=1)
        return count
