// Suggested Google review for a vehicle-sale customer, from the stars they pick on /review.
// The customer edits it and posts it from their own Google account. Several wordings per
// tier so reviews don't come out identical (Google filters copy-paste duplicates as spam).

export const GOOGLE_REVIEW_URL = 'https://search.google.com/local/writereview?placeid=ChIJd_Mt4MPjrTsRLmE9ia2rZbA';


const DRAFTS = {
  5: [
    b => `Bought my new ${b} from MM Motors, Malur. The staff explained every model patiently, the paperwork was quick and delivery was on time. Very happy with the whole experience!`,
    b => `Excellent experience buying my ${b} at MM Motors. Transparent pricing, friendly team and a smooth delivery. Highly recommended for anyone buying a bike in Malur.`,
    b => `Great showroom! Got my ${b} from MM Motors and everything from finance to registration was handled without any hassle. Thank you to the team.`,
  ],
  4: [
    b => `Good experience buying my ${b} at MM Motors, Malur. Helpful staff and clear pricing. Delivery took a little time, but overall very satisfied.`,
    b => `Purchased my ${b} from MM Motors. The team was polite and guided me well with the paperwork. Happy with my purchase.`,
  ],
  3: [
    b => `Bought my ${b} at MM Motors, Malur. The purchase was okay overall; the process could be a bit faster and communication could be better.`,
    b => `Average experience buying my ${b}. Staff were helpful, though there is room for improvement in delivery time.`,
  ],
  2: [
    b => `Bought my ${b} from MM Motors. The experience was below my expectations; delivery and updates could have been better.`,
  ],
  1: [
    b => `I was not satisfied with my experience buying my ${b} at MM Motors and hope they improve their service.`,
  ],
};

// variant: any whole number; the same one keeps the same wording while the bike name changes.
export function reviewDraft({ rating, vehicle, variant = 0 }) {
  const list = DRAFTS[rating] || DRAFTS[5];
  const bike = (vehicle || '').trim() || 'bike';
  return list[variant % list.length](bike);
}
