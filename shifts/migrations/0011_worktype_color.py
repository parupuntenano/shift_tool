from django.core.validators import RegexValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shifts", "0010_previousmonthshiftday"),
    ]

    operations = [
        migrations.AddField(
            model_name="worktype",
            name="color",
            field=models.CharField(
                blank=True,
                help_text="例：#2563eb。未入力なら標準色で表示します。",
                max_length=7,
                validators=[
                    RegexValidator(
                        regex="^#[0-9A-Fa-f]{6}$",
                        message="色は #RRGGBB 形式で入力してください。",
                    )
                ],
                verbose_name="表示色",
            ),
        ),
    ]
