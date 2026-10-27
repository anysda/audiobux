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

type Step = { start: () => void; shrink: () => void; end: () => void; moved: () => boolean };

// titles due are measured together, one layout for all of them instead of one each:
// a book laid out in columns is reflowed whole by every layout
const due = new Set<Step>();
const live = new Map<Element, Set<Step>>();
// a box resized: again only the titles it made wider or narrower, or held to a height
const ro = new ResizeObserver((es) => es.forEach((e) => live.get(e.target)?.forEach((x) => x.moved() && later(x))));
// a font come late changes every measure
document.fonts.addEventListener('loadingdone', () => live.forEach((all) => all.forEach(later)));

function later(step: Step) {
  if (!due.size)
    queueMicrotask(() => {
      const all = [...due];
      due.clear();
      all.forEach((x) => x.start());
      all.forEach((x) => x.shrink());
      all.forEach((x) => x.end());
    });
  due.add(step);
}

export function fit(el: HTMLElement, o: Fit) {
  let s = o.max,
    W = 0;
  const start = () => {
    s = o.max;
    el.style.fontSize = s + 'px';
    // measured unclamped: the clamp would hide the lines that call for a smaller size
    el.style.display = 'block';
  };
  const box = el.parentElement;
  const shrink = () => {
    W = el.clientWidth;
    const own = () => el.offsetHeight - parseFloat(getComputedStyle(el).paddingBottom);
    const parts = [...el.querySelectorAll<HTMLElement>('.wi')];
    const wide = () => parts.some((w) => w.scrollWidth > W + 0.5) || el.scrollWidth > W + 0.5;
    const tall = () => {
      if (o.lines && own() > s * 0.86 * o.lines + 4) return true;
      const h = o.height?.();
      if (!h || !box) return false;
      // by layout, not scrollHeight: a neighbour moving in on a transform must not shrink the title
      const kids = [...box.children] as HTMLElement[];
      const gap = parseFloat(getComputedStyle(box).rowGap) || 0;
      return kids.reduce((a, k) => a + (k === el ? own() : k.offsetHeight), 0) + gap * (kids.length - 1) > h;
    };
    while (s > o.min && (wide() || tall())) el.style.fontSize = (s -= 2) + 'px';
  };
  const step = { start, shrink, end: () => (el.style.display = ''), moved: () => !!o.height || el.clientWidth !== W };
  if (box) {
    live.set(box, (live.get(box) ?? new Set()).add(step));
    ro.observe(box);
  }
  later(step);
  return {
    update(n: Fit) {
      o = n;
      later(step);
    },
    destroy() {
      due.delete(step);
      const mine = box && live.get(box);
      if (!box || !mine) return;
      mine.delete(step);
      if (mine.size) return;
      live.delete(box);
      ro.unobserve(box);
    },
  };
}
