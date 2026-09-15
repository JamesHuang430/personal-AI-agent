\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('validation_graph');
SELECT *
FROM cypher('validation_graph', $$
    CREATE (:Entity {name: '测试实体'})
    RETURN 1
$$) AS (result agtype);
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('vector', 'age')
ORDER BY extname;
