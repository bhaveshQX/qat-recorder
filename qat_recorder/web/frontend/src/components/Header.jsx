import logoUrl from '../assets/nogrunt.svg';

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
          <img className="logo-mark" src={logoUrl} alt="nogrunt" />
          <span className="logo-divider" />
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
