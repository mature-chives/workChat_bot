BEGIN;

CREATE TABLE outbound_deliveries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    event_id uuid NOT NULL,
    request_id text NOT NULL,
    message_id uuid NOT NULL REFERENCES messages(id),
    recipient_user_id uuid NOT NULL REFERENCES users(id),
    channel text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'SENDING', 'SENT', 'RETRYABLE_FAILED', 'FINAL_FAILED')),
    attempt integer NOT NULL DEFAULT 0,
    external_message_id text,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    UNIQUE (tenant_id, event_id)
);

CREATE INDEX outbound_deliveries_status_idx
    ON outbound_deliveries (tenant_id, status, updated_at);

COMMIT;
