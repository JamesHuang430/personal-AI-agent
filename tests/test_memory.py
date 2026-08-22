from assistant_app.services.age_graph import _agtype_object, _cypher_string
from assistant_app.services.memory import _parse_extraction, _safe_memory


def test_memory_extraction_parses_json_code_fence() -> None:
    result = _parse_extraction(
        """```json
        {
          "memories": [{
            "memory_type": "preference",
            "content": "用户喜欢安静的旅行",
            "confidence": 0.9,
            "importance": 0.8
          }],
          "entities": [{
            "key": "user",
            "entity_type": "Person",
            "name": "当前用户",
            "aliases": []
          }],
          "relations": []
        }
        ```"""
    )

    assert result.memories[0].content == "用户喜欢安静的旅行"
    assert result.entities[0].key == "user"


def test_sensitive_memory_is_rejected() -> None:
    extraction = _parse_extraction(
        '{"memories":[{"memory_type":"fact","content":"API Key 是 abc",'
        '"confidence":1,"importance":1}],"entities":[],"relations":[]}'
    )

    assert not _safe_memory(extraction.memories[0])


def test_cypher_literal_escapes_untrusted_text() -> None:
    assert _cypher_string("Alice's \\ notes") == "'Alice\\'s \\\\ notes'"


def test_agtype_vertex_is_decoded_for_graph_api() -> None:
    parsed = _agtype_object(
        '{"id": 1, "label": "Entity", "properties": {"canonical_name": "北京"}}::vertex'
    )

    assert parsed is not None
    assert parsed["properties"]["canonical_name"] == "北京"
