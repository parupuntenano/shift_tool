import calendar
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from .entities import (
    Assignment,
    Availability,
    ConstraintRule,
    GenerationResult,
    GenerationWarningData,
    PreviousShiftDay,
    SkillRating,
    StaffMember,
    Work,
)


SAME_WORK_PENALTY = 45
SKILL_PRIORITY_WEIGHT = 12
TOTAL_ASSIGNMENT_WEIGHT = 22
SAME_WORK_ASSIGNMENT_WEIGHT = 16
MULTI_SKILL_PENALTY = 5
SHORT_STAFFED_WORK_BONUS = 10


@dataclass(frozen=True, order=True)
class Candidate:
    """1枠に入れるスタッフ候補。先頭の4項目だけで並び替える。"""

    score: int
    total_count: int
    work_count: int
    staff_id: int
    work: Work = field(compare=False)
    member: StaffMember = field(compare=False)


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
        previous_shift_days: Iterable[PreviousShiftDay] = (),
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
        previous_days = sorted(previous_shift_days, key=lambda item: item.day)
        pattern_anchor_by_staff = self._latest_public_holidays(previous_days)
        total_assignments: Counter[int] = Counter()
        work_assignments: Counter[tuple[int, int]] = Counter()
        last_work: dict[int, int] = self._latest_work_by_staff(previous_days)
        assigned_by_day: set[tuple[int, date]] = set()
        assignments: list[Assignment] = [
            Assignment(item.staff_id, item.work_id, item.day)
            for item in previous_days
            if item.status == "work" and item.work_id
        ]
        warnings: list[GenerationWarningData] = []

        for current in self._days_in_month(month):
            remaining_slots = {
                work.id: max(1, work.required_staff_per_day) for work in work_list
            }
            work_by_id = {work.id: work for work in work_list}

            self._assign_daily_coverage(
                current=current,
                staff_list=staff_list,
                work_by_id=work_by_id,
                skill_map=skill_map,
                availability_map=availability_map,
                rules=rules,
                assigned_by_day=assigned_by_day,
                assignments=assignments,
                warnings=warnings,
                remaining_slots=remaining_slots,
                last_work=last_work,
                total_assignments=total_assignments,
                work_assignments=work_assignments,
                assignable_work_counts=assignable_work_counts,
                eligible_staff_counts=eligible_staff_counts,
                pattern_anchor_by_staff=pattern_anchor_by_staff,
            )
            self._fill_daily_remaining_slots(
                current=current,
                staff_list=staff_list,
                work_by_id=work_by_id,
                skill_map=skill_map,
                availability_map=availability_map,
                rules=rules,
                assigned_by_day=assigned_by_day,
                assignments=assignments,
                warnings=warnings,
                remaining_slots=remaining_slots,
                last_work=last_work,
                total_assignments=total_assignments,
                work_assignments=work_assignments,
                assignable_work_counts=assignable_work_counts,
                eligible_staff_counts=eligible_staff_counts,
                pattern_anchor_by_staff=pattern_anchor_by_staff,
            )
            self._assign_daily_trainees(
                current=current,
                staff_list=staff_list,
                work_by_id=work_by_id,
                skill_map=skill_map,
                availability_map=availability_map,
                rules=rules,
                assigned_by_day=assigned_by_day,
                assignments=assignments,
                last_work=last_work,
                total_assignments=total_assignments,
                work_assignments=work_assignments,
                assignable_work_counts=assignable_work_counts,
                eligible_staff_counts=eligible_staff_counts,
                pattern_anchor_by_staff=pattern_anchor_by_staff,
            )
        current_month_assignments = tuple(
            item for item in assignments if item.day >= month
        )
        return GenerationResult(current_month_assignments, tuple(warnings))

    @staticmethod
    def _latest_public_holidays(previous_days):
        result = {}
        for item in previous_days:
            if item.status == "public_holiday":
                result[item.staff_id] = item.day
        return result

    @staticmethod
    def _latest_work_by_staff(previous_days):
        result = {}
        for item in previous_days:
            if item.status == "work" and item.work_id:
                result[item.staff_id] = item.work_id
        return result

    @staticmethod
    def _days_in_month(month: date):
        day_count = calendar.monthrange(month.year, month.month)[1]
        for day_number in range(1, day_count + 1):
            yield month.replace(day=day_number)

    @classmethod
    def _assign_daily_coverage(
        cls,
        *,
        current,
        staff_list,
        work_by_id,
        skill_map,
        availability_map,
        rules,
        assigned_by_day,
        assignments,
        warnings,
        remaining_slots,
        last_work,
        total_assignments,
        work_assignments,
        assignable_work_counts,
        eligible_staff_counts,
        pattern_anchor_by_staff,
    ):
        """各業務にまず最低1人ずつ配置する。"""

        uncovered_work_ids = set(remaining_slots)

        while uncovered_work_ids:
            assigned_coverage = False

            for work_id in cls._sorted_work_ids(
                uncovered_work_ids,
                work_by_id,
                staff_list,
                skill_map,
                availability_map,
                assigned_by_day,
                current,
                assignments,
                rules,
                pattern_anchor_by_staff,
            ):
                work = work_by_id[work_id]
                candidates = cls._work_candidates(
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
                    pattern_anchor_by_staff,
                )

                if not candidates:
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

                cls._assign_candidate(
                    candidates[0],
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

    @classmethod
    def _fill_daily_remaining_slots(
        cls,
        *,
        current,
        staff_list,
        work_by_id,
        skill_map,
        availability_map,
        rules,
        assigned_by_day,
        assignments,
        warnings,
        remaining_slots,
        last_work,
        total_assignments,
        work_assignments,
        assignable_work_counts,
        eligible_staff_counts,
        pattern_anchor_by_staff,
    ):
        """最低1人を確保したあと、各業務の残り必要人数を埋める。"""

        while remaining_slots:
            best_candidate = None
            exhausted_work_ids = []

            for work_id in cls._sorted_work_ids(
                remaining_slots,
                work_by_id,
                staff_list,
                skill_map,
                availability_map,
                assigned_by_day,
                current,
                assignments,
                rules,
                pattern_anchor_by_staff,
            ):
                work = work_by_id[work_id]
                candidates = cls._work_candidates(
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
                    pattern_anchor_by_staff,
                )

                if not candidates:
                    exhausted_work_ids.append(work.id)
                    continue

                candidate = candidates[0]
                if best_candidate is None or candidate < best_candidate:
                    best_candidate = candidate

            if best_candidate is None:
                cls._warn_all_remaining_slots(
                    current, work_by_id, remaining_slots, warnings
                )
                break

            cls._warn_exhausted_works(
                current, work_by_id, remaining_slots, exhausted_work_ids, warnings
            )
            if not remaining_slots:
                break

            cls._assign_candidate(
                best_candidate,
                current,
                assignments,
                assigned_by_day,
                total_assignments,
                work_assignments,
                last_work,
                remaining_slots,
            )

    @classmethod
    def _sorted_work_ids(
        cls,
        work_ids,
        work_by_id,
        staff_list,
        skill_map,
        availability_map,
        assigned_by_day,
        current,
        assignments,
        rules,
        pattern_anchor_by_staff=None,
    ):
        return sorted(
            work_ids,
            key=lambda work_id: (
                cls._eligible_count(
                    work_by_id[work_id],
                    staff_list,
                    skill_map,
                    availability_map,
                    assigned_by_day,
                    current,
                    assignments,
                    rules,
                    pattern_anchor_by_staff,
                ),
                work_by_id[work_id].display_order,
                work_by_id[work_id].id,
            ),
        )

    @staticmethod
    def _warn_all_remaining_slots(current, work_by_id, remaining_slots, warnings):
        for work_id, missing in list(remaining_slots.items()):
            work = work_by_id[work_id]
            warnings.append(
                GenerationWarningData(
                    current,
                    work.id,
                    f"{work.name}の配置が{missing}名不足しています。",
                )
            )
        remaining_slots.clear()

    @staticmethod
    def _warn_exhausted_works(
        current, work_by_id, remaining_slots, exhausted_work_ids, warnings
    ):
        for work_id in exhausted_work_ids:
            if work_id not in remaining_slots:
                continue

            work = work_by_id[work_id]
            missing = remaining_slots.pop(work_id)
            warnings.append(
                GenerationWarningData(
                    current,
                    work.id,
                    f"{work.name}の配置が{missing}名不足しています。",
                )
            )

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
        pattern_anchor_by_staff,
    ):
        assigned_same_work = {
            item.staff_id
            for item in assignments
            if item.day == current and item.work_id == work.id
        }
        instructor_assigned = cls._has_instructor_assigned(
            assigned_same_work, work.id, skill_map
        )
        candidates = []
        for member in staff_list:
            available = availability_map.get((member.id, current))
            rating = skill_map.get((member.id, work.id))
            if (
                not available
                or not available.available
                or available.preferred_off
                or available.paid_leave
            ):
                continue
            if (
                not rating
                or not rating.assignable
                or (member.id, current) in assigned_by_day
            ):
                continue
            if rating.trainee:
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
                pattern_anchor_by_staff,
            )
            if not allowed:
                continue
            score = (
                rule_penalty
                + cls._alternation_bonus(member.id, work.id, assignments, rules)
                + (SAME_WORK_PENALTY if last_work.get(member.id) == work.id else 0)
                + (cls._effective_priority(rating) * SKILL_PRIORITY_WEIGHT)
                + (total_assignments[member.id] * TOTAL_ASSIGNMENT_WEIGHT)
                + (
                    work_assignments[(member.id, work.id)]
                    * SAME_WORK_ASSIGNMENT_WEIGHT
                )
                + (assignable_work_counts[member.id] * MULTI_SKILL_PENALTY)
                - (
                    max(0, 10 - eligible_staff_counts[work.id])
                    * SHORT_STAFFED_WORK_BONUS
                )
            )
            candidates.append(
                Candidate(
                    score=score,
                    total_count=total_assignments[member.id],
                    work_count=work_assignments[(member.id, work.id)],
                    staff_id=member.id,
                    work=work,
                    member=member,
                )
            )
        candidates.sort()
        return candidates

    @classmethod
    def _assign_daily_trainees(
        cls,
        *,
        current,
        staff_list,
        work_by_id,
        skill_map,
        availability_map,
        rules,
        assigned_by_day,
        assignments,
        last_work,
        total_assignments,
        work_assignments,
        assignable_work_counts,
        eligible_staff_counts,
        pattern_anchor_by_staff,
    ):
        """必要人数を満たし、監督できる人がいる業務に研修者を追加配置する。"""

        for work in sorted(
            work_by_id.values(), key=lambda item: (item.display_order, item.id)
        ):
            if (
                cls._effective_required_staff_count(
                    work.id, current, assignments, skill_map
                )
                < max(1, work.required_staff_per_day)
            ):
                continue
            supervision_slots = cls._available_supervision_slots(
                work.id, current, assignments, skill_map
            )
            if supervision_slots <= 0:
                continue
            assigned_same_work = {
                item.staff_id
                for item in assignments
                if item.day == current and item.work_id == work.id
            }
            if not cls._has_instructor_assigned(assigned_same_work, work.id, skill_map):
                continue
            while supervision_slots > 0:
                candidates = cls._trainee_candidates(
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
                    pattern_anchor_by_staff,
                )
                if not candidates:
                    break
                cls._assign_candidate(
                    candidates[0],
                    current,
                    assignments,
                    assigned_by_day,
                    total_assignments,
                    work_assignments,
                    last_work,
                    {},
                    count_toward_required=False,
                )
                supervision_slots -= 1

    @classmethod
    def _trainee_candidates(
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
        pattern_anchor_by_staff,
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
            if (
                not available
                or not available.available
                or available.preferred_off
                or available.paid_leave
                or not rating
                or not rating.assignable
                or not rating.trainee
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
                pattern_anchor_by_staff,
            )
            if not allowed:
                continue
            score = (
                rule_penalty
                + (SAME_WORK_PENALTY if last_work.get(member.id) == work.id else 0)
                + (cls._effective_priority(rating) * SKILL_PRIORITY_WEIGHT)
                + (total_assignments[member.id] * TOTAL_ASSIGNMENT_WEIGHT)
                + (
                    work_assignments[(member.id, work.id)]
                    * SAME_WORK_ASSIGNMENT_WEIGHT
                )
                + (assignable_work_counts[member.id] * MULTI_SKILL_PENALTY)
                - (
                    max(0, 10 - eligible_staff_counts[work.id])
                    * SHORT_STAFFED_WORK_BONUS
                )
            )
            candidates.append(
                Candidate(
                    score=score,
                    total_count=total_assignments[member.id],
                    work_count=work_assignments[(member.id, work.id)],
                    staff_id=member.id,
                    work=work,
                    member=member,
                )
            )
        candidates.sort()
        return candidates

    @staticmethod
    def _effective_required_staff_count(work_id, current, assignments, skill_map) -> int:
        count = 0
        for item in assignments:
            if item.day != current or item.work_id != work_id:
                continue
            rating = skill_map.get((item.staff_id, item.work_id))
            if rating and rating.trainee:
                continue
            count += 1
        return count

    @staticmethod
    def _available_supervision_slots(work_id, current, assignments, skill_map) -> int:
        instructor_count = 0
        trainee_count = 0
        for item in assignments:
            if item.day != current or item.work_id != work_id:
                continue
            rating = skill_map.get((item.staff_id, item.work_id))
            if not rating:
                continue
            if rating.trainee:
                trainee_count += 1
            elif rating.instructor_capable:
                instructor_count += 1
        return max(0, instructor_count - trainee_count)

    @staticmethod
    def _has_instructor_assigned(staff_ids, work_id, skill_map) -> bool:
        return any(
            (rating := skill_map.get((staff_id, work_id)))
            and rating.assignable
            and rating.instructor_capable
            for staff_id in staff_ids
        )

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
        count_toward_required=True,
    ):
        work = candidate.work
        member = candidate.member
        assignments.append(Assignment(member.id, work.id, current))
        assigned_by_day.add((member.id, current))
        total_assignments[member.id] += 1
        work_assignments[(member.id, work.id)] += 1
        last_work[member.id] = work.id
        if count_toward_required:
            remaining_slots[work.id] -= 1
            if remaining_slots[work.id] <= 0:
                del remaining_slots[work.id]

    @classmethod
    def _evaluate_rules(
        cls,
        staff_id,
        work_id,
        current,
        assigned_same_work,
        assignments,
        rules,
        pattern_anchor_by_staff=None,
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
            elif rule.operator == "max_consecutive":
                limit = rule.numeric_value or 6
                violated = cls._consecutive_days(staff_id, current, assignments) >= limit
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
                    index = cls._work_rest_pattern_index(
                        staff_id, current, pattern, pattern_anchor_by_staff or {}
                    )
                    violated = not pattern[index]
            if violated and cls._is_blocking_rule(rule):
                return False, penalty
            if violated:
                penalty += cls._soft_penalty(rule.operator, rule)
        return True, penalty

    @classmethod
    def _soft_penalty(cls, operator: str, rule: ConstraintRule) -> int:
        if operator == "work_rest_pattern":
            base_penalty = 20
        elif operator == "work_alternation":
            base_penalty = 80
        elif operator in {"avoid_same_work", "avoid_specific_work"}:
            base_penalty = 60
        elif operator == "no_single_rest":
            base_penalty = 70
        else:
            base_penalty = 100

        # 強度5を従来のSoft相当として、1〜9で弱〜強のペナルティに変換する。
        return max(1, round(base_penalty * cls._rule_strength(rule) / 5))

    @staticmethod
    def _rule_strength(rule: ConstraintRule) -> int:
        if rule.strength is not None:
            return min(10, max(1, rule.strength))
        return 10 if rule.is_hard else 5

    @classmethod
    def _is_blocking_rule(cls, rule: ConstraintRule) -> bool:
        return cls._rule_strength(rule) >= 10

    @classmethod
    def _alternation_reward(cls, rule: ConstraintRule) -> int:
        if cls._is_blocking_rule(rule):
            return 80
        return max(1, round(55 * cls._rule_strength(rule) / 5))

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
    def _work_rest_pattern_index(staff_id, current, pattern, pattern_anchor_by_staff):
        anchor = pattern_anchor_by_staff.get(staff_id)
        if anchor and current > anchor:
            return ((current - anchor).days - 1) % len(pattern)
        return (current.day - 1) % len(pattern)

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
        pattern_anchor_by_staff=None,
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
                or available.paid_leave
                or not rating
                or not rating.assignable
                or (member.id, current) in assigned_by_day
            ):
                continue
            if rating.trainee:
                continue
            if (
                cls._consecutive_days(member.id, current, assignments)
                >= member.max_consecutive_days
            ):
                continue
            allowed, _penalty = cls._evaluate_rules(
                member.id,
                work.id,
                current,
                assigned_same_work,
                assignments,
                rules,
                pattern_anchor_by_staff,
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
                bonus -= cls._alternation_reward(rule)
        return bonus
