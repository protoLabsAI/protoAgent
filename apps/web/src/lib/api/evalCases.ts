import { request } from "./http";

export type EvalCaseSource = { session_id: string; trace_id: string };
export type EvalCase = {
  id: string;
  name: string;
  category: string;
  kind: "ask" | "stream";
  prompt: string;
  source_trace_id: string;
  tier: "gate" | "tracked";
  comment?: string;
  expected_tools?: string[];
  expected_any_tools?: string[];
  forbidden_tools?: string[];
  expected_patterns?: string[];
  forbidden_patterns?: string[];
  verify_rubric?: { criteria: string[]; threshold?: number };
};

export const evalCasesApi = {
  draft(source: EvalCaseSource) {
    return request<EvalCase>("/api/evals/cases/draft", { method: "POST", body: source });
  },
  save(draft: EvalCase) {
    return request<{ ok: boolean; case: EvalCase }>("/api/evals/cases", { method: "POST", body: { case: draft } });
  },
};
