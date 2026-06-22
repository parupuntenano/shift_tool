import calendar
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from shifts.infrastructure.models import (
    AvailabilityDay, AvailabilitySubmission, Company, CompanyMembership,
    ConstraintType, IndividualConstraint, SkillLevel, Staff, StaffSkill, WorkType,
)


class Command(BaseCommand):
    help = "デモ企業・管理者・スタッフ・提出データを登録します"

    def handle(self, *args, **options):
        company, _ = Company.objects.get_or_create(code="demo", defaults={"name": "デモ株式会社"})
        for name, operator, description, default_is_hard in (
            ("最大連続勤務日数", "max_consecutive", "指定日数を超える連続勤務を禁止します。", True),
            ("2業務の交互アサイン", "work_alternation", "対象スタッフを指定した2業務へ交互に配置します。", True),
            ("指定スタッフとの同一業務アサイン禁止", "incompatible_same_work", "2名を同じ日・同じ業務へ同時配置しません。", True),
            ("同一業務の連続回避", "avoid_same_work", "同じ業務が連続しないようにします。", False),
            ("勤休パターン", "work_rest_pattern", "2,1や2,1,3,1のような勤務・休日パターンを繰り返します。", False),
            ("単休禁止・連休確保", "no_single_rest", "勤務日の間に休日を入れる場合は2日以上確保します。", True),
            ("特定業務の連続回避", "avoid_specific_work", "指定した業務が連続しないようにします。", True),
            ("特定業務アサイン禁止", "forbid_specific_work", "指定スタッフを対象業務へ配置しません。", True),
            ("曜日別業務アサイン禁止", "forbid_works_on_weekdays", "指定曜日に対象業務へ配置しません。業務未指定なら全業務を禁止します。", True),
            ("備考・将来拡張", "custom", "生成時の自動判定を行わない記録用条件です。", False),
        ):
            ConstraintType.objects.update_or_create(company=company, name=name, defaults={"operator": operator, "description": description, "default_is_hard": default_is_hard})
        User = get_user_model()
        admin_user, _ = User.objects.get_or_create(username="admin", defaults={"is_staff": True})
        admin_user.set_password("admin123"); admin_user.save()
        CompanyMembership.objects.update_or_create(company=company, user=admin_user, defaults={"role": "admin"})

        levels = {}
        for symbol, meaning, priority, assignable in [("◎", "リーダー", 1, True), ("○", "対応可能", 2, True), ("△", "訓練中", 3, True), ("×", "対応不可", 99, False)]:
            levels[symbol], _ = SkillLevel.objects.get_or_create(company=company, symbol=symbol, defaults={"meaning": meaning, "priority": priority, "assignable": assignable})
        works = []
        for order, name in enumerate(("受付", "検品", "出荷"), 1):
            work, _ = WorkType.objects.get_or_create(company=company, name=name, defaults={"display_order": order, "required_staff_per_day": 1})
            works.append(work)

        month = timezone.localdate().replace(day=1)
        for index, name in enumerate(("青木 花", "井上 翔", "佐藤 海", "田中 陽", "山本 凛"), 1):
            user, _ = User.objects.get_or_create(username=f"staff{index}")
            user.set_password("staff123"); user.save()
            CompanyMembership.objects.update_or_create(company=company, user=user, defaults={"role": "staff"})
            staff, _ = Staff.objects.update_or_create(company=company, employee_number=f"S{index:03}", defaults={"name": name, "user": user, "active": True})
            for work_index, work in enumerate(works):
                level = levels["◎" if index == work_index + 1 else "○" if (index + work_index) % 2 == 0 else "△"]
                StaffSkill.objects.update_or_create(staff=staff, work_type=work, defaults={"level": level})
            submission, _ = AvailabilitySubmission.objects.get_or_create(staff=staff, month=month)
            for day_number in range(1, calendar.monthrange(month.year, month.month)[1] + 1):
                AvailabilityDay.objects.update_or_create(submission=submission, day=month.replace(day=day_number), defaults={"available": True, "preferred_off": day_number % (index + 5) == 0})
            submission.status = "submitted"; submission.submitted_at = timezone.now(); submission.save()
        first_staff = Staff.objects.filter(company=company).first()
        IndividualConstraint.objects.get_or_create(company=company, staff=first_staff, name="4勤以上禁止", defaults={"kind": "max_consecutive", "is_hard": True, "parameters": {"days": 3}})
        self.stdout.write(self.style.SUCCESS("デモデータを登録しました。管理者: admin / admin123、スタッフ: staff1 / staff123"))
