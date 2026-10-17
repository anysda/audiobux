<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { lib } from './state.svelte';
  import { url, type Block } from './api';
  import { still } from './motion';
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

  const book = $derived(lib.book(id));
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
    const chapters = (sync?.chapters ?? []).map((c) => ({ title: c.title, pieces: body(c).map(piece) }));
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
    return () => ro.disconnect();
  });

  // pages settle on a whole page after a swipe
  function settle() {
    if (!pages || !box) return;
    const W = box.clientWidth;
    box.scrollTo({ left: Math.round(box.scrollLeft / W) * W, behavior: still() ? 'auto' : 'smooth' });
  }

  function off() {
    follow = false;
    if (now >= 0) el(now)?.classList.remove('now');
    arrow?.classList.add('off');
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

<div
  class={wide ? 'prd' : 'pane rd'}
  class:pages
  data-s={wide ? undefined : lib.scheme}
  role="tabpanel"
  tabindex="-1"
  bind:this={box}
  style:--pw={pw + 'px'}
  style:--z={lib.zoom / 100}
  onclick={off}
  onkeydown={(e) => e.key.startsWith('Arrow') && off()}
  onwheel={() => (moved = true)}
  ontouchstart={() => (moved = true)}
  onscrollend={settle}
>
  <svg class="ar" bind:this={arrow} viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h16M13 6l6 6-6 6" /></svg>
  <article>
    {#if sync === null}
      <p class="gone mono">Текст этой книги недоступен</p>
    {/if}
    {#each view.chapters as c, n (n)}
      <section>
        <div class="rk mono"><span>Глава {n + 1}</span><span>{book?.author}</span></div>
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
