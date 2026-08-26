import { useState } from 'react';
import { DEFAULTS, seesImages } from '../llm';

/**
 * Where the model lives, and the key to reach it.
 *
 * Anything OpenAI-compatible: the default is Groq's endpoint and gpt-oss-120b.
 * The key is kept in this browser and sent to that endpoint and nowhere else --
 * never to the agent, never into a recording, never into a generated test.
 */
export default function LlmSettings({ settings, onSave, onClose }) {
  const [draft, setDraft] = useState(settings);
  const set = (key, value) => setDraft(prev => ({ ...prev, [key]: value }));
  const vision = seesImages(draft.model);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()} style={{ minWidth: 560 }}>
        <h2>Ask a model to identify a control</h2>
        <p>
          Used only to <strong>choose between objects the recorder already found</strong>
          {' '}in the running application. It never writes a locator: it answers with one
          of their ids, and the step that gets inserted is the one the recorder resolved
          itself.
        </p>

        <div className="modal-form">
          <label>Endpoint (anything OpenAI-compatible)</label>
          <input value={draft.baseUrl} spellCheck="false"
                 onChange={e => set('baseUrl', e.target.value)}
                 placeholder={DEFAULTS.baseUrl} />

          <label>Model</label>
          <input value={draft.model} spellCheck="false"
                 onChange={e => set('model', e.target.value)}
                 placeholder={DEFAULTS.model} />
          <div className="field-hint">
            {vision
              ? 'This model can see, so the screenshot of the gap is sent with the evidence.'
              : 'This model reads text only, so the evidence is sent without the screenshot — '
                + 'the object list, the properties, and the text found next to each control. '
                + 'Point this at a vision model to include the picture.'}
          </div>

          <label>API key</label>
          <input type="password" value={draft.apiKey} spellCheck="false"
                 onChange={e => set('apiKey', e.target.value)}
                 placeholder="kept in this browser only" />
          <div className="field-hint">
            Stored in this browser and sent to the endpoint above. It never reaches
            the machine under test.
          </div>

          <label className="field-check">
            <input type="checkbox" checked={!!draft.enabled}
                   onChange={e => set('enabled', e.target.checked)} />
            Offer this on gaps
          </label>
        </div>

        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary"
                  disabled={draft.enabled && !draft.apiKey.trim()}
                  onClick={() => { onSave(draft); onClose(); }}>
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
