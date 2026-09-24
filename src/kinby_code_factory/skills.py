"""Resolve the skills package.yaml selects through kinby's instance, package, workspace tiers."""

from kinby.instance import Instance
from kinby.plugins import Skill, load_skills

from kinby_code_factory.clients import CodingClientError, ImplementationSkills
from kinby_code_factory.config import Skills


def implementation_skills(instance: Instance, selection: Skills) -> ImplementationSkills:
    """The implement and pull request skills, and every skill the instance offers."""
    skills = _skills(instance)
    return ImplementationSkills(
        implement=_selected(skills, selection.implement),
        pull_request=_selected(skills, selection.pull_request),
        available=skills,
    )


def review_skill(instance: Instance, selection: Skills) -> Skill:
    """The skill whose reviewer briefs both review axes follow."""
    return _selected(_skills(instance), selection.review)


def _skills(instance: Instance) -> tuple[Skill, ...]:
    skills, _ = load_skills(instance)
    return skills


def _selected(skills: tuple[Skill, ...], name: str) -> Skill:
    skill = next((skill for skill in skills if skill.name == name), None)
    if skill is None:
        raise CodingClientError(f'package.yaml selects skill "{name}", which this instance lacks')
    return skill
