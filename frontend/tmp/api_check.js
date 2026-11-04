// src/services/api.js
import axios from "axios";
var api = axios.create({
  baseURL: "http://127.0.0.1:8000",
  timeout: 6e4,
  headers: {
    "Content-Type": "application/json"
  }
});
api.interceptors.request.use(
  (config) => config,
  (error) => Promise.reject(error)
);
function normalizeDetail(detail) {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const msgs = detail.map((d) => typeof d === "string" ? d : d?.msg).filter((m) => typeof m === "string" && m.trim());
    if (msgs.length) return msgs.join("\uFF1B");
  }
  return "";
}
api.interceptors.response.use(
  (response) => response.data,
  (error) => {
    let message = "\u7F51\u7EDC\u5F02\u5E38\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5";
    if (error.response) {
      const { status, data } = error.response;
      message = normalizeDetail(data?.detail) || data?.message || `\u8BF7\u6C42\u5931\u8D25\uFF08${status}\uFF09`;
    } else if (error.code === "ECONNABORTED") {
      message = "\u8BF7\u6C42\u8D85\u65F6\uFF0C\u8BF7\u68C0\u67E5\u540E\u7AEF\u670D\u52A1\u662F\u5426\u5DF2\u542F\u52A8";
    } else if (error.request) {
      message = "\u65E0\u6CD5\u8FDE\u63A5\u670D\u52A1\u5668\uFF0C\u8BF7\u786E\u8BA4\u540E\u7AEF\u5DF2\u5728 127.0.0.1:8000 \u542F\u52A8";
    }
    return Promise.reject(new Error(message));
  }
);
function normalizeCompetitionId(id) {
  if (id === null || id === void 0 || id === "") return null;
  const n = typeof id === "number" ? id : Number(String(id).trim());
  return Number.isInteger(n) && n > 0 ? n : null;
}
function getCompetitions(params = {}) {
  return api.get("/api/competitions", { params }).then((data) => {
    if (Array.isArray(data)) return data;
    if (Array.isArray(data?.data)) return data.data;
    if (Array.isArray(data?.items)) return data.items;
    return [];
  });
}
function searchCompetitions(query) {
  const q = typeof query === "string" ? query.trim() : "";
  if (!q) return Promise.reject(new Error("\u8BF7\u8F93\u5165\u8981\u641C\u7D22\u7684\u8D5B\u4E8B\u63CF\u8FF0"));
  return api.post("/api/ai/search", { query: q }).then((data) => {
    const items = Array.isArray(data?.items) ? data.items : Array.isArray(data?.data) ? data.data : Array.isArray(data) ? data : [];
    const clean = items.filter(
      (it) => it && typeof it === "object" && it.id !== null && it.id !== void 0
    );
    return {
      query: typeof data?.query === "string" ? data.query : q,
      total: Number.isInteger(data?.total) ? data.total : clean.length,
      items: clean
    };
  });
}
function getCompetitionDetail(id) {
  const cid = normalizeCompetitionId(id);
  if (cid === null) return Promise.reject(new Error("\u7ADE\u8D5B ID \u65E0\u6548"));
  return api.get(`/api/competitions/${cid}`).then((data) => {
    if (data && typeof data === "object") {
      if (data.data && typeof data.data === "object") return data.data;
      return data;
    }
    return {};
  });
}
function analyzeWork(workId, options = {}) {
  const wid = normalizeCompetitionId(workId);
  if (wid === null) return Promise.reject(new Error("\u4F5C\u54C1 ID \u65E0\u6548"));
  const cid = normalizeCompetitionId(options.competitionId);
  if (options.competitionId !== void 0 && options.competitionId !== null && cid === null) {
    return Promise.reject(new Error("\u7ADE\u8D5B ID \u65E0\u6548"));
  }
  const body = { work_id: wid };
  if (cid !== null) body.competition_id = cid;
  return api.post("/api/ai/analyze/work", body).then((data) => ({
    analysis: typeof data?.analysis === "string" ? data.analysis : ""
  }));
}
function analyzeWorks(competitionId) {
  const cid = normalizeCompetitionId(competitionId);
  if (cid === null) return Promise.reject(new Error("\u7ADE\u8D5B ID \u65E0\u6548"));
  return api.post("/api/ai/analyze/competition", { competition_id: cid }).then((data) => ({
    analysis: typeof data?.analysis === "string" ? data.analysis : ""
  }));
}
function getCompetitionAwards(competitionId, options = {}) {
  const cid = normalizeCompetitionId(competitionId);
  if (cid === null) return Promise.reject(new Error("\u7ADE\u8D5B ID \u65E0\u6548"));
  const params = {};
  const limit = Number(options.limit);
  if (Number.isInteger(limit) && limit > 0) params.limit = limit;
  return api.get(`/api/competitions/${cid}/awards`, { params }).then((data) => {
    const items = Array.isArray(data?.items) ? data.items : Array.isArray(data?.data) ? data.data : Array.isArray(data) ? data : [];
    return { items };
  });
}
function generatePlan(payload = {}) {
  const cid = normalizeCompetitionId(payload.competitionId);
  if (cid === null) return Promise.reject(new Error("\u7ADE\u8D5B ID \u65E0\u6548"));
  const body = {
    competition_id: cid,
    user_input: typeof payload.userInput === "string" ? payload.userInput : "",
    current_plan: typeof payload.currentPlan === "string" && payload.currentPlan.trim() ? payload.currentPlan : null
  };
  return api.post("/api/ai/plan", body).then((data) => {
    const status = typeof data?.status === "string" ? data.status : "plan";
    return {
      status,
      reply: typeof data?.reply === "string" ? data.reply : ""
    };
  });
}
var api_default = api;
export {
  analyzeWork,
  analyzeWorks,
  api_default as default,
  generatePlan,
  getCompetitionAwards,
  getCompetitionDetail,
  getCompetitions,
  normalizeCompetitionId,
  searchCompetitions
};
