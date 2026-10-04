from pathlib import Path

import httpx
import pytest
from business_signals.datasources.safety import (
    UnsafeQueryError,
    query_references_table,
    validate_cross_datasource_lookup_projection,
    validate_query_tables,
    validate_read_query,
)
from business_signals.external import ExternalResearcher
from business_signals.research_agents import (
    ApiKeyAuthentication,
    ResearchAgentCatalog,
    ResearchAgentCatalogModel,
    render_template,
)


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM sales_events",
        "WITH gone AS (DELETE FROM events RETURNING *) SELECT * FROM gone",
        "DROP TABLE products",
        "SELECT * INTO backup FROM products",
        "SELECT * FROM products FOR UPDATE",
        "SELECT 1; SELECT 2",
        "SELECT * FROM products -- bypass",
    ],
)
def test_rejects_unsafe_queries(query: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_query(query)


def test_rejects_dbapi_percent_style_parameters_before_sqlalchemy_compilation() -> None:
    with pytest.raises(UnsafeQueryError, match="percent-style"):
        validate_read_query("SELECT * FROM public.sales_events WHERE region = %(region)s")


def test_adds_and_caps_limit() -> None:
    assert validate_read_query("SELECT * FROM products", 100).endswith("LIMIT 100")
    assert validate_read_query("WITH p AS (SELECT * FROM products) SELECT * FROM p LIMIT 999", 50).endswith("LIMIT 50")


def test_allows_read_only_cte() -> None:
    result = validate_read_query("WITH totals AS (SELECT sum(revenue) AS total FROM sales) SELECT * FROM totals")
    assert result.startswith("WITH totals AS")


def test_rejects_a_table_not_discovered_for_the_selected_datasource() -> None:
    with pytest.raises(UnsafeQueryError, match="outside the selected datasource"):
        validate_query_tables("SELECT * FROM public.products", {"public.sales_events"})


def test_rejects_a_datasource_id_as_a_postgres_catalog() -> None:
    with pytest.raises(UnsafeQueryError, match="datasource IDs cannot appear"):
        validate_query_tables(
            "SELECT * FROM ds_39ac9dbd5c.public.sales_events",
            {"public.sales_events"},
        )


def test_allows_discovered_tables_and_cte_references() -> None:
    query = "WITH totals AS (SELECT sum(units) AS total FROM public.sales_events) SELECT * FROM totals"
    assert validate_query_tables(query, {"public.sales_events"}) == query


def test_recognizes_qualified_and_unqualified_metric_table_references() -> None:
    assert query_references_table("SELECT * FROM public.sales_events", "public.sales_events")
    assert query_references_table("SELECT * FROM sales_events", "public.sales_events")
    assert not query_references_table("SELECT * FROM inventory_history", "public.sales_events")


def test_cross_datasource_lookup_must_return_one_neutral_relationship_key() -> None:
    validate_cross_datasource_lookup_projection(
        "SELECT product.id AS relationship_key FROM public.products AS product WHERE product.category_id = 'outdoor'",
    )
    with pytest.raises(UnsafeQueryError, match="do not combine different keys with UNION"):
        validate_cross_datasource_lookup_projection(
            "SELECT product.id, NULL::text AS store_id FROM public.products AS product "
            "UNION ALL SELECT NULL::text, store.id FROM public.stores AS store",
        )
    with pytest.raises(UnsafeQueryError, match="relationship_key"):
        validate_cross_datasource_lookup_projection(
            "SELECT product.id AS product_id FROM public.products AS product",
        )
    with pytest.raises(UnsafeQueryError, match="approved relationship field"):
        validate_cross_datasource_lookup_projection(
            "SELECT product.category_id AS relationship_key FROM public.products AS product",
            relationship_field="public.products.id",
        )


def test_external_research_validates_bounded_dates_and_fixed_input_formats() -> None:
    researcher = ExternalResearcher()

    assert researcher._validated_period("2024-01-01", "2024-01-02")
    with pytest.raises(ValueError, match="end date"):
        researcher._validated_period("2024-02-01", "2024-01-01")
    with pytest.raises(ValueError, match="ten years"):
        researcher._validated_period("2010-01-01", "2024-01-01")
    with pytest.raises(ValueError, match="ordinary characters"):
        researcher._validated_subject("north\nitaly", "location")


def test_weather_agent_rejects_an_explanatory_sentence_as_a_location() -> None:
    researcher = ExternalResearcher()

    _, subject = researcher.validate_request("weather", "Milan, Italy", "2024-06-01", "2024-07-31")

    assert subject == "Milan, Italy"
    with pytest.raises(ValueError, match="required format"):
        researcher.validate_request(
            "weather",
            "Milan, Italy — daily weather summaries for June and July 2024",
            "2024-06-01",
            "2024-07-31",
        )


def test_research_agent_catalog_loads_builtins_and_keeps_secrets_out_of_json() -> None:
    catalog = ResearchAgentCatalog.load(Path("config/research-agents.json"))

    assert [agent.id for agent in catalog.list()] == ["weather", "economy-fx", "guardian-news", "fred-macro"]
    assert "gdeltproject" not in Path("config/research-agents.json").read_text(encoding="utf-8")
    runner = catalog.get("weather").runner
    assert runner.kind == "pipeline"
    assert [step.type for step in runner.steps] == [
        "split_strings",
        "http_json",
        "select_geographic_locations",
        "http_json",
        "sample_boundary",
        "batch_coordinates",
        "http_json",
        "summarize_daily_series",
    ]


def test_research_agent_requires_its_secret_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("GUARDIAN_API_KEY", raising=False)
    catalog = ResearchAgentCatalog.load(Path("config/research-agents.json"))

    with pytest.raises(ValueError, match="FRED_API_KEY"):
        catalog.get("fred-macro")
    with pytest.raises(ValueError, match="GUARDIAN_API_KEY"):
        catalog.get("guardian-news")


def test_research_executor_removes_query_string_secrets_from_source_links() -> None:
    url = ExternalResearcher._safe_source_url(
        "https://api.example.test/data?api_key=private&series=GDP",
        ApiKeyAuthentication(type="api_key", environment="EXAMPLE_KEY", location="query", name="api_key"),
    )

    assert url == "https://api.example.test/data?series=GDP"


@pytest.mark.asyncio
async def test_generic_research_agent_returns_a_safe_finding_when_its_api_has_no_result(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    catalog = ResearchAgentCatalog(
        ResearchAgentCatalogModel.model_validate(
            {
                "version": 1,
                "agents": [
                    {
                        "id": "public-events",
                        "name": "Public events",
                        "enabled": True,
                        "purpose": "Find relevant public events during a requested period.",
                        "subjects": ["events"],
                        "runner": {
                            "kind": "http_json",
                            "steps": [
                                {
                                    "name": "search",
                                    "url": "https://api.example.test/events",
                                    "query": {"query": "{subject}"},
                                }
                            ],
                            "response": {
                                "items_path": "articles",
                                "observation_template": "{title}",
                                "no_result_observation_template": "No matching public event was found.",
                            },
                        },
                    }
                ],
            }
        )
    )

    class EmptyResponse:
        url = httpx.URL("https://api.example.test/events?query=Milan")
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[object]]:
            return {"articles": []}

    class EmptyClient:
        async def __aenter__(self) -> "EmptyClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, *args: object, **kwargs: object) -> EmptyResponse:
            return EmptyResponse()

    monkeypatch.setattr("business_signals.external.httpx.AsyncClient", lambda **kwargs: EmptyClient())
    with caplog.at_level("INFO", logger="uvicorn.error"):
        finding = await ExternalResearcher(catalog=catalog).research(
            "public-events", "Milan", "2024-07-01", "2024-07-31", "Sales declined."
        )

    assert finding.observation == "No matching public event was found."
    assert finding.relationship == "correlated"
    assert finding.source_url == "https://api.example.test/events?query=Milan"
    assert "External agent HTTP request: agent=public-events step=search method=GET" in caplog.text
    assert "External agent HTTP response: agent=public-events step=search status=200" in caplog.text
    assert "External agent JSON response: agent=public-events step=search" in caplog.text
    assert "External agent finding: agent=public-events" in caplog.text


@pytest.mark.asyncio
async def test_generic_research_agent_reads_an_item_list_at_a_nested_json_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = ResearchAgentCatalog(
        ResearchAgentCatalogModel.model_validate(
            {
                "version": 1,
                "agents": [
                    {
                        "id": "historic-news",
                        "name": "Historic news",
                        "enabled": True,
                        "purpose": "Find reported events during a requested period.",
                        "subjects": ["news"],
                        "runner": {
                            "kind": "http_json",
                            "steps": [{"name": "search", "url": "https://api.example.test/search"}],
                            "response": {
                                "items_path": "response.results",
                                "observation_template": "{webTitle}",
                                "source_url_template": "{webUrl}",
                            },
                        },
                    }
                ],
            }
        )
    )

    class Response:
        url = httpx.URL("https://api.example.test/search")
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "response": {
                    "results": [
                        {"webTitle": "Milan event", "webUrl": "https://news.example.test/milan"}
                    ]
                }
            }

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, *args: object, **kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr("business_signals.external.httpx.AsyncClient", lambda **kwargs: Client())

    finding = await ExternalResearcher(catalog=catalog).research(
        "historic-news", "Milan", "2024-07-01", "2024-07-31", "Sales declined."
    )

    assert finding.observation == "Milan event"
    assert finding.source_url == "https://news.example.test/milan"


@pytest.mark.asyncio
async def test_guardian_agent_uses_the_documented_content_search_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the catalog aligned with Guardian's public Content API contract."""
    monkeypatch.setenv("GUARDIAN_API_KEY", "test-secret")
    catalog = ResearchAgentCatalog.load(Path("config/research-agents.json"))
    requests: list[dict[str, object]] = []

    class Response:
        url = httpx.URL(
            "https://content.guardianapis.com/search?"
            "q=Tel+Aviv&from-date=2024-07-01&to-date=2024-07-30&api-key=test-secret"
        )
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "response": {
                    "results": [
                        {
                            "webTitle": "Reported event in Tel Aviv",
                            "webUrl": "https://www.theguardian.com/world/example",
                            "sectionName": "World news",
                        }
                    ]
                }
            }

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, method: str, url: str, **kwargs: object) -> Response:
            requests.append({"method": method, "url": url, **kwargs})
            return Response()

    monkeypatch.setattr("business_signals.external.httpx.AsyncClient", lambda **kwargs: Client())

    finding = await ExternalResearcher(catalog=catalog).research(
        "guardian-news", "Tel Aviv", "2024-07-01", "2024-07-30", "Sales declined."
    )

    assert requests == [
        {
            "method": "GET",
            "url": "https://content.guardianapis.com/search",
            "params": {
                "q": "Tel Aviv",
                "from-date": "2024-07-01",
                "to-date": "2024-07-30",
                "order-by": "newest",
                "page-size": "10",
                "show-fields": "trailText",
                "api-key": "test-secret",
            },
            "headers": {},
            "json": None,
        }
    ]
    assert finding.observation == "Reported event in Tel Aviv"
    assert finding.source_title == "The Guardian: World news"
    assert "api-key" not in finding.source_url


def test_research_agent_catalog_supports_a_declarative_keyed_json_api() -> None:
    catalog = ResearchAgentCatalogModel.model_validate(
        {
            "version": 1,
            "agents": [
                {
                    "id": "footfall",
                    "name": "Store footfall",
                    "enabled": True,
                    "purpose": "Check recorded store visits for a place and time period.",
                    "subjects": ["store visits", "footfall"],
                    "runner": {
                        "kind": "http_json",
                        "authentication": {
                            "type": "api_key",
                            "environment": "FOOTFALL_API_KEY",
                            "location": "header",
                            "name": "X-Api-Key",
                        },
                        "steps": [{
                            "name": "visits",
                            "url": "https://api.example.test/v1/visits",
                            "method": "GET",
                            "query": {
                              "place": "{subject}",
                              "from": "{start_date}",
                              "to": "{end_date}"
                            }
                        }],
                        "response": {
                            "items_path": "data",
                            "observation_template": "{visits} visits",
                        },
                    },
                }
            ],
        }
    )

    runner = catalog.agents[0].runner
    assert runner.kind == "http_json"
    assert runner.steps[0].url == "https://api.example.test/v1/visits"


def test_research_agent_templates_do_not_allow_attribute_access() -> None:
    assert render_template("{subject} during {start_date}", {"subject": "Milan", "start_date": "2024-01-01"})
    with pytest.raises(ValueError, match="simple named"):
        render_template("{subject.__class__}", {"subject": "Milan"})
