/**
 * API client for the QAT Recorder agent.
 *
 * The agent URL is dynamic — the user types it into the connection bar.
 * REST calls for commands, WebSocket for real-time event streaming.
 * Token is passed as Bearer header for REST, query param for WebSocket.
 */

function headers(token) {
  const h = {
    'Content-Type': 'application/json',
    'ngrok-skip-browser-warning': 'true',   // bypass ngrok free-tier interstitial
  };
  if (token) h['Authorization'] = `Bearer ${token}`;
  return h;
}

async function request(base, method, path, body, token) {
  const opts = { method, headers: headers(token) };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(`${base}${path}`, opts);
  const data = await res.json();
  if (!res.ok) {
    const msg = data?.error || data?.detail?.error || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

// ── REST calls ──────────────────────────────────────────────────
// Every function takes `base` (the agent URL) as the first argument.

export const api = {
  health:       (base, token) => request(base, 'GET', '/v1/health', undefined, token),
  applications: (base, token) => request(base, 'GET', '/v1/applications', undefined, token),
  currentSession: (base, token) => request(base, 'GET', '/v1/sessions', undefined, token),

  startSession: (base, body, token) => request(base, 'POST', '/v1/sessions', body, token),
  command:      (base, sid, cmd, args, token) =>
    request(base, 'POST', `/v1/sessions/${sid}/command`, { command: cmd, args: args || {} }, token),
  artifacts:    (base, sid, custom_script, token) => 
    request(base, 'POST', `/v1/sessions/${sid}/artifacts`, { custom_script }, token),
  preview:      (base, sid, token) => request(base, 'GET', `/v1/sessions/${sid}/preview`, undefined, token),
  evidence:     (base, sid, index, token) =>
    request(base, 'GET', `/v1/sessions/${sid}/gaps/${index}/evidence`, undefined, token),
  media:        (base, sid, token) => request(base, 'GET', `/v1/sessions/${sid}/media`, undefined, token),
  replay:       (base, sid, timeout, token) =>
    request(base, 'POST', `/v1/sessions/${sid}/replay`, { timeout: timeout || 0 }, token),
  keep:         (base, sid, name, verify, token) =>
    request(base, 'POST', `/v1/sessions/${sid}/keep`, { name, verify }, token),
  release:      (base, sid, token) => request(base, 'DELETE', `/v1/sessions/${sid}`, undefined, token),

  tests:        (base, app, token) =>
    request(base, 'GET', `/v1/tests${app ? `?app=${encodeURIComponent(app)}` : ''}`, undefined, token),
  runTest:      (base, testId, timeout, token) =>
    request(base, 'POST', '/v1/tests/run', { test: testId, timeout: timeout || 0 }, token),
};

// A still or the video, as a URL an <img> or <video> can load directly. The
// agent answers range requests, so the video seeks without being downloaded
// whole. The token rides in the query string because a browser cannot put a
// header on an <img src>.
export function mediaUrl(base, sid, name, token) {
  const auth = token ? `?token=${encodeURIComponent(token)}` : '';
  return `${base}/v1/sessions/${sid}/media/${encodeURIComponent(name)}${auth}`;
}

/**
 * A still, fetched with the same headers every other call uses.
 *
 * Not an <img src> pointing straight at the agent: an <img> cannot carry a
 * header, and an ngrok free tunnel answers a header-less browser request with
 * its interstitial page rather than the file -- so the picture arrives as HTML
 * and renders as a broken image, with nothing anywhere saying why. Fetched here
 * and handed to the <img> as a blob, it works on a tunnel, on a LAN address and
 * on loopback alike.
 */
export async function fetchMedia(base, sid, name, token, thumb = false) {
  const one = async (which) => {
    const response = await fetch(
      `${base}/v1/sessions/${sid}/media/${encodeURIComponent(which)}`,
      { headers: headers(token) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return URL.createObjectURL(await response.blob());
  };
  if (thumb) {
    // The small copy written beside the still. Falling back rather than
    // failing: a session recorded before these existed, or one photographed
    // through a fallback grabber, has only the full-size one.
    try {
      return await one(name.replace(/\.png$/, '.thumb.png'));
    } catch { /* the full one, then */ }
  }
  return one(name);
}

/** A still as a data: URL, which is how an OpenAI-compatible API takes an image. */
export async function fetchMediaDataUrl(base, sid, name, token) {
  const response = await fetch(
    `${base}/v1/sessions/${sid}/media/${encodeURIComponent(name)}`,
    { headers: headers(token) });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const blob = await response.blob();
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('could not read the still'));
    reader.readAsDataURL(blob);
  });
}

// ── WebSocket for real-time events ──────────────────────────────

export function connectEvents(base, sessionId, token, onMessage, onClose) {
  // Convert http(s) URL to ws(s)
  const wsBase = base.replace(/^http/, 'ws');
  const url = `${wsBase}/ws/events/${sessionId}?token=${encodeURIComponent(token || '')}`;

  const ws = new WebSocket(url);

  ws.onmessage = (evt) => {
    try {
      const data = JSON.parse(evt.data);
      onMessage(data);
    } catch (_) { /* skip bad frames */ }
  };

  ws.onerror = () => {};
  ws.onclose = () => {
    if (onClose) onClose();
  };

  return ws;
}
