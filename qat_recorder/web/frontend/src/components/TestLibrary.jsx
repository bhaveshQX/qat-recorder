export default function TestLibrary({ tests, testResults, selectedTest, onSelect, onRefresh, onRunOne, onRunAll, disabled }) {
  const verdictFor = (c) => {
    const result = testResults[c.id];
    if (result) return result.ok ? 'passed' : 'FAILED';
    const v = c.verified;
    if (v === 'passed') return 'passed';
    if (v === 'failed') return 'FAILED';
    return '';
  };

  if (tests.length === 0) {
    return (
      <>
        <div className="empty-state">
          <p>No saved tests yet</p>
        </div>
        <div className="library-actions">
          <button className="btn btn-ghost btn-sm" onClick={onRefresh}>Refresh</button>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="table-wrapper">
        <table className="data-table">
          <thead>
            <tr>
              <th>Test</th>
              <th className="col-steps">Steps</th>
              <th className="col-date">Recorded</th>
              <th className="col-verdict">Last run</th>
            </tr>
          </thead>
          <tbody>
            {tests.map((c, idx) => {
              const verdict = verdictFor(c);
              return (
                <tr key={c.id || idx}
                    className={idx === selectedTest ? 'selected' : ''}
                    onClick={() => onSelect(idx)}>
                  <td>{c.name}</td>
                  <td className="col-steps">{c.steps}</td>
                  <td className="col-date">{(c.created || '').slice(0, 16).replace('T', ' ')}</td>
                  <td className="col-verdict">
                    <span className={verdict === 'passed' ? 'verdict-passed' : verdict === 'FAILED' ? 'verdict-failed' : ''}>
                      {verdict}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="library-actions">
        <button className="btn btn-ghost btn-sm" onClick={onRefresh}>Refresh</button>
        <div className="spacer" />
        <button className="btn btn-secondary btn-sm" disabled={disabled || selectedTest < 0} onClick={onRunOne}>
          Run selected
        </button>
        <button className="btn btn-secondary btn-sm" disabled={disabled} onClick={onRunAll}>
          Run all
        </button>
      </div>
    </>
  );
}
