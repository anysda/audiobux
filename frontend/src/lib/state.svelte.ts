import { api, ApiError, url, type Book, type BookDetails, type Listening, type Status, type Sync } from './api';

const SAVE_EVERY = 5000;
// the events stream: asked again this long after it is refused; trusted no longer than its pings (15 s)
const AGAIN = 5000;
const QUIET = 20000;
// what an upload refused is told by
const REFUSED: Record<number, string> = {
  413: 'Файл слишком большой',
  415: 'Это не книга: нужен PDF, DjVu или EPUB',
  429: 'Очередь полна: дождитесь, пока озвучатся книги в ней',
  507: 'На сервере мало места, книга не принята',
};

class Library {
  books = $state<Book[]>([]);
  marks = $state<Record<string, Listening>>({});
  status = $state<Status | null>(null);
  loaded = $state(false);
  details = $state<Record<string, BookDetails>>({});
  syncs = $state<Record<string, Sync | null>>({});
  toast = $state({ text: '', n: 0 });
  sel = $state<string[]>([]);
  // whether the service asks for the sign-in: then the screen offers to sign out
  login = $state(false);

  // the player: one book at a time, one <audio> for both layouts
  cur = $state<string | null>(null);
  pos = $state(0);
  dur = $state(0);
  playing = $state(false);
  speed = $state(1);

  // the reader's look, for this visit only
  mode = $state(0);
  scheme = $state(0);
  zoom = $state(100);

  #audio: HTMLAudioElement | null = null;
  #saved = 0;
  #frame = 0;
  #poll = 0;
  #es: EventSource | null = null;
  #again = 0;
  #hidden = 0;

  get done() {
    return this.books.filter((b) => b.state === 'done');
  }

  get formats() {
    return (this.status?.formats ?? []).flatMap((f) => f.extensions);
  }

  get ready() {
    return this.status?.models.ready ?? true;
  }

  book(id: string | null) {
    return this.books.find((b) => b.id === id);
  }

  // where the listener is in a book, live for the one playing
  at(id: string) {
    return id === this.cur ? this.pos : (this.marks[id]?.position ?? 0);
  }

  length(b: Book) {
    return b.id === this.cur && this.dur ? this.dur : (b.duration ?? 0);
  }

  listened(id: string) {
    return this.marks[id]?.listened ?? false;
  }

  chapters(id: string) {
    const b = this.book(id);
    const ch = this.details[id]?.chapters ?? [];
    return ch.map((c, i) => ({ ...c, end: ch[i + 1]?.start ?? b?.duration ?? c.start }));
  }

  chapter(id: string) {
    const t = this.at(id);
    const ch = this.details[id]?.chapters ?? [];
    let k = 0;
    ch.forEach((c, i) => {
      if (c.start <= t + 0.5) k = i;
    });
    return k;
  }

  // the book to go on with: the one in the player, else the latest touched with a position
  get going() {
    const cur = this.book(this.cur);
    if (cur?.state === 'done' && this.at(cur.id) > 0) return cur;
    return [...this.done].sort((a, b) => b.updated - a.updated).find((b) => this.at(b.id) > 0);
  }

  say(text: string) {
    this.toast = { text, n: this.toast.n + 1 };
  }

  start() {
    this.#wake();
    api.auth().then(
      (a) => (this.login = a.required),
      () => {},
    );
    addEventListener('pagehide', () => this.#save(true));
    // a phone puts the page away and gives it back as it was: what moved meanwhile is asked for again
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') this.#hidden = Date.now();
      // a stream quiet for longer than its pings may be dead with nothing said
      else this.#wake(Date.now() - this.#hidden > QUIET);
    });
    addEventListener('pageshow', (e) => e.persisted && this.#wake(true));
  }

  #wake(anew = false) {
    void this.refresh();
    void this.loadStatus();
    if (anew || !this.#es || this.#es.readyState === EventSource.CLOSED) this.#listen();
  }

  #listen() {
    clearTimeout(this.#again);
    this.#es?.close();
    const es = (this.#es = new EventSource('/api/events'));
    es.onopen = () => void this.refresh();
    // the browser comes back by itself after a cut, but not after an answer that is no
    // stream (the worker restarting, the sign-in page): then it is asked again here
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED && this.#es === es) this.#again = setTimeout(() => this.#listen(), AGAIN);
    };
    es.addEventListener('book', (e) => this.#upsert(JSON.parse((e as MessageEvent).data).book));
    es.addEventListener('deleted', (e) => this.#drop(JSON.parse((e as MessageEvent).data).id));
  }

  async refresh() {
    try {
      const [books, marks] = await Promise.all([api.books(), api.listening()]);
      this.books = books;
      this.marks = Object.fromEntries(marks.map((m) => [m.id, m]));
    } catch {
      // the events stream reconnects and refreshes; the screen keeps what it has
    } finally {
      this.loaded = true;
    }
  }

  async loadStatus() {
    clearTimeout(this.#poll);
    try {
      this.status = await api.status();
    } catch {
      // asked again below
    }
    if (!this.ready || !this.status) this.#poll = setTimeout(() => void this.loadStatus(), 5000);
  }

  async loadDetails(id: string, again = false) {
    if (this.details[id] && !again) return;
    try {
      this.details[id] = await api.book(id);
    } catch {
      // chapters stay empty
    }
  }

  async loadSync(id: string) {
    if (id in this.syncs) return;
    try {
      this.syncs[id] = await api.sync(id);
    } catch {
      this.syncs[id] = null;
    }
  }

  #upsert(b: Book) {
    const i = this.books.findIndex((x) => x.id === b.id);
    const was = this.books[i];
    if (i < 0) this.books.unshift(b);
    else this.books[i] = b;
    // the chapters grow with the book's steps: the ones on screen are asked for again as it moves on
    if (this.details[b.id] && (was?.step !== b.step || was?.state !== b.state)) void this.loadDetails(b.id, true);
  }

  #drop(id: string) {
    this.books = this.books.filter((b) => b.id !== id);
    this.sel = this.sel.filter((x) => x !== id);
    if (this.cur === id) this.#unload();
  }

  async upload(files: FileList | null) {
    for (const f of files ?? []) {
      try {
        const r = await api.upload(f);
        this.say(r.known ? 'Эта книга уже есть' : 'Добавлено в очередь');
      } catch (e) {
        const s = e instanceof ApiError ? e.status : 0;
        this.say(REFUSED[s] ?? 'Не удалось загрузить');
      }
    }
    void this.refresh();
  }

  async logout() {
    try {
      await api.logout();
    } catch {
      // a session already gone is signed out as well
    }
    location.assign('/login');
  }

  // selection
  toggle(id: string) {
    this.sel = this.sel.includes(id) ? this.sel.filter((x) => x !== id) : [...this.sel, id];
  }

  clear() {
    this.sel = [];
  }

  get picked() {
    return this.books.filter((b) => this.sel.includes(b.id));
  }

  download(kind: 'm4b' | 'epub') {
    const ok = this.picked.filter((b) => b.state === 'done' && (kind === 'm4b' || b.has_epub));
    for (const b of ok) {
      const a = document.createElement('a');
      a.href = url[kind](b.id);
      a.download = '';
      a.click();
    }
    this.say(`Скачивание ${kind === 'm4b' ? 'm4b' : 'EPUB'}: ${ok.length}`);
    this.clear();
  }

  async mark(yes: boolean) {
    const ok = this.picked.filter((b) => b.state === 'done');
    let n = 0;
    for (const b of ok) {
      try {
        this.marks[b.id] = await api.listened(b.id, yes);
        n++;
      } catch {
        // counted below
      }
    }
    this.say(n < ok.length ? 'Не удалось отметить' : yes ? `Прослушано: ${n}` : `Снята отметка: ${n}`);
    this.clear();
  }

  // the screen folds the rows away first, then the books go
  async remove(ids: string[]) {
    let n = 0;
    for (const id of ids) {
      try {
        await api.remove(id);
        this.#drop(id);
        n++;
      } catch {
        // counted below
      }
    }
    this.say(n < ids.length ? 'Не удалось удалить' : `Удалено: ${n}`);
    this.clear();
  }

  async retry(id: string) {
    try {
      this.#upsert(await api.retry(id));
    } catch {
      this.say('Не удалось повторить');
    }
  }

  // player
  #el() {
    if (!this.#audio) {
      const a = (this.#audio = new Audio());
      a.preload = 'metadata';
      a.onplay = () => {
        this.playing = true;
        this.#tick();
      };
      a.onpause = () => {
        this.playing = false;
        this.pos = a.currentTime;
        this.#save();
      };
      a.onended = a.onpause;
      a.ontimeupdate = () => {
        if (Date.now() - this.#saved > SAVE_EVERY) this.#save();
      };
      a.ondurationchange = () => {
        if (Number.isFinite(a.duration)) this.dur = a.duration;
      };
      a.onerror = () => {
        if (a.src) this.say('Не удалось включить звук');
      };
    }
    return this.#audio;
  }

  #tick() {
    cancelAnimationFrame(this.#frame);
    const step = () => {
      if (!this.#audio || this.#audio.paused) return;
      this.pos = this.#audio.currentTime;
      this.#frame = requestAnimationFrame(step);
    };
    step();
  }

  #save(keepalive = false) {
    const id = this.cur;
    if (!id) return;
    this.#saved = Date.now();
    const seconds = Math.max(0, this.pos);
    this.marks[id] = { id, listened: this.listened(id), position: seconds };
    api.position(id, seconds, keepalive).catch(() => {
      // the next save carries the position again
    });
  }

  #unload() {
    const a = this.#audio;
    if (a) {
      a.pause();
      a.removeAttribute('src');
      a.load();
    }
    this.cur = null;
    this.pos = 0;
    this.dur = 0;
  }

  // the player takes a book without playing it, at the place the listener left it
  load(id: string) {
    if (this.cur === id) return;
    const b = this.book(id);
    if (!b || b.state !== 'done') return;
    if (this.playing) this.#el().pause();
    const a = this.#el();
    const from = this.marks[id]?.position ?? 0;
    this.cur = id;
    this.pos = from;
    this.dur = b.duration ?? 0;
    a.src = url.audio(id);
    a.playbackRate = this.speed;
    a.onloadedmetadata = () => {
      a.currentTime = from;
    };
    void this.loadDetails(id);
  }

  play(id = this.cur) {
    if (!id) return;
    this.load(id);
    const a = this.#el();
    if (a.paused) {
      a.playbackRate = this.speed;
      a.play().catch(() => this.say('Не удалось включить звук'));
    } else a.pause();
  }

  // `keep`: the new place is kept at once, playing or not
  seek(id: string, t: number, keep = false) {
    this.load(id);
    const b = this.book(id);
    const end = this.length(b!) || t;
    this.pos = Math.min(Math.max(0, t), end);
    const a = this.#el();
    if (a.readyState > 0) a.currentTime = this.pos;
    else
      a.onloadedmetadata = () => {
        a.currentTime = this.pos;
      };
    if (keep || a.paused) this.#save();
  }

  skip(id: string, by: number) {
    this.seek(id, this.at(id) + by);
  }

  setSpeed(v: number) {
    this.speed = v;
    if (this.#audio) this.#audio.playbackRate = v;
  }
}

export const lib = new Library();
