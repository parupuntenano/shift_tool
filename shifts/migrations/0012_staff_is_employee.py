from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shifts", "0011_worktype_color"),
    ]

    operations = [
        migrations.AddField(
            model_name="staff",
            name="is_employee",
            field=models.BooleanField(
                default=False,
                help_text="人手不足時に応援投入する社員です。手動編集ではスキル未設定警告を出しません。",
                verbose_name="社員タグ",
            ),
        ),
    ]
