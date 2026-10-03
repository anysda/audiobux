// The parts of docs/openapi.json the interface reads, written out by hand: the API is small and changes with it.

export type State = 'queued' | 'running' | 'paused' | 'done' | 'failed';

export interface Book {
  id: string;
  title: string;
  author: string;
  format: string;
  state: State;
  step: string | null;
  pieces_done: number;
  pieces_total: number;
  percent: number;
  // seconds until the book is ready, when the worker can tell
  eta?: number | null;
  error: string | null;
  added: number;
  updated: number;
  duration: number | null;
  has_epub: boolean;
  has_cover: boolean;
}

export interface Chapter {
  title: string;
  start: number;
}

export interface BookDetails extends Book {
  chapters: Chapter[];
}

export interface Listening {
  id: string;
  position: number;
  listened: boolean;
}

export interface Status {
  models: { ready: boolean; done: number | null; total: number | null };
  queue: number;
  formats: { name: string; extensions: string[] }[];
}

export interface Submitted {
  id: string;
  known: boolean;
}

// sync.json: the text of the book and when each piece of it is heard; null times are not heard
export type Block =
  | { type: 'h'; text: string; t0: number | null; t1: number | null }
  | { type: 'p'; sentences: { text: string; t0: number | null; t1: number | null }[] }
  | { type: 'code'; text: string; t0: number | null; t1: number | null }
  | { type: 'figure'; src: string | null; alt: string; t0: number | null; t1: number | null };

export interface Sync {
  version: 1;
  duration: number;
  chapters: { title: string; t0: number; t1: number; blocks: Block[] }[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

async function call<T>(method: string, path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { method, ...init });
  } catch {
    throw new ApiError(0, 'offline');
  }
  if (!res.ok) {
    let code = '';
    try {
      code = String((await res.json()).detail ?? '');
    } catch {
      // the body is not ours: a proxy's page or nothing
    }
    throw new ApiError(res.status, code);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

// a file the book has not got (an old book without sync.json): nothing, quietly
async function orNothing<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

const json = (body: unknown): RequestInit => ({
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body),
});

export const url = {
  audio: (id: string) => `/api/books/${id}/audio`,
  cover: (id: string, v: number) => `/api/books/${id}/cover?v=${v}`,
  m4b: (id: string) => `/api/books/${id}/m4b`,
  epub: (id: string) => `/api/books/${id}/epub`,
  // a figure's src in sync.json is `figures/<name>.jpg`, relative to the book
  figure: (id: string, src: string) => `/api/books/${id}/${src}`,
};

export const api = {
  books: () => call<Book[]>('GET', '/api/books'),
  book: (id: string) => call<BookDetails>('GET', `/api/books/${id}`),
  status: () => call<Status>('GET', '/api/status'),
  listening: () => call<Listening[]>('GET', '/api/listening'),
  sync: (id: string) => orNothing(call<Sync>('GET', `/api/books/${id}/sync`)),
  upload: (file: File) =>
    call<Submitted>('POST', `/api/books?name=${encodeURIComponent(file.name)}`, {
      headers: { 'content-type': 'application/octet-stream' },
      body: file,
    }),
  remove: (id: string) => call<void>('DELETE', `/api/books/${id}`),
  retry: (id: string) => call<Book>('POST', `/api/books/${id}/retry`),
  listened: (id: string, yes: boolean) => call<Listening>(yes ? 'PUT' : 'DELETE', `/api/books/${id}/listened`),
  position: (id: string, seconds: number, keepalive = false) =>
    call<Listening>('PUT', `/api/books/${id}/position`, { ...json({ seconds }), keepalive }),
};
