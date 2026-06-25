import re

from django.db import transaction
from django.contrib.auth import get_user_model

from shifts.domain.import_data import ImportedSkillMap
from .models import Company, CompanyMembership, SkillLevel, Staff, StaffSkill, WorkType
from .models import ConstraintType, IndividualConstraint


NOTE_RULE_SOURCE = "staff_note"


NOTE_RULE_TYPE_DEFAULTS = {
    ConstraintType.Operator.MAX_CONSECUTIVE: (
        "最大連続勤務日数",
        "指定日数を超える連続勤務を禁止します。",
        10,
    ),
    ConstraintType.Operator.WORK_ALTERNATION: (
        "2業務の交互アサイン",
        "対象スタッフを指定した2業務へ交互に配置します。",
        10,
    ),
    ConstraintType.Operator.WORK_REST_PATTERN: (
        "勤休パターン",
        "2,1や2,1,3,1のような勤務・休日パターンを繰り返します。",
        5,
    ),
    ConstraintType.Operator.NO_SINGLE_REST: (
        "単休禁止・連休確保",
        "勤務日の間に休日を入れる場合は2日以上確保します。",
        10,
    ),
    ConstraintType.Operator.AVOID_SPECIFIC_WORK: (
        "特定業務の連続回避",
        "指定した業務が連続しないようにします。",
        10,
    ),
    ConstraintType.Operator.FORBID_SPECIFIC_WORK: (
        "特定業務アサイン禁止",
        "指定スタッフを対象業務へ配置しません。",
        10,
    ),
}


class DjangoMasterRepository:
    @transaction.atomic
    def save_skill_map(self, company_id: int, data: ImportedSkillMap) -> dict[str, int]:
        staff_count = skill_count = account_count = constraint_count = level_count = 0
        work_count = 0
        User = get_user_model()
        company = Company.objects.get(pk=company_id)

        for index, work_row in enumerate(data.work_types, start=1):
            WorkType.objects.update_or_create(
                company_id=company_id,
                name=work_row.name,
                defaults={
                    "required_staff_per_day": work_row.minimum_staff_per_day,
                    "display_order": index,
                    "active": work_row.active,
                    "color": work_row.color,
                },
            )
            work_count += 1

        for level_row in data.skill_levels:
            SkillLevel.objects.update_or_create(
                company_id=company_id,
                symbol=level_row.symbol,
                defaults={
                    "meaning": level_row.meaning,
                    "priority": level_row.priority,
                    "assignable": level_row.assignable,
                    "display_order": level_row.priority,
                },
            )
            level_count += 1

        for row in data.rows:
            staff, _ = Staff.objects.update_or_create(
                company_id=company_id,
                employee_number=row.employee_number,
                defaults={
                    "name": row.name,
                    "note": row.note,
                    "monthly_public_holidays": row.monthly_public_holidays,
                    "desired_off_limit": company.default_desired_off_limit,
                    "active": True,
                },
            )
            if not staff.user_id:
                user = User.objects.filter(username=row.employee_number).first()
                if user and (
                    Staff.objects.filter(user=user).exclude(pk=staff.pk).exists()
                    or user.company_memberships.exists()
                ):
                    raise ValueError(
                        f"社員番号「{row.employee_number}」は別のログインアカウントで使用されています。"
                    )
                if not user:
                    user = User(username=row.employee_number)
                    user.set_password("0000")
                    user.save()
                staff.user = user
                staff.save(update_fields=["user"])
                account_count += 1
            CompanyMembership.objects.get_or_create(
                company_id=company_id,
                user=staff.user,
                defaults={"role": CompanyMembership.Role.STAFF},
            )
            staff_count += 1
            for work_name, symbol in row.skills.items():
                work, _ = WorkType.objects.get_or_create(
                    company_id=company_id, name=work_name
                )
                level, _ = SkillLevel.objects.get_or_create(
                    company_id=company_id,
                    symbol=symbol,
                    defaults={
                        "meaning": symbol,
                        "priority": 99,
                        "assignable": symbol not in {"×", "-"},
                    },
                )
                StaffSkill.objects.update_or_create(
                    staff=staff, work_type=work, defaults={"level": level}
                )
                skill_count += 1
            constraint_count += self._sync_constraints_from_note(company_id, staff)
        return {
            "staff": staff_count,
            "skills": skill_count,
            "accounts": account_count,
            "constraints": constraint_count,
            "levels": level_count,
            "works": work_count,
        }

    @transaction.atomic
    def _sync_constraints_from_note(self, company_id: int, staff: Staff) -> int:
        IndividualConstraint.objects.filter(
            company_id=company_id,
            staff=staff,
            parameters__source=NOTE_RULE_SOURCE,
        ).delete()
        parsed_rules = self._parse_note_rules(company_id, staff.note)
        for rule in parsed_rules:
            rule_type = self._rule_type_for_operator(company_id, rule["operator"])
            strength = rule.get("strength", rule_type.default_strength)
            parameters = {
                "source": NOTE_RULE_SOURCE,
                "note": staff.note,
                "token": rule["token"],
            }
            if rule.get("numeric_value"):
                parameters["value"] = rule["numeric_value"]
            IndividualConstraint.objects.create(
                company_id=company_id,
                staff=staff,
                rule_type=rule_type,
                name=f"{staff.name}：{rule['label']}",
                kind=rule_type.operator,
                work_type_a=rule.get("work_type_a"),
                work_type_b=rule.get("work_type_b"),
                numeric_value=rule.get("numeric_value"),
                text_value=rule.get("text_value", ""),
                weekdays=[],
                is_hard=strength == 10,
                strength=strength,
                parameters=parameters,
                active=True,
            )
        return len(parsed_rules)

    @staticmethod
    def _note_tokens(note: str) -> list[str]:
        return [
            token.strip()
            for token in re.split(r"[\n\r;；。]+", note or "")
            if token.strip()
        ]

    def _parse_note_rules(self, company_id: int, note: str) -> list[dict]:
        rules = []
        works = list(WorkType.objects.filter(company_id=company_id, active=True))
        for token in self._note_tokens(note):
            compact = token.replace(" ", "").replace("　", "")
            work_rule = self._parse_work_note_rule(compact, token, works)
            if work_rule:
                rules.append(work_rule)
                continue

            rest_pattern = re.search(r"(\d+)\s*勤\s*(\d+)\s*休", compact)
            if rest_pattern:
                work_days, rest_days = rest_pattern.groups()
                rules.append(
                    {
                        "operator": ConstraintType.Operator.WORK_REST_PATTERN,
                        "label": f"{work_days}勤{rest_days}休",
                        "text_value": f"{work_days},{rest_days}",
                        "strength": 5,
                        "token": token,
                    }
                )
                continue

            max_until = re.search(r"(?:最大|最長)?(\d+)(?:連勤|勤)(?:まで|以内)", compact)
            if max_until:
                max_days = int(max_until.group(1))
                rules.append(
                    {
                        "operator": ConstraintType.Operator.MAX_CONSECUTIVE,
                        "label": f"最大{max_days}連勤",
                        "numeric_value": max_days,
                        "strength": 10,
                        "token": token,
                    }
                )
                continue

            max_forbid = re.search(r"(\d+)(?:連勤|勤)(?:不可|禁止|NG|ＮＧ)", compact)
            if max_forbid:
                max_days = max(1, int(max_forbid.group(1)) - 1)
                rules.append(
                    {
                        "operator": ConstraintType.Operator.MAX_CONSECUTIVE,
                        "label": f"{int(max_forbid.group(1))}連勤不可",
                        "numeric_value": max_days,
                        "strength": 10,
                        "token": token,
                    }
                )
                continue

            if re.search(r"単休(?:不可|禁止|NG|ＮＧ)", compact):
                rules.append(
                    {
                        "operator": ConstraintType.Operator.NO_SINGLE_REST,
                        "label": "単休禁止",
                        "strength": 10,
                        "token": token,
                    }
                )
        return rules

    @staticmethod
    def _parse_work_note_rule(compact: str, token: str, works: list[WorkType]):
        matched_works = [work for work in works if work.name and work.name in compact]
        if "交互" in compact and len(matched_works) >= 2:
            return {
                "operator": ConstraintType.Operator.WORK_ALTERNATION,
                "label": f"{matched_works[0].name}と{matched_works[1].name}を交互",
                "work_type_a": matched_works[0],
                "work_type_b": matched_works[1],
                "strength": 10,
                "token": token,
            }
        if not matched_works:
            return None

        work = matched_works[0]
        if "連続" in compact and re.search(r"(不可|禁止|NG|ＮＧ|避け)", compact):
            return {
                "operator": ConstraintType.Operator.AVOID_SPECIFIC_WORK,
                "label": f"{work.name}の連続回避",
                "work_type_a": work,
                "strength": 10,
                "token": token,
            }
        if re.search(r"(不可|禁止|NG|ＮＧ)", compact):
            return {
                "operator": ConstraintType.Operator.FORBID_SPECIFIC_WORK,
                "label": f"{work.name}アサイン禁止",
                "work_type_a": work,
                "strength": 10,
                "token": token,
            }
        return None

    @staticmethod
    def _rule_type_for_operator(company_id: int, operator: str) -> ConstraintType:
        name, description, strength = NOTE_RULE_TYPE_DEFAULTS[operator]
        rule_type = ConstraintType.objects.filter(
            company_id=company_id,
            operator=operator,
            active=True,
        ).first()
        if rule_type:
            return rule_type
        rule_type, _ = ConstraintType.objects.get_or_create(
            company_id=company_id,
            name=name,
            defaults={
                "operator": operator,
                "description": description,
                "default_is_hard": strength == 10,
                "default_strength": strength,
                "active": True,
            },
        )
        return rule_type
