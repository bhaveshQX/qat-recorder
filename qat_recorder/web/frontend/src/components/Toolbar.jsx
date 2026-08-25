const S = { IDLE: 'idle', RECORDING: 'recording', PAUSED: 'paused', PICKING: 'picking', STOPPED: 'stopped' };

export default function Toolbar({ state, busy, onRecord, onPause, onStop, onCheckpoint, onInsertCode, onScreenshot, onUndo, onSave, onKeep, onReplay }) {
  const recording = state === S.RECORDING || state === S.PICKING;
  const idle      = state === S.IDLE || state === S.STOPPED;

  return (
    <nav className="toolbar">
      <div className="toolbar-group" style={{ display: 'flex', gap: 6 }}>
        <button className="btn btn-primary" disabled={!idle || busy} onClick={onRecord}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="5" fill="currentColor"/></svg>
          Record
        </button>
        <button className="btn btn-secondary" disabled={!(state === S.RECORDING || state === S.PAUSED) || busy}
                onClick={onPause}>
          {state === S.PAUSED ? (
            <>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M5 3l8 5-8 5V3z" fill="currentColor"/></svg>
              <span>Resume</span>
            </>
          ) : (
            <>
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><rect x="3" y="3" width="3.5" height="10" rx="1" fill="currentColor"/><rect x="9.5" y="3" width="3.5" height="10" rx="1" fill="currentColor"/></svg>
              <span>Pause</span>
            </>
          )}
        </button>
        <button className="btn btn-danger" disabled={idle || busy} onClick={onStop}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/></svg>
          Stop
        </button>
      </div>

      <div className="toolbar-divider" />

      <div className="toolbar-group" style={{ display: 'flex', gap: 6 }}>
        <button className="btn btn-accent" disabled={state !== S.RECORDING || busy} onClick={onCheckpoint}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M6.5 12l-4-4 1.4-1.4L6.5 9.2l5.6-5.6L13.5 5l-7 7z" fill="currentColor"/></svg>
          Checkpoint
        </button>
        <button className="btn btn-accent" disabled={(state !== S.RECORDING && state !== S.PAUSED) || busy} onClick={onInsertCode}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M5.5 4.5l-3 3.5 3 3.5M10.5 4.5l3 3.5-3 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          + Code
        </button>
        <button className="btn btn-accent" disabled={!(recording || state === S.PAUSED) || busy} onClick={onScreenshot}
                title="photograph the application, on the machine it runs on">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 5h3l1-2h4l1 2h3v8H2V5z" stroke="currentColor" strokeWidth="1.3" fill="none"/><circle cx="8" cy="9" r="2.5" stroke="currentColor" strokeWidth="1.3" fill="none"/></svg>
          Shot
        </button>
        <button className="btn btn-ghost" disabled={(!recording && state !== S.STOPPED) || busy} onClick={onUndo}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M4 7h6a3 3 0 110 6H8" stroke="currentColor" strokeWidth="1.5" fill="none"/><path d="M6 5L4 7l2 2" stroke="currentColor" strokeWidth="1.5" fill="none"/></svg>
          Undo
        </button>
      </div>

      <div className="toolbar-divider" />

      <div className="toolbar-group" style={{ display: 'flex', gap: 6 }}>
        <button className="btn btn-ghost" disabled={state !== S.STOPPED || busy} onClick={onSave}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M3 3v10h10V5.5L10.5 3H3zm7 0v3H5V3m0 10v-4h6v4" stroke="currentColor" strokeWidth="1.3" fill="none"/></svg>
          Save
        </button>
        <button className="btn btn-ghost" disabled={state !== S.STOPPED || busy} onClick={onKeep}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M4 2h8v12l-4-2.5L4 14V2z" stroke="currentColor" strokeWidth="1.3" fill="none"/></svg>
          Keep
          {busy && <span className="spinner" />}
        </button>
        <button className="btn btn-ghost" disabled={state !== S.STOPPED || busy} onClick={onReplay}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M5 3l8 5-8 5V3z" fill="currentColor"/></svg>
          Replay
        </button>
      </div>
    </nav>
  );
}
