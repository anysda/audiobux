// A swipe turns one page at most: far enough, or quick enough, it goes the way the finger went;
// otherwise the page stays where it was.
export const FAR = 0.2; // of the page's width
export const FAST = 0.35; // px per ms
export const SLOP = 16; // px, below this even a quick flick is a tap

// `page` held to 0..`last`
export const hold = (page: number, last: number) => Math.min(Math.max(page, 0), Math.max(last, 0));

// the page shown after a swipe of `dx` px (right is positive) over `ms`, from `page` in 0..`last`
export function turn(page: number, last: number, dx: number, ms: number, width: number) {
  const d = Math.abs(dx);
  const far = d >= width * FAR;
  const fast = d >= SLOP && d / Math.max(ms, 1) >= FAST;
  if (!far && !fast) return page;
  return hold(page + (dx < 0 ? 1 : -1), last);
}

// The wheel and the trackpad turn one page per push. Small deltas add up to STEP; after a turn the
// rest of the push, a trackpad's fling included, is let go until it falls QUIET. A mouse wheel's
// next notch (a delta of NOTCH or more) turns again, though no sooner than PACE after the last turn.
export const STEP = 40; // px
export const QUIET = 160; // ms
export const NOTCH = 100; // px
export const PACE = 220; // ms

// feeds deltas (down or right is positive, in px) and times (ms); gives the pages to turn: -1, 0 or 1
export function wheel() {
  let sum = 0,
    last = -Infinity,
    turned = -Infinity,
    lock = false;
  return (d: number, t: number) => {
    const gap = t - last;
    last = t;
    if (gap >= QUIET || (lock && Math.abs(d) >= NOTCH && t - turned >= PACE)) {
      lock = false;
      sum = 0;
    }
    if (lock || !d) return 0;
    if (Math.sign(d) !== Math.sign(sum)) sum = 0;
    sum += d;
    if (Math.abs(sum) < STEP) return 0;
    lock = true;
    turned = t;
    sum = 0;
    return d > 0 ? 1 : -1;
  };
}

// a key held down turns a page every REPEAT ms, not at the keyboard's own rate
export const REPEAT = 200; // ms

// the pages a key turns: -1, 0 or 1
export function step(key: string, shift = false) {
  if (key === 'ArrowRight' || key === 'PageDown') return 1;
  if (key === 'ArrowLeft' || key === 'PageUp') return -1;
  if (key === ' ') return shift ? -1 : 1;
  return 0;
}
