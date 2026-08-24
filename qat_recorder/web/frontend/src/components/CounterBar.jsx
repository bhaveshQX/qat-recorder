export default function CounterBar({ summary, statusMsg }) {
  const s = summary || {};
  const steps      = s.actions ?? 0;
  const events     = s.events ?? 0;
  const fragile    = s.fragile ?? 0;
  const unresolved = s.unresolved ?? 0;
  const secrets    = s.secrets ?? 0;
  const dropped    = s.dropped ?? 0;

  return (
    <footer className="counter-bar">
      <div className="counters">
        <span>steps <strong>{steps}</strong></span>
        <span className="counter-dot">·</span>
        <span>events <strong>{events}</strong></span>
        {fragile > 0 && (
          <>
            <span className="counter-dot">·</span>
            <span className="counter-fragile">needs review <strong>{fragile}</strong></span>
          </>
        )}
        {unresolved > 0 && (
          <>
            <span className="counter-dot">·</span>
            <span className="counter-unresolved">unidentified <strong>{unresolved}</strong></span>
          </>
        )}
        {secrets > 0 && (
          <>
            <span className="counter-dot">·</span>
            <span>redacted <strong>{secrets}</strong></span>
          </>
        )}
        {dropped > 0 && (
          <>
            <span className="counter-dot">·</span>
            <span>discarded <strong>{dropped}</strong></span>
          </>
        )}
      </div>
      <div className="status-message">{statusMsg}</div>
    </footer>
  );
}
