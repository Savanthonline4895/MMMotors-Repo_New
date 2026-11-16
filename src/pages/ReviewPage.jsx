import { useState, useEffect } from 'react';
import Monogram from '../components/Monogram';
import { reviewDraft, GOOGLE_REVIEW_URL } from '../utils/reviewDraft';

// Public page behind the printed sales QR (mmmotors.biz/review). The customer picks stars here
// because Google's own review page can't be pre-filled; we write a draft they edit, copy and
// paste on Google from their own account. Every rating gets the Google button — Google forbids
// sending only happy customers to reviews.
export default function ReviewPage() {
  const [rating, setRating]   = useState(0);
  const [vehicle, setVehicle] = useState('');
  const [text, setText]       = useState('');
  const [copied, setCopied]   = useState(false);
  const [variant, setVariant] = useState(() => Math.floor(Math.random() * 100));
  const [edited, setEdited]   = useState(false);   // customer typed in the review: stop rewriting it

  useEffect(() => { document.title = 'Review · MM Motors'; }, []);

  const choose = (n, v = variant) => {
    setRating(n); setVariant(v); setEdited(false); setCopied(false);
    setText(reviewDraft({ rating: n, vehicle, variant: v }));
  };
  const copyAndGo = async () => {
    try { await navigator.clipboard.writeText(text); setCopied(true); } catch { /* text stays on screen to copy by hand */ }
    window.location.href = GOOGLE_REVIEW_URL;
  };

  return (
    <main className="login-form-col" style={{ minHeight: '100vh' }}>
      <div className="login-form-wrap">
        <div className="login-wordmark">
          <Monogram size={36} className="login-wordmark-mark" />
          <div className="login-wordmark-name">MM Motors</div>
        </div>

        <h1 className="login-title">How was buying your bike with us?</h1>
        <p className="login-sub">Tap the stars — we'll write a review you can post on Google.</p>

        <div className="login-form">
          <div className="field">
            <span className="field-label" id="rv-rating">Your rating</span>
            <div role="group" aria-labelledby="rv-rating" style={{ display: 'flex', gap: 'var(--space-2xs)' }}>
              {[1, 2, 3, 4, 5].map(n => (
                <button key={n} type="button" onClick={() => choose(n)}
                  aria-label={`${n} star${n > 1 ? 's' : ''}`} aria-pressed={rating === n}
                  style={{ fontSize: 40, lineHeight: 1, minWidth: 48, minHeight: 48, background: 'none', border: 0, cursor: 'pointer',
                           color: n <= rating ? 'var(--color-accent)' : 'var(--color-rule-2)' }}>
                  ★
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="rv-vehicle">Which bike did you buy? (optional)</label>
            <input id="rv-vehicle" value={vehicle} maxLength={60} placeholder="e.g. Honda Shine"
              onChange={e => {
                setVehicle(e.target.value);
                if (rating && !edited) setText(reviewDraft({ rating, vehicle: e.target.value, variant }));
              }} />
          </div>

          {rating > 0 && (
            <>
              <div className="field">
                <label className="field-label" htmlFor="rv-text">Your review — change anything you like</label>
                <textarea id="rv-text" rows={6} maxLength={1000} value={text} onChange={e => { setText(e.target.value); setEdited(true); }} />
              </div>
              <button type="button" className="btn btn-primary login-submit" onClick={copyAndGo} disabled={!text.trim()}>
                {copied ? 'Copied — opening Google…' : 'Copy & post on Google'}
              </button>
              <button type="button" className="btn btn-ghost" onClick={() => choose(rating, variant + 1)}>Suggest different wording</button>
              <p className="login-sub" style={{ fontSize: 'var(--text-xs)' }}>
                On Google, tap {rating} star{rating > 1 ? 's' : ''}, then long-press the text box and choose Paste.
              </p>
            </>
          )}
        </div>
      </div>
    </main>
  );
}
