export async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  const text = await res.text();
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    const msg =
      (data && (data.detail || data.message || data.error)) ||
      res.statusText ||
      'Request failed';
    const error = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    error.status = res.status;
    error.detail = data?.detail ?? null;
    error.response = data;
    throw error;
  }
  return data;
}

export const getTools = () => api('/api/tools');
export const getToolStats = (id) => api(`/api/tools/${id}/stats`);
export const getToolResults = (id, limit = 100) =>
  api(`/api/tools/${id}/results?limit=${limit}`);
export const getCurrentJob = (toolId = '', logFrom = 0) => {
  const q = new URLSearchParams();
  if (toolId) q.set('tool_id', toolId);
  if (logFrom) q.set('log_from', String(logFrom));
  const qs = q.toString();
  return api(`/api/jobs/current${qs ? '?' + qs : ''}`);
};
export const startJob = (tool_id, params) =>
  api('/api/jobs/start', {
    method: 'POST',
    body: JSON.stringify({ tool_id, params }),
  });
export const stopJob = (job_id = null) =>
  api('/api/jobs/stop', {
    method: 'POST',
    body: JSON.stringify({ job_id }),
  });
export const clearJobLogs = (jobId) =>
  api(`/api/jobs/${encodeURIComponent(jobId)}/logs`, { method: 'DELETE' });
export const clearToolLogs = (toolId) =>
  api(`/api/tools/${encodeURIComponent(toolId)}/logs`, { method: 'DELETE' });
export const getConfigSummary = () => api('/api/config/summary');
export const updateConfig = (config) =>
  api('/api/config', {
    method: 'PUT',
    body: JSON.stringify(config),
  });
export const getHealth = () => api('/api/health');
export const getHotmails = (id) => api(`/api/tools/${id}/hotmails`);
export const importHotmails = (id, text, mode = 'append') =>
  api(`/api/tools/${id}/hotmails`, {
    method: 'POST',
    body: JSON.stringify({ text, mode }),
  });
export const generateSub2apiKeys = (params) =>
  api('/api/sub2api/keys/generate', {
    method: 'POST',
    body: JSON.stringify(params),
  });
export const listSub2apiKeys = (page = 1, pageSize = 50) =>
  api(`/api/sub2api/keys/list?page=${page}&page_size=${pageSize}`);

