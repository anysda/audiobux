<script lang="ts">
  import { tick } from 'svelte';
  import { MediaQuery } from 'svelte/reactivity';
  import { lib } from './lib/state.svelte';
  import { url, type Book } from './lib/api';
  import { books, fmt, fmtShort, fmtSpeed, gb, left, stage } from './lib/text';
  import { ED, EO, inset, reveal, rise, still } from './lib/motion';
  import Cover from './lib/Cover.svelte';
  import Title from './lib/Title.svelte';
  import Silhouette from './lib/Silhouette.svelte';
  import Seg from './lib/Seg.svelte';
  import Scrub from './lib/Scrub.svelte';
  import Reader from './lib/Reader.svelte';
  import Toast from './lib/Toast.svelte';

  const SPEEDS = [0.75, 1, 1.25, 1.5, 2];
  const narrow = new MediaQuery('(max-width: 1279px)');

  let main = $state<HTMLElement>();
  let plib = $state<HTMLElement>();
  let bookEl = $state<HTMLElement>();
  let pop = $state<'spd' | 'aa' | null>(null);
  let openId = $state<string | null>(null);
  // the library's top bar comes back as the book starts to close, so the way back lands where the card is
  let closing = $state(false);
  let src: HTMLElement | null = null;

  const picking = $derived(lib.sel.length > 0);
  const going = $derived(lib.going);
  const open = $derived(lib.book(openId));
  const now = $derived(lib.book(lib.cur));

  // the player starts on the book to go on with
  $effect(() => {
    if (!lib.cur && going) lib.load(going.id);
  });
  $effect(() => {
    if (going) void lib.loadDetails(going.id);
  });
  $effect(() => {
    if (openId && !open) openId = null;
  });

  async function show(b: Book, from?: HTMLElement | null) {
    if (b.state === 'failed') return;
    pop = null;
    src = from ?? null;
    lib.load(b.id);
    void lib.loadDetails(b.id);
    openId = b.id;
    await tick();
    if (still() || !bookEl || !main || !plib) return;
    const m = main.getBoundingClientRect();
    const r = src?.querySelector('.cv')?.getBoundingClientRect();
    if (r) bookEl.animate([{ clipPath: inset(r, m, ' round 12px') }, { clipPath: 'inset(0px round 0px)' }], { duration: 560, easing: ED });
    plib.animate([{ transform: 'none', opacity: 1 }, { transform: 'scale(.97)', opacity: 0.3 }], { duration: 560, easing: ED, fill: 'forwards' });
    reveal(bookEl.querySelector('.hero2 h2'), 160, 55);
    rise([...bookEl.querySelectorAll('.pside .hero2 .mono,.pside .wk,.pside .dl,.pside .chh,.pchl,.prh,.prd article')], 240, 60, 18);
  }

  async function hide() {
    pop = null;
    const done = () => {
      openId = null;
      closing = false;
    };
    closing = true;
    await tick();
    plib?.getAnimations().forEach((a) => a.cancel());
    const r = src?.isConnected ? src.querySelector('.cv')?.getBoundingClientRect() : null;
    if (still() || !r || !bookEl || !main || !plib) return done();
    plib.animate([{ transform: 'scale(.97)', opacity: 0.3 }, { transform: 'none', opacity: 1 }], { duration: 380, easing: EO });
    bookEl.animate([{ clipPath: 'inset(0px round 0px)' }, { clipPath: inset(r, main.getBoundingClientRect(), ' round 12px') }], {
      duration: 380,
      easing: EO,
    }).onfinish = done;
  }

  function card(e: MouseEvent, b: Book) {
    if ((e.target as Element).closest('[data-own]')) return;
    const own = (e.target as Element).closest('.dck');
    if (own || picking || e.ctrlKey || e.metaKey || e.shiftKey) lib.toggle(b.id);
    else void show(b, e.currentTarget as HTMLElement);
  }

  function cardKey(e: KeyboardEvent, b: Book) {
    if (e.key === 'Enter') void show(b, e.currentTarget as HTMLElement);
    else if (e.key === ' ') {
      e.preventDefault();
      lib.toggle(b.id);
    }
  }

  function remove() {
    const ids = [...lib.sel];
    const cards = ids.map((id) => plib?.querySelector<HTMLElement>(`.dc[data-id="${id}"]`)).filter((c) => !!c);
    if (still() || !cards.length) return void lib.remove(ids);
    const anims = cards.map((c) =>
      c.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(.94)' }], { duration: 220, easing: EO, fill: 'forwards' }),
    );
    void Promise.all(anims.map((a) => a.finished)).then(async () => {
      await lib.remove(ids);
      anims.forEach((a) => a.cancel());
    });
  }

  function key(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      if (pop) pop = null;
      else if (picking) lib.clear();
      else if (openId) void hide();
      return;
    }
    const t = e.target as Element;
    if (e.key === ' ' && !t.closest('button,input,[role=button],[role=slider],[tabindex]') && lib.cur) {
      e.preventDefault();
      lib.play();
    }
  }

  function outside(e: PointerEvent) {
    if (pop && !(e.target as Element).closest('.pop,[data-pop]')) pop = null;
  }

  const toggle = (p: 'spd' | 'aa') => (pop = pop === p ? null : p);
</script>

<svelte:window onkeydown={key} onpointerdown={outside} />

<div class="pc" class:psel={picking} class:bare={open && !closing}>
  <Silhouette id="pl" pc />
  {#if !open || closing}
    <header class="ptop">
      <span class="mono">{books(lib.books.length)}</span>
      {#if picking}
        <div class="pacts">
          <span class="mono">Выбрано: {lib.sel.length}</span>
          <button class="pbtn" onclick={() => lib.download('m4b')}>Скачать m4b</button>
          {#if lib.picked.some((b) => b.has_epub)}
            <button class="pbtn" onclick={() => lib.download('epub')}>Скачать EPUB</button>
          {/if}
          <button class="pbtn" onclick={() => lib.mark(true)}>Прослушано</button>
          <button class="pbtn" onclick={() => lib.mark(false)}>Не прослушано</button>
          <button class="pbtn" onclick={remove}>Удалить</button>
          <button class="pbtn solid" onclick={() => lib.clear()}>Отмена</button>
        </div>
      {:else}
        <label class="padd"
          ><svg viewBox="0 0 24 24"><path d="M12 4v16M4 12h16" /></svg>Добавить книгу<input
            type="file"
            multiple
            accept={lib.formats.join(',')}
            onchange={(e) => {
              void lib.upload(e.currentTarget.files);
              e.currentTarget.value = '';
            }}
          /></label
        >
      {/if}
    </header>
  {/if}

  <main class="pmain" bind:this={main}>
    <section class="plib" bind:this={plib}>
      {#if !lib.ready}
        <div class="phero note">
          <div class="hl">
            <span class="mono">Первый запуск</span>
            <Title text="Готовлю модели" max={narrow.current ? 104 : 132} min={56} lines={2} />
            {#if lib.status?.models.total}
              <p class="mono">{gb(lib.status.models.done ?? 0)} из {gb(lib.status.models.total)} ГБ</p>
              <div class="bar"><i style:--p={(lib.status.models.done ?? 0) / lib.status.models.total}></i></div>
            {/if}
          </div>
        </div>
      {:else if lib.loaded && !lib.books.length}
        <div class="phero note">
          <div class="hl">
            <Title text="Библиотека пуста" max={narrow.current ? 104 : 132} min={56} lines={2} />
            <p class="mono">Добавьте PDF, DjVu или EPUB кнопкой «Добавить книгу»</p>
          </div>
        </div>
      {:else if going}
        {@const n = lib.chapters(going.id).length}
        {@const at = lib.at(going.id)}
        {@const len = lib.length(going)}
        <div class="phero" data-id={going.id}>
          <div class="hl">
            <span class="mono"
              >{at > 0 ? `Продолжить${n ? ` · глава ${lib.chapter(going.id) + 1} из ${n}` : ''}` : `Начать${n ? ` · ${n} глав` : ''}`}</span
            >
            <Title text={going.title} max={narrow.current ? 104 : 132} min={56} lines={2} />
            <div class="pmeta"><span class="mono">{going.author}</span></div>
            <div class="pmeta">
              <button class="pill mono" onclick={() => lib.play(going.id)}
                ><svg viewBox="0 0 10 10"><path d="M1 0v10l9-5z" /></svg>{at > 0 ? `Слушать с ${fmt(at)}` : 'Слушать'}</button
              >
              <div class="pprog">
                <div class="bar"><i style:--p={len ? at / len : 0}></i></div>
                <span class="mono">Осталось {fmt(Math.max(0, len - at))}</span>
              </div>
            </div>
          </div>
          <button class="cvb" data-own aria-label="Открыть книгу" onclick={(e) => show(going, e.currentTarget.parentElement)}
            ><Cover book={going} /></button
          >
        </div>
      {/if}

      {#if lib.books.length}
        <div class="pshh mono"><span>Все книги</span><span>{lib.books.length}</span></div>
        <div class="pgrid">
          {#each lib.books as b (b.id)}
            <div
              class="dc"
              class:ld={b.state === 'failed'}
              class:on={lib.sel.includes(b.id)}
              class:cur={lib.cur === b.id}
              data-id={b.id}
              tabindex="0"
              role="button"
              onclick={(e) => card(e, b)}
              onkeydown={(e) => cardKey(e, b)}
            >
              <div class="dcv">
                <Cover book={b} />
                <span class="dck"><svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10" /></svg></span>
                {#if b.state === 'running' || b.state === 'queued' || b.state === 'paused'}
                  <div class="dld">
                    <span class="mono">{stage(b)}</span>
                    <div class="bar"><i style:--p={b.percent / 100}></i></div>
                    {#if b.state === 'running' && left(b.eta)}<span class="mono">{left(b.eta)}</span>{/if}
                  </div>
                {/if}
              </div>
              <h3 class="disp">{b.title}</h3>
              <span class="mono">{b.author}</span>
              {#if b.state === 'failed'}
                <div class="tags"><span class="tag">Ошибка</span></div>
                {#if b.error}<span class="mono err">{b.error}</span>{/if}
                <button class="pill mono" data-own onclick={() => lib.retry(b.id)}>Повторить</button>
              {:else}
                <div class="tags">
                  {#if b.duration}<span class="tag">{fmt(b.duration)}</span>{/if}
                  {#if lib.listened(b.id)}<span class="tag">Прослушано</span>
                  {:else if b.state === 'done' && b.duration && lib.at(b.id) / b.duration >= 0.01}<span class="tag"
                      >{Math.round((lib.at(b.id) / b.duration) * 100)}%</span
                    >{/if}
                </div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </section>

    {#if open}
      {@const ch = lib.chapters(open.id)}
      {@const ci = lib.chapter(open.id)}
      <section class="pbook" bind:this={bookEl}>
        <Silhouette id="pb" pc />
        <aside class="pside">
          <button class="pback mono" onclick={hide}
            ><svg viewBox="0 0 24 24"><path d="M20 12H5M11 6l-6 6 6 6" /></svg>Библиотека</button
          >
          <div class="hero2">
            <div class="hl">
              <Title text={open.title} max={64} min={30} />
              <span class="mono">{open.author}</span>
            </div>
            <Cover book={open} />
          </div>
          {#if open.state === 'done'}
            <div class="dl">
              <a class="pill mono" href={url.m4b(open.id)} download>Скачать m4b</a>
              {#if open.has_epub}<a class="pill mono" href={url.epub(open.id)} download>Скачать EPUB</a>{/if}
            </div>
          {:else}
            <div class="wk">
              <span class="mono">{stage(open)}</span>
              <div class="bar"><i style:--p={open.percent / 100}></i></div>
              {#if open.state === 'running' && left(open.eta)}<span class="mono">{left(open.eta)}</span>{/if}
            </div>
          {/if}
          <div class="chh mono"><span>Главы</span><span>{ch.length || ''}</span></div>
          <div class="chl pchl">
            {#each ch as c, i (i)}
              {#if open.state === 'done'}
                <button aria-current={i === ci} onclick={() => lib.seek(open.id, c.start)}
                  ><span class="mono">{String(i + 1).padStart(2, '0')}</span><span>{c.title}</span><span class="mono"
                    >{fmtShort(c.end - c.start)}</span
                  ></button
                >
              {:else}
                <p class="toc"><span class="mono">{String(i + 1).padStart(2, '0')}</span><span>{c.title}</span></p>
              {/if}
            {:else}
              <p class="mono none">Оглавление появится, когда прочитаю файл</p>
            {/each}
          </div>
        </aside>
        <div class="pread" data-s={lib.scheme}>
          {#if open.state === 'done'}
            <div class="prh">
              <button class="gear mono" data-pop aria-label="Вид текста" aria-expanded={pop === 'aa'} onclick={() => toggle('aa')}>Aa</button>
            </div>
            <Reader id={open.id} wide />
          {/if}
        </div>
      </section>
    {/if}
  </main>

  {#if now}
    {@const at = lib.at(now.id)}
    {@const len = lib.length(now)}
    <footer class="pbar">
      <button class="pnow" aria-label="Открыть книгу" onclick={() => openId !== now.id && show(now, plib?.querySelector(`[data-id="${now.id}"]`))}
        ><Cover book={now} />
        <div><b class="disp">{now.title}</b><span class="mono">{now.author}</span></div></button
      >
      <div class="pmid">
        <div class="pctl">
          <button class="sk mono" aria-label="Назад на 15 секунд" onclick={() => lib.skip(now.id, -15)}>−15</button>
          <button class="play" class:on={lib.playing} aria-label={lib.playing ? 'Пауза' : 'Слушать'} onclick={() => lib.play(now.id)}
            >{#if lib.playing}<svg viewBox="0 0 24 24"><path d="M6.5 4.5h4v15h-4zM13.5 4.5h4v15h-4z" /></svg>{:else}<svg viewBox="0 0 24 24"
                ><path d="M7 4.5v15l12.5-7.5z" /></svg
              >{/if}</button
          >
          <button class="sk mono" aria-label="Вперёд на 30 секунд" onclick={() => lib.skip(now.id, 30)}>+30</button>
        </div>
        <div class="pline">
          <span class="mono">{fmt(at)}</span>
          <Scrub id={now.id} />
          <span class="mono">−{fmt(Math.max(0, len - at))}</span>
        </div>
      </div>
      <div class="pright">
        <button class="gear mono" data-pop aria-label="Скорость" aria-expanded={pop === 'spd'} onclick={() => toggle('spd')}
          >{fmtSpeed(lib.speed)}</button
        >
      </div>
    </footer>
  {/if}

  <div class="pop spd" class:off={pop !== 'spd'} role="dialog" aria-label="Скорость" inert={pop !== 'spd'}>
    <h4 class="disp">Скорость</h4>
    <Seg label="Скорость" options={SPEEDS.map(fmtSpeed)} value={SPEEDS.indexOf(lib.speed)} onpick={(i) => lib.setSpeed(SPEEDS[i])} />
  </div>
  <div class="pop aa" class:off={pop !== 'aa'} role="dialog" aria-label="Вид текста" inert={pop !== 'aa'}>
    <h4 class="disp">Вид</h4>
    <div class="grp">
      <span class="mono">Отображение</span>
      <Seg label="Отображение" options={['Прокрутка', 'Разворот']} value={lib.mode} onpick={(i) => (lib.mode = i)} />
    </div>
    <div class="grp">
      <span class="mono">Цветовая схема</span>
      <Seg label="Цветовая схема" options={['Основная', 'Светлая', 'Сепия', 'Тёмная']} value={lib.scheme} onpick={(i) => (lib.scheme = i)} />
    </div>
    <div class="grp">
      <span class="mono">Масштаб</span>
      <div class="zoom">
        <button aria-label="Меньше" disabled={lib.zoom <= 80} onclick={() => (lib.zoom -= 10)}>A−</button><span class="mono">{lib.zoom}%</span
        ><button aria-label="Больше" disabled={lib.zoom >= 150} onclick={() => (lib.zoom += 10)}>A+</button>
      </div>
    </div>
  </div>
  <Toast />
</div>

<style>
  .bare {
    grid-template-rows: minmax(0, 1fr) auto;
  }
  .prh {
    justify-content: flex-end;
  }
  .wk {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 24px;
  }
  .dl {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 22px;
  }
  .dl a:active {
    transform: scale(0.96);
  }
  .dl a + a {
    background: none;
    color: inherit;
    box-shadow: inset 0 0 0 1.2px currentColor;
  }
  .toc {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr);
    gap: 10px;
    align-items: baseline;
    margin: 0;
    padding: 13px 0;
    border-bottom: 1px solid color-mix(in srgb, var(--white) 28%, transparent);
  }
  .toc span:nth-child(2) {
    font:
      600 20px/1.02 var(--display);
    font-stretch: 32%;
    text-transform: uppercase;
    letter-spacing: 0.005em;
  }
  .none {
    margin: 0;
    padding: 13px 0;
  }
</style>
