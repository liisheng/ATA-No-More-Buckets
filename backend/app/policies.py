from __future__ import annotations

from dataclasses import dataclass

from .models import (
    CompletionEvidence,
    CompletionPhotoFacts,
    EvidenceAssessment,
    Incident,
    ObservableFacts,
    PropertyConfig,
    Vendor,
    WorkOrder,
)


@dataclass(frozen=True)
class SafetyDecision:
    safe_to_contain: bool
    escalate: bool
    rule_id: str
    reason: str | None = None


def evaluate_safety(facts: ObservableFacts) -> SafetyDecision:
    if facts.source_confidence < 0.75:
        return SafetyDecision(
            False, True, "FACTS_LOW_CONFIDENCE", "observable evidence confidence is low"
        )
    if facts.issue_type.value == "unknown":
        return SafetyDecision(
            False, True, "FACTS_UNRECOGNIZABLE_INCIDENT", "no recognizable plumbing incident was established"
        )
    if facts.occupant_danger:
        return SafetyDecision(False, True, "SAFETY_OCCUPANT_DANGER", "occupant danger reported")
    if facts.electrical_hazard:
        return SafetyDecision(
            False, True, "SAFETY_ELECTRICAL_HAZARD", "water and electricity may interact"
        )
    if facts.structural_hazard:
        return SafetyDecision(False, True, "SAFETY_STRUCTURAL_HAZARD", "possible structural damage")
    if facts.gas_hazard:
        return SafetyDecision(
            False, True, "SAFETY_GAS_HAZARD", "gas-related danger reported"
        )
    if facts.uncontrolled_flooding or (
        facts.issue_type.value == "flood" and facts.severity.value == "critical"
    ):
        return SafetyDecision(
            False, True, "SAFETY_UNCONTROLLED_FLOODING", "critical uncontrolled flooding reported"
        )
    return SafetyDecision(True, False, "SAFETY_CONTAINMENT_ALLOWED")


def property_specific_containment(config: PropertyConfig, facts: ObservableFacts) -> str:
    if facts.electrical_hazard:
        return (
            f"Do not touch outlets or appliances near water at {config.display_name}. "
            f"Move away from the area and call {config.emergency_contact}."
        )
    return (
        f"At {config.display_name}, place a container under the visible leak if safe. "
        f"Close the unit shutoff using this configured instruction: {config.under_sink_valve_instructions} "
        "Do not open walls or ceilings. "
        f"Keep belongings and children away from standing water, then reply when the shutoff is closed."
    )


def build_bounded_work_order(
    incident: Incident, config: PropertyConfig, facts: ObservableFacts
) -> WorkOrder:
    estimate = round(facts.estimated_cost, 2) if facts.estimated_cost is not None else None
    scope = (
        "Diagnose and repair the reported under-sink plumbing leak. Stop water loss, identify the failed "
        "component, and document before/after condition. Do not exceed S$250 without approval."
    )
    return WorkOrder(
        work_order_id=f"wo_{incident.incident_id}",
        incident_id=incident.incident_id,
        property_name=config.display_name,
        scope=scope,
        currency=config.currency,
        spending_limit=config.spending_limit,
        estimated_cost=estimate,
        authorized_amount=config.spending_limit,
        status=(
            "bounded"
            if estimate is None or estimate <= config.spending_limit
            else "approval_required"
        ),
        approved=estimate is None or estimate <= config.spending_limit,
    )


def requires_spending_approval(work_order: WorkOrder) -> bool:
    return (
        work_order.estimated_cost is not None
        and work_order.estimated_cost > work_order.spending_limit
        and not work_order.approved
    )


def rank_eligible_vendors(vendors: list[Vendor], config: PropertyConfig) -> list[Vendor]:
    eligible = [
        vendor
        for vendor in vendors
        if vendor.active
        and vendor.insured
        and config.region == vendor.region
        and "plumbing" in vendor.trades
    ]
    return sorted(
        eligible, key=lambda vendor: (vendor.response_minutes, vendor.distance_km, vendor.vendor_id)
    )


def assess_completion(
    evidence: CompletionEvidence,
    photo_facts: CompletionPhotoFacts,
    work_order: WorkOrder | None,
    expected_vendor_id: str | None,
) -> EvidenceAssessment:
    photo_present = evidence.photo is not None
    photo_matches = photo_facts.photo_matches
    invoice_present = evidence.invoice is not None
    invoice_scope_match = False
    invoice_total = evidence.invoice.total if evidence.invoice else None
    blocking_reasons: list[str] = []

    if not photo_present:
        blocking_reasons.append("completion photo is missing")
    if photo_present and not photo_matches:
        blocking_reasons.append("completion photo does not match the reported repair")
    if photo_present and photo_facts.photo_match_confidence < 0.85:
        blocking_reasons.append("completion photo confidence is below 0.85")
    if not invoice_present:
        blocking_reasons.append("invoice is missing")
    else:
        invoice = evidence.invoice
        assert invoice is not None
        if expected_vendor_id and invoice.vendor_id != expected_vendor_id:
            blocking_reasons.append("invoice vendor does not match assigned vendor")
        if work_order and invoice.currency != work_order.currency:
            blocking_reasons.append("invoice currency does not match the work order")
        allowed_terms = ("plumb", "leak", "repair", "labor", "part", "diagnostic")
        invoice_scope_match = all(
            any(term in item.description.lower() for term in allowed_terms)
            for item in invoice.line_items
        )
        if not invoice_scope_match:
            blocking_reasons.append("invoice contains out-of-scope line items")
    authorized_amount = (
        (work_order.authorized_amount or work_order.spending_limit) if work_order else 0
    )
    within_spending_limit = (
        invoice_total is not None and work_order is not None and invoice_total <= authorized_amount
    )
    if invoice_present and not within_spending_limit:
        blocking_reasons.append("invoice exceeds approved spending limit")
    return EvidenceAssessment(
        photo_present=photo_present,
        photo_matches=photo_matches,
        photo_confidence=photo_facts.photo_match_confidence,
        invoice_present=invoice_present,
        invoice_scope_match=invoice_scope_match,
        invoice_total=invoice_total,
        within_spending_limit=within_spending_limit,
        passed=not blocking_reasons,
        blocking_reasons=blocking_reasons,
    )
