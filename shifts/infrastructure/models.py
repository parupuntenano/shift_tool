from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Company(models.Model):
    name = models.CharField("企業名", max_length=150)
    code = models.SlugField("企業コード", max_length=50, unique=True)
    active = models.BooleanField("有効", default=True)

    class Meta:
        verbose_name = "企業"
        verbose_name_plural = "企業"

    def __str__(self):
        return self.name

    @property
    def default_strength_label(self):
        return strength_label(self.default_strength)

    @property
    def default_strength_class(self):
        return strength_class(self.default_strength)


class CompanyMembership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "管理者"
        STAFF = "staff", "スタッフ"

    company = models.ForeignKey(
        Company, related_name="memberships", on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="company_memberships",
        on_delete=models.CASCADE,
    )
    role = models.CharField("権限", max_length=10, choices=Role.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "user"], name="unique_company_user"
            )
        ]

    def __str__(self):
        return f"{self.company} / {self.user}"


class Staff(models.Model):
    company = models.ForeignKey(Company, related_name="staff", on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="staff_profiles",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    employee_number = models.CharField("社員番号", max_length=50)
    name = models.CharField("氏名", max_length=100)
    monthly_public_holidays = models.PositiveSmallIntegerField(
        "月公休数", default=8, validators=[MinValueValidator(0)]
    )
    desired_off_limit = models.PositiveSmallIntegerField(
        "希望上限日数",
        default=4,
        validators=[MinValueValidator(0)],
        help_text="公休希望と有給希望を合わせて申請できる上限日数です。",
    )
    note = models.TextField("備考", blank=True)
    active = models.BooleanField("有効", default=True)

    class Meta:
        verbose_name = "スタッフ"
        verbose_name_plural = "スタッフ"
        ordering = ["employee_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "employee_number"], name="unique_company_employee"
            )
        ]

    def __str__(self):
        return f"{self.employee_number} {self.name}"


class WorkType(models.Model):
    company = models.ForeignKey(
        Company, related_name="work_types", on_delete=models.CASCADE
    )
    name = models.CharField("業務名", max_length=100)
    display_order = models.PositiveIntegerField("表示順", default=0)
    required_staff_per_day = models.PositiveSmallIntegerField(
        "必要人数", default=1, validators=[MinValueValidator(1)]
    )
    active = models.BooleanField("有効", default=True)

    class Meta:
        verbose_name = "業務"
        verbose_name_plural = "業務"
        ordering = ["display_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"], name="unique_company_work"
            )
        ]

    def __str__(self):
        return self.name


class SkillLevel(models.Model):
    company = models.ForeignKey(
        Company, related_name="skill_levels", on_delete=models.CASCADE
    )
    symbol = models.CharField("記号", max_length=20)
    meaning = models.CharField("意味", max_length=100)
    priority = models.PositiveIntegerField("優先度", default=1)
    assignable = models.BooleanField("アサイン可", default=True)
    display_order = models.PositiveIntegerField("表示順", default=0)

    class Meta:
        verbose_name = "スキル区分"
        verbose_name_plural = "スキル区分"
        ordering = ["display_order", "priority"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "symbol"], name="unique_company_skill_symbol"
            )
        ]

    def __str__(self):
        return f"{self.symbol}（{self.meaning}）"


class StaffSkill(models.Model):
    staff = models.ForeignKey(
        Staff, related_name="work_skills", on_delete=models.CASCADE
    )
    work_type = models.ForeignKey(
        WorkType, related_name="staff_skills", on_delete=models.CASCADE
    )
    level = models.ForeignKey(
        SkillLevel, related_name="staff_skills", on_delete=models.PROTECT
    )

    class Meta:
        verbose_name = "スタッフスキル"
        verbose_name_plural = "スタッフスキル"
        constraints = [
            models.UniqueConstraint(
                fields=["staff", "work_type"], name="unique_staff_work_skill"
            )
        ]


class ConstraintType(models.Model):
    class Operator(models.TextChoices):
        MAX_CONSECUTIVE = "max_consecutive", "最大連続勤務日数"
        WORK_ALTERNATION = "work_alternation", "指定した2業務を交互に配置"
        INCOMPATIBLE_SAME_WORK = (
            "incompatible_same_work",
            "指定スタッフ同士を同一業務へ同時配置しない",
        )
        AVOID_SAME_WORK = "avoid_same_work", "同一業務の連続を回避"
        WORK_REST_PATTERN = "work_rest_pattern", "勤休パターンを繰り返す"
        NO_SINGLE_REST = "no_single_rest", "単休を禁止する"
        AVOID_SPECIFIC_WORK = "avoid_specific_work", "特定業務の連続を回避"
        FORBID_SPECIFIC_WORK = "forbid_specific_work", "特定業務への配置を禁止"
        FORBID_WORKS_ON_WEEKDAYS = (
            "forbid_works_on_weekdays",
            "指定曜日の業務配置を禁止",
        )
        CUSTOM = "custom", "判定なし（備考・将来拡張用）"

    company = models.ForeignKey(
        Company, related_name="constraint_types", on_delete=models.CASCADE
    )
    name = models.CharField("条件種別名", max_length=100)
    operator = models.CharField("判定方式", max_length=40, choices=Operator.choices)
    description = models.TextField("説明", blank=True)
    default_is_hard = models.BooleanField("初期値をHard Constraintにする", default=True)
    default_strength = models.PositiveSmallIntegerField(
        "初期強度",
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="10は絶対守る、1〜9は数字が大きいほど強く優先します。",
    )
    active = models.BooleanField("有効", default=True)

    class Meta:
        verbose_name = "条件種別"
        verbose_name_plural = "条件種別"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"], name="unique_company_constraint_type"
            )
        ]

    def __str__(self):
        return self.name


class IndividualConstraint(models.Model):
    class Kind(models.TextChoices):
        MAX_CONSECUTIVE = "max_consecutive", "最大連続勤務日数"
        NO_SINGLE_REST = "no_single_rest", "単休不可"
        AVOID_SAME_WORK = "avoid_same_work", "同一業務連続回避"
        INCOMPATIBLE_STAFF = "incompatible_staff", "同時配置禁止"
        CUSTOM = "custom", "その他"

    company = models.ForeignKey(
        Company, related_name="constraints", on_delete=models.CASCADE
    )
    rule_type = models.ForeignKey(
        ConstraintType,
        verbose_name="条件種別",
        related_name="constraints",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    staff = models.ForeignKey(
        Staff,
        related_name="constraints",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    related_staff = models.ForeignKey(
        Staff,
        verbose_name="相手スタッフ",
        related_name="related_constraints",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    work_type_a = models.ForeignKey(
        WorkType,
        verbose_name="対象業務A",
        related_name="constraint_work_a",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    work_type_b = models.ForeignKey(
        WorkType,
        verbose_name="対象業務B",
        related_name="constraint_work_b",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
    )
    numeric_value = models.PositiveIntegerField("数値設定", null=True, blank=True)
    text_value = models.CharField(
        "パターン設定", max_length=100, blank=True, help_text="例: 2,1 または 2,1,3,1"
    )
    weekdays = models.JSONField("対象曜日", default=list, blank=True)
    name = models.CharField("条件名", max_length=100)
    kind = models.CharField("条件種別", max_length=30, choices=Kind.choices)
    is_hard = models.BooleanField("Hard Constraint", default=True)
    strength = models.PositiveSmallIntegerField(
        "強度",
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="10は絶対守る、1〜9は数字が大きいほど強く優先します。",
    )
    parameters = models.JSONField("パラメータ", default=dict, blank=True)
    active = models.BooleanField("有効", default=True)

    class Meta:
        verbose_name = "個別制約条件"
        verbose_name_plural = "個別制約条件"
        ordering = ["staff_id", "id"]

    def __str__(self):
        return self.name

    @property
    def strength_label(self):
        return strength_label(self.strength)

    @property
    def strength_class(self):
        return strength_class(self.strength)

    @property
    def weekday_labels(self):
        labels = ["月", "火", "水", "木", "金", "土", "日"]
        return "・".join(
            labels[int(value)] for value in self.weekdays if 0 <= int(value) <= 6
        )


def strength_label(value):
    if value >= 10:
        return "絶対"
    if value >= 8:
        return "強め"
    if value >= 5:
        return "標準"
    return "弱め"


def strength_class(value):
    if value >= 10:
        return "warning"
    if value >= 8:
        return "strength-high"
    if value >= 5:
        return "strength-middle"
    return "strength-low"


class AvailabilitySubmission(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "下書き"
        SUBMITTED = "submitted", "提出済み"

    staff = models.ForeignKey(
        Staff, related_name="submissions", on_delete=models.CASCADE
    )
    month = models.DateField("対象月", help_text="月初日を保存")
    status = models.CharField(
        "状態", max_length=12, choices=Status.choices, default=Status.DRAFT
    )
    submitted_at = models.DateTimeField("提出日時", null=True, blank=True)

    class Meta:
        verbose_name = "シフト提出"
        verbose_name_plural = "シフト提出"
        constraints = [
            models.UniqueConstraint(
                fields=["staff", "month"], name="unique_staff_submission_month"
            )
        ]


class AvailabilityDay(models.Model):
    submission = models.ForeignKey(
        AvailabilitySubmission, related_name="days", on_delete=models.CASCADE
    )
    day = models.DateField("日付")
    available = models.BooleanField("勤務可能", default=True)
    preferred_off = models.BooleanField("公休希望", default=False)
    paid_leave = models.BooleanField("有給希望", default=False)
    note = models.CharField("備考", max_length=200, blank=True)

    class Meta:
        ordering = ["day"]
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "day"], name="unique_submission_day"
            )
        ]


class ShiftPeriod(models.Model):
    class Status(models.TextChoices):
        NOT_GENERATED = "not_generated", "未生成"
        DRAFT = "draft", "下書き"
        PUBLISHED = "published", "公開済み"

    company = models.ForeignKey(
        Company, related_name="shift_periods", on_delete=models.CASCADE
    )
    month = models.DateField("対象月")
    status = models.CharField(
        "状態", max_length=20, choices=Status.choices, default=Status.NOT_GENERATED
    )
    warning_count = models.PositiveIntegerField("警告件数", default=0)
    generated_at = models.DateTimeField("生成日時", null=True, blank=True)
    published_at = models.DateTimeField("公開日時", null=True, blank=True)

    class Meta:
        verbose_name = "月間シフト"
        verbose_name_plural = "月間シフト"
        ordering = ["-month"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "month"], name="unique_company_shift_month"
            )
        ]

    def __str__(self):
        return f"{self.company} {self.month:%Y-%m}"


class ShiftAssignment(models.Model):
    period = models.ForeignKey(
        ShiftPeriod, related_name="assignments", on_delete=models.CASCADE
    )
    staff = models.ForeignKey(
        Staff, related_name="assignments", on_delete=models.PROTECT
    )
    day = models.DateField("日付")
    work_type = models.ForeignKey(
        WorkType,
        related_name="assignments",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    note = models.CharField("備考", max_length=200, blank=True)
    manually_edited = models.BooleanField("手動修正", default=False)

    class Meta:
        ordering = ["staff__employee_number", "day"]
        constraints = [
            models.UniqueConstraint(
                fields=["period", "staff", "day"], name="unique_period_staff_day"
            )
        ]


class ShiftLeaveRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "未対応"
        APPROVED = "approved", "承認済み"
        REJECTED = "rejected", "却下"

    period = models.ForeignKey(
        ShiftPeriod, related_name="leave_requests", on_delete=models.CASCADE
    )
    staff = models.ForeignKey(
        Staff, related_name="shift_leave_requests", on_delete=models.CASCADE
    )
    assignment = models.ForeignKey(
        ShiftAssignment,
        related_name="leave_requests",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    day = models.DateField("休み申請日")
    work_type = models.ForeignKey(
        WorkType,
        verbose_name="対象業務",
        related_name="shift_leave_requests",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    reason = models.CharField("理由", max_length=200, blank=True)
    status = models.CharField(
        "状態", max_length=20, choices=Status.choices, default=Status.PENDING
    )
    admin_note = models.CharField("管理者メモ", max_length=200, blank=True)
    requested_at = models.DateTimeField("申請日時", auto_now_add=True)
    resolved_at = models.DateTimeField("対応日時", null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="対応者",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        verbose_name = "急な休み申請"
        verbose_name_plural = "急な休み申請"
        ordering = ["status", "day", "staff__employee_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["period", "staff", "day"],
                name="unique_shift_leave_request_day",
            )
        ]

    @property
    def status_class(self):
        if self.status == self.Status.APPROVED:
            return "published"
        if self.status == self.Status.REJECTED:
            return "draft"
        return "warning"

    def __str__(self):
        return f"{self.staff} {self.day:%Y-%m-%d} {self.get_status_display()}"


class GenerationWarning(models.Model):
    period = models.ForeignKey(
        ShiftPeriod, related_name="warnings", on_delete=models.CASCADE
    )
    day = models.DateField("日付")
    work_type = models.ForeignKey(
        WorkType, null=True, blank=True, on_delete=models.SET_NULL
    )
    message = models.CharField("警告内容", max_length=250)

    class Meta:
        ordering = ["day", "id"]


class ImportJob(models.Model):
    company = models.ForeignKey(
        Company, related_name="import_jobs", on_delete=models.CASCADE
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    filename = models.CharField("ファイル名", max_length=255)
    result = models.JSONField("取込結果", default=dict)
    created_at = models.DateTimeField("取込日時", auto_now_add=True)
