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
