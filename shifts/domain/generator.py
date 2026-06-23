import calendar
from collections import Counter
from datetime import date, timedelta
from typing import Iterable

from .entities import (
    Assignment,
    Availability,
    ConstraintRule,
    GenerationResult,
    GenerationWarningData,
    SkillRating,
    StaffMember,
    Work,
)


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
        assignable_work_counts: Counter[int] = Counter(
            item.staff_id for item in skills if item.assignable
        )
        eligible_staff_counts: Counter[int] = Counter(
            item.work_id for item in skills if item.assignable
        )
        availability_map = {(item.staff_id, item.day): item for item in availability}
        rules = list(constraints)
        total_assignments: Counter[int] = Counter()
        work_assignments: Counter[tuple[int, int]] = Counter()
        last_work: dict[int, int] = {}
        assigned_by_day: set[tuple[int, date]] = set()
        assignments: list[Assignment] = []
        warnings: list[GenerationWarningData] = []

        for day_number in range(1, calendar.monthrange(month.year, month.month)[1] + 1):
            current = month.replace(day=day_number)
            remaining_slots = {
                work.id: max(1, work.required_staff_per_day) for work in work_list
            }
            work_by_id = {work.id: work for work in work_list}
            uncovered_work_ids = set(remaining_slots)

            while uncovered_work_ids:
                assigned_coverage = False

                for work_id in sorted(
                    uncovered_work_ids,
                    key=lambda item: (
                        self._eligible_count(
                            work_by_id[item],
                            staff_list,
                            skill_map,
                            availability_map,
                            assigned_by_day,
                            current,
                            assignments,
                            rules,
                        ),
                        work_by_id[item].display_order,
                        work_by_id[item].id,
                    ),
                ):
                    work = work_by_id[work_id]
                    work_candidates = self._work_candidates(
                        work,
                        staff_list,
                        skill_map,
                        availability_map,
                        assigned_by_day,
                        current,
                        assignments,
                        rules,
                        last_work,
                        total_assignments,
                        work_assignments,
                        assignable_work_counts,
                        eligible_staff_counts,
                    )

                    if not work_candidates:
                        missing = remaining_slots.pop(work.id)
                        uncovered_work_ids.remove(work.id)
                        warnings.append(
                            GenerationWarningData(
                                current,
                                work.id,
                                f"{work.name}に最低1名を配置できません。{missing}名不足しています。",
                            )
                        )
                        assigned_coverage = True
                        break

                    self._assign_candidate(
                        work_candidates[0],
                        current,
                        assignments,
                        assigned_by_day,
                        total_assignments,
                        work_assignments,
                        last_work,
                        remaining_slots,
                    )
                    uncovered_work_ids.remove(work.id)
                    assigned_coverage = True
                    break

                if not assigned_coverage:
                    break

            while remaining_slots:
                best_candidate = None
                exhausted_work_ids = []

                for work_id in sorted(
                    remaining_slots,
                    key=lambda item: (
                        self._eligible_count(
                            work_by_id[item],
                            staff_list,
                            skill_map,
                            availability_map,
                            assigned_by_day,
                            current,
                            assignments,
                            rules,
                        ),
                        work_by_id[item].display_order,
                        work_by_id[item].id,
                    ),
                ):
                    work = work_by_id[work_id]
                    work_candidates = self._work_candidates(
                        work,
                        staff_list,
                        skill_map,
                        availability_map,
                        assigned_by_day,
                        current,
                        assignments,
                        rules,
                        last_work,
                        total_assignments,
                        work_assignments,
                        assignable_work_counts,
                        eligible_staff_counts,
                    )

                    if not work_candidates:
                        exhausted_work_ids.append(work.id)
                        continue

                    work_candidates.sort(key=lambda item: item[:4])
                    candidate = work_candidates[0]
                    if best_candidate is None or candidate[:4] < best_candidate[:4]:
                        best_candidate = candidate

                if best_candidate is None:
                    for work_id, missing in list(remaining_slots.items()):
                        work = work_by_id[work_id]
                        warnings.append(
                            GenerationWarningData(
                                current,
                                work.id,
                                f"{work.name}の配置が{missing}名不足しています。",
                            )
                        )
                    remaining_slots = {}
                    break

                for work_id in exhausted_work_ids:
                    if work_id in remaining_slots:
                        work = work_by_id[work_id]
                        missing = remaining_slots.pop(work_id)
                        warnings.append(
                            GenerationWarningData(
                                current,
                                work.id,
                                f"{work.name}の配置が{missing}名不足しています。",
                            )
                        )

                if not remaining_slots:
                    break

                _score, _total_count, _work_count, _member_id, work, member = (
                    best_candidate
                )
                self._assign_candidate(
                    best_candidate,
                    current,
                    assignments,
                    assigned_by_day,
                    total_assignments,
                    work_assignments,
                    last_work,
                    remaining_slots,
                )

            for work_id, missing in remaining_slots.items():
                if missing:
                    work = work_by_id[work_id]
                    warnings.append(
                        GenerationWarningData(
                            current,
                            work.id,
                            f"{work.name}の配置が{missing}名不足しています。",
                        )
                    )
        return GenerationResult(tuple(assignments), tuple(warnings))

    @classmethod
    def _work_candidates(
        cls,
        work,
        staff_list,
        skill_map,
        availability_map,
        assigned_by_day,
        current,
        assignments,
        rules,
        last_work,
        total_assignments,
        work_assignments,
        assignable_work_counts,
        eligible_staff_counts,
    ):
        assigned_same_work = {
            item.staff_id
            for item in assignments
            if item.day == current and item.work_id == work.id
        }
        candidates = []
        for member in staff_list:
            available = availability_map.get((member.id, current))
            rating = skill_map.get((member.id, work.id))
            if not available or not available.available or available.preferred_off:
                continue
            if (
                not rating
                or not rating.assignable
                or (member.id, current) in assigned_by_day
            ):
                continue
            if (
                cls._consecutive_days(member.id, current, assignments)
                >= member.max_consecutive_days
            ):
                continue
            allowed, rule_penalty = cls._evaluate_rules(
                member.id,
                work.id,
                current,
                assigned_same_work,
                assignments,
                rules,
            )
            if not allowed:
                continue
            score = (
                rule_penalty
                + cls._alternation_bonus(member.id, work.id, assignments, rules)
                + (45 if last_work.get(member.id) == work.id else 0)
                + (cls._effective_priority(rating) * 12)
                + (total_assignments[member.id] * 22)
                + (work_assignments[(member.id, work.id)] * 16)
                + (assignable_work_counts[member.id] * 5)
                - (max(0, 10 - eligible_staff_counts[work.id]) * 10)
            )
            candidates.append(
                (
                    score,
                    total_assignments[member.id],
                    work_assignments[(member.id, work.id)],
                    member.id,
                    work,
                    member,
                )
            )
        candidates.sort(key=lambda item: item[:4])
        return candidates

    @staticmethod
    def _assign_candidate(
        candidate,
        current,
        assignments,
        assigned_by_day,
        total_assignments,
        work_assignments,
        last_work,
        remaining_slots,
    ):
        _score, _total_count, _work_count, _member_id, work, member = candidate
        assignments.append(Assignment(member.id, work.id, current))
        assigned_by_day.add((member.id, current))
        total_assignments[member.id] += 1
        work_assignments[(member.id, work.id)] += 1
        last_work[member.id] = work.id
        remaining_slots[work.id] -= 1
        if remaining_slots[work.id] <= 0:
            del remaining_slots[work.id]

    @classmethod
    def _evaluate_rules(
        cls, staff_id, work_id, current, assigned_same_work, assignments, rules
    ):
        penalty = 0
        for rule in rules:
            violated = False
            if rule.operator == "incompatible_same_work":
                pair = {rule.staff_id, rule.related_staff_id}
                if staff_id not in pair:
                    continue
                if rule.work_ids and work_id not in rule.work_ids:
                    continue
                other_staff = (
                    rule.related_staff_id
                    if staff_id == rule.staff_id
                    else rule.staff_id
                )
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
                violated = current.weekday() in rule.weekdays and (
                    not rule.work_ids or work_id in rule.work_ids
                )
            elif rule.operator == "no_single_rest":
                worked = {item.day for item in assignments if item.staff_id == staff_id}
                violated = (
                    current - timedelta(days=1) not in worked
                    and current - timedelta(days=2) in worked
                )
            elif rule.operator == "work_rest_pattern":
                pattern = cls._work_rest_pattern(rule.text_value)
                if pattern:
                    violated = not pattern[(current.day - 1) % len(pattern)]
            if violated and rule.is_hard:
                return False, penalty
            if violated:
                penalty += cls._soft_penalty(rule.operator)
        return True, penalty

    @staticmethod
    def _soft_penalty(operator: str) -> int:
        if operator == "work_rest_pattern":
            return 20
        if operator == "work_alternation":
            return 80
        if operator in {"avoid_same_work", "avoid_specific_work"}:
            return 60
        if operator == "no_single_rest":
            return 70
        return 100

    @classmethod
    def _eligible_count(
        cls,
        work,
        staff_list,
        skill_map,
        availability_map,
        assigned_by_day,
        current,
        assignments,
        rules,
    ) -> int:
        count = 0
        assigned_same_work = {
            item.staff_id
            for item in assignments
            if item.day == current and item.work_id == work.id
        }
        for member in staff_list:
            available = availability_map.get((member.id, current))
            rating = skill_map.get((member.id, work.id))
            if (
                not available
                or not available.available
                or available.preferred_off
                or not rating
                or not rating.assignable
                or (member.id, current) in assigned_by_day
            ):
                continue
            if (
                cls._consecutive_days(member.id, current, assignments)
                >= member.max_consecutive_days
            ):
                continue
            allowed, _penalty = cls._evaluate_rules(
                member.id, work.id, current, assigned_same_work, assignments, rules
            )
            if allowed:
                count += 1
        return count

    @classmethod
    def _alternation_bonus(cls, staff_id, work_id, assignments, rules) -> int:
        bonus = 0
        for rule in rules:
            if (
                rule.operator != "work_alternation"
                or rule.staff_id != staff_id
                or work_id not in rule.work_ids
            ):
                continue
            previous = cls._last_work_in(staff_id, rule.work_ids, assignments)
            if previous and previous != work_id:
                bonus -= 80 if rule.is_hard else 55
        return bonus

    @staticmethod
    def _effective_priority(rating: SkillRating) -> int:
        if rating.assignable and rating.priority >= 90:
            return 4
        return rating.priority

    @staticmethod
    def _work_rest_pattern(value):
        try:
            counts = [
                int(part.strip())
                for part in value.replace("、", ",").split(",")
                if part.strip()
            ]
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
    def _consecutive_days(
        staff_id: int, current: date, assignments: list[Assignment]
    ) -> int:
        worked = {item.day for item in assignments if item.staff_id == staff_id}
        count, target = 0, current - timedelta(days=1)
        while target in worked:
            count += 1
            target -= timedelta(days=1)
        return count
