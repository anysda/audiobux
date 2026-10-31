// A swipe turns one page at most: far enough, or quick enough, it goes the way the finger went;
// otherwise the page stays where it was.
export const FAR = 0.2; // of the page's width
export const FAST = 0.35; // px per ms
export const SLOP = 16; // px, below this even a quick flick is a tap

// the page shown after a swipe of `dx` px (right is positive) over `ms`, from `page` in 0..`last`
export function turn(page: number, last: number, dx: number, ms: number, width: number) {
  const d = Math.abs(dx);
  const far = d >= width * FAR;
  const fast = d >= SLOP && d / Math.max(ms, 1) >= FAST;
  if (!far && !fast) return page;
  return Math.min(Math.max(page + (dx < 0 ? 1 : -1), 0), Math.max(last, 0));
}

