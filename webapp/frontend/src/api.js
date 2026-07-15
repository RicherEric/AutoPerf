async function request(path, options) {
  const response = await fetch(`/api${path}`, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.error || `Request failed: ${response.status}`)
  }
  return response.json()
}

export function listDevices() {
  return request('/devices')
}

export function refreshDevices() {
  return request('/devices/refresh', { method: 'POST' })
}

export function listRuns() {
  return request('/runs')
}

export function triggerRun(serial, duration) {
  return request('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ serial, duration }),
  })
}

export function getRun(runId) {
  return request(`/runs/${runId}`)
}

export function listSamples(runId, sinceId = 0) {
  return request(`/runs/${runId}/samples?since_id=${sinceId}`)
}
