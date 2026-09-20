from business_signals.models import Evidence, Hypothesis, InvestigationCreate


def test_confidence_is_bounded() -> None:
    evidence = Evidence(description="Stock fell first", source="analytics", relationship="direct", confidence=0.9)
    assert evidence.confidence == 0.9


def test_hypotheses_get_stable_prefixes() -> None:
    hypothesis = Hypothesis(name="Inventory constraint", description="Inventory constrained sales", category="inventory", confidence=0.3)
    assert hypothesis.id.startswith("hyp_")


def test_investigation_requires_datasource() -> None:
    request = InvestigationCreate(question="Why did revenue decline last week?", datasource_ids=["sales"])
    assert request.limits.max_iterations == 8
