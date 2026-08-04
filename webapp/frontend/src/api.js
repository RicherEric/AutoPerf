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

export function controlDevice(serial, action, payload = {}) {
  return request(`/devices/${encodeURIComponent(serial)}/control`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, ...payload }),
  })
}

export function discoverMdnsDevices() {
  return request('/devices/mdns')
}

export function connectDiscoveredDevices() {
  return request('/devices/connect-discovered', { method: 'POST' })
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

// connectedOnly narrows to devices adb can see right now -- what anything
// about to drive a device needs, as opposed to everything ever registered.
// The demo's plan, from the same definition scripts/demo.py runs.
export function getDemoPlan() {
  return request('/demo')
}

export function listConnectedDevices() {
  return request('/devices?connected=1')
}

export function listRuns(deviceSerial = '') {
  const deviceParam = deviceSerial ? `?device=${encodeURIComponent(deviceSerial)}` : ''
  return request(`/runs${deviceParam}`)
}

export function triggerRun(serial, duration, youtubeScenario = '', blindTargets = []) {
  return request('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      serial, duration, youtube_scenario: youtubeScenario || null,
      blind_targets: blindTargets.length ? blindTargets : undefined,
    }),
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

// Preflight grades the selector table against a device without measuring
// anything. It holds the device for as long as it runs, which is why it is
// enqueued rather than performed inline.
export function triggerPreflight(serial, youtubeScenario = '') {
  return request('/preflights', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ serial, youtube_scenario: youtubeScenario || null }),
  })
}

export function listPreflights(deviceSerial = '', limit = 5) {
  const device = deviceSerial ? `&device=${encodeURIComponent(deviceSerial)}` : ''
  return request(`/preflights?limit=${limit}${device}`)
}

export function cancelPreflight(preflightId) {
  return request(`/preflights/${encodeURIComponent(preflightId)}/cancel`, { method: 'POST' })
}

export function getPreflight(preflightId) {
  return request(`/preflights/${encodeURIComponent(preflightId)}`)
}

// Which targets one scenario resolves. An empty list means preflight has
// nothing to grade there -- the deep-link presets are all like that.
export function getScenarioTargets(youtubeScenario) {
  return request(`/selector-targets?scenario=${encodeURIComponent(youtubeScenario)}`)
}

// Only the running-runs half, and without the Celery broadcast behind it.
export function getRunningRuns() {
  return request('/queue?running=1')
}

export function getQueueStatus() {
  return request('/queue')
}

export function listCampaigns(deviceSerial = '') {
  const deviceParam = deviceSerial ? `?device=${encodeURIComponent(deviceSerial)}` : ''
  return request(`/campaigns${deviceParam}`)
}

export function triggerCampaign(payload) {
  return request('/campaigns', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function getCampaign(campaignId) {
  return request(`/campaigns/${campaignId}`)
}

export function cancelCampaign(campaignId) {
  return request(`/campaigns/${campaignId}/cancel`, { method: 'POST' })
}

export function deleteCampaign(campaignId) {
  return request(`/campaigns/${campaignId}`, { method: 'DELETE' })
}

// Bucket-averaged series for charting a finished run. listSamples() streams
// raw rows and stays the right call while a run is live (the client only ever
// asks for what's newer than since_id); this one bounds the point count so a
// multi-hour run costs the same to render as a short one.
export function getRunSeries(runId, buckets = 300) {
  return request(`/runs/${runId}/series?buckets=${buckets}`)
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

export function getRunRecording(runId) {
  return request(`/runs/${runId}/recording`)
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
