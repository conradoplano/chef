"""Family members, rules, household settings and the usual week."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import schedule
from .. import shopping as shopping_list
from ..forms import FamilyMemberForm, HouseholdForm, RuleForm
from ..models import FamilyMember, Household, Rule


@login_required
def family(request):
    """Overview of family members, planning rules and household preferences."""
    rule_form = RuleForm(request.POST or None, initial={"active": True}, prefix="rule")
    if request.method == "POST" and rule_form.is_valid():
        rule = rule_form.save()
        messages.success(request, f"Added rule “{rule}”")
        return redirect("meals:family")
    household = Household.load()
    today = timezone.localdate()
    members = list(FamilyMember.objects.all())
    for member in members:
        member.age_now = member.age(today)
    return render(
        request,
        "meals/family.html",
        {
            "members": members,
            "usual_week": schedule.summary(schedule.usual_week(members), members),
            "rules": Rule.objects.all(),
            "staples": [
                {"kind": kind, "icon": icon, "label": label.capitalize(), "items": shopping_list.staple_items(kind),
                 "example": {"pantry": "olive oil", "freezer": "frozen peas"}[kind]}
                for kind, (icon, label) in shopping_list.STAPLE_LISTS.items()
            ],
            "rule_form": rule_form,
            "household": household,
            "household_rows": [
                (household._meta.get_field(name).verbose_name, value)
                for name, value in [
                    ("weekday_minutes", household.weekday_minutes and f"{household.weekday_minutes} min"),
                    ("weekend_minutes", household.weekend_minutes and f"{household.weekend_minutes} min"),
                    ("adventurousness", household.adventurousness and f"{household.adventurousness} / 10"),
                    ("cuisines", household.cuisines),
                    ("equipment", household.equipment),
                    ("shops", household.shops),
                    ("priority", household.get_priority_display()),
                    ("recipe_sites", household.recipe_sites),
                    ("other_sites", household.recipe_sites and household.get_other_sites_display()),
                ]
                if value
            ],
        },
    )


@login_required
def member_edit(request, pk=None):
    member = get_object_or_404(FamilyMember, pk=pk) if pk else None
    if request.method == "POST" and member and "delete" in request.POST:
        member.delete()
        messages.success(request, f"Removed {member}")
        return redirect("meals:family")
    form = FamilyMemberForm(request.POST or None, instance=member)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        messages.success(request, f"Saved {member}")
        return redirect("meals:family")
    return render(request, "meals/member_edit.html", {"form": form, "member": member})


@login_required
def rule_edit(request, pk):
    rule = get_object_or_404(Rule, pk=pk)
    if request.method == "POST" and "delete" in request.POST:
        rule.delete()
        messages.success(request, "Rule removed.")
        return redirect("meals:family")
    form = RuleForm(request.POST or None, instance=rule)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Rule saved.")
        return redirect("meals:family")
    return render(request, "meals/rule_edit.html", {"form": form, "rule": rule})


@login_required
@require_POST
def rule_toggle(request, pk):
    rule = get_object_or_404(Rule, pk=pk)
    rule.active = not rule.active
    rule.save(update_fields=["active"])
    return redirect("meals:family")


@login_required
def household_edit(request):
    form = HouseholdForm(request.POST or None, instance=Household.load())
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Household saved.")
        return redirect("meals:family")
    return render(request, "meals/household_edit.html", {"form": form})


@login_required
def usual_week(request):
    """Which meals we usually need on each weekday, and who eats them."""
    members = list(FamilyMember.objects.all())
    keys = [(str(i), name) for i, name in enumerate(schedule.WEEKDAYS)]
    if request.method == "POST":
        grid, errors = schedule.parse_grid(request.POST, keys, members)
        if not errors:
            schedule.save_usual_week({int(k): v for k, v in grid.items()})
            messages.success(request, "Usual week saved.")
            return redirect("meals:family")
        for error in errors:
            messages.error(request, error)
    else:
        grid = {str(k): v for k, v in schedule.usual_week(members).items()}
    return render(
        request, "meals/usual_week.html", {"rows": schedule.rows(keys, grid, members), "members": members}
    )
