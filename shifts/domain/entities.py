from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class StaffMember:
    id: int
    name: str
    max_consecutive_days: int = 6


@dataclass(frozen=True)
class Work:
    id: int
    name: str
    required_staff_per_day: int
    display_order: int = 0


@dataclass(frozen=True)
class SkillRating:
    staff_id: int
    work_id: int
    priority: int
    assignable: bool


@dataclass(frozen=True)
class Availability:
    staff_id: int
    day: date
    available: bool
    preferred_off: bool = False
    paid_leave: bool = False


@dataclass(frozen=True)
class ConstraintRule:
    operator: str
    staff_id: int | None = None
    related_staff_id: int | None = None
    work_ids: tuple[int, ...] = ()
    numeric_value: int | None = None
    text_value: str = ""
    weekdays: tuple[int, ...] = ()
    is_hard: bool = True
    strength: int | None = None


@dataclass(frozen=True)
class Assignment:
    staff_id: int
    work_id: int
    day: date


@dataclass(frozen=True)
class GenerationWarningData:
    day: date
    work_id: int | None
    message: str


@dataclass(frozen=True)
class GenerationResult:
    assignments: tuple[Assignment, ...]
    warnings: tuple[GenerationWarningData, ...]
