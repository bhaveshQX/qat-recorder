import { useState } from 'react';

export default function KeepModal({ onSubmit, onCancel }) {
  const [name, setName] = useState('');

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <h2>Keep this test</h2>
        <p>Name it — what does this test do?</p>
        <div className="modal-form">
          <input type="text" value={name}
                 onChange={e => setName(e.target.value)}
                 onKeyDown={e => e.key === 'Enter' && name.trim() && onSubmit(name)}
                 placeholder="e.g. add-a-torrent" spellCheck="false" autoFocus />
        </div>
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="btn btn-primary" disabled={!name.trim()} onClick={() => onSubmit(name)}>
            Keep
          </button>
        </div>
      </div>
    </div>
  );
}
