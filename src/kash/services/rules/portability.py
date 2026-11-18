"""Exporting rules to a JSON file and importing them again (Rules > Export / Import).

Rules point at categories by database id, and ids differ between databases: after a
reset, or on another Kash, every category has a new id. So an export also writes each
category's name (and group), and an import finds the category for each id it doesn't
know:

1. the same id exists here: use it;
2. the file names the category and one here has that name: use it;
3. otherwise the user is asked. For older files, which only have ids, a rule here with
   the same name and type as one in the file (e.g. the built-in "Groceries" rule)
   shows which category that id most likely was, and that is preselected. The user
   can also have the category created (when the file names it) or import the rule
   without a category (paused, to edit).

Rules already here with the same name and type are updated to the file's version;
identical ones are left alone; the rest are added. Rules not in the file stay.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from kash.domain.models import Account, Category, CategoryGroup, Rule

MAX_FILE_BYTES = 2_000_000
NO_CATEGORY = "none"
CREATE = "create"


class ImportFileError(ValueError):
    """The file isn't a rules export (the message says why)."""


# --- Export ---------------------------------------------------------------------------


def export_rules(
    rules: list[Rule], categories: list[Category], groups: list[CategoryGroup]
) -> list[dict[str, Any]]:
    """Rules as JSON-ready dicts; category actions also carry the category's name."""
    group_names = {g.id: g.name for g in groups}
    by_id = {str(c.id): c for c in categories}
    out = []
    for rule in rules:
        data = rule.model_dump(mode="json")
        for action in data.get("actions") or []:
            category = by_id.get(str(action.get("value"))) if _sets_category(action) else None
            if category is not None:
                action["category"] = category.name
                if category.group_id in group_names:
                    action["category_group"] = group_names[category.group_id]
        out.append(data)
    return out


# --- Import: reading and planning -----------------------------------------------------


@dataclass
class UnknownCategory:
    """A category id in the file that this Kash doesn't have."""

    key: str  # the id as written in the file
    name: str | None = None  # from newer exports
    group: str | None = None
    rule_names: list[str] = field(default_factory=list)
    suggestion: UUID | None = None  # an existing category it most likely is

    @property
    def default_choice(self) -> str:
        if self.suggestion:
            return str(self.suggestion)
        return CREATE if self.name else ""


@dataclass
class ImportPlan:
    rules: list[Rule]  # as in the file (ids are new; category ids as written)
    raw: list[dict[str, Any]]  # the file's rules, to carry from the preview to the import
    unknown: list[UnknownCategory]  # the user chooses these
    by_name: dict[str, UUID]  # file id -> the category here with the file's name for it
    skipped: list[str]  # rules that couldn't be read, and why
    unknown_accounts: list[str]  # names of rules whose conditions name missing accounts


def parse_file(content: bytes | str) -> list[dict[str, Any]]:
    """The rules in an export file (a JSON list, or an object with a "rules" list)."""
    if isinstance(content, bytes):
        if len(content) > MAX_FILE_BYTES:
            raise ImportFileError("That file is too big for a rules export.")
        try:
            content = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ImportFileError(
                "That file isn't text; choose the .json file from Export."
            ) from exc
    if not content.strip():
        raise ImportFileError("Choose an exported rules file, or paste its contents.")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ImportFileError(f"That isn't valid JSON (line {exc.lineno}).") from exc
    if isinstance(data, dict) and isinstance(data.get("rules"), list):
        data = data["rules"]
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ImportFileError("That isn't a rules export (expected a list of rules).")
    if not data:
        raise ImportFileError("That file has no rules.")
    return data


def plan_import(
    raw: list[dict[str, Any]],
    user_id: UUID,
    existing: list[Rule],
    categories: list[Category],
    accounts: list[Account],
) -> ImportPlan:
    """Read the file's rules and work out which categories need the user's choice."""
    rules: list[Rule] = []
    kept_raw: list[dict[str, Any]] = []
    skipped: list[str] = []
    for position, item in enumerate(raw, start=1):
        label = str(item.get("name") or f"Rule {position}")
        try:
            rule = Rule.model_validate(
                {
                    **{k: v for k, v in item.items() if k not in ("id", "user_id")},
                    "id": uuid4(),
                    "user_id": user_id,
                }
            )
        except ValidationError as exc:
            problem = exc.errors()[0]
            where = ".".join(str(p) for p in problem.get("loc", ()))
            skipped.append(f"{label}: {problem.get('msg', 'invalid')} ({where})")
            continue
        rules.append(rule)
        kept_raw.append(item)

    category_ids = {str(c.id) for c in categories}
    by_name = {c.name.strip().lower(): c for c in categories}
    unknown: dict[str, UnknownCategory] = {}
    for rule, item in zip(rules, kept_raw, strict=True):
        for action in item.get("actions") or []:
            key = str(action.get("value") or "")
            if not _sets_category(action) or not key or key in category_ids:
                continue
            ref = unknown.setdefault(key, UnknownCategory(key=key))
            ref.name = ref.name or action.get("category")
            ref.group = ref.group or action.get("category_group")
            if rule.name not in ref.rule_names:
                ref.rule_names.append(rule.name)

    # The file names it and a category here has that name: that's the one
    matched: dict[str, UUID] = {}
    for key, ref in list(unknown.items()):
        named = by_name.get((ref.name or "").strip().lower())
        if named is not None:
            matched[key] = named.id
            del unknown[key]

    # Otherwise suggest what the same-named rule here uses
    here = {(r.name.strip().lower(), r.rule_type): r for r in existing}
    votes: dict[str, Counter[str]] = {key: Counter() for key in unknown}
    for rule in rules:
        twin = here.get((rule.name.strip().lower(), rule.rule_type))
        if twin is None:
            continue
        theirs = [k for k in _category_ids(rule) if k in unknown]
        ours = [k for k in _category_ids(twin) if k in category_ids]
        if len(theirs) == 1 and len(ours) == 1:
            votes[theirs[0]][ours[0]] += 1
    for key, ref in unknown.items():
        if votes[key]:
            ref.suggestion = UUID(votes[key].most_common(1)[0][0])

    account_ids = {str(a.id) for a in accounts}
    unknown_accounts = [
        r.name
        for r in rules
        if any(v not in account_ids for v in _account_values(r.conditions_data))
    ]
    return ImportPlan(
        rules=rules,
        raw=kept_raw,
        unknown=sorted(unknown.values(), key=lambda u: ((u.name or "~").lower(), u.key)),
        by_name=matched,
        skipped=skipped,
        unknown_accounts=unknown_accounts,
    )


# --- Import: applying ---------------------------------------------------------------------


@dataclass
class ImportChanges:
    add: list[Rule] = field(default_factory=list)
    update: list[Rule] = field(default_factory=list)  # existing ids, file's content
    unchanged: int = 0
    paused: list[str] = field(default_factory=list)  # imported without a category


def resolve(
    plan: ImportPlan, choices: dict[str, UUID | None], existing: list[Rule]
) -> ImportChanges:
    """The rules to add and update once every unknown category has a choice
    (``choices``: file id -> category id here, or None for "no category")."""
    choices = {**plan.by_name, **choices}
    out = ImportChanges()
    here = {(r.name.strip().lower(), r.rule_type): r for r in existing}
    for rule in plan.rules:
        actions = []
        lost_category = False
        for action in rule.actions_data:
            key = str(action.get("value") or "")
            if _sets_category(action) and key in choices:
                target = choices[key]
                if target is None:
                    lost_category = True
                    continue
                action = {**action, "value": str(target)}
            actions.append({"op": action.get("op"), "value": action.get("value")})
        rule.actions = actions  # type: ignore[assignment]
        if lost_category:
            rule.is_active = False
            out.paused.append(rule.name)

        twin = here.get((rule.name.strip().lower(), rule.rule_type))
        if twin is None:
            out.add.append(rule)
        elif _same(twin, rule):
            out.unchanged += 1
        else:
            out.update.append(
                rule.model_copy(update={"id": twin.id, "created_at": twin.created_at})
            )
    return out


def _same(a: Rule, b: Rule) -> bool:
    def shape(r: Rule) -> str:
        return json.dumps(
            [
                r.priority,
                r.is_active,
                r.action_mode,
                r.config,
                r.conditions_op,
                r.conditions_data,
                [{"op": x.get("op"), "value": x.get("value")} for x in r.actions_data],
            ],
            sort_keys=True,
            default=str,
        )

    return shape(a) == shape(b)


def _sets_category(action: dict[str, Any]) -> bool:
    return action.get("op") == "set_category"


def _category_ids(rule: Rule) -> list[str]:
    return [str(a.get("value")) for a in rule.actions_data if _sets_category(a) and a.get("value")]


def _account_values(conditions: list[Any]) -> list[str]:
    """Account ids named in (possibly nested) conditions."""
    found: list[str] = []
    for c in conditions or []:
        if not isinstance(c, dict):
            continue
        if "conditions" in c:
            found += _account_values(c["conditions"])
        elif c.get("field") == "account_id":
            value = c.get("value")
            found += [str(v) for v in value] if isinstance(value, list) else [str(value)]
    return found
