// Thin wrapper around the Cardinal API.

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
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
};
