from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from assistant_app.db.runtime import RuntimeDependencies

GRAPH_NAME = "user_memory"
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class GraphEntity:
    key: str
    entity_type: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphRelation:
    source_key: str
    predicate: str
    target_key: str
    confidence: float = 0.7


def _cypher_string(value: str, maximum: int = 1000) -> str:
    """Encode an untrusted value as a Cypher string literal."""

    safe = value[:maximum].replace("\\", "\\\\").replace("'", "\\'")
    safe = safe.replace("\x00", "")
    return f"'{safe}'"


def _canonical_key(value: str) -> str:
    return _SPACE_RE.sub(" ", value.strip().lower())[:300]


def _agtype_object(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    raw = str(value).strip()
    for suffix in ("::vertex", "::edge", "::path"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _prepare_age_connection(connection: Any) -> None:
    await connection.execute(text("LOAD 'age'"))
    await connection.execute(text('SET LOCAL search_path = ag_catalog, "$user", public'))


async def upsert_memory_graph(
    runtime: RuntimeDependencies,
    user_id: UUID,
    source_message_id: UUID,
    entities: list[GraphEntity],
    relations: list[GraphRelation],
) -> None:
    clean_entities = [item for item in entities if item.key and item.name.strip()][:30]
    by_key = {item.key: item for item in clean_entities}
    clean_relations = [
        item
        for item in relations[:50]
        if item.source_key in by_key and item.target_key in by_key
    ]
    if not clean_entities:
        return

    statements: list[str] = []
    aliases: dict[str, str] = {}
    user_literal = _cypher_string(str(user_id))
    source_literal = _cypher_string(str(source_message_id))
    updated_literal = _cypher_string(datetime.now(UTC).isoformat(), 64)
    for index, entity in enumerate(clean_entities):
        alias = f"entity_{index}"
        aliases[entity.key] = alias
        aliases_json = json.dumps(list(entity.aliases[:10]), ensure_ascii=False)
        statements.extend(
            [
                (
                    f"MERGE ({alias}:Entity {{user_id: {user_literal}, "
                    f"canonical_key: {_cypher_string(_canonical_key(entity.name), 300)}}})"
                ),
                (
                    f"SET {alias}.canonical_name = {_cypher_string(entity.name, 300)}, "
                    f"{alias}.entity_type = {_cypher_string(entity.entity_type, 64)}, "
                    f"{alias}.aliases_json = {_cypher_string(aliases_json, 2000)}, "
                    f"{alias}.source_message_id = {source_literal}, "
                    f"{alias}.updated_at = {updated_literal}"
                ),
            ]
        )

    for index, relation in enumerate(clean_relations):
        source_alias = aliases[relation.source_key]
        target_alias = aliases[relation.target_key]
        edge_alias = f"relation_{index}"
        confidence = max(0.0, min(1.0, float(relation.confidence)))
        predicate = relation.predicate.strip().upper().replace(" ", "_")[:80] or "RELATED_TO"
        statements.extend(
            [
                (
                    f"MERGE ({source_alias})-[{edge_alias}:RELATED {{user_id: {user_literal}, "
                    f"predicate: {_cypher_string(predicate, 80)}}}]->({target_alias})"
                ),
                (
                    f"SET {edge_alias}.confidence = {confidence:.6f}, "
                    f"{edge_alias}.source_message_id = {source_literal}"
                ),
            ]
        )
    # Returning a vertex makes AGE try to coerce the internal vertex agtype
    # through asyncpg. Return a scalar instead; callers only need completion.
    statements.append("RETURN 1")
    cypher = "\n".join(statements)
    sql = text(
        f"""
        SELECT result::text
        FROM ag_catalog.cypher('{GRAPH_NAME}', $cypher$
        {cypher}
        $cypher$) AS (result ag_catalog.agtype)
        """
    )
    async with runtime.database.begin() as connection:
        await _prepare_age_connection(connection)
        await connection.execute(sql)


async def load_memory_graph(
    runtime: RuntimeDependencies,
    user_id: UUID,
    limit: int = 200,
) -> dict[str, list[dict[str, object]]]:
    safe_limit = max(1, min(limit, 1000))
    user_literal = _cypher_string(str(user_id))
    node_sql = text(
        f"""
        SELECT node_id::text AS node_id, properties::text AS properties
        FROM ag_catalog.cypher('{GRAPH_NAME}', $cypher$
            MATCH (node:Entity)
            WHERE node.user_id = {user_literal}
            RETURN id(node), properties(node)
            LIMIT {safe_limit}
        $cypher$) AS (node_id ag_catalog.agtype, properties ag_catalog.agtype)
        """
    )
    edge_sql = text(
        f"""
        SELECT
            source_id::text AS source_id,
            source_properties::text AS source_properties,
            edge_id::text AS edge_id,
            edge_properties::text AS edge_properties,
            target_id::text AS target_id,
            target_properties::text AS target_properties
        FROM ag_catalog.cypher('{GRAPH_NAME}', $cypher$
            MATCH (source:Entity)-[edge:RELATED]->(target:Entity)
            WHERE source.user_id = {user_literal}
              AND target.user_id = {user_literal}
              AND edge.user_id = {user_literal}
            RETURN
                id(source), properties(source),
                id(edge), properties(edge),
                id(target), properties(target)
            LIMIT {safe_limit}
        $cypher$) AS (
            source_id ag_catalog.agtype,
            source_properties ag_catalog.agtype,
            edge_id ag_catalog.agtype,
            edge_properties ag_catalog.agtype,
            target_id ag_catalog.agtype,
            target_properties ag_catalog.agtype
        )
        """
    )

    async with runtime.database.connect() as connection:
        await _prepare_age_connection(connection)
        node_rows = (await connection.execute(node_sql)).mappings().all()
        edge_rows = (await connection.execute(edge_sql)).mappings().all()

    nodes: dict[str, dict[str, object]] = {}
    edges: list[dict[str, object]] = []

    def add_node(raw_id: object, raw_properties: object) -> str | None:
        internal_id = str(raw_id).strip()
        properties = _agtype_object(raw_properties)
        if not internal_id or not properties:
            return None
        nodes[internal_id] = {
            "id": internal_id,
            "label": str(properties.get("canonical_name", "未命名实体")),
            "type": str(properties.get("entity_type", "Other")),
            "properties": properties,
        }
        return internal_id

    for row in node_rows:
        add_node(row["node_id"], row["properties"])
    for row in edge_rows:
        source_id = add_node(row["source_id"], row["source_properties"])
        target_id = add_node(row["target_id"], row["target_properties"])
        properties = _agtype_object(row["edge_properties"])
        if not source_id or not target_id or not properties:
            continue
        edges.append(
            {
                "id": str(row["edge_id"]),
                "source": source_id,
                "target": target_id,
                "label": str(properties.get("predicate", "RELATED_TO")),
                "properties": properties,
            }
        )

    return {"nodes": list(nodes.values()), "edges": edges}


async def delete_memory_graph_source(
    runtime: RuntimeDependencies,
    user_id: UUID,
    source_message_id: UUID,
) -> None:
    user_literal = _cypher_string(str(user_id))
    source_literal = _cypher_string(str(source_message_id))
    statements = [
        text(
            f"""
            SELECT result::text
            FROM ag_catalog.cypher('{GRAPH_NAME}', $cypher$
                MATCH ()-[edge:RELATED]->()
                WHERE edge.user_id = {user_literal}
                  AND edge.source_message_id = {source_literal}
                DELETE edge
                RETURN count(edge)
            $cypher$) AS (result ag_catalog.agtype)
            """
        ),
        text(
            f"""
            SELECT result::text
            FROM ag_catalog.cypher('{GRAPH_NAME}', $cypher$
                MATCH (node:Entity)
                WHERE node.user_id = {user_literal}
                  AND node.source_message_id = {source_literal}
                  AND NOT (node)--()
                DELETE node
                RETURN count(node)
            $cypher$) AS (result ag_catalog.agtype)
            """
        ),
    ]
    async with runtime.database.begin() as connection:
        await _prepare_age_connection(connection)
        for statement in statements:
            await connection.execute(statement)
