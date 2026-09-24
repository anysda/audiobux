export const EO = 'cubic-bezier(0.23,1,0.32,1)';
export const EIO = 'cubic-bezier(0.77,0,0.175,1)';
export const ED = 'cubic-bezier(0.32,0.72,0,1)';

export const still = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

// words come up out of their own line, one after another
export function reveal(root: Element | null | undefined, delay = 0, step = 45) {
  if (!root || still()) return;
  root.querySelectorAll('.wi').forEach((w, i) =>
    w.animate([{ transform: 'translateY(108%)' }, { transform: 'none' }], {
      duration: 760,
      delay: delay + i * step,
      easing: EO,
      fill: 'backwards',
    }),
  );
}

export function rise(els: (Element | null | undefined)[], delay = 0, step = 70, dist = 24) {
  if (still()) return;
  els.forEach((e, i) =>
    e?.animate([{ transform: `translateY(${dist}px)`, opacity: 0 }, { transform: 'none', opacity: 1 }], {
      duration: 640,
      delay: delay + i * step,
      easing: EO,
      fill: 'backwards',
    }),
  );
}

// the rectangle of `r` inside `box` as a clip-path
export const inset = (r: DOMRect, box: DOMRect, round = '') =>
  `inset(${r.top - box.top}px ${box.right - r.right}px ${box.bottom - r.bottom}px ${r.left - box.left}px${round})`;
