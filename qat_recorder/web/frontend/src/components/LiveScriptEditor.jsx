import React, { useLayoutEffect, useMemo, useRef, useState } from 'react';

/**
 * The generated script, with the gaps shown where they happened.
 *
 * The recorder writes a `# QAT_DROPPED_EVENT: {...}` line at the point an event
 * could not be turned into a step. This splits the script on those lines and
 * puts a widget in the gap, so the operator sees what was lost between the two
 * steps it fell between rather than as a count at the end of the session --
 * which is all they had, minutes later, from the other side of a VM.
 *
 * Filling a gap goes to the agent, not to this text. Everything is generated
 * from the recording -- the pytest file, the feature file, the object map --
 * and Keep regenerates all of them before it verifies, so a fix that lived only
 * here would be discarded by the act of keeping it.
 */

// The raw event kinds the native filter sends. capture.py:532 groups the first
// three as one interaction; there is no kind called "click".
const CLICK_KINDS = ['mouse_press', 'mouse_release', 'mouse_double'];

/** A Qat definition built from what the filter reported about the object.
 *
 * `seen` is not a locator: nothing in it was validated against the application,
 * which is precisely why the event was dropped. It is the best starting point
 * available, and the replay says so loudly if it is wrong.
 */
function definitionFrom(seen) {
  const def = {};
  if (!seen) return def;
  if (seen.class) def.type = seen.class;          // Qat calls it `type`
  if (seen.objectName) def.objectName = seen.objectName;
  if (seen.text) def.text = seen.text;
  return def;
}

const UNVALIDATED = '  # filled by hand: this locator was never validated';

function fixesFor(data) {
  const def = definitionFrom(data.seen);
  const rendered = JSON.stringify(def);
  const named = Object.keys(def).length > 0;

  if (CLICK_KINDS.includes(data.kind)) {
    const fixes = [];
    if (named) {
      fixes.push({
        label: 'Click it',
        hint: 'do what the click would have done',
        code: `qat.mouse_click(${rendered})${UNVALIDATED}`,
      });
      if (data.kind === 'mouse_double') {
        fixes.push({
          label: 'Double-click it',
          hint: 'the interaction was a double-click',
          code: `qat.double_click(${rendered})${UNVALIDATED}`,
        });
      }
    }
    return fixes;
  }

  if (data.kind === 'close_window') {
    // `close` is a slot on every QWidget, and what the window manager's X asks
    // the application to do. Not close_application: that ends the session.
    return named ? [{
      label: 'Close that window',
      hint: 'close() is the slot the title-bar X calls',
      code: `qat.wait_for_object(${rendered}).close()${UNVALIDATED}`,
    }] : [];
  }

  if (data.kind === 'key_press') {
    // The filter never transmits characters, so nobody here knows what was
    // typed. The operator does.
    return named ? [{
      label: 'Type into it',
      hint: 'the recorder never saw the characters -- say what they were',
      needsText: true,
      code: (text) => `qat.type_in(${rendered}, ${JSON.stringify(text)})${UNVALIDATED}`,
    }] : [];
  }

  return [];
}

function DroppedEventWidget({ data, onRepair, onPoint, onCancelPoint, onWriteCode, canPoint, picking, busy }) {
  const [typed, setTyped] = useState('');
  const fixes = fixesFor(data);
  const def = definitionFrom(data.seen);

  return (
    <div className="drop-gap">
      <div className="drop-gap-body">
        <div className="drop-gap-title">
          Not recorded: {data.kind || 'an event'} on {data.label || 'something'}
        </div>
        <div className="drop-gap-reason">{data.reason}</div>
        {Object.keys(def).length > 0 && (
          <div className="drop-gap-seen">{JSON.stringify(def)}</div>
        )}
        {canPoint && (
          <div className="drop-gap-note">
            {picking
              ? 'Waiting — click that control in the application.'
              : 'Point at it and the recorder identifies it the way it identifies every other step. Nothing to write, and the locator is checked against the running application.'}
          </div>
        )}
        {fixes.some(fix => fix.needsText) && (
          <input
            className="drop-gap-input"
            value={typed}
            placeholder="what was typed here"
            spellCheck="false"
            onChange={event => setTyped(event.target.value)}
          />
        )}
      </div>

      <div className="drop-gap-actions">
        {/* First, because it is the only fix that produces a locator anyone
            checked. The rest are assembled from what the filter reported about
            an object it could not find. */}
        {canPoint && (
          picking ? (
            // Armed. The operator may have changed their mind, and without this
            // the only way out is to click something in the application.
            <button
              className="btn btn-secondary btn-sm"
              title="stop waiting for a click"
              onClick={onCancelPoint}
            >
              Waiting — cancel
            </button>
          ) : (
            <button
              className="btn btn-primary btn-sm"
              title="click the control in the application and the recorder works out how to address it"
              disabled={busy}
              onClick={() => onPoint(data.index)}
            >
              Point at it
            </button>
          )
        )}
        {fixes.map(fix => (
          <button
            key={fix.label}
            className={canPoint ? 'btn btn-secondary btn-sm' : 'btn btn-primary btn-sm'}
            title={`${fix.hint} — this locator is not checked against the application`}
            disabled={busy || picking || (fix.needsText && !typed.trim())}
            onClick={() => onRepair(
              data.index,
              typeof fix.code === 'function' ? fix.code(typed) : fix.code)}
          >
            {fix.label}
          </button>
        ))}
        <button
          className="btn btn-ghost btn-sm"
          disabled={busy || picking}
          title="write the step yourself"
          onClick={() => onWriteCode(data)}
        >
          Write it
        </button>
      </div>
    </div>
  );
}

export default function LiveScriptEditor({ script, onChange, onRepair, onPoint, onCancelPoint, onWriteCode, canPoint, picking, readOnly, busy }) {
  // Derived during render, not held in state. The script prop is the single
  // source of truth: an edit goes up through onChange and comes back down as
  // new text, so keeping a parsed copy in state only added a second render per
  // keystroke -- which is what moved the caret to the end of the line.
  const segments = useMemo(() => {
    const lines = (script || '').split('\n');
    const parsed = [];
    let text = [];

    const flush = () => {
      if (text.length > 0) {
        parsed.push({ type: 'text', content: text.join('\n') });
        text = [];
      }
    };

    for (const line of lines) {
      if (line.trim().startsWith('# QAT_DROPPED_EVENT:')) {
        try {
          const data = JSON.parse(line.replace(/^\s*# QAT_DROPPED_EVENT:\s*/, ''));
          flush();
          parsed.push({ type: 'dropped', data });
          continue;
        } catch {
          // Unparseable: leave it in the script as the comment it is, rather
          // than swallowing a line the operator can see in the saved file.
        }
      }
      text.push(line);
    }
    flush();
    return parsed;
  }, [script]);

  const assemble = (parts) => parts.map(part => (
    part.type === 'text'
      ? part.content
      : `    # QAT_DROPPED_EVENT: ${JSON.stringify(part.data)}`
  )).join('\n');

  const handleTextChange = (index, content) => {
    onChange(assemble(
      segments.map((part, at) => (at === index ? { ...part, content } : part))));
  };

  const open = segments.filter(part => part.type === 'dropped').length;
  let numbered = 0;

  // Where the operator scrolled to. While recording, the script is re-fetched
  // every time a step is added, and every one of those re-renders would
  // otherwise put them back at the top -- which is the one place the gap they
  // were reaching for is not.
  const scroller = useRef(null);
  const wanted = useRef(0);

  const remember = () => {
    if (scroller.current) wanted.current = scroller.current.scrollTop;
  };

  // After the blocks have re-measured themselves (child layout effects run
  // first), put the view back where it was.
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element && Math.abs(element.scrollTop - wanted.current) > 1) {
      element.scrollTop = wanted.current;
    }
  });

  const goToFirstGap = () => {
    const pane = scroller.current;
    const first = document.getElementById('drop-gap-0');
    if (!pane || !first) return;
    // Measured between the two rectangles rather than from offsetTop, which is
    // relative to the nearest *positioned* ancestor -- and the pane is not one.
    const offset = first.getBoundingClientRect().top
                 - pane.getBoundingClientRect().top;
    pane.scrollTop += offset - (pane.clientHeight - first.offsetHeight) / 2;
    remember();
  };

  return (
    <div className="script-editor">
      {open > 0 && (
        <div className="script-editor-banner">
          <span>
            {open} event{open === 1 ? '' : 's'} could not be recorded. The script
            does not do {open === 1 ? 'it' : 'them'}.
          </span>
          <button className="btn btn-secondary btn-sm" onClick={goToFirstGap}>
            Go to the first
          </button>
        </div>
      )}

      <div className="script-editor-scroll" ref={scroller} onScroll={remember}>
        {segments.map((part, index) => {
          if (part.type === 'text') {
            return (
              <ScriptBlock
                key={index}
                content={part.content}
                readOnly={readOnly}
                onChange={value => handleTextChange(index, value)}
              />
            );
          }
          const position = numbered++;
          return (
            <div key={index} id={`drop-gap-${position}`} className="drop-gap-slot">
              <DroppedEventWidget
                data={part.data}
                busy={busy}
                canPoint={canPoint}
                picking={picking}
                onPoint={onPoint}
                onCancelPoint={onCancelPoint}
                onRepair={onRepair}
                onWriteCode={onWriteCode}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}

/**
 * A run of script between two gaps.
 *
 * While the recording is running this is read-only, and a <pre> sizes itself
 * from its content with nothing to measure. A textarea does not: it has to be
 * collapsed to `auto` and re-measured, and collapsing it shrinks the scroll
 * container, which makes the browser clamp scrollTop -- so every refresh threw
 * the operator back to the top of the script. Only the editable case pays that,
 * and only when its text actually changes.
 */
function ScriptBlock({ content, readOnly, onChange }) {
  const box = useRef(null);

  useLayoutEffect(() => {
    const element = box.current;
    if (!element || readOnly) return;
    element.style.height = 'auto';
    element.style.height = `${element.scrollHeight}px`;
  }, [content, readOnly]);

  if (readOnly) {
    return <pre className="script-editor-text">{content}</pre>;
  }
  return (
    <textarea
      ref={box}
      className="script-editor-text"
      value={content}
      spellCheck="false"
      onChange={event => onChange(event.target.value)}
    />
  );
}
