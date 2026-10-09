#!/bin/sh
set -eu

psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set identity_password="$IDENTITY_DATABASE_PASSWORD" \
  --set catalog_password="$CATALOG_DATABASE_PASSWORD" \
  --set processing_password="$PROCESSING_DATABASE_PASSWORD" \
  --set search_password="$SEARCH_DATABASE_PASSWORD" <<'SQL'
CREATE ROLE memes_identity LOGIN PASSWORD :'identity_password';
CREATE ROLE memes_catalog LOGIN PASSWORD :'catalog_password';
CREATE ROLE memes_processing LOGIN PASSWORD :'processing_password';
CREATE ROLE memes_search LOGIN PASSWORD :'search_password';

CREATE DATABASE memes_identity OWNER memes_identity;
CREATE DATABASE memes_catalog OWNER memes_catalog;
CREATE DATABASE memes_processing OWNER memes_processing;
CREATE DATABASE memes_search OWNER memes_search;

REVOKE ALL ON DATABASE memes_identity FROM PUBLIC;
REVOKE ALL ON DATABASE memes_catalog FROM PUBLIC;
REVOKE ALL ON DATABASE memes_processing FROM PUBLIC;
REVOKE ALL ON DATABASE memes_search FROM PUBLIC;

\connect memes_identity
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO memes_identity;

\connect memes_catalog
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO memes_catalog;

\connect memes_processing
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO memes_processing;

\connect memes_search
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO memes_search;
SQL
