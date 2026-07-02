import calendar
from dataclasses import dataclass
from datetime import date

from shifts.domain.entities import PreviousShiftDay
from shifts.domain.rest_patterns import (
    next_pattern_index_from_previous,
    parse_work_rest_counts,
    previous_consecutive_work_count,
    previous_work_statuses,
    work_rest_pattern_from_counts,
)

WORK_REST_PATTERN = "work_rest_pattern"
NO_SINGLE_REST = "no_single_rest"
MAX_CONSECUTIVE = "max_consecutive"


@dataclass(frozen=True)
class RestRuleInput:
    name: str
    operator: str
    kind: str
    numeric_value: int | None = None
    text_value: str = ""
    strength: int | None = None
    strength_label: str = ""
    strength_class: str = ""


def build_suggested_off_day_map(
    month: date,
    rules: list[RestRuleInput],
    previous_days: list[PreviousShiftDay],
) -> dict[int, list[str]]:
    day_count = calendar.monthrange(month.year, month.month)[1]
    suggestions: dict[int, list[str]] = {}
    previous_statuses = previous_work_statuses(previous_days)
    has_previous_data = bool(previous_statuses)

    pattern_rule = next(
        (rule for rule in rules if rule.operator == WORK_REST_PATTERN),
        None,
    )
    if pattern_rule:
        counts = parse_work_rest_counts(pattern_rule.text_value)
        if counts:
            pattern_states = work_rest_pattern_from_counts(counts)
            pattern_index = next_pattern_index_from_previous(
                pattern_states,
                previous_statuses,
            )
            reason = "過去実績+休み候補" if has_previous_data else "休み候補"
            for number in range(1, day_count + 1):
                is_work_day = pattern_states[pattern_index % len(pattern_states)]
                if not is_work_day:
                    _add_suggestion_reason(suggestions, number, reason)
                pattern_index += 1

    max_days_candidates = [
        int(rule.numeric_value)
        for rule in rules
        if rule.operator == MAX_CONSECUTIVE and rule.numeric_value
    ]
    if max_days_candidates:
        max_days = max(1, min(max_days_candidates))
        streak = previous_consecutive_work_count(previous_days)
        reason = "過去実績+休み候補" if streak else "休み候補"
        for number in range(1, day_count + 1):
            if streak >= max_days:
                _add_suggestion_reason(suggestions, number, reason)
                streak = 0
            else:
                streak += 1

    return suggestions


def build_staff_rest_constraint_notes(
    rules: list[RestRuleInput],
) -> list[dict[str, str | int | None]]:
    notes = []
    for rule in rules:
        if rule.operator not in _REST_OPERATORS and rule.kind not in _REST_KINDS:
            continue

        label = rule.name.split("：")[-1].split(":")[-1].strip()
        detail = ""
        if rule.operator == WORK_REST_PATTERN:
            counts = parse_work_rest_counts(rule.text_value)
            if counts:
                pairs = [
                    f"{counts[index]}勤{counts[index + 1]}休"
                    for index in range(0, len(counts), 2)
                ]
                label = "・".join(pairs)
                detail = "この流れを希望"
        elif rule.operator == NO_SINGLE_REST:
            label = "単休禁止"
            detail = "休みを1日だけにせず、できれば連休にする"
        elif rule.operator == MAX_CONSECUTIVE and rule.numeric_value:
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


def _add_suggestion_reason(
    suggestions: dict[int, list[str]], day_number: int, reason: str
) -> None:
    if day_number < 1:
        return
    suggestions.setdefault(day_number, [])
    if reason not in suggestions[day_number]:
        suggestions[day_number].append(reason)


_REST_OPERATORS = {WORK_REST_PATTERN, NO_SINGLE_REST, MAX_CONSECUTIVE}
_REST_KINDS = {NO_SINGLE_REST, MAX_CONSECUTIVE}
