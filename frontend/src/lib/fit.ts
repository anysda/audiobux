// Words are never broken: a title that does not fit gets a smaller size instead.
// A word glued from parts (a file name) may wrap after its `_`, `-` and `.`; a part still too wide ends in an ellipsis.
export const words = (text: string) => text.split(/\s+/).filter(Boolean).map((w) => w.split(/(?<=[_.-])(?=.)/));

export interface Fit {
  text: string;
  max: number;
  min: number;
  // at most this many lines of `line-height: .86`
  lines?: number;
  // the title's block may be at most this tall (the cover beside it)
  height?: () => number | undefined;
}

export function fit(el: HTMLElement, o: Fit) {
  const run = () => {
    let s = o.max;
    el.style.fontSize = s + 'px';
    // measured unclamped: the clamp would hide the lines that call for a smaller size
    el.style.display = 'block';
    const W = el.clientWidth;
    const own = () => el.offsetHeight - parseFloat(getComputedStyle(el).paddingBottom);
    const parts = [...el.querySelectorAll<HTMLElement>('.wi')];
    const wide = () => parts.some((w) => w.scrollWidth > W + 0.5) || el.scrollWidth > W + 0.5;
    const tall = () => {
      if (o.lines && own() > s * 0.86 * o.lines + 4) return true;
      const h = o.height?.();
      const box = el.parentElement;
      if (!h || !box) return false;
      // by layout, not scrollHeight: a neighbour moving in on a transform must not shrink the title
      const kids = [...box.children] as HTMLElement[];
      const gap = parseFloat(getComputedStyle(box).rowGap) || 0;
      return kids.reduce((a, k) => a + (k === el ? own() : k.offsetHeight), 0) + gap * (kids.length - 1) > h;
    };
    while (s > o.min && (wide() || tall())) el.style.fontSize = (s -= 2) + 'px';
    el.style.display = '';
  };
  const ro = new ResizeObserver(run);
  if (el.parentElement) ro.observe(el.parentElement);
  void document.fonts.ready.then(run);
  run();
  return {
    update(n: Fit) {
      o = n;
      run();
    },
    destroy: () => ro.disconnect(),
  };
}
