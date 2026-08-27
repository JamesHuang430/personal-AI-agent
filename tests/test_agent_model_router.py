from assistant_app.services.agent_model_router import route_agent_models


def test_agent_model_router_prefers_reasoning_and_multimodal_models() -> None:
    assignments = {
        item["agent"]: item
        for item in route_agent_models(
            ["glm-4-air", "deepseek-reasoner", "qwen2.5-vl-72b"]
        )
    }

    assert assignments["story"]["model"] == "deepseek-reasoner"
    assert assignments["visual"]["model"] == "qwen2.5-vl-72b"
    assert assignments["media"]["model"] == "video+speech+ffmpeg"
    assert assignments["quality"]["model"] == "ffprobe+quality-rules"


def test_agent_model_router_degrades_to_any_available_model() -> None:
    assignments = route_agent_models(["custom-model"])

    assert len(assignments) == 4
    assert assignments[0]["model"] == "custom-model"
    assert assignments[1]["model"] == "custom-model"
    assert assignments[2]["status"] == "tool"
    assert assignments[3]["status"] == "tool"


def test_agent_model_router_reports_unavailable() -> None:
    assignments = route_agent_models([])

    assert len(assignments) == 4
    assert [item["status"] for item in assignments] == [
        "unavailable",
        "unavailable",
        "tool",
        "tool",
    ]
