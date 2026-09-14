-- Run this file once while connected to PostgreSQL's "postgres" database as
-- an administrator. Replace the placeholder before executing it.
--
-- This database belongs exclusively to the SportMatch users service.

CREATE ROLE sportmatch_users
    LOGIN
    PASSWORD 'replace-with-a-long-random-password'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT;

CREATE DATABASE users_db
    OWNER sportmatch_users
    ENCODING 'UTF8';

\connect users_db

GRANT USAGE, CREATE ON SCHEMA public TO sportmatch_users;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES TO sportmatch_users;
