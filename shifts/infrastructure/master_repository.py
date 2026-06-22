from django.db import transaction
from django.contrib.auth import get_user_model

from shifts.domain.import_data import ImportedSkillMap
from .models import CompanyMembership, SkillLevel, Staff, StaffSkill, WorkType


class DjangoMasterRepository:
    @transaction.atomic
    def save_skill_map(self, company_id: int, data: ImportedSkillMap) -> dict[str, int]:
        staff_count = skill_count = account_count = 0
        User = get_user_model()
        for row in data.rows:
            staff, _ = Staff.objects.update_or_create(
                company_id=company_id, employee_number=row.employee_number,
                defaults={"name": row.name, "note": row.note, "active": True},
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
                company_id=company_id, user=staff.user,
                defaults={"role": CompanyMembership.Role.STAFF},
            )
            staff_count += 1
            for work_name, symbol in row.skills.items():
                work, _ = WorkType.objects.get_or_create(company_id=company_id, name=work_name)
                level, _ = SkillLevel.objects.get_or_create(
                    company_id=company_id, symbol=symbol,
                    defaults={"meaning": symbol, "priority": 99, "assignable": symbol not in {"×", "-"}},
                )
                StaffSkill.objects.update_or_create(staff=staff, work_type=work, defaults={"level": level})
                skill_count += 1
        return {"staff": staff_count, "skills": skill_count, "accounts": account_count}
