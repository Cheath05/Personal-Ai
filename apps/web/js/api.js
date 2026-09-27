// Thin wrapper around the Cardinal API.

// Which device you're on. Everything is stored on the hub, so all devices share one memory;
// this only labels where a message came from.
export const DEVICE = (() => {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return "iPad";
  if (/Windows/.test(ua)) return "Windows PC";
  if (/Macintosh/.test(ua)) return "Mac";
  if (/Android/.test(ua)) return "Android";
  return "Browser";
})();

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", "X-Cardinal-Device": DEVICE },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `Request failed (${res.status}).`;
    throw new Error(detail);
  }
  return data;
}

async function upload(path, file) {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(path, { method: "POST", body: form, headers: { "X-Cardinal-Device": DEVICE } });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(data && typeof data.detail === "string" ? data.detail : `Upload failed (${res.status}).`);
  return data;
}

export const api = {
  agents: () => request("/api/agents"),
  brains: () => request("/api/brains"),
  messages: (agentId) => request(`/api/agents/${agentId}/messages`),
  clearChat: (agentId) => request(`/api/agents/${agentId}/messages`, { method: "DELETE" }),
  chat: (agentId, message) => request("/api/chat", { method: "POST", body: { agent_id: agentId, message } }),
  askClaude: (agentId) => request("/api/chat", { method: "POST", body: { agent_id: agentId, retry_with_claude: true } }),
  usage: () => request("/api/usage/summary"),
  addCredit: (amount) => request("/api/usage/credit", { method: "POST", body: { amount_usd: amount } }),
  today: (refresh = false) => request(`/api/today${refresh ? "?refresh=true" : ""}`),
  writeBriefing: () => request("/api/briefing", { method: "POST" }),
  disconnectGoogle: (slot) => request("/api/google/disconnect", { method: "POST", body: { slot } }),
  calendarDay: (date, refresh = false) => request(`/api/calendar/day?date=${date}${refresh ? "&refresh=true" : ""}`),
  addItem: (item) => request("/api/calendar/items", { method: "POST", body: item }),
  deleteItem: (id) => request(`/api/calendar/items/${id}`, { method: "DELETE" }),
  feeds: () => request("/api/calendar/feeds"),
  addFeed: (name, url) => request("/api/calendar/feeds", { method: "POST", body: { name, url } }),
  deleteFeed: (id) => request(`/api/calendar/feeds/${id}`, { method: "DELETE" }),
  startSyllabus: (body) => request("/api/syllabus", { method: "POST", body }),
  syllabus: (id) => request(`/api/syllabus/${id}`),
  addSyllabusItems: (id, items) => request(`/api/syllabus/${id}/add`, { method: "POST", body: { items } }),
  actions: () => request("/api/actions"),
  actionLog: () => request("/api/actions/log"),
  approve: (id, remember = false) => request(`/api/actions/${id}/approve`, { method: "POST", body: { remember } }),
  deny: (id) => request(`/api/actions/${id}/deny`, { method: "POST" }),
  undo: (id) => request(`/api/actions/${id}/undo`, { method: "POST" }),
  rulePreview: (id) => request(`/api/actions/${id}/rule-preview`),
  remember: (id) => request(`/api/actions/${id}/remember`, { method: "POST" }),
  rules: () => request("/api/rules"),
  revokeRule: (id) => request(`/api/rules/${id}`, { method: "DELETE" }),
  runPlanner: () => request("/api/planner/run", { method: "POST" }),
  review: () => request("/api/review"),
  morning: (body) => request("/api/review/morning", { method: "POST", body }),
  evening: (body) => request("/api/review/evening", { method: "POST", body }),
  addTask: (title) => request("/api/review/tasks", { method: "POST", body: { title } }),
  setTask: (id, status) => request(`/api/review/tasks/${id}`, { method: "POST", body: { status } }),
  markExperiment: (id, result) => request(`/api/review/experiments/${id}`, { method: "POST", body: { result } }),
  writeRollup: () => request("/api/review/rollup", { method: "POST" }),
  memory: () => request("/api/memory"),
  addMemory: (text, kind) => request("/api/memory", { method: "POST", body: { text, kind } }),
  editMemory: (id, text) => request(`/api/memory/${id}`, { method: "PATCH", body: { text } }),
  deleteMemory: (id) => request(`/api/memory/${id}`, { method: "DELETE" }),
  running: () => request("/api/running"),
  logRun: (body) => request("/api/running/runs", { method: "POST", body }),
  deleteRun: (id) => request(`/api/running/runs/${id}`, { method: "DELETE" }),
  proposeRuns: () => request("/api/running/propose", { method: "POST" }),
  ingestToken: () => request("/api/health/token", { method: "POST" }),
  importHealth: (file) => upload("/api/health/import", file),
  brain: () => request("/api/brain"),
  uploadDoc: (file, course) => upload(`/api/brain/documents${course ? `?course=${encodeURIComponent(course)}` : ""}`, file),
  addNote: (body) => request("/api/brain/notes", { method: "POST", body }),
  doc: (id) => request(`/api/brain/documents/${id}`),
  editDoc: (id, body) => request(`/api/brain/documents/${id}`, { method: "PATCH", body }),
  deleteDoc: (id) => request(`/api/brain/documents/${id}`, { method: "DELETE" }),
  docDates: (id) => request(`/api/brain/documents/${id}/dates`, { method: "POST" }),
  askNotes: (body) => request("/api/brain/ask", { method: "POST", body }),
  dueCards: (course) => request(`/api/brain/cards/due${course ? `?course=${encodeURIComponent(course)}` : ""}`),
  addCard: (body) => request("/api/brain/cards", { method: "POST", body }),
  reviewCard: (id, rating) => request(`/api/brain/cards/${id}/review`, { method: "POST", body: { rating } }),
  deleteCard: (id) => request(`/api/brain/cards/${id}`, { method: "DELETE" }),
  startQuiz: (body) => request("/api/brain/quizzes", { method: "POST", body }),
  quiz: (id) => request(`/api/brain/quizzes/${id}`),
  gradeQuiz: (id, answers) => request(`/api/brain/quizzes/${id}/grade`, { method: "POST", body: { answers } }),
  inbox: () => request("/api/inbox"),
  sortInbox: () => request("/api/inbox/sort", { method: "POST" }),
  draftEmail: (id, instructions) => request(`/api/inbox/${id}/draft`, { method: "POST", body: { instructions } }),
  saveDraft: (id, draft) => request(`/api/inbox/${id}/save-draft`, { method: "POST", body: draft }),
  sendEmail: (id, draft) => request(`/api/inbox/${id}/send`, { method: "POST", body: draft }),
  emailDone: (id, done = true) => request(`/api/inbox/${id}/done`, { method: "POST", body: { done } }),
  emailTask: (id) => request(`/api/inbox/${id}/task`, { method: "POST" }),
  emailDue: (id) => request(`/api/inbox/${id}/due`, { method: "POST" }),
  saveResearch: (messageId) => request("/api/research/save", { method: "POST", body: { message_id: messageId } }),
  appsToken: () => request("/api/apps/token", { method: "POST" }),
  appsDay: (date) => request(`/api/apps/day${date ? `?date=${date}` : ""}`),
  agentStatus: () => request("/api/agents/status"),
};
