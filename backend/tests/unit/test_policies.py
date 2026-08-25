from app.models import (
    CompletionEvidence,
    CompletionPhotoFacts,
    Invoice,
    InvoiceLineItem,
    ObservableFacts,
    PropertyConfig,
    Vendor,
    WorkOrder,
)
from app.policies import (
    assess_completion,
    evaluate_safety,
    rank_eligible_vendors,
    requires_spending_approval,
)


def test_vendor_ranking_filters_and_orders_eligible_vendors() -> None:
    config = PropertyConfig(property_id="p", display_name="P", region="demo")
    vendors = [
        Vendor(vendor_id="slow", name="Slow", region="demo", response_minutes=90),
        Vendor(vendor_id="fast", name="Fast", region="demo", response_minutes=30),
        Vendor(vendor_id="wrong-region", name="Wrong", region="other"),
        Vendor(vendor_id="inactive", name="Inactive", region="demo", active=False),
    ]
    assert [vendor.vendor_id for vendor in rank_eligible_vendors(vendors, config)] == [
        "fast",
        "slow",
    ]


def test_spending_limit_requires_approval() -> None:
    work_order = WorkOrder(
        work_order_id="wo",
        incident_id="inc",
        scope="repair",
        currency="SGD",
        spending_limit=500,
        estimated_cost=501,
    )
    assert requires_spending_approval(work_order)


def test_evidence_gate_rejects_mismatch_and_accepts_scoped_invoice(completion_media) -> None:
    work_order = WorkOrder(
        work_order_id="wo",
        incident_id="inc",
        scope="repair",
        currency="SGD",
        spending_limit=250,
        estimated_cost=400,
    )
    bad = CompletionEvidence(photo=completion_media)
    bad_result = assess_completion(
        bad, CompletionPhotoFacts(photo_matches=False, photo_match_confidence=0.99), work_order, "vendor-b"
    )
    assert not bad_result.passed
    assert any("does not match" in reason for reason in bad_result.blocking_reasons)
    good = CompletionEvidence(
        photo=completion_media,
        invoice=Invoice(
            invoice_id="invoice",
            vendor_id="vendor-b",
            total=220,
            line_items=[
                InvoiceLineItem(
                    description="leak repair labor and parts", quantity=1, unit_price=220
                )
            ],
        ),
    )
    assert assess_completion(
        good, CompletionPhotoFacts(photo_matches=True, photo_match_confidence=0.95), work_order, "vendor-b"
    ).passed


def test_conflicting_or_low_confidence_facts_escalate() -> None:
    assert evaluate_safety(ObservableFacts(source_confidence=0.4)).rule_id == "FACTS_LOW_CONFIDENCE"
    assert evaluate_safety(ObservableFacts(uncertainties=["photo conflicts with text"], source_confidence=0.95)).rule_id == "FACTS_CONFLICT_OR_UNCERTAIN"
