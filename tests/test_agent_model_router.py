from assistant_app.services.agent_model_router import route_agent_models


def test_agent_model_router_prefers_reasoning_and_multimodal_models() -> None:
    assignments = {
        item["agent"]: item
        for item in route_agent_models(
            ["glm-4-air", "deepseek-reasoner", "qwen2.5-vl-72b"]
        )
    }

    assert assignments["director"]["model"] == "deepseek-reasoner"
    assert assignments["script"]["model"] == "deepseek-reasoner"
    assert assignments["assets"]["model"] == "qwen2.5-vl-72b"
    assert assignments["storyboard"]["model"] == "qwen2.5-vl-72b"
    assert assignments["video"]["model"] == "qwen2.5-vl-72b"


def test_agent_model_router_degrades_to_any_available_model() -> None:
    assignments = route_agent_models(["custom-model"])

    assert len(assignments) == 9
    assert all(item["model"] == "custom-model" for item in assignments)
    assert all(item["status"] == "fallback" for item in assignments)


def test_agent_model_router_reports_unavailable() -> None:
    assignments = route_agent_models([])

    assert len(assignments) == 9
    assert all(item["status"] == "unavailable" for item in assignments)

