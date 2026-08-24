export default function SetupBar({ collapsed, appPath, libPath, appName, onAppChange, onLibChange, onNameChange, disabled }) {
  return (
    <section className={`setup-section ${collapsed ? 'collapsed' : ''}`}>
      <div className="setup-grid">
        <div className="field-group">
          <label htmlFor="field-app">Application</label>
          <input id="field-app" type="text" value={appPath}
                 onChange={e => onAppChange(e.target.value)}
                 placeholder="/path/to/your/app" spellCheck="false" disabled={disabled} />
        </div>
        <div className="field-group">
          <label htmlFor="field-lib">Filter library</label>
          <input id="field-lib" type="text" value={libPath}
                 onChange={e => onLibChange(e.target.value)}
                 placeholder="/path/to/libqatrec.*.so" spellCheck="false" disabled={disabled} />
        </div>
        <div className="field-group">
          <label htmlFor="field-name">Record as</label>
          <input id="field-name" type="text" value={appName}
                 onChange={e => onNameChange(e.target.value)}
                 placeholder="name used in generated test" spellCheck="false" disabled={disabled} />
        </div>
      </div>
    </section>
  );
}
