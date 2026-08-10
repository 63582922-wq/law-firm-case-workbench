-- Every externally approved cost ceiling must state its currency. Existing
-- append-only rows retain the ISO 4217 no-currency marker XXX; new requests
-- are rejected by the application unless they specify an actual currency.

BEGIN;

ALTER TABLE external_request_authorizations
    ADD COLUMN cost_currency char(3) NOT NULL DEFAULT 'XXX'
    CHECK (cost_currency ~ '^[A-Z]{3}$');

COMMIT;
