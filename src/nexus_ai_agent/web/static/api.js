// The only module that talks to the server. Same-origin, cookie-authenticated.
// There is intentionally no code path that reads, stores or sends a token other
// than the one-time login body; no storage API is used here.
export class ApiError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status; // 0 = network failure
    this.code = code;
  }
}

async function request(method, path, { body, signal } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-Nagar-Studio"] = "1"; // CSRF marker required by the server
  }
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
    throw new ApiError(0, "network");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const code = response.status === 503 ? "unconfigured" : response.status === 429 ? "throttled" : "http_" + response.status;
    throw new ApiError(response.status, code);
  }
  return data;
}

const seg = (value) => encodeURIComponent(value);

export const api = {
  session: (signal) => request("GET", "/api/studio/session", { signal }),
  login: (token) => request("POST", "/api/studio/session", { body: { token } }),
  logout: () => request("DELETE", "/api/studio/session"),
  overview: (signal) => request("GET", "/api/studio/overview", { signal }),
  jobState: (projectId, jobId, signal) =>
    request("GET", `/api/studio/projects/${seg(projectId)}/jobs/${seg(jobId)}/state`, { signal }),
};

export const PROJECT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

// Mirrors the server rule (non-empty, <=128, no "/" and no ".."); the server stays authoritative.
export function isValidJobId(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 128 && !/[\s/]/.test(value) && !value.includes("..");
}
