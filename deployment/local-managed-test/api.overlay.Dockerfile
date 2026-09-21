# A minimal, reproducible local-runtime overlay for source-only hotfixes when
# Docker Desktop has too little free sparse-disk headroom for a full dependency
# rebuild. The base is the already verified managed API runtime image; this
# layer replaces only Python sources that are imported from /app/backend at
# runtime. It never changes dependencies, operating-system packages, or data.
ARG BASE_IMAGE=lawcase-managed-alpha-api-runtime:local
FROM ${BASE_IMAGE} AS planning-recovery
COPY backend/case_kernel/case_agent_planner.py /app/backend/case_kernel/case_agent_planner.py
COPY backend/case_kernel/case_agent_supervisor.py /app/backend/case_kernel/case_agent_supervisor.py
COPY backend/case_kernel/case_agent_postgres.py /app/backend/case_kernel/case_agent_postgres.py

FROM planning-recovery AS review-recovery
COPY backend/case_kernel/case_agent_ledger_extraction_adapters.py /app/backend/case_kernel/case_agent_ledger_extraction_adapters.py
COPY backend/case_api/web_agent_ledger_extraction_review.py /app/backend/case_api/web_agent_ledger_extraction_review.py

FROM ${BASE_IMAGE}

COPY backend/case_kernel/case_agent_lawyer_analysis.py /app/backend/case_kernel/case_agent_lawyer_analysis.py
COPY backend/case_kernel/case_agent_lawyer_analysis_adapters.py /app/backend/case_kernel/case_agent_lawyer_analysis_adapters.py
COPY backend/case_kernel/case_agent_lawyer_analysis_transport.py /app/backend/case_kernel/case_agent_lawyer_analysis_transport.py
COPY backend/case_kernel/case_agent_worker.py /app/backend/case_kernel/case_agent_worker.py
COPY backend/case_api/case_agent_worker_runtime.py /app/backend/case_api/case_agent_worker_runtime.py
COPY backend/case_kernel/official_source_private_store.py /app/backend/case_kernel/official_source_private_store.py
COPY backend/scripts/replay_managed_defence_v3_sealed_response.py /app/backend/scripts/replay_managed_defence_v3_sealed_response.py
COPY backend/scripts/replay_managed_defence_v4_sealed_response.py /app/backend/scripts/replay_managed_defence_v4_sealed_response.py
COPY backend/scripts/render_managed_defence_v4_sealed_candidate.py /app/backend/scripts/render_managed_defence_v4_sealed_candidate.py
COPY backend/scripts/run_managed_defence_acceptance.py /app/backend/scripts/run_managed_defence_acceptance.py
