BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DISABLED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE departments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    external_id text NOT NULL,
    name text NOT NULL,
    parent_id uuid REFERENCES departments(id),
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DISABLED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, external_id)
);

CREATE TABLE department_closure (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    ancestor_id uuid NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    descendant_id uuid NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    depth integer NOT NULL CHECK (depth >= 0),
    PRIMARY KEY (tenant_id, ancestor_id, descendant_id)
);

CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    wecom_user_id text NOT NULL,
    display_name text NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DISABLED')),
    permission_version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, wecom_user_id)
);

CREATE TABLE roles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    code text NOT NULL,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DISABLED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code)
);

CREATE TABLE user_roles (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, role_id)
);

CREATE TABLE user_departments (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    department_id uuid NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    is_primary boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, department_id)
);

CREATE TABLE knowledge_bases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    code text NOT NULL,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DISABLED')),
    active_index_version text NOT NULL DEFAULT 'rag-default-v1',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code)
);

CREATE TABLE documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
    title text NOT NULL,
    source_code text,
    classification_code text NOT NULL DEFAULT 'INTERNAL',
    acl_mode text NOT NULL DEFAULT 'INHERIT' CHECK (acl_mode IN ('INHERIT', 'RESTRICT')),
    status text NOT NULL DEFAULT 'UPLOADED'
        CHECK (status IN ('UPLOADED', 'PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING', 'READY', 'FAILED', 'DISABLED', 'DELETING', 'DELETED')),
    current_version_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE document_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_number integer NOT NULL CHECK (version_number > 0),
    object_key text NOT NULL,
    file_name text NOT NULL,
    file_size bigint NOT NULL CHECK (file_size >= 0),
    sha256 text NOT NULL,
    index_status text NOT NULL DEFAULT 'QUEUED'
        CHECK (index_status IN ('QUEUED', 'PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXING', 'READY', 'FAILED')),
    index_version text,
    effective_at timestamptz,
    expires_at timestamptz,
    is_current boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    indexed_at timestamptz,
    UNIQUE (tenant_id, document_id, version_number)
);

ALTER TABLE documents
    ADD CONSTRAINT documents_current_version_fk
    FOREIGN KEY (current_version_id) REFERENCES document_versions(id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE acl_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    resource_type text NOT NULL CHECK (resource_type IN ('KNOWLEDGE_BASE', 'DOCUMENT')),
    resource_id uuid NOT NULL,
    subject_type text NOT NULL CHECK (subject_type IN ('ALL_EMPLOYEES', 'DEPARTMENT', 'ROLE', 'USER')),
    subject_id uuid,
    include_descendants boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((subject_type = 'ALL_EMPLOYEES' AND subject_id IS NULL) OR
           (subject_type <> 'ALL_EMPLOYEES' AND subject_id IS NOT NULL)),
    CHECK (include_descendants = false OR subject_type = 'DEPARTMENT')
);

CREATE UNIQUE INDEX acl_entries_unique_idx
    ON acl_entries (tenant_id, resource_type, resource_id, subject_type, COALESCE(subject_id, '00000000-0000-0000-0000-000000000000'::uuid));

CREATE TABLE conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    user_id uuid NOT NULL REFERENCES users(id),
    channel text NOT NULL CHECK (channel IN ('WECOM', 'WEB', 'EVAL')),
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'CLOSED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, user_id, channel)
);

CREATE TABLE messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    user_id uuid NOT NULL REFERENCES users(id),
    request_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('USER', 'ASSISTANT')),
    content text NOT NULL,
    refused boolean NOT NULL DEFAULT false,
    refusal_reason text,
    model_name text,
    prompt_version text,
    retrieval_config_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, request_id, role)
);

CREATE TABLE inbound_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    integration_id text NOT NULL,
    external_message_id text NOT NULL,
    external_user_id text NOT NULL,
    request_id text NOT NULL,
    input_message_id uuid NOT NULL REFERENCES messages(id),
    user_id uuid NOT NULL REFERENCES users(id),
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    received_at timestamptz NOT NULL DEFAULT now(),
    queued_at timestamptz,
    UNIQUE (tenant_id, integration_id, external_message_id),
    UNIQUE (tenant_id, request_id)
);

CREATE TABLE query_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    request_id text NOT NULL,
    request_fingerprint text NOT NULL,
    user_id uuid NOT NULL REFERENCES users(id),
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    status text NOT NULL
        CHECK (status IN ('RECEIVED', 'IN_PROGRESS', 'COMPLETED', 'COMPLETED_WITH_REFUSAL', 'RETRYABLE_FAILED', 'FINAL_FAILED')),
    attempt integer NOT NULL DEFAULT 1,
    result_message_id uuid REFERENCES messages(id),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, request_id)
);

CREATE TABLE chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    knowledge_base_id uuid NOT NULL REFERENCES knowledge_bases(id),
    document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    document_version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    index_version text NOT NULL,
    ordinal integer NOT NULL,
    content text NOT NULL,
    content_hash text NOT NULL,
    heading_path text[] NOT NULL DEFAULT '{}',
    locator_type text NOT NULL,
    locator_value text NOT NULL,
    embedding vector(1024),
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, document_version_id, index_version, ordinal, content_hash)
);

CREATE TABLE citations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    citation_index integer NOT NULL CHECK (citation_index > 0),
    chunk_id uuid NOT NULL REFERENCES chunks(id),
    document_id uuid NOT NULL REFERENCES documents(id),
    document_version_id uuid NOT NULL REFERENCES document_versions(id),
    document_version_number integer NOT NULL,
    title_snapshot text NOT NULL,
    locator_type text NOT NULL,
    locator_value text NOT NULL,
    effective_at timestamptz,
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, message_id, citation_index)
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    job_type text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('QUEUED', 'IN_PROGRESS', 'RETRYING', 'SUCCEEDED', 'FAILED')),
    stage text,
    attempt integer NOT NULL DEFAULT 0,
    error_code text,
    error_message text,
    leased_until timestamptz,
    worker_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX users_tenant_status_idx ON users (tenant_id, status);
CREATE INDEX acl_entries_lookup_idx ON acl_entries (tenant_id, resource_type, resource_id);
CREATE INDEX documents_kb_status_idx ON documents (tenant_id, knowledge_base_id, status);
CREATE INDEX document_versions_current_idx ON document_versions (tenant_id, document_id) WHERE is_current;
CREATE INDEX messages_conversation_created_idx ON messages (tenant_id, conversation_id, created_at);
CREATE INDEX chunks_filter_idx ON chunks (tenant_id, knowledge_base_id, document_id, document_version_id, index_version) WHERE is_active;
CREATE INDEX chunks_search_idx ON chunks USING gin (search_vector) WHERE is_active;
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops) WHERE is_active AND embedding IS NOT NULL;
CREATE INDEX jobs_status_idx ON jobs (tenant_id, status, created_at);

INSERT INTO tenants (id, name)
VALUES ('00000000-0000-0000-0000-000000000001', '本地开发企业')
ON CONFLICT (id) DO NOTHING;

INSERT INTO knowledge_bases (id, tenant_id, code, name, description)
VALUES (
    '00000000-0000-0000-0000-000000000101',
    '00000000-0000-0000-0000-000000000001',
    'general',
    '默认知识库',
    '本地开发默认知识库'
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO acl_entries (tenant_id, resource_type, resource_id, subject_type)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'KNOWLEDGE_BASE',
    '00000000-0000-0000-0000-000000000101',
    'ALL_EMPLOYEES'
)
ON CONFLICT DO NOTHING;

COMMIT;
