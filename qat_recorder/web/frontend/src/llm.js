/**
 * Asking a model which object the operator meant.
 *
 * The call is made from the panel, not from the agent: the machine under test
 * is usually a locked-down VM with no way out to the internet, and the browser
 * already has the network the tester's own machine has. The key never reaches
 * the VM and is never written into a recording.
 *
 * The contract that keeps this honest: **the model never writes a locator.** It
 * is given the objects the recorder found in the running application, each one
 * already resolved and graded, and it answers with the `id` of one of them. The
 * agent then inserts the Target it resolved itself. A wrong answer is the wrong
 * control -- it cannot be a control that does not exist, or a definition nobody
 * checked, which is the failure mode every self-healing locator ships with.
 */

const STORE_KEY = 'qatrec.llm';

//: Long enough for a slow model, short enough that a wedged call gives the
//: button back rather than leaving the panel thinking forever.
const TIMEOUT_MS = 60000;

export const DEFAULTS = {
  baseUrl: 'https://api.groq.com/openai/v1',
  model: 'openai/gpt-oss-120b',
  apiKey: '',
  enabled: false,
};

/** Models that can be shown the screenshot. Everything else gets the text. */
const VISION_HINTS = ['llama-4', 'scout', 'maverick', 'gpt-4o', 'gpt-4.1', 'o4',
                      'claude', 'gemini', 'pixtral', 'vision', 'qwen-vl'];

export function seesImages(model) {
  const name = (model || '').toLowerCase();
  return VISION_HINTS.some(hint => name.includes(hint));
}

export function loadSettings() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(STORE_KEY) || '{}') };
  } catch {
    return { ...DEFAULTS };          // private window, cleared storage, anything
  }
}

export function saveSettings(settings) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(settings));
  } catch { /* the panel still works; the key just will not be remembered */ }
}

/** The evidence, as something a model can read. */
function describe(pack, kind, label, reason) {
  const lines = [
    `The operator ${kind === 'key_press' ? 'typed into' : 'clicked'} a control`,
    `in a Qt application, and the recorder could not turn it into a test step.`,
    ``,
    `What the recorder saw: ${label}`,
    `Why it failed: ${reason}`,
    `Class of the control: ${pack.class || 'unknown'}`,
    pack.objectName ? `objectName it reported: ${pack.objectName}` : '',
    pack.text ? `text it reported: ${JSON.stringify(pack.text)}` : '',
    pack.sibling_index >= 0
      ? `The filter said it was sibling number ${pack.sibling_index} of that class under its parent.`
      : `The filter could not say which of the siblings it was. That is usually why this failed.`,
    pack.path?.length
      ? `Its ancestors, innermost first: ${pack.path.map(p => `${p.class}${p.objectName ? '#' + p.objectName : ''}`).join(' < ')}`
      : '',
    ``,
    `These are the objects of that class the application actually has. Each was`,
    `resolved against the running application; "addressable as" is what a test`,
    `step would use, and its grade is how durable that is.`,
    ``,
  ].filter(Boolean);

  for (const candidate of pack.candidates || []) {
    const props = candidate.properties || {};
    const shown = Object.entries(props)
      .filter(([, v]) => v !== '' && v !== null)
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(' ');
    const near = (candidate.labels_near || []).map(l => l.text).join(' | ');
    lines.push(`id ${candidate.id}: ${shown || '(no readable properties)'}`);
    if (near) lines.push(`    text nearby: ${near}`);
    lines.push(`    addressable as: ${candidate.target
      ? JSON.stringify(candidate.target.definition) + `  (${candidate.robustness})`
      : 'nothing durable — this one cannot be addressed'}`);
  }
  if (pack.truncated) lines.push(`(there were more of these than are listed)`);
  return lines.join('\n');
}

const SYSTEM = [
  'You identify which control in a Qt application a person operated, so a',
  'recorded test can address it.',
  '',
  'You are given the objects the recorder found in the running application.',
  'Answer with the id of exactly one of them. Do not invent a locator, a',
  'property or an id: only the ids listed exist, and anything else is discarded.',
  '',
  'Prefer a candidate that is addressable over one that is not, and a more',
  'durable grade over a weaker one, but only where the evidence genuinely',
  'points at it. Text sitting next to a control is strong evidence of what it',
  'is for; an unnamed check box beside "Enable DHT" is the DHT one.',
  '',
  'If nothing in the list is convincingly the control, say so instead of',
  'guessing: a wrong step is worse than a gap somebody can still see.',
  '',
  'Reply as JSON only: {"id": <number or null>, "confidence": "high"|"medium"',
  '|"low", "why": "<one sentence a tester can check>"}',
].join('\n');

/**
 * Ask which candidate it is. Returns {id, confidence, why} or throws.
 *
 * `imageDataUrl` is sent only when the configured model can see -- the default,
 * gpt-oss-120b, is a text model, and attaching an image to it is an error
 * rather than a wasted opportunity.
 */
export async function chooseCandidate(settings, pack, meta, imageDataUrl) {
  const prompt = describe(pack, meta.kind, meta.label, meta.reason);
  const withImage = imageDataUrl && seesImages(settings.model);

  const content = withImage
    ? [{ type: 'text', text: prompt },
       { type: 'image_url', image_url: { url: imageDataUrl } }]
    : prompt;

  // Bounded. A call that never settles leaves the panel saying "Asking the
  // model..." for the rest of the session with no way back to the button.
  const stop = new AbortController();
  const timer = setTimeout(() => stop.abort(), TIMEOUT_MS);
  let response;
  try {
    response = await fetch(`${settings.baseUrl.replace(/\/+$/, '')}/chat/completions`, {
      method: 'POST',
      signal: stop.signal,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${settings.apiKey}`,
      },
      body: JSON.stringify({
        model: settings.model,
        temperature: 0,
        messages: [
          { role: 'system', content: SYSTEM },
          { role: 'user', content },
        ],
      }),
    });
  } catch (error) {
    if (error.name === 'AbortError') {
      throw new Error(`no answer from ${settings.baseUrl} within `
                    + `${Math.round(TIMEOUT_MS / 1000)}s`);
    }
    // fetch rejects with a bare TypeError for a blocked cross-origin request,
    // which is the commonest way this fails and says nothing useful on its own.
    throw new Error(
      `could not reach ${settings.baseUrl} from this browser (${error.message}). `
      + 'If the endpoint does not allow browser requests, its CORS policy is '
      + 'blocking this and no error body is available to show.');
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body?.error?.message || detail;
    } catch { /* keep the status */ }
    throw new Error(detail);
  }

  const body = await response.json();
  const said = body?.choices?.[0]?.message?.content || '';
  const parsed = parseAnswer(said);
  if (!parsed) throw new Error(`could not read the answer: ${said.slice(0, 200)}`);

  // Only an id that is actually in the pack. A model naming a candidate that
  // does not exist is the one failure this design has to refuse outright.
  if (parsed.id !== null && !(pack.candidates || []).some(c => c.id === parsed.id)) {
    throw new Error(`answered with id ${parsed.id}, which is not one of the candidates`);
  }
  return { ...parsed, usedImage: !!withImage };
}

function parseAnswer(said) {
  const attempt = (text) => {
    try {
      const value = JSON.parse(text);
      if (typeof value !== 'object' || value === null) return null;
      const id = value.id === null || value.id === undefined ? null : Number(value.id);
      if (id !== null && !Number.isInteger(id)) return null;
      return { id, confidence: String(value.confidence || 'low'),
               why: String(value.why || '') };
    } catch { return null; }
  };
  // Models fence their JSON, or reason around it. Take the first object that parses.
  const direct = attempt(said.trim());
  if (direct) return direct;
  const fenced = said.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fenced) {
    const inside = attempt(fenced[1].trim());
    if (inside) return inside;
  }
  const braced = said.match(/\{[\s\S]*\}/);
  return braced ? attempt(braced[0]) : null;
}
