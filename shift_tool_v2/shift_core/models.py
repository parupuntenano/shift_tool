from django.db import models


class Staff(models.Model):
    employee_number = models.CharField("社員番号", max_length=30, unique=True)
    name = models.CharField("氏名", max_length=80)
    public_holiday_count = models.PositiveSmallIntegerField("月公休数", default=8)
    note = models.TextField("その他・制約メモ", blank=True)
    is_active = models.BooleanField("有効", default=True)

    class Meta:
        ordering = ["employee_number"]
        verbose_name = "スタッフ"
        verbose_name_plural = "スタッフ"

    def __str__(self):
        return f"{self.employee_number} {self.name}"


class WorkType(models.Model):
    name = models.CharField("業務名", max_length=80, unique=True)
    required_staff_per_day = models.PositiveSmallIntegerField("必要人数", default=1)
    color = models.CharField("色", max_length=7, blank=True)
    display_order = models.PositiveSmallIntegerField("表示順", default=0)
    active = models.BooleanField("有効", default=True)

    class Meta:
        ordering = ["display_order", "name"]
        verbose_name = "業務"
        verbose_name_plural = "業務"

    def __str__(self):
        return self.name


class SkillLevel(models.Model):
    symbol = models.CharField("記号", max_length=10, unique=True)
    label = models.CharField("意味", max_length=80, blank=True)
    priority = models.PositiveSmallIntegerField("優先度", default=5)
    assignable = models.BooleanField("配置可", default=True)
    instructor = models.BooleanField("指導可能", default=False)
    trainee = models.BooleanField("研修中", default=False)

    class Meta:
        ordering = ["priority", "symbol"]
        verbose_name = "スキル区分"
        verbose_name_plural = "スキル区分"

    def __str__(self):
        return self.symbol


class StaffSkill(models.Model):
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="skills")
    work_type = models.ForeignKey(WorkType, on_delete=models.CASCADE, related_name="skills")
    level = models.ForeignKey(SkillLevel, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["staff", "work_type"], name="unique_staff_work_skill_v2")
        ]
        verbose_name = "スタッフスキル"
        verbose_name_plural = "スタッフスキル"


class PreviousShiftRecord(models.Model):
    class Status(models.TextChoices):
        WORK = "work", "勤務"
        PUBLIC_HOLIDAY = "public_holiday", "公休"
        PAID_LEAVE = "paid_leave", "有休"
        STANDBY = "standby", "余剰"
        BLANK = "blank", "空欄"

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="previous_records")
    day = models.DateField("日付")
    status = models.CharField("状態", max_length=20, choices=Status.choices)
    work_type = models.ForeignKey(WorkType, null=True, blank=True, on_delete=models.SET_NULL)
    raw_value = models.CharField("取込値", max_length=100, blank=True)

    class Meta:
        ordering = ["day", "staff__employee_number"]
        constraints = [
            models.UniqueConstraint(fields=["staff", "day"], name="unique_previous_shift_record_v2")
        ]


class ShiftRequest(models.Model):
    class Kind(models.TextChoices):
        PUBLIC_HOLIDAY = "public_holiday", "公休希望"
        PAID_LEAVE = "paid_leave", "有休希望"
        UNAVAILABLE = "unavailable", "勤務不可"

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="shift_requests")
    day = models.DateField("日付")
    kind = models.CharField("希望", max_length=20, choices=Kind.choices)
    raw_value = models.CharField("取込値", max_length=100, blank=True)

    class Meta:
        ordering = ["day", "staff__employee_number"]
        constraints = [
            models.UniqueConstraint(fields=["staff", "day"], name="unique_shift_request_v2")
        ]


class ShiftPeriod(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "下書き"
        PUBLISHED = "published", "確定"

    month = models.DateField("対象月")
    status = models.CharField("状態", max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        ordering = ["-month"]
        constraints = [
            models.UniqueConstraint(fields=["month"], name="unique_shift_period_month_v2")
        ]

    def __str__(self):
        return f"{self.month:%Y年%m月}"


class ShiftAssignment(models.Model):
    class Status(models.TextChoices):
        WORK = "work", "勤務"
        PUBLIC_HOLIDAY = "public_holiday", "公休"
        PAID_LEAVE = "paid_leave", "有休"
        STANDBY = "standby", "余剰"

    class Source(models.TextChoices):
        AUTO = "auto", "自動"
        MANUAL = "manual", "手動"

    period = models.ForeignKey(ShiftPeriod, on_delete=models.CASCADE, related_name="assignments")
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="assignments")
    day = models.DateField("日付")
    status = models.CharField("状態", max_length=20, choices=Status.choices)
    work_type = models.ForeignKey(WorkType, null=True, blank=True, on_delete=models.SET_NULL)
    source = models.CharField("作成元", max_length=10, choices=Source.choices, default=Source.AUTO)

    class Meta:
        ordering = ["staff__employee_number", "day"]
        constraints = [
            models.UniqueConstraint(fields=["period", "staff", "day"], name="unique_shift_assignment_v2")
        ]


class ShiftWarning(models.Model):
    period = models.ForeignKey(ShiftPeriod, on_delete=models.CASCADE, related_name="warnings")
    day = models.DateField("日付", null=True, blank=True)
    staff = models.ForeignKey(Staff, null=True, blank=True, on_delete=models.CASCADE)
    work_type = models.ForeignKey(WorkType, null=True, blank=True, on_delete=models.CASCADE)
    message = models.CharField("警告", max_length=255)

    class Meta:
        ordering = ["day", "staff__employee_number", "work_type__display_order"]

# Create your models here.
