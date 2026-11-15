// Oracle knowledge base schema (Neo4j 5.x Community). Idempotent: safe to re-run.
// Commit identity is repo-scoped (uid = commit:<repo>:<sha>); the old global hash constraint is dropped.
// Kept identical to infra/neo4j/schema.cypher (enforced by tests/unit/test_schema_files.py).
CREATE CONSTRAINT repo_uid    IF NOT EXISTS FOR (n:Repository) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT dir_uid     IF NOT EXISTS FOR (n:Directory)  REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT file_uid    IF NOT EXISTS FOR (n:File)       REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT symbol_uid  IF NOT EXISTS FOR (n:Symbol)     REQUIRE n.uid IS UNIQUE;
DROP CONSTRAINT commit_hash IF EXISTS;
CREATE CONSTRAINT commit_uid  IF NOT EXISTS FOR (n:Commit)     REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT author_uid  IF NOT EXISTS FOR (n:Author)     REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT branch_uid  IF NOT EXISTS FOR (n:Branch)     REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT insight_uid IF NOT EXISTS FOR (n:Insight)    REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT vuln_uid    IF NOT EXISTS FOR (n:Vulnerability) REQUIRE n.uid IS UNIQUE;
CREATE CONSTRAINT state_repo  IF NOT EXISTS FOR (n:IngestState) REQUIRE n.repo IS UNIQUE;
CREATE INDEX file_path      IF NOT EXISTS FOR (n:File)    ON (n.repo, n.path);
CREATE INDEX symbol_name    IF NOT EXISTS FOR (n:Symbol)  ON (n.name);
CREATE INDEX symbol_alive   IF NOT EXISTS FOR (n:Symbol)  ON (n.alive, n.kind);
CREATE INDEX commit_ts      IF NOT EXISTS FOR (n:Commit)  ON (n.ts);
CREATE INDEX commit_sha     IF NOT EXISTS FOR (n:Commit)  ON (n.hash);
CREATE INDEX commit_revert  IF NOT EXISTS FOR (n:Commit)  ON (n.is_revert);
CREATE INDEX insight_status IF NOT EXISTS FOR (n:Insight) ON (n.status, n.kind);
CREATE INDEX cochange_jacc  IF NOT EXISTS FOR ()-[r:CO_COMMITTED]-() ON (r.jaccard);
CREATE INDEX touched_change IF NOT EXISTS FOR ()-[r:TOUCHED]-()      ON (r.change);
CREATE FULLTEXT INDEX commit_msg IF NOT EXISTS FOR (n:Commit) ON EACH [n.message];
