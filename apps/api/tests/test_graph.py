from business_signals.datasources.registry import DatasourceRegistry
from business_signals.engine import InvestigationEngine


def test_graph_has_real_investigation_cycles() -> None:
    graph = InvestigationEngine(DatasourceRegistry()).graph.get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert ("decide", "select_investigation") in edges
    assert ("human_input", "select_investigation") in edges
    assert ("external_research", "decide") in edges
