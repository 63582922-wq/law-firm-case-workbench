/**
 * The only calculation payload exposed by the Alpha browser shell. It is
 * deliberately synthetic and is sent to the local synthetic API for a real
 * deterministic preview; no browser-side monetary calculation is performed.
 */

export const alphaCalculationPreviewRequest = {
  scenario_id: "alpha_interest_001",
  legal_bundle_id: "alpha_legal_bundle_interest_001",
  version: 1,
  start_date: "2020-08-20",
  end_date: "2020-09-20",
  allocation_policy: "INTEREST_THEN_PRINCIPAL",
  approval_hash: "scenario-approval",
  events: [
    {
      event_id: "alpha_disbursement_001",
      effective_date: "2020-08-20",
      sequence: 1,
      kind: "DISBURSEMENT",
      amount: "10000.00",
      currency: "CNY",
      evidence_ids: ["alpha_evidence_disbursement"],
      approval_hash: "event-approval-1",
    },
    {
      event_id: "alpha_payment_001",
      effective_date: "2020-09-04",
      sequence: 1,
      kind: "PAYMENT",
      amount: "1000.00",
      currency: "CNY",
      evidence_ids: ["alpha_evidence_payment"],
      approval_hash: "event-approval-2",
    },
  ],
  rule_segments: [
    {
      segment_id: "alpha_segment_001",
      start_date: "2020-08-20",
      end_date: "2020-09-05",
      annual_rate: "0.10",
      source_rule_version: "SYNTHETIC-RULE-1",
      applicability_anchor: "CONTRACT_FORMED_AT",
      approval_hash: "rule-approval-1",
    },
    {
      segment_id: "alpha_segment_002",
      start_date: "2020-09-05",
      end_date: "2020-09-20",
      annual_rate: "0.05",
      source_rule_version: "SYNTHETIC-RULE-2",
      applicability_anchor: "FILED_AT",
      approval_hash: "rule-approval-2",
    },
  ],
} as const;

export type CalculationLineItem = {
  period_start: string;
  period_end: string;
  opening_principal: string;
  annual_rate: string;
  day_count: number;
  accrued_interest: string;
  closing_principal: string;
  accrued_unpaid_interest: string;
  rule_segment_id: string;
  source_rule_version: string;
  evidence_ids: string[];
};

export type PaymentAllocation = {
  payment_event_id: string;
  effective_date: string;
  payment_amount: string;
  allocated_interest: string;
  allocated_principal: string;
  unapplied_amount: string;
  evidence_ids: string[];
};

export type CalculationPreview = {
  run_id: string;
  engine_version: string;
  legal_bundle_id: string;
  legal_bundle_hash: string;
  input_hash: string;
  output_hash: string;
  independent_check_match: boolean;
  total_interest_accrued: string;
  total_interest_paid: string;
  remaining_principal: string;
  remaining_unpaid_interest: string;
  unapplied_payments: string;
  line_items: CalculationLineItem[];
  payment_allocations: PaymentAllocation[];
};

export const alphaCalculationApiBase = process.env.NEXT_PUBLIC_ALPHA_API_BASE_URL ?? "http://[::1]:8000";
