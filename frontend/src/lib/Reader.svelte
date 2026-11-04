<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { lib } from './state.svelte';
  import { url, type Block } from './api';
  import { still } from './motion';
  import { hold, REPEAT, step, turn, wheel } from './swipe';
  import Title from './Title.svelte';

  let { id, wide = false }: { id: string; wide?: boolean } = $props();

  let box = $state<HTMLElement>();
  let arrow = $state<SVGElement>();
  let pw = $state(0);
  // the voice is lit and followed until the reader takes the text into their own hands
  let follow = $state(true);
  let now = -1;
  let shown = false;
  // the reader has moved the text by hand since it opened
  let moved = false;

  type Part = { text: string; k: number | null };
  type Piece =
    | { kind: 'h' | 'code'; text: string; k: number | null }
    | { kind: 'p'; parts: Part[] }
    | { kind: 'figure'; src: string | null; alt: string; k: number | null };

  const sync = $derived(lib.syncs[id]);

  // every heard piece gets a number; `times` holds when each is heard, in order
  const view = $derived.by(() => {
    const times: [number, number][] = [];
    const num = (t0: number | null, t1: number | null) => {
      if (t0 === null || t1 === null) return null;
      times.push([t0, t1]);
      return times.length - 1;
    };
    const piece = (b: Block): Piece =>
      b.type === 'p'
        ? { kind: 'p', parts: b.sentences.map((s) => ({ text: s.text, k: num(s.t0, s.t1) })) }
        : b.type === 'figure'
          ? { kind: 'figure', src: b.src, alt: b.alt, k: num(b.t0, b.t1) }
          : { kind: b.type, text: b.text, k: num(b.t0, b.t1) };
    // the chapter's own heading is its title, shown once
    const body = (c: { title: string; blocks: Block[] }) =>
      c.blocks.filter((b, j) => !(j === 0 && b.type === 'h' && b.text === c.title));
    const chapters = (sync?.chapters ?? []).map((c) => ({ title: c.title, t0: c.t0, pieces: body(c).map(piece) }));
    return { chapters, times };
  });

  // the last piece begun by `t`, or -1
  function begun(t: number) {
    const ts = view.times;
    let lo = 0,
      hi = ts.length - 1,
      i = -1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (ts[m][0] <= t) {
        i = m;
        lo = m + 1;
      } else hi = m - 1;
    }
    return i;
  }

  // the last piece begun by `t`, while it is still being heard (a short pause keeps it lit)
  function find(t: number) {
    const i = begun(t);
    return i >= 0 && t < view.times[i][1] + 2 ? i : -1;
  }

  const el = (k: number) => box?.querySelector<HTMLElement>(`[data-k="${k}"]`) ?? null;
  const pages = $derived(lib.mode === 1);
  // pages are cut to the pane's width: until it is measured the text is not laid out at all,
  // zero-wide columns would give every word its own page and every title a fit over the whole book
  const chapters = $derived(pages && !pw ? [] : view.chapters);

  function place(instant = false) {
    const a = arrow,
      b = box,
      s = now >= 0 ? el(now) : null;
    if (!a || !b) return;
    // between pieces, as at the start of a chapter, the text goes to the next one heard and the arrow waits
    const r = (s ?? el(begun(lib.at(id)) + 1))?.getClientRects()[0];
    a.classList.toggle('off', !s || !r || !follow);
    if (!r) return;
    const rr = b.getBoundingClientRect();
    const y = r.top - rr.top + b.scrollTop + (r.height - 24) / 2;
    const lx = r.left - rr.left + b.scrollLeft;
    const W = b.clientWidth;
    const pg = Math.floor(lx / W);
    let x: number;
    if (!wide) x = pages ? pg * W + 12 : 12;
    else if (!pages) x = (b.querySelector('article')?.offsetLeft ?? 96) - 40;
    else {
      const cw = (W - 256) / 2;
      x = lx - pg * W - 64 > cw + 64 ? pg * W + 64 + cw + 128 - 40 : pg * W + 28;
    }
    if (instant) {
      a.style.transition = 'none';
      requestAnimationFrame(() => (a.style.transition = ''));
    }
    a.style.transform = `translate(${x}px,${y}px)`;
    // opened on a book already begun: start where the voice is, playing or not
    const first = !shown;
    shown = true;
    if (!first && (!lib.playing || lib.cur !== id || !follow)) return;
    const behavior = first || still() ? 'auto' : 'smooth';
    if (pages) {
      // a finger on the page, or a page still turning, is not taken away from the reader
      if (g?.on || glide) return;
      if (pg * W !== b.scrollLeft) b.scrollTo({ left: pg * W, behavior });
    } else {
      const top = r.top - rr.top;
      if (top < 40 || top > b.clientHeight - 80) b.scrollTo({ top: b.scrollTop + top - b.clientHeight * 0.35, behavior });
    }
  }

  // light the piece being heard
  $effect(() => {
    const i = view.times.length ? find(lib.at(id)) : -1;
    if (i === now) return;
    if (now >= 0) el(now)?.classList.remove('now');
    now = i;
    if (i >= 0 && follow) el(i)?.classList.add('now');
    place();
  });

  // after a new text, layout or size: light again and put the arrow back
  $effect(() => {
    void [view, pages, lib.zoom, pw];
    void tick().then(() => {
      if (now >= 0 && follow) el(now)?.classList.add('now');
      place(true);
    });
  });

  onMount(() => {
    void lib.loadSync(id);
    const ro = new ResizeObserver(() => (pw = box?.clientWidth ?? 0));
    if (box) ro.observe(box);
    // not passive: in pages the wheel turns them, the browser's own scroll is held back
    box?.addEventListener('wheel', roll, { passive: false });
    return () => {
      ro.disconnect();
      box?.removeEventListener('wheel', roll);
    };
  });

  // pages settle on a whole page after the wheel or the keys moved them
  function settle() {
    if (!pages || !box || g || glide) return;
    const W = box.clientWidth;
    box.scrollTo({ left: Math.round(box.scrollLeft / W) * W, behavior: still() ? 'auto' : 'smooth' });
  }

  // a finger, or the mouse, turns one page per swipe: the text follows it, and on release goes to the page
  // `turn` picks. the browser's own pan is off (touch-action), its fling would fly over several pages.
  // the mouse also selects: a drag down, a drag after a double click, or one begun on a selection, selects
  let g: { id: number; x: number; y: number; t: number; left: number; on: boolean } | null = null;
  let glide = 0;
  let aim = 0;
  let swiped = false;
  let clicks = 0;
  const last = (b: HTMLElement) => Math.round(b.scrollWidth / b.clientWidth) - 1;
  function onSel(e: PointerEvent) {
    const s = getSelection();
    if (!s || s.isCollapsed || !s.rangeCount) return false;
    return [...s.getRangeAt(0).getClientRects()].some(
      (r) => e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom,
    );
  }
  function grab(e: PointerEvent) {
    swiped = false;
    if (!pages || !box || !e.isPrimary) return;
    if (e.pointerType === 'mouse' && (e.button !== 0 || e.shiftKey || onSel(e) || (e.target as Element).closest('a, button, img')))
      return;
    cancelAnimationFrame(glide);
    glide = 0;
    g = { id: e.pointerId, x: e.clientX, y: e.clientY, t: e.timeStamp, left: box.scrollLeft, on: false };
  }
  function drag(e: PointerEvent) {
    if (!g || !box || e.pointerId !== g.id) return;
    const mouse = e.pointerType === 'mouse';
    // a button let go outside, before the drag began, never came back as a pointerup
    if (mouse && !(e.buttons & 1)) return void (g = null);
    const dx = e.clientX - g.x,
      dy = e.clientY - g.y;
    if (!g.on) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      // across is a page turn; down, or a selection being drawn, is not
      if (Math.abs(dy) >= Math.abs(dx) || (mouse ? clicks > 1 : !getSelection()?.isCollapsed)) return void (g = null);
      g.on = true;
      moved = true;
      box.setPointerCapture(e.pointerId);
      if (mouse) box.style.userSelect = 'none';
    }
    // the few letters the mouse took on its way to the threshold are not a selection
    if (mouse && !getSelection()?.isCollapsed) getSelection()?.removeAllRanges();
    box.scrollLeft = g.left - dx;
  }
  function drop(e: PointerEvent) {
    if (!g || !box || e.pointerId !== g.id) return;
    const s = g;
    g = null;
    if (!s.on) return;
    box.style.userSelect = '';
    swiped = true;
    const W = box.clientWidth;
    const from = Math.round(s.left / W);
    const to = e.type === 'pointercancel' ? from : turn(from, last(box), e.clientX - s.x, e.timeStamp - s.t, W);
    go(to * W);
  }

  // `n` pages on from the one shown, or from the one a turn is already going to
  function flip(n: number) {
    const b = box;
    if (!b || g?.on) return;
    const W = b.clientWidth;
    moved = true;
    go(hold(Math.round((glide ? aim : b.scrollLeft) / W) + n, last(b)) * W);
  }

  // the wheel and the trackpad: one push, one page
  const push = wheel();
  function roll(e: WheelEvent) {
    moved = true;
    if (!pages || !box || e.ctrlKey) return;
    e.preventDefault();
    const k = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? box.clientWidth : 1;
    const n = push((Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) * k, e.timeStamp);
    if (n) flip(n);
  }

  // the keys turn a page at once, held they go on at REPEAT. in the text, or with nothing else in focus;
  // the space only in the text, elsewhere it is the player's
  let pressed = -Infinity;
  function key(e: KeyboardEvent) {
    if (!pages || !box || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    const t = e.target as Node,
      inside = box.contains(t);
    if (!inside && t !== document.body && t !== document.documentElement) return;
    const n = step(e.key, e.shiftKey);
    if (!n || (e.key === ' ' && !inside)) return;
    e.preventDefault();
    if (e.repeat && e.timeStamp - pressed < REPEAT) return;
    pressed = e.timeStamp;
    flip(n);
  }
  // a short ease out to `left`, quicker than the browser's own smooth scroll
  function go(left: number) {
    const b = box;
    if (!b) return;
    cancelAnimationFrame(glide);
    glide = 0;
    aim = left;
    const x0 = b.scrollLeft,
      d = left - x0;
    if (still() || !d) return void (b.scrollLeft = left);
    const t0 = performance.now(),
      ms = 220;
    const frame = (now: number) => {
      const k = Math.min((now - t0) / ms, 1);
      b.scrollLeft = x0 + d * (1 - (1 - k) ** 3);
      glide = k < 1 ? requestAnimationFrame(frame) : 0;
    };
    glide = requestAnimationFrame(frame);
  }

  function off() {
    follow = false;
    if (now >= 0) el(now)?.classList.remove('now');
    arrow?.classList.add('off');
  }

  // a tap on a piece heard, or on a chapter's title, sends the voice there and the text follows it again;
  // a drag that selects text, a link or a button keep their own meaning
  let down = { x: 0, y: 0 };
  function tap(e: MouseEvent) {
    const t = e.target as Element;
    if (t.closest('a, button')) return;
    if (swiped) return void (swiped = false);
    const s = t.closest<HTMLElement>('[data-k]');
    const h = s ? null : t.closest('.ttl') && t.closest('section');
    const n = h ? [...(box?.querySelectorAll('section') ?? [])].indexOf(h as HTMLElement) : -1;
    const at = s ? view.times[Number(s.dataset.k)][0] : n >= 0 ? view.chapters[n].t0 : null;
    const dragged = Math.hypot(e.clientX - down.x, e.clientY - down.y) > 6 || !getSelection()?.isCollapsed;
    if (at === null || dragged) return off();
    lib.seek(id, at, true);
    was = { t: lib.at(id), w: performance.now(), on: lib.playing && lib.cur === id };
    follow = true;
    moved = false;
    if (now >= 0) el(now)?.classList.remove('now');
    now = find(was.t);
    if (now >= 0) el(now)?.classList.add('now');
    place();
  }

  // play after a pause, a skip, the scrub or a chapter sends the voice on: the text goes back to it;
  // while playing and followed it is already there, paused it goes too
  let was: { t: number; w: number; on: boolean } | null = null;
  $effect(() => {
    const t = lib.at(id),
      on = lib.playing && lib.cur === id,
      w = performance.now(),
      p = was;
    was = { t, w, on };
    if (!p) return;
    const jump = Math.abs(t - p.t - (p.on ? ((w - p.w) / 1000) * lib.speed : 0)) > 1;
    if (follow ? on || !jump : !jump && !(on && !p.on)) return;
    follow = true;
    moved = false;
    shown = false;
    if (now >= 0) el(now)?.classList.add('now');
    place();
  });

  // a picture above pushes the text down once it loads: put the arrow back, and the voice in view
  function loaded() {
    if (!moved) shown = false;
    place(true);
  }
</script>

<svelte:window onkeydown={key} />

<div
  class={wide ? 'prd' : 'pane rd'}
  class:pages
  data-s={wide ? undefined : lib.scheme}
  role="tabpanel"
  tabindex="-1"
  bind:this={box}
  style:--pw={pw + 'px'}
  style:--z={lib.zoom / 100}
  onpointerdown={(e) => {
    down = { x: e.clientX, y: e.clientY };
    grab(e);
  }}
  onmousedown={(e) => (clicks = e.detail)}
  onpointermove={drag}
  onpointerup={drop}
  onpointercancel={drop}
  onclick={tap}
  onkeydown={(e) => e.key.startsWith('Arrow') && off()}
  ontouchstart={() => (moved = true)}
  onscrollend={settle}
>
  <svg class="ar" bind:this={arrow} viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h16M13 6l6 6-6 6" /></svg>
  <article>
    {#if sync === null}
      <p class="gone mono">Текст этой книги недоступен</p>
    {/if}
    {#each chapters as c, n (n)}
      <section>
        <Title text={c.title} max={wide ? 64 : 46} min={wide ? 30 : 24} />
        {#each c.pieces as p, j (j)}
          {#if p.kind === 'p'}
            <p>
              {#each p.parts as s, m (m)}{#if s.k === null}{s.text}{:else}<span class="s" data-k={s.k}>{s.text}</span
                  >{/if}{' '}{/each}
            </p>
          {:else if p.kind === 'h'}
            <h3 class="disp s" data-k={p.k}>{p.text}</h3>
          {:else if p.kind === 'code'}
            <pre class="s" data-k={p.k}>{p.text}</pre>
          {:else if p.kind === 'figure'}
            <figure data-k={p.k}>
              {#if p.src}<img src={url.figure(id, p.src)} alt={p.alt} loading="lazy" onload={loaded} />{:else}<p class="gone mono">{p.alt}</p>{/if}
            </figure>
          {/if}
        {/each}
      </section>
    {/each}
  </article>
</div>
