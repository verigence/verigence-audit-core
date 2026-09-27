"""Phase 2 template registry -- documents, stage gates, controls and tasks.

The YAML files under ``audit_core/p2_templates`` are the single executable
description of what the audit expects: which documents exist, which stage
owns them, how their pages group, which fields must be verified and at what
confidence, which controls they feed and which tasks those controls raise.
P2 code asks the registry instead of hard-coding document or rule lists.

The registry is validated on load (``validate_registry``) and again in CI, so
a template can never reference an unknown DI type, field, control, gate
document or task type.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_TEMPLATE_DIR = Path(__file__).with_name("p2_templates")

REQUIREMENT_LEVELS = frozenset({"REQUIRED", "OPTIONAL", "CONDITIONAL", "SUPPORTING"})
PAGE_SHAPES = frozenset({"SINGLE", "PARTS", "MULTI_PAGE"})
PAGE_GROUPS = frozenset({"ADJACENT", "BATCH"})
STAGES = ("BOOKING", "DELIVERY")
GATE_KINDS = frozenset({"DOCUMENT_READY", "PAYMENT_MINIMUM", "NO_OPEN_TASKS", "FIELDS_REVIEWED"})
CONTROL_EXECUTORS = frozenset({"NATIVE", "EXTERNAL_RULE_ENGINE"})
CONTROL_MODES = frozenset({"RERUN", "EXTERNAL", "GATE", "PAGES", "SYNC", "EVENT"})
# Readiness gates computed by the stage engine outside the configured gate list.
SYNTHETIC_GATES = frozenset({"DELIVERY:REQUIRED_DOCUMENTS"})
SUPPORTING_TEMPLATE = "supporting_document"

# Rule Engine operands that name documents the current Audit Core checklist
# does not carry (blueprint v2.2 section 10: kept exactly as the Rule Engine
# defines them). Controls depending on them stay WAITING_FOR_FACTS.
EXTERNAL_ONLY_OPERAND_DOCUMENTS = frozenset(
    {"purchase_order", "debit_note", "valuation_report", "discount_approval_form"}
)


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class PageRule:
    shape: str
    max_pages: int
    group: str
    absorb_unknown: bool = False


@dataclass(frozen=True)
class DocumentTemplate:
    key: str
    display_name: str
    di_types: tuple[str, ...]
    stage: str
    requirement: str
    condition: str | None
    accepted_early: bool
    pages: PageRule
    di_schema: str
    di_schema_version: str | None
    key_fields: tuple[str, ...]
    review_threshold: float
    strict_fields: frozenset[str]
    strict_review_threshold: float | None
    controls: tuple[str, ...]
    journey_360: tuple[str, ...]
    processors: tuple[str, ...]
    task_effects: tuple[str, ...]
    persistence: dict[str, Any]
    notes: str | None = None

    @property
    def is_supporting(self) -> bool:
        return self.requirement == "SUPPORTING"

    def review_threshold_for(self, field_key: str) -> float:
        if field_key in self.strict_fields and self.strict_review_threshold is not None:
            return self.strict_review_threshold
        return self.review_threshold

    def needs_review(self, field_key: str, confidence: float | None) -> bool:
        """Missing confidence is never trusted."""
        if confidence is None:
            return True
        return float(confidence) < self.review_threshold_for(field_key)


@dataclass(frozen=True)
class Gate:
    key: str
    kind: str
    label: str
    missing: str
    documents: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageTemplate:
    code: str
    order: int
    states: tuple[str, ...]
    gates: tuple[Gate, ...]
    completion_approved: bool


@dataclass(frozen=True)
class ControlTemplate:
    code: str
    executor: str
    category: str
    task_type: str
    task_owner: str
    severity: str | None = None
    owner: str | None = None
    finding_class: str | None = None
    resolution: str | None = None
    rerun: str | None = None
    triggers: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()
    operands: dict[str, Any] = field(default_factory=dict)
    depends_on_documents: tuple[str, ...] = ()
    mode: str = "EXTERNAL"
    gates: tuple[str, ...] = ()
    page_statuses: tuple[str, ...] = ()
    event: str | None = None
    rule_engine_phases: tuple[str, ...] = ()

    def applies_to_stage(self, stage: str) -> bool:
        return not self.phases or stage in self.phases


@dataclass(frozen=True)
class TaskTemplate:
    task_type: str
    category: str
    owner: str
    origin: str
    source: str
    completion: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    documents: dict[str, DocumentTemplate]
    stages: dict[str, StageTemplate]
    controls: dict[str, ControlTemplate]
    tasks: dict[str, TaskTemplate]
    actions: frozenset[str]
    di_schemas: dict[str, dict[str, Any]]
    di_source: dict[str, Any]

    # ------------------------------------------------------------- lookups
    def document(self, key: str) -> DocumentTemplate:
        try:
            return self.documents[key]
        except KeyError as exc:
            raise RegistryError(f"Unknown document template {key!r}") from exc

    def templates_for_di_type(self, di_type: str | None) -> list[DocumentTemplate]:
        if not di_type:
            return []
        return [t for t in self.documents.values() if di_type in t.di_types]

    def template_for_di_type(self, di_type: str | None, *, stage: str | None = None) -> DocumentTemplate:
        """Business template for a DI classification.

        Receipts are one DI type (dealer_receipt) but two business documents
        (Booking vs Delivery payment); ``stage`` disambiguates. Anything DI
        could not classify is a supporting document, never an error."""
        matches = self.templates_for_di_type(di_type)
        if not matches:
            return self.documents[SUPPORTING_TEMPLATE]
        if len(matches) > 1 and stage:
            for template in matches:
                if template.stage == stage:
                    return template
        return matches[0]

    def candidate_di_types(self) -> list[str]:
        """Every DI classification key a P2 page may legitimately be."""
        seen: dict[str, None] = {}
        for template in self.documents.values():
            for di_type in template.di_types:
                seen.setdefault(di_type, None)
        return list(seen)

    def stage_documents(self, stage: str, *, conditions: set[str] | None = None) -> list[DocumentTemplate]:
        """Checklist documents for a stage: REQUIRED/OPTIONAL plus applicable CONDITIONAL."""
        active = conditions or set()
        return [
            t for t in self.documents.values()
            if t.stage == stage
            and not t.is_supporting
            and (t.requirement != "CONDITIONAL" or (t.condition in active))
        ]

    def required_documents(self, stage: str, *, conditions: set[str] | None = None) -> list[DocumentTemplate]:
        return [
            t for t in self.stage_documents(stage, conditions=conditions)
            if t.requirement in {"REQUIRED", "CONDITIONAL"}
        ]

    def controls_for_document(self, key: str) -> list[ControlTemplate]:
        template = self.document(key)
        by_dependency = [c for c in self.controls.values() if key in c.depends_on_documents]
        declared = [self.controls[c] for c in template.controls if c in self.controls]
        merged: dict[str, ControlTemplate] = {c.code: c for c in declared + by_dependency}
        return list(merged.values())

    def controls_by_mode(self, mode: str) -> list[ControlTemplate]:
        return [c for c in self.controls.values() if c.mode == mode]

    def di_fields(self, di_type: str) -> list[dict[str, Any]]:
        return list((self.di_schemas.get(di_type) or {}).get("fields") or [])


def _load_yaml(name: str) -> dict[str, Any]:
    with (_TEMPLATE_DIR / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in (value or []))


def build_registry(
    documents_doc: dict[str, Any],
    controls_doc: dict[str, Any],
    tasks_doc: dict[str, Any],
    di_snapshot: dict[str, Any],
) -> Registry:
    documents: dict[str, DocumentTemplate] = {}
    for key, raw in (documents_doc.get("document_types") or {}).items():
        pages = raw.get("pages") or {}
        extraction = raw.get("extraction") or {}
        documents[key] = DocumentTemplate(
            key=key,
            display_name=str(raw["display_name"]),
            di_types=_tuple(raw.get("di_types")),
            stage=str(raw["stage"]),
            requirement=str(raw["requirement"]),
            condition=raw.get("condition"),
            accepted_early=bool(raw.get("accepted_early", True)),
            pages=PageRule(
                shape=str(pages.get("shape", "SINGLE")),
                max_pages=int(pages.get("max_pages", 1)),
                group=str(pages.get("group", "ADJACENT")),
                absorb_unknown=bool(pages.get("absorb_unknown", False)),
            ),
            di_schema=str(extraction.get("di_schema", "FALLBACK")),
            di_schema_version=extraction.get("di_schema_version"),
            key_fields=_tuple(extraction.get("key_fields")),
            review_threshold=float(extraction.get("review_threshold", 90)),
            strict_fields=frozenset(_tuple(extraction.get("strict_fields"))),
            strict_review_threshold=(
                float(extraction["strict_review_threshold"])
                if extraction.get("strict_review_threshold") is not None
                else None
            ),
            controls=_tuple(raw.get("controls")),
            journey_360=_tuple(raw.get("journey_360")),
            processors=_tuple(raw.get("processors")),
            task_effects=_tuple(raw.get("task_effects")),
            persistence=dict(raw.get("persistence") or {}),
            notes=raw.get("notes"),
        )

    stages: dict[str, StageTemplate] = {}
    for code, raw in (documents_doc.get("stages") or {}).items():
        stages[code] = StageTemplate(
            code=code,
            order=int(raw["order"]),
            states=_tuple(raw.get("states")),
            gates=tuple(
                Gate(
                    key=str(g["key"]),
                    kind=str(g["kind"]),
                    label=str(g["label"]),
                    missing=str(g["missing"]),
                    documents=_tuple(g.get("documents")),
                    task_types=_tuple(g.get("task_types")),
                )
                for g in raw.get("gates") or []
            ),
            completion_approved=bool(raw.get("completion_approved", True)),
        )

    controls: dict[str, ControlTemplate] = {}
    for code, raw in (controls_doc.get("controls") or {}).items():
        task = raw.get("task") or {}
        controls[code] = ControlTemplate(
            code=code,
            executor=str(raw["executor"]),
            category=str(raw.get("category") or ""),
            task_type=str(task.get("type") or ""),
            task_owner=str(task.get("owner") or ""),
            severity=raw.get("severity"),
            owner=raw.get("owner"),
            finding_class=raw.get("finding_class"),
            resolution=raw.get("resolution"),
            rerun=raw.get("rerun"),
            triggers=_tuple(raw.get("triggers")),
            phases=_tuple(raw.get("phases")),
            operands=dict(raw.get("operands") or {}),
            depends_on_documents=_tuple(raw.get("depends_on_documents")),
            mode=str(raw.get("mode") or "EXTERNAL"),
            gates=_tuple(raw.get("gates")),
            page_statuses=_tuple(raw.get("page_statuses")),
            event=raw.get("event"),
            rule_engine_phases=_tuple(raw.get("rule_engine_phases")),
        )

    tasks = {
        task_type: TaskTemplate(
            task_type=task_type,
            category=str(raw["category"]),
            owner=str(raw["owner"]),
            origin=str(raw["origin"]),
            source=str(raw["source"]),
            completion=str(raw["completion"]),
            actions=_tuple(raw.get("actions")),
        )
        for task_type, raw in (tasks_doc.get("task_types") or {}).items()
    }

    return Registry(
        documents=documents,
        stages=stages,
        controls=controls,
        tasks=tasks,
        actions=frozenset(_tuple(tasks_doc.get("actions"))),
        di_schemas=dict(di_snapshot.get("schemas") or {}),
        di_source=dict(di_snapshot.get("source") or {}),
    )


def validate_registry(registry: Registry) -> list[str]:
    """Every inconsistency as a readable message; empty means valid."""
    problems: list[str] = []
    known_di = set(registry.di_schemas)
    if SUPPORTING_TEMPLATE not in registry.documents:
        problems.append(f"missing the {SUPPORTING_TEMPLATE} template")

    for key, t in registry.documents.items():
        where = f"document {key}"
        if t.stage not in (*STAGES, "ANY"):
            problems.append(f"{where}: unknown stage {t.stage}")
        if t.requirement not in REQUIREMENT_LEVELS:
            problems.append(f"{where}: unknown requirement {t.requirement}")
        if t.requirement == "CONDITIONAL" and not t.condition:
            problems.append(f"{where}: CONDITIONAL requirement without a condition")
        if t.pages.shape not in PAGE_SHAPES:
            problems.append(f"{where}: unknown page shape {t.pages.shape}")
        if t.pages.group not in PAGE_GROUPS:
            problems.append(f"{where}: unknown page group {t.pages.group}")
        if t.pages.shape != "SINGLE" and t.pages.max_pages < 2:
            problems.append(f"{where}: multi-page shape needs max_pages >= 2")
        if not t.di_types and key != SUPPORTING_TEMPLATE:
            problems.append(f"{where}: no DI classification type")
        for di_type in t.di_types:
            if t.di_schema != "FALLBACK" and di_type not in known_di:
                problems.append(f"{where}: DI type {di_type} is not in the DI schema snapshot")
        if t.di_types and t.di_schema != "FALLBACK":
            field_keys = {f["key"] for f in registry.di_fields(t.di_types[0])}
            for field_key in (*t.key_fields, *sorted(t.strict_fields)):
                if field_key not in field_keys:
                    problems.append(f"{where}: field {field_key} is not extracted by DI {t.di_types[0]}")
        if t.strict_fields and t.strict_review_threshold is None:
            problems.append(f"{where}: strict_fields without strict_review_threshold")
        if not 0 < t.review_threshold <= 100:
            problems.append(f"{where}: review_threshold out of range")
        for control in t.controls:
            if control not in registry.controls:
                problems.append(f"{where}: unknown control {control}")

    for code, stage in registry.stages.items():
        for gate in stage.gates:
            where = f"stage {code} gate {gate.key}"
            if gate.kind not in GATE_KINDS:
                problems.append(f"{where}: unknown kind {gate.kind}")
            for document in gate.documents:
                if document not in registry.documents:
                    problems.append(f"{where}: unknown document {document}")
            if gate.kind == "DOCUMENT_READY" and not gate.documents:
                problems.append(f"{where}: DOCUMENT_READY gate without documents")
            for task_type in gate.task_types:
                if task_type not in registry.tasks:
                    problems.append(f"{where}: unknown task type {task_type}")

    for code, control in registry.controls.items():
        where = f"control {code}"
        if control.executor not in CONTROL_EXECUTORS:
            problems.append(f"{where}: unknown executor {control.executor}")
        if control.mode not in CONTROL_MODES:
            problems.append(f"{where}: unknown mode {control.mode}")
        if (control.mode == "EXTERNAL") != (control.executor == "EXTERNAL_RULE_ENGINE"):
            problems.append(f"{where}: mode {control.mode} does not match executor {control.executor}")
        known_gates = {
            f"{code}:{gate.key}" for code, stage in registry.stages.items() for gate in stage.gates
        } | SYNTHETIC_GATES
        if control.mode == "GATE" and not control.gates:
            problems.append(f"{where}: GATE control without gates")
        for gate in control.gates:
            if gate not in known_gates:
                problems.append(f"{where}: unknown gate {gate}")
        if control.mode == "EVENT" and not control.event:
            problems.append(f"{where}: EVENT control without an event")
        if control.task_type not in registry.tasks:
            problems.append(f"{where}: unknown task type {control.task_type}")
        for document in control.depends_on_documents:
            if document not in registry.documents and document not in EXTERNAL_ONLY_OPERAND_DOCUMENTS:
                problems.append(f"{where}: depends on unknown document {document}")

    for task_type, task in registry.tasks.items():
        for action in task.actions:
            if action not in registry.actions:
                problems.append(f"task {task_type}: unknown action {action}")
        if task.completion not in {"MACHINE_VERIFIED", "REQUESTER_CONFIRMED"}:
            problems.append(f"task {task_type}: unknown completion protocol {task.completion}")
    return problems


@lru_cache(maxsize=1)
def get_registry() -> Registry:
    registry = build_registry(
        _load_yaml("document_templates.yaml"),
        _load_yaml("control_templates.yaml"),
        _load_yaml("task_templates.yaml"),
        json.loads((_TEMPLATE_DIR / "di_schema_snapshot.json").read_text(encoding="utf-8")),
    )
    problems = validate_registry(registry)
    if problems:
        raise RegistryError("Invalid P2 templates:\n- " + "\n- ".join(problems))
    return registry
