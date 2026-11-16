import { useState, useEffect } from 'react';
import toast from 'react-hot-toast';
import { filesApi, errMsg } from '../../api/client';
import { compressImage } from '../../utils/compressImage';
import { C, inp, labelSt } from './shared';

// Bike's condition when it arrives — photos (scratches, dents, odometer), fuel level and
// damage notes — so a "you scratched my bike" dispute can be settled from the job card.
const FUEL_LEVELS = ['Reserve', '1/4', '1/2', '3/4', 'Full'];
const MAX_CHECKIN_PHOTOS = 8;

function Thumb({ id, onRemove }) {
  const [url, setUrl] = useState(null);
  useEffect(() => {
    let u = null, cancelled = false;
    filesApi.getFileBlob(id).then(b => { u = URL.createObjectURL(b); if (cancelled) URL.revokeObjectURL(u); else setUrl(u); })
      .catch(() => {});
    return () => { cancelled = true; if (u) URL.revokeObjectURL(u); };
  }, [id]);
  return (
    <div style={{ position:'relative', width:76, height:76 }}>
      <button type="button" onClick={() => url && window.open(url, '_blank')} aria-label="Open check-in photo"
        style={{ width:'100%', height:'100%', padding:0, border:`1px solid ${C.border}`, borderRadius:6, overflow:'hidden', background:C.s2, cursor:'zoom-in' }}>
        {url ? <img src={url} alt="" style={{ width:'100%', height:'100%', objectFit:'cover', display:'block' }} />
             : <span style={{ fontSize:10, color:C.muted }}>…</span>}
      </button>
      {onRemove && (
        <button type="button" onClick={onRemove} aria-label="Remove photo"
          style={{ position:'absolute', top:-6, right:-6, width:22, height:22, borderRadius:'50%', border:0,
                   background:'var(--color-ink)', color:'var(--color-paper)', fontSize:13, lineHeight:'22px', cursor:'pointer' }}>×</button>
      )}
    </div>
  );
}

export default function CheckinCondition({ value, onChange }) {
  const ids = value.checkin_photo_ids || [];
  const [busy, setBusy] = useState(0);

  const add = async e => {
    const files = [...e.target.files].slice(0, MAX_CHECKIN_PHOTOS - ids.length);
    e.target.value = '';
    if (!files.length) return toast.error(`Up to ${MAX_CHECKIN_PHOTOS} photos per job`);
    setBusy(files.length);
    const added = [];
    for (const f of files) {
      try {
        const small = await compressImage(f).catch(() => f);   // unreadable format → server decides
        added.push((await filesApi.upload(small)).file_id);
      } catch (err) {
        toast.error(errMsg(err, `Could not upload ${f.name}`));
      } finally {
        setBusy(n => n - 1);
      }
    }
    if (added.length) onChange({ checkin_photo_ids: [...ids, ...added] });
  };

  return (
    <div style={{ marginBottom:16 }}>
      <div style={{ fontSize:10, fontWeight:700, color:C.muted, marginBottom:10 }}>Check-in condition</div>
      <div style={{ display:'flex', flexWrap:'wrap', gap:10, alignItems:'center', marginBottom:12 }}>
        {ids.map(id => <Thumb key={id} id={id} onRemove={() => onChange({ checkin_photo_ids: ids.filter(x => x !== id) })} />)}
        {ids.length < MAX_CHECKIN_PHOTOS && (
          <label className="btn btn-ghost btn-sm" style={{ cursor: busy ? 'wait' : 'pointer' }}>
            {busy ? `Uploading ${busy}…` : ids.length ? '+ Add photo' : '📷 Take photos'}
            <input type="file" accept="image/*" capture="environment" multiple onChange={add} disabled={!!busy}
              className="visually-hidden" />
          </label>
        )}
        {!ids.length && !busy && <span style={{ fontSize:11, color:C.muted }}>Scratches, dents, odometer — up to {MAX_CHECKIN_PHOTOS}</span>}
      </div>
      <div style={{ display:'grid', gridTemplateColumns:'1fr 2fr', gap:12 }}>
        <label>
          <span style={labelSt}>Fuel level</span>
          <select value={value.fuel_level || ''} onChange={e => onChange({ fuel_level: e.target.value })} style={inp}>
            <option value="">—</option>
            {FUEL_LEVELS.map(l => <option key={l}>{l}</option>)}
          </select>
        </label>
        <label>
          <span style={labelSt}>Existing damage</span>
          <input value={value.damage_notes || ''} maxLength={500} onChange={e => onChange({ damage_notes: e.target.value })}
            placeholder="e.g. scratch on left side panel, cracked indicator" style={inp} />
        </label>
      </div>
    </div>
  );
}
