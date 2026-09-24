// A tap opens, a long press (or space) picks; a finger that moves is scrolling, not pressing.
export interface Press {
  open: () => void;
  toggle: () => void;
  picking: () => boolean;
}

export function press(node: HTMLElement, o: Press) {
  let timer = 0,
    long = false,
    sx = 0,
    sy = 0;
  const cancel = () => {
    clearTimeout(timer);
    node.classList.remove('press');
  };
  const down = (e: PointerEvent) => {
    if (e.button || (e.target as Element).closest('[data-own]')) return;
    long = false;
    sx = e.clientX;
    sy = e.clientY;
    node.classList.add('press');
    timer = setTimeout(() => {
      long = true;
      node.classList.remove('press');
      navigator.vibrate?.(10);
      o.toggle();
    }, 500);
  };
  const move = (e: PointerEvent) => {
    if (Math.hypot(e.clientX - sx, e.clientY - sy) > 10) cancel();
  };
  const click = (e: MouseEvent) => {
    if ((e.target as Element).closest('[data-own]')) return;
    if (long) long = false;
    else if (o.picking()) o.toggle();
    else o.open();
  };
  const key = (e: KeyboardEvent) => {
    if (e.target !== node) return;
    if (e.key === 'Enter') {
      e.preventDefault();
      o.open();
    } else if (e.key === ' ') {
      e.preventDefault();
      o.toggle();
    }
  };
  const menu = (e: Event) => e.preventDefault();
  const on: [string, EventListener][] = [
    ['pointerdown', down as EventListener],
    ['pointermove', move as EventListener],
    ['pointerup', cancel],
    ['pointercancel', cancel],
    ['pointerleave', cancel],
    ['click', click as EventListener],
    ['keydown', key as EventListener],
    ['contextmenu', menu],
  ];
  on.forEach(([n, f]) => node.addEventListener(n, f));
  return {
    update: (n: Press) => (o = n),
    destroy: () => {
      cancel();
      on.forEach(([n, f]) => node.removeEventListener(n, f));
    },
  };
}
