import { useEffect, useState } from 'react';
import { fetchMedia } from '../api';

/**
 * A still from the agent.
 *
 * Fetched rather than pointed at. An <img src> cannot carry the headers the
 * rest of the panel sends, and a tunnel that answers header-less browser
 * requests with an interstitial page turns every screenshot into a broken image
 * icon with no explanation. This says what went wrong instead.
 */
export default function MediaImage({ base, sid, name, token, alt, className }) {
  const [url, setUrl] = useState('');
  const [failed, setFailed] = useState('');

  useEffect(() => {
    if (!base || !sid || !name) return undefined;
    let live = true;
    let made = '';
    setFailed('');
    fetchMedia(base, sid, name, token)
      .then(objectUrl => {
        if (!live) { URL.revokeObjectURL(objectUrl); return; }
        made = objectUrl;
        setUrl(objectUrl);
      })
      .catch(error => { if (live) setFailed(error.message); });
    return () => {
      live = false;
      if (made) URL.revokeObjectURL(made);   // the blob is ours to release
    };
  }, [base, sid, name, token]);

  if (failed) {
    return <div className="media-failed">could not load {name}: {failed}</div>;
  }
  if (!url) return <div className="media-loading">loading {name}…</div>;
  return <img className={className} src={url} alt={alt || name} />;
}
