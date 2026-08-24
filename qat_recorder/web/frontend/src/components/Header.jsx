export default function Header({ connected, state, agentHost }) {
  const dotClass = state === 'recording' || state === 'picking'
    ? 'status-dot recording'
    : connected ? 'status-dot connected' : 'status-dot';

  const label = !connected ? 'Disconnected'
    : state === 'recording' ? 'Recording'
    : state === 'paused' ? 'Paused'
    : state === 'picking' ? 'Picking'
    : state === 'stopped' ? 'Stopped'
    : 'Connected';

  return (
    <header className="app-header">
      <div className="header-left">
        <div className="logo">
          <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
            <circle cx="14" cy="14" r="12" stroke="url(#grad1)" strokeWidth="2.5"/>
            <circle cx="14" cy="14" r="5" fill="url(#grad1)"/>
            <defs>
              <linearGradient id="grad1" x1="0" y1="0" x2="28" y2="28">
                <stop offset="0%" stopColor="#7c5cfc"/>
                <stop offset="100%" stopColor="#00d4aa"/>
              </linearGradient>
            </defs>
          </svg>
          <h1>QAT Recorder</h1>
        </div>
        {connected && <div className="badge badge-muted">{agentHost || state}</div>}
      </div>
      <div className="header-right">
        <div className="status-indicator">
          <span className={dotClass}></span>
          <span>{label}</span>
        </div>
      </div>
    </header>
  );
}
