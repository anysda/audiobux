import type { Book } from './api';

export const fmt = (s: number) => {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600),
    m = Math.floor((s % 3600) / 60),
    x = s % 60;
  return `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}`;
};

// a chapter's length without the hours when there are none
export const fmtShort = (s: number) => fmt(s).replace(/^0:/, '');

export const plural = (n: number, one: string, few: string, many: string) => {
  const m = n % 10,
    h = n % 100;
  return m === 1 && h !== 11 ? one : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? few : many;
};

export const books = (n: number) => `${n} ${plural(n, 'книга', 'книги', 'книг')}`;

export const fmtSpeed = (v: number) => String(v).replace('.', ',') + '×';

export const gb = (bytes: number) => (bytes / 1e9).toFixed(1).replace('.', ',');

export const letter = (title: string) => (title.trim()[0] ?? '·').toUpperCase();

const STEPS: Record<string, string> = {
  extract: 'Чтение файла',
  ocr: 'Распознавание текста',
  figures: 'Описание рисунков',
  fix: 'Правка текста',
  epub: 'Сборка EPUB',
  normalize: 'Подготовка текста к чтению',
  chunk: 'Нарезка на фрагменты',
  tts: 'Озвучка',
  verify: 'Проверка на слух',
  assemble: 'Сборка аудиокниги',
  sync: 'Связка текста и звука',
};

// what the worker is busy with, in words; a book short of done never reads 100%
export const stage = (b: Book) =>
  b.state === 'queued'
    ? 'В очереди'
    : b.state === 'paused'
      ? 'На паузе'
      : `${(b.step && STEPS[b.step]) || 'В работе'} · ${Math.min(99, Math.floor(b.percent))}%`;

// a rough time to wait: minutes under an hour, five-minute steps under ten hours, then whole hours
export const left = (eta: number | null | undefined) => {
  if (eta == null || !Number.isFinite(eta) || eta < 0) return '';
  if (eta < 60) return 'Осталось меньше минуты';
  const m = eta < 3600 ? Math.max(1, Math.round(eta / 60)) : eta < 36000 ? Math.round(eta / 300) * 5 : Math.round(eta / 3600) * 60;
  const h = Math.floor(m / 60),
    r = m % 60;
  return `Осталось ~${h ? `${h} ч` : ''}${h && r ? ' ' : ''}${r ? `${r} мин` : ''}`;
};
