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

export const api = {
  agents: () => request("/api/agents"),
  brains: () => request("/api/brains"),
  messages: (agentId) => request(`/api/agents/${agentId}/messages`),
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
};
