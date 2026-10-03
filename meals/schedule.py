"""
Which meals are needed and who eats them.

The same grid (days x lunch/dinner, with the family members who eat) is used for
the usual week on the family page and for a concrete week when creating a menu.
Form fields are named "<row key>-<slot>-on" (checkbox) and "<row key>-<slot>-eaters"
(one checkbox per member).
"""
from datetime import timedelta

from .models import Household, PlannedMeal

SLOTS = [(value, label) for value, label in PlannedMeal.Slot.choices]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def default_slot(weekday, slot, member_ids):
    """Without a saved usual week: dinner every day, lunch at the weekend, for everyone."""
    on = slot == PlannedMeal.Slot.DINNER or weekday >= 5
    return {"on": on, "eaters": list(member_ids)}


def usual_week(members):
    """{weekday: {slot: {"on": bool, "eaters": [ids]}}} from the household, with defaults."""
    saved = Household.load().usual_week or {}
    ids = [m.pk for m in members]
    week = {}
    for weekday in range(7):
        week[weekday] = {}
        for slot, _ in SLOTS:
            entry = saved.get(str(weekday), {}).get(slot)
            if entry is None:
                entry = default_slot(weekday, slot, ids)
            # Drop members that no longer exist.
            week[weekday][slot] = {"on": bool(entry.get("on")), "eaters": [i for i in entry.get("eaters", []) if i in ids]}
    return week


def save_usual_week(week):
    household = Household.load()
    household.usual_week = {str(day): slots for day, slots in week.items()}
    household.save(update_fields=["usual_week"])


def parse_grid(data, keys, members):
    """Reads the grid from POST data. Returns ({key: {slot: {"on", "eaters"}}}, errors)."""
    ids = {m.pk for m in members}
    names = {m.pk: m.name for m in members}
    grid, errors = {}, []
    for key, label in keys:
        grid[key] = {}
        for slot, slot_label in SLOTS:
            on = data.get(f"{key}-{slot}-on") == "on"
            eaters = []
            for raw in data.getlist(f"{key}-{slot}-eaters"):
                if raw.isdigit() and int(raw) in ids and int(raw) not in eaters:
                    eaters.append(int(raw))
            if on and ids and not eaters:
                errors.append(f"{label} {slot_label.lower()}: choose who eats.")
            grid[key][slot] = {"on": on, "eaters": sorted(eaters, key=lambda i: names[i])}
    return grid, errors


def rows(keys, grid, members):
    """Rows for the _slots_grid.html template."""
    result = []
    for key, label in keys:
        slots = []
        for slot, slot_label in SLOTS:
            entry = grid[key][slot]
            slots.append({
                "slot": slot,
                "label": slot_label,
                "on": entry["on"],
                "members": [{"member": m, "eats": m.pk in entry["eaters"]} for m in members],
            })
        result.append({"key": key, "label": label, "slots": slots})
    return result


def week_keys(start):
    """(key, label) per day of the week starting on Monday `start`; keys are ISO dates."""
    days = [start + timedelta(days=i) for i in range(7)]
    return [(d.isoformat(), f"{d:%A} {d.day} {d:%b}") for d in days]


def summary(week, members):
    """Short text per weekday for the family page, e.g. "Dinner: everyone"."""
    names = {m.pk: m.name for m in members}
    lines = []
    for weekday in range(7):
        parts = []
        for slot, label in SLOTS:
            entry = week[weekday][slot]
            if not entry["on"]:
                continue
            if not members or len(entry["eaters"]) == len(members):
                who = "everyone"
            else:
                who = ", ".join(names[i] for i in entry["eaters"])
            parts.append(f"{label} – {who}")
        lines.append((WEEKDAYS[weekday], " · ".join(parts) or "No meals"))
    return lines
