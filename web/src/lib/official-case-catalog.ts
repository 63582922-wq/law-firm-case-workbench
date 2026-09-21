import catalog from "../../../knowledge/official_cases/registry.json";

export type OfficialCaseResearchCandidate = {
  candidateId: string;
  sourceId: string;
  title: string;
  officialUrl: string;
  discoveredOn: string;
  issueTags: string[];
  evaluationUses: string[];
  acquisitionMode: string;
  status: string;
  formalUseAllowed: false;
};

type RawCandidate = (typeof catalog.candidates)[number];

function toResearchCandidate(candidate: RawCandidate): OfficialCaseResearchCandidate {
  if (candidate.formal_use_allowed !== false) {
    throw new Error(`官方案例候选 ${candidate.candidate_id} 不能在前端标记为正式可用`);
  }
  return {
    candidateId: candidate.candidate_id,
    sourceId: candidate.source_id,
    title: candidate.title,
    officialUrl: candidate.official_url,
    discoveredOn: candidate.discovered_on,
    issueTags: [...candidate.issue_tags],
    evaluationUses: [...candidate.evaluation_uses],
    acquisitionMode: candidate.acquisition_mode,
    status: candidate.status,
    formalUseAllowed: false,
  };
}

export const officialCaseResearchCatalog = {
  schemaVersion: catalog.schema_version,
  verifiedOn: catalog.verified_on,
  candidates: catalog.candidates.map(toResearchCandidate),
} as const;

export const officialCasePolicyLinks = [
  {
    label: "人民法院案例库建设运行工作规程",
    url: "https://www.court.gov.cn/fabu/xiangqing/431662.html",
  },
  {
    label: "人民法院案例库使用帮助",
    url: "https://rmfyalk.court.gov.cn/helper.html",
  },
  {
    label: "人民法院案例库版权声明",
    url: "https://rmfyalk.court.gov.cn/banquan.html",
  },
] as const;
