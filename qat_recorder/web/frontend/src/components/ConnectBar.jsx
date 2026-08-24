import { useState } from 'react';

export default function ConnectBar({ connected, onConnect, onDisconnect }) {
  const [url, setUrl]     = useState('');
  const [tok, setTok]     = useState('');
  const [showToken, setShowToken] = useState(false);

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!url.trim()) return;
    onConnect(url.trim(), tok.trim());
  };

  if (connected) {
    return (
      <section className="connect-bar connected">
        <div className="connect-status">
          <span className="status-dot connected"></span>
          <span>Connected to <strong>{url}</strong></span>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={onDisconnect}>
          Disconnect
        </button>
      </section>
    );
  }

  return (
    <section className="connect-bar">
      <form className="connect-form" onSubmit={handleSubmit}>
        <div className="connect-main">
          <label htmlFor="agent-url">Agent URL</label>
          <input
            id="agent-url"
            type="text"
            value={url}
            onChange={e => setUrl(e.target.value)}
            placeholder="e.g. https://abc.ngrok.io  or  http://192.168.1.50:8765"
            spellCheck="false"
            autoFocus
          />
          <button className="btn btn-primary" type="submit" disabled={!url.trim()}>
            Connect
          </button>
        </div>
        <div className="connect-extras">
          <button type="button" className="btn btn-ghost btn-sm"
                  onClick={() => setShowToken(!showToken)}>
            {showToken ? 'Hide token' : 'Token (optional)'}
          </button>
          {showToken && (
            <input
              type="password"
              value={tok}
              onChange={e => setTok(e.target.value)}
              placeholder="Bearer token (if agent requires one)"
              spellCheck="false"
              className="token-input"
            />
          )}
        </div>
      </form>
    </section>
  );
}
