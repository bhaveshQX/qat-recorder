import { useState, useEffect } from 'react';

const INTERESTING = ['text', 'title', 'checked', 'enabled', 'visible', 'currentText', 'value'];

export default function CheckpointModal({ label, properties, onSubmit, onCancel }) {
  const propKeys = [
    ...INTERESTING.filter(k => k in properties),
    ...Object.keys(properties).filter(k => !INTERESTING.includes(k)).sort(),
  ];

  const [selectedProp, setSelectedProp] = useState(propKeys[0] || '');
  const [expected, setExpected]         = useState('');

  useEffect(() => {
    if (selectedProp && properties[selectedProp] != null) {
      setExpected(String(properties[selectedProp]));
    }
  }, [selectedProp, properties]);

  const handleSubmit = () => onSubmit(selectedProp, expected);

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <h2>Add checkpoint</h2>
        <p>Checking <strong>{label}</strong></p>
        <div className="modal-form">
          <label htmlFor="cp-property">Property</label>
          <select id="cp-property" value={selectedProp}
                  onChange={e => setSelectedProp(e.target.value)}>
            {propKeys.map(k => <option key={k} value={k}>{k}</option>)}
          </select>
          <label htmlFor="cp-expected">Expected value</label>
          <input id="cp-expected" type="text" value={expected}
                 onChange={e => setExpected(e.target.value)}
                 onKeyDown={e => e.key === 'Enter' && handleSubmit()}
                 spellCheck="false" autoFocus />
        </div>
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="btn btn-primary" onClick={handleSubmit}>Add</button>
        </div>
      </div>
    </div>
  );
}
