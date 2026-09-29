// Same-origin JSON API client for cca play - part of CCA (Apache-2.0).

export class ApiError extends Error {
  constructor(status, message, state = null, retryAfter = null, limit = null) {
    super(message)
    this.status = status
    this.state = state
    this.retryAfter = retryAfter  // whole seconds from Retry-After (public-mode limits), or null
    this.limit = limit  // the public-mode limit that refused (e.g. "requests_at_once"), or null
  }
}

function retryAfter(response) {
  const seconds = Number.parseInt(response.headers.get("Retry-After") || "", 10)
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null
}

async function request(method, path, body) {
  const init = {method, headers: {Accept: "application/json"}, credentials: "same-origin", cache: "no-store"}
  if (method === "POST") {
    init.headers["Content-Type"] = "application/json"
    init.body = JSON.stringify(body ?? {})
  }
  let response
  try {
    response = await fetch(path, init)
  } catch (err) {
    throw new ApiError(0, String(err && err.message ? err.message : err))
  }
  const type = response.headers.get("Content-Type") || ""
  if (!type.startsWith("application/json")) {
    const text = await response.text()
    if (!response.ok) throw new ApiError(response.status, text || response.statusText)
    return text
  }
  const data = await response.json()
  if (!response.ok) {
    const limit = typeof data.limit === "string" ? data.limit : null
    throw new ApiError(response.status, data.error || response.statusText, data.state || null, retryAfter(response), limit)
  }
  return data
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body)
}
