<script lang="ts">
  import { tick } from 'svelte';
  import { lib } from './lib/state.svelte';
  import type { Book } from './lib/api';
  import { books, fmt, fmtShort, fmtSpeed, gb, left, stage } from './lib/text';
  import { ED, EIO, EO, inset, reveal, rise, still } from './lib/motion';
  import { press } from './lib/press';
  import Cover from './lib/Cover.svelte';
  import Title from './lib/Title.svelte';
  import Seg from './lib/Seg.svelte';
  import Scrub from './lib/Scrub.svelte';
  import Reader from './lib/Reader.svelte';
  import Toast from './lib/Toast.svelte';

  const SPEEDS = [0.75, 1, 1.25, 1.5, 2];

  let phone = $state<HTMLElement>();
  let libEl = $state<HTMLElement>();
  let list = $state<HTMLElement>();
  let heroCover = $state<HTMLElement>();
  let menu = $state(false);

  let openId = $state<string | null>(null);
  let src: HTMLElement | null = null;
  let bookEl = $state<HTMLElement>();
  let pl = $state<HTMLElement>();
  let chl = $state<HTMLElement>();
  let cover2 = $state<HTMLElement>();
  let tab = $state(0);
  let sheet = $state(false);
  let sheetIn = $state(false);
  let sheetEl = $state<HTMLElement>();

  const picking = $derived(lib.sel.length > 0);
  const going = $derived(lib.going);
  const voicing = $derived(lib.books.filter((b) => b.state !== 'done'));
  const rest = $derived(lib.done.filter((b) => b !== going));
  const open = $derived(lib.book(openId));

  $effect(() => {
    if (going) void lib.loadDetails(going.id);
  });
  $effect(() => {
    if (!picking) menu = false;
  });
  $effect(() => {
    // a book that went away takes its screen with it
    if (openId && !open) openId = null;
  });

  const hold = (b: Book) => ({
    open: () => b.state !== 'failed' && void show(b),
    toggle: () => lib.toggle(b.id),
    picking: () => picking,
  });

  async function show(b: Book) {
    src = list?.querySelector<HTMLElement>(`[data-id="${b.id}"]`) ?? null;
    lib.load(b.id);
    void lib.loadDetails(b.id);
    openId = b.id;
    tab = 0;
    await tick();
    const c = chl?.querySelector<HTMLElement>('[aria-current="true"]');
    if (chl) chl.scrollTop = c ? c.offsetTop - 12 : 0;
    if (still() || !bookEl || !phone || !libEl) return;
    const p = phone.getBoundingClientRect();
    if (src) bookEl.animate([{ clipPath: inset(src.getBoundingClientRect(), p) }, { clipPath: 'inset(0px)' }], { duration: 560, easing: ED });
    libEl.animate([{ transform: 'none', opacity: 1 }, { transform: 'scale(.94)', opacity: 0.4 }], { duration: 560, easing: ED, fill: 'forwards' });
    const sc = src?.querySelector('.cv');
    if (sc && cover2) {
      const a = sc.getBoundingClientRect(),
        z = cover2.getBoundingClientRect();
      cover2.animate(
        [
          { transform: `translate(${a.left - z.left}px,${a.top - z.top}px) scale(${a.width / z.width})`, transformOrigin: '0 0' },
          { transform: 'none', transformOrigin: '0 0' },
        ],
        { duration: 620, easing: ED },
      );
    }
    reveal(bookEl.querySelector('.hero2 h2'), 160, 55);
    rise([...bookEl.querySelectorAll('.hero2 .mono,.wk,.time,.scrub,.ctl,.chh,.chl')], 260, 60, 20);
  }

  function hide() {
    if (lib.playing) lib.play();
    sheet = sheetIn = false;
    const done = () => (openId = null);
    libEl?.getAnimations().forEach((a) => a.cancel());
    if (still() || !src?.isConnected || !bookEl || !phone || !libEl) return done();
    const p = phone.getBoundingClientRect();
    libEl.animate([{ transform: 'scale(.94)', opacity: 0.4 }, { transform: 'none', opacity: 1 }], { duration: 380, easing: EO });
    bookEl.animate([{ clipPath: 'inset(0px)' }, { clipPath: inset(src.getBoundingClientRect(), p) }], { duration: 380, easing: EO }).onfinish =
      done;
  }

  async function setTab(i: number) {
    tab = i;
    await tick();
    if (i === 0 && pl) rise([...pl.children].slice(0, 4), 0, 50, 16);
    if (i === 1) rise([...(bookEl?.querySelectorAll('article > section:first-child > *') ?? [])].slice(0, 4), 40, 50, 16);
  }

  // the rows fold away before the books go
  function remove() {
    const ids = [...lib.sel];
    menu = false;
    const rows = ids.map((id) => list?.querySelector<HTMLElement>(`[data-id="${id}"]`)).filter((r) => !!r);
    if (still() || !rows.length) return void lib.remove(ids);
    const anims = rows.map((r) =>
      r.animate(
        [
          { height: r.offsetHeight + 'px', opacity: 1 },
          { height: '0px', opacity: 0, paddingTop: '0px', paddingBottom: '0px', borderBottomWidth: '0px' },
        ],
        { duration: 300, easing: EIO, fill: 'forwards' },
      ),
    );
    void Promise.all(anims.map((a) => a.finished)).then(async () => {
      await lib.remove(ids);
      anims.forEach((a) => a.cancel());
    });
  }

  function openSheet() {
    sheet = true;
    requestAnimationFrame(() => requestAnimationFrame(() => (sheetIn = true)));
  }
  function closeSheet() {
    sheetIn = false;
    if (sheetEl) {
      sheetEl.style.transform = '';
      sheetEl.style.transition = '';
    }
    setTimeout(() => !sheetIn && (sheet = false), still() ? 220 : 420);
  }

  // the sheet follows the finger down and goes on a flick
  let y0: number | null = null,
    t0 = 0,
    dy = 0;
  function grab(e: PointerEvent) {
    if ((e.target as Element).closest('button') || !sheetEl) return;
    sheetEl.setPointerCapture(e.pointerId);
    y0 = e.clientY;
    t0 = performance.now();
    sheetEl.style.transition = 'none';
  }
  function drag(e: PointerEvent) {
    if (y0 === null || !sheetEl) return;
    let d = e.clientY - y0;
    if (d < 0) d = -Math.sqrt(-d) * 2;
    dy = d;
    sheetEl.style.transform = `translateY(${d}px)`;
  }
  function drop() {
    if (y0 === null || !sheetEl) return;
    const v = dy / (performance.now() - t0);
    y0 = null;
    sheetEl.style.transition = '';
    if (dy > sheetEl.offsetHeight * 0.3 || v > 0.11) closeSheet();
    else sheetEl.style.transform = '';
    dy = 0;
  }

  // pulled down from the top, the list is asked for again
  const PULL = 56;
  let py: number | null = null;
  let pull = $state(0);
  let held = $state(false);
  let asking = $state(false);
  function pullFrom(e: TouchEvent) {
    py = libEl && libEl.scrollTop <= 0 && !asking && e.touches.length === 1 ? e.touches[0].clientY : null;
  }
  function pullBy(e: TouchEvent) {
    if (py === null || !libEl) return;
    const d = e.touches[0].clientY - py;
    if (d <= 0 || libEl.scrollTop > 0) {
      py = null;
      held = false;
      pull = 0;
      return;
    }
    held = true;
    pull = Math.min(PULL * 1.5, d / 2);
  }
  $effect(() => {
    const el = libEl;
    if (!el) return;
    const on: [string, (e: TouchEvent) => void][] = [
      ['touchstart', pullFrom],
      ['touchmove', pullBy],
      ['touchend', () => void pullDone()],
      ['touchcancel', () => void pullDone()],
    ];
    for (const [t, f] of on) el.addEventListener(t, f as EventListener, { passive: true });
    return () => on.forEach(([t, f]) => el.removeEventListener(t, f as EventListener));
  });
  async function pullDone() {
    if (py === null) return;
    py = null;
    held = false;
    if (pull < PULL) return void (pull = 0);
    asking = true;
    pull = PULL;
    await Promise.all([lib.refresh(), lib.loadStatus()]);
    asking = false;
    pull = 0;
  }

  function key(e: KeyboardEvent) {
    if (e.key !== 'Escape') return;
    if (sheet) closeSheet();
    else if (menu) menu = false;
  }

  function addClick(e: MouseEvent) {
    if (!picking) return;
    e.preventDefault();
    lib.clear();
  }

</script>

<svelte:window onkeydown={key} />

<div class="ph" class:sel={picking} bind:this={phone}>
  <section class="lib" bind:this={libEl}>
    <header class="lh">
      <div class="lead">
        {#if lib.login}<button class="mono out" onclick={() => lib.logout()}>Выйти</button>{/if}
        <p class="mono">{books(lib.books.length)}</p>
      </div>
    </header>
    <div class="pull mono" class:held class:go={pull >= PULL} style:height="{pull}px" aria-hidden="true">
      <span>{asking ? 'Обновление' : pull >= PULL ? 'Отпустите' : 'Обновить'}</span>
    </div>
    <div bind:this={list}>
      {#if !lib.ready}
        <div class="hero note">
          <div class="hm mono"><span>Первый запуск</span></div>
          <Title
            class="h-t"
            text={lib.status?.models.failed ? 'Модели не скачались' : 'Подготовка моделей'}
            max={60}
            min={32}
          />
          {#if lib.status?.models.total}
            <p class="mono">{gb(lib.status.models.done ?? 0)} из {gb(lib.status.models.total)} ГБ</p>
            <div class="bar"><i style:--p={(lib.status.models.done ?? 0) / lib.status.models.total}></i></div>
          {/if}
          {#if lib.status?.models.failed}
            <p class="mono">Загрузка прервалась. Запустите ./install.sh снова, она продолжится</p>
          {/if}
        </div>
      {:else if lib.loaded && !lib.books.length}
        <div class="hero note">
          <Title class="h-t" text="Библиотека пуста" max={60} min={32} />
          <p class="mono">Добавьте PDF, DjVu или EPUB кнопкой «+»</p>
        </div>
      {/if}

      {#if going}
        {@const c = lib.chapter(going.id)}
        {@const n = lib.chapters(going.id).length}
        <div class="bk hero" class:on={lib.sel.includes(going.id)} data-id={going.id} tabindex="0" role="button" use:press={hold(going)}>
          <div class="hm mono"><span>Продолжить</span>{#if n}<span>Глава {c + 1} / {n}</span>{/if}</div>
          <div class="tr">
            <div class="hl">
              <Title class="h-t" text={going.title} max={60} min={26} height={() => heroCover?.offsetHeight} />
              <div class="hb">
                <span class="mono">{going.author}</span>
                <span class="pill mono"
                  ><svg viewBox="0 0 10 10"><path d="M1 0v10l9-5z" /></svg>{fmt(lib.at(going.id))} из {fmt(lib.length(going))}</span
                >
              </div>
            </div>
            <Cover book={going} bind:el={heroCover} />
          </div>
          {#if lib.listened(going.id)}<div class="tags"><span class="tag">Прослушано</span></div>{/if}
          <div class="ck"><svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10" /></svg></div>
          <i class="hold"></i>
        </div>
      {/if}

      {#each voicing as b (b.id)}
        <div class="bk row ld" class:on={lib.sel.includes(b.id)} data-id={b.id} tabindex="0" role="button" use:press={hold(b)}>
          <Cover book={b} />
          <div class="m">
            <h3 class="disp">{b.title}</h3>
            <span class="mono">{b.author}</span>
            {#if b.state === 'failed'}
              <div class="tags"><span class="tag">Ошибка</span></div>
              {#if b.error}<span class="mono err">{b.error}</span>{/if}
              <button class="pill mono" data-own onclick={() => lib.retry(b.id)}>Повторить</button>
            {:else}
              <span class="mono">{stage(b)}</span>
              <div class="bar"><i style:--p={b.percent / 100}></i></div>
              {#if b.state === 'running' && left(b.eta)}<span class="mono">{left(b.eta)}</span>{/if}
            {/if}
          </div>
          <div class="ck"><svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10" /></svg></div>
          <i class="hold"></i>
        </div>
      {/each}

      {#each rest as b (b.id)}
        <div class="bk row" class:on={lib.sel.includes(b.id)} data-id={b.id} tabindex="0" role="button" use:press={hold(b)}>
          <Cover book={b} />
          <div class="m">
            <h3 class="disp">{b.title}</h3>
            <span class="mono">{b.author}</span>
            <div class="tags">
              {#if b.duration}<span class="tag">{fmt(b.duration)}</span>{/if}
              {#if lib.listened(b.id)}<span class="tag">Прослушано</span>{/if}
            </div>
          </div>
          <div class="ck"><svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10" /></svg></div>
          <i class="hold"></i>
        </div>
      {/each}
    </div>
  </section>

  <button class="dots" aria-label="Действия с выбранными" aria-expanded={menu} onclick={() => (menu = !menu)}
    ><svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="2" /><circle cx="12" cy="12" r="2" /><circle cx="19" cy="12" r="2" /></svg
    ></button
  >
  <div class="menu" class:off={!menu} role="menu" inert={!menu}>
    <button role="menuitem" onclick={remove}>Удалить <span>{lib.sel.length}</span></button>
    <button role="menuitem" onclick={() => lib.download('m4b')}>Скачать <span>m4b</span></button>
    {#if lib.picked.some((b) => b.has_epub)}
      <button role="menuitem" onclick={() => lib.download('epub')}>Скачать <span>EPUB</span></button>
    {/if}
    <button role="menuitem" onclick={() => lib.mark(true)}>Прослушано</button>
    <button role="menuitem" onclick={() => lib.mark(false)}>Не прослушано</button>
  </div>
  <label class="fab" aria-label={picking ? 'Снять выбор' : 'Добавить книгу'}
    ><svg viewBox="0 0 24 24"><path d="M12 4v16M4 12h16" /></svg><input
      type="file"
      multiple
      accept={lib.formats.join(',')}
      onclick={addClick}
      onchange={(e) => {
        void lib.upload(e.currentTarget.files);
        e.currentTarget.value = '';
        libEl?.scrollTo({ top: 0, behavior: still() ? 'auto' : 'smooth' });
      }}
    /></label
  >
  <Toast />

  {#if open}
    <section
      class="book"
      class:paper={lib.scheme > 0}
      class:playing={lib.playing && lib.cur === open.id}
      data-tab={tab}
      bind:this={bookEl}
    >
      <header class="bt">
        <button class="back mono" onclick={hide}>Назад</button>
        {#if open.state === 'done'}
          <div class="tabs" role="tablist">
            <i></i><button role="tab" aria-selected={tab === 0} onclick={() => setTab(0)}>Аудио</button><button
              role="tab"
              aria-selected={tab === 1}
              onclick={() => setTab(1)}>Книга</button
            >
          </div>
          <button class="gear mono" aria-label="Настройки" onclick={openSheet}>{tab === 0 ? fmtSpeed(lib.speed) : 'Aa'}</button>
        {/if}
      </header>

      {#if open.state !== 'done'}
        {@const ch = lib.chapters(open.id)}
        <div class="pane pl">
          <div class="hero2">
            <div class="hl">
              <Title text={open.title} max={60} min={26} height={() => cover2?.offsetHeight} />
              <span class="mono">{open.author}</span>
            </div>
            <Cover book={open} bind:el={cover2} />
          </div>
          <div class="wk">
            <span class="mono">{stage(open)}</span>
            <div class="bar"><i style:--p={open.percent / 100}></i></div>
            {#if open.state === 'running' && left(open.eta)}<span class="mono">{left(open.eta)}</span>{/if}
          </div>
          <div class="chh mono"><span>Главы</span><span>{ch.length || ''}</span></div>
          <div class="chl" bind:this={chl}>
            {#each ch as c, i (i)}
              <p class="toc"><span class="mono">{String(i + 1).padStart(2, '0')}</span><span>{c.title}</span></p>
            {:else}
              <p class="mono none">Оглавление появится после чтения файла</p>
            {/each}
          </div>
        </div>
      {:else if tab === 0}
        {@const at = lib.at(open.id)}
        {@const len = lib.length(open)}
        {@const ch = lib.chapters(open.id)}
        {@const ci = lib.chapter(open.id)}
        <div class="pane pl" role="tabpanel" bind:this={pl}>
          <div class="hero2">
            <div class="hl">
              <Title text={open.title} max={60} min={26} height={() => cover2?.offsetHeight} />
              <span class="mono">{open.author}</span>
            </div>
            <Cover book={open} bind:el={cover2} />
          </div>
          <div class="time"><span class="tnow disp">{fmt(at)}</span><span class="mono">−{fmt(len - at)}</span></div>
          <Scrub id={open.id} />
          <div class="ctl">
            <button class="sk mono" aria-label="Назад на 15 секунд" onclick={() => lib.skip(open.id, -15)}>−15</button>
            <button class="play" aria-label={lib.playing && lib.cur === open.id ? 'Пауза' : 'Слушать'} onclick={() => lib.play(open.id)}
              ><svg class="py" viewBox="0 0 24 24"><path d="M7 4.5v15l12.5-7.5z" /></svg><svg class="pz" viewBox="0 0 24 24"
                ><path d="M6.5 4.5h4v15h-4zM13.5 4.5h4v15h-4z" /></svg
              ></button
            >
            <button class="sk mono" aria-label="Вперёд на 30 секунд" onclick={() => lib.skip(open.id, 30)}>+30</button>
          </div>
          <div class="chh mono"><span>Главы</span><span>{ch.length || ''}</span></div>
          <div class="chl" bind:this={chl}>
            {#each ch as c, i (i)}
              <button aria-current={i === ci} onclick={() => lib.seek(open.id, c.start)}
                ><span class="mono">{String(i + 1).padStart(2, '0')}</span><span>{c.title}</span><span class="mono"
                  >{fmtShort(c.end - c.start)}</span
                ></button
              >
            {/each}
          </div>
        </div>
      {:else}
        <Reader id={open.id} />
      {/if}

      {#if sheet}
        <div class="ov" class:open={sheetIn}>
          <div class="scrim" role="presentation" onclick={closeSheet}></div>
          <div
            class="sheet"
            role="dialog"
            aria-label="Настройки"
            tabindex="-1"
            bind:this={sheetEl}
            onpointerdown={grab}
            onpointermove={drag}
            onpointerup={drop}
            onpointercancel={drop}
          >
            <span class="grab"></span>
            {#if tab === 0}
              <h4 class="disp">Скорость</h4>
              <div class="grp">
                <Seg label="Скорость" options={SPEEDS.map(fmtSpeed)} value={SPEEDS.indexOf(lib.speed)} onpick={(i) => lib.setSpeed(SPEEDS[i])} />
              </div>
            {:else}
              <h4 class="disp">Вид</h4>
              <div class="grp">
                <span class="mono">Отображение</span>
                <Seg label="Отображение" options={['Прокрутка', 'Страницы']} value={lib.mode} onpick={(i) => (lib.mode = i)} />
              </div>
              <div class="grp">
                <span class="mono">Цветовая схема</span>
                <Seg
                  label="Цветовая схема"
                  options={['Основная', 'Светлая', 'Сепия', 'Тёмная']}
                  value={lib.scheme}
                  onpick={(i) => (lib.scheme = i)}
                />
              </div>
              <div class="grp">
                <span class="mono">Масштаб</span>
                <div class="zoom">
                  <button aria-label="Меньше" disabled={lib.zoom <= 80} onclick={() => (lib.zoom -= 10)}>A−</button><span class="mono"
                    >{lib.zoom}%</span
                  ><button aria-label="Больше" disabled={lib.zoom >= 150} onclick={() => (lib.zoom += 10)}>A+</button>
                </div>
              </div>
            {/if}
          </div>
        </div>
      {/if}
    </section>
  {/if}
</div>

<style>
  .wk {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 26px;
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
