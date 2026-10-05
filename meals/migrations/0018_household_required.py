import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("meals", "0017_households"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dish",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
        migrations.AlterField(
            model_name="plannedmeal",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
        migrations.AlterField(
            model_name="familymember",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
        migrations.AlterField(
            model_name="rule",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
        migrations.AlterField(
            model_name="extraitem",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
        migrations.AlterField(
            model_name="shoppingcheck",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
        migrations.AlterField(
            model_name="menurequest",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
        migrations.AlterField(
            model_name="recipeimport",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
        migrations.AlterField(
            model_name="recipephoto",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
        migrations.AlterField(
            model_name="weeklyitem",
            name="household",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="meals.household"),
        ),
    ]
