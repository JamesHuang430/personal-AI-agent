# syntax=docker/dockerfile:1.7
# Both sources are official, version-pinned PostgreSQL 16 images. Copying the
# AGE artifacts avoids shipping compilers or fetching source during builds,
# while the final base continues to provide pgvector.
FROM apache/age:release_PG16_1.6.0 AS age

FROM pgvector/pgvector:0.8.6-pg16-trixie

COPY --from=age /usr/lib/postgresql/16/lib/age.so /usr/lib/postgresql/16/lib/age.so
COPY --from=age /usr/share/postgresql/16/extension/age* /usr/share/postgresql/16/extension/
