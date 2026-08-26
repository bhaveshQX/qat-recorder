import { useEffect, useRef } from 'react';
import MediaImage from './MediaImage';

/** What was on screen when this step was recorded.
 *
 * The step says what was done; only the picture says what it was done *to* --
 * which dialog was open, which tab, what the control looked like -- and that is
 * gone a second later.
 */
function StepShot({ shot, onOpen }) {
  if (!shot) return <span className="col-shot-none">—</span>;
  return (
    <button type="button" className="step-shot"
            title="what was on screen — click to enlarge"
            onClick={(event) => { event.stopPropagation(); if (onOpen) onOpen(shot); }}>
      <MediaImage {...shot} alt="the screen for this step" />
    </button>
  );
}

function robustnessClass(r) {
  if (!r) return '';
  return `robustness robustness-${r}`;
}

export default function ActionTable({ actions, selectedRow, onSelect,
                                      shotForStep, onOpenShot }) {
  const tbodyRef = useRef(null);

  // Auto-scroll to bottom when new actions arrive
  useEffect(() => {
    if (tbodyRef.current) {
      const wrapper = tbodyRef.current.closest('.table-wrapper');
      if (wrapper) wrapper.scrollTop = wrapper.scrollHeight;
    }
  }, [actions.length]);

  if (actions.length === 0) {
    return (
      <div className="empty-state">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none" opacity="0.3">
          <circle cx="24" cy="24" r="20" stroke="currentColor" strokeWidth="2"/>
          <circle cx="24" cy="24" r="7" fill="currentColor"/>
        </svg>
        <p>Press <strong>Record</strong> to begin capturing</p>
      </div>
    );
  }

  return (
    <div className="table-wrapper">
      <table className="data-table">
        <thead>
          <tr>
            <th className="col-num">#</th>
            <th className="col-action">Action</th>
            <th>Object</th>
            <th className="col-durability">Durability</th>
            <th className="col-shot">Screen</th>
          </tr>
        </thead>
        <tbody ref={tbodyRef}>
          {actions.map((action, idx) => {
            const target = action.target;
            const robustness = target?.robustness || '';
            return (
              <tr key={idx}
                  className={idx === selectedRow ? 'selected' : ''}
                  onClick={() => onSelect(idx)}>
                <td className="col-num">{idx + 1}</td>
                <td><span className="action-kind">{action.kind}</span></td>
                <td>{target?.label || '—'}</td>
                <td>
                  {robustness && (
                    <span className={robustnessClass(robustness)}>{robustness}</span>
                  )}
                </td>
                <td className="col-shot">
                  <StepShot shot={shotForStep && shotForStep(idx)}
                            onOpen={onOpenShot} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
