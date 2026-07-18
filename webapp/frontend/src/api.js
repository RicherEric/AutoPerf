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

export function connectDevice(address) {
  return request('/devices/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ address }),
  })
}

export function pairDevice(address, code) {
  return request('/devices/pair', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ address, code }),
  })
}

export function setDeviceNickname(serial, nickname) {
  return request(`/devices/${serial}/nickname`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nickname }),
  })
}

export function listRuns() {
  return request('/runs')
}

export function triggerRun(serial, duration, youtubeScenario = '') {
  return request('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ serial, duration, youtube_scenario: youtubeScenario || null }),
  })
}

export function listYoutubeScenarios(tier = '') {
  return request(`/youtube-scenarios${tier ? `?tier=${tier}` : ''}`)
}

export function triggerSuite(serial, tier, duration) {
  return request('/suites', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ serial, tier, duration }),
  })
}

export function getQueueStatus() {
  return request('/queue')
}

export function getStats(limit = 50, deviceSerial = '') {
  const deviceParam = deviceSerial ? `&device=${encodeURIComponent(deviceSerial)}` : ''
  return request(`/stats?limit=${limit}${deviceParam}`)
}

export function getRun(runId) {
  return request(`/runs/${runId}`)
}

export function deleteRun(runId) {
  return request(`/runs/${runId}`, { method: 'DELETE' })
}

export function cancelRun(runId) {
  return request(`/runs/${runId}/cancel`, { method: 'POST' })
}

export function listSamples(runId, sinceId = 0) {
  return request(`/runs/${runId}/samples?since_id=${sinceId}`)
}

export function setBaseline(serial, runId) {
  return request(`/devices/${serial}/baseline`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_id: runId }),
  })
}

export async function getComparison(runId) {
  const response = await fetch(`/api/runs/${runId}/comparison`)
  if (response.status === 404) {
    return null
  }
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`)
  }
  return response.json()
}
