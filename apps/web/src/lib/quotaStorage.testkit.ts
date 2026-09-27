// Test-only: a Web Storage with a quota (ADR 0114). Imported by unit tests only — never by
// app code. Over N UTF-16 bytes, setItem throws the WebKit-shaped QuotaExceededError (the
// one that crashed the console on 2026-09-27), or whatever `makeError` builds.

export class QuotaStorage implements Storage {
  private map = new Map<string, string>();
  setCalls = 0;
  constructor(
    public quota: number,
    private makeError: () => unknown = () => new DOMException("The quota has been exceeded.", "QuotaExceededError"),
  ) {}
  get length() {
    return this.map.size;
  }
  key(i: number) {
    return [...this.map.keys()][i] ?? null;
  }
  getItem(k: string) {
    return this.map.has(k) ? (this.map.get(k) as string) : null;
  }
  used(): number {
    let n = 0;
    for (const [k, v] of this.map) n += 2 * (k.length + v.length);
    return n;
  }
  setItem(k: string, v: string) {
    this.setCalls++;
    const prev = this.map.get(k);
    const next = this.used() - (prev === undefined ? 0 : 2 * (k.length + prev.length)) + 2 * (k.length + v.length);
    if (next > this.quota) throw this.makeError();
    this.map.set(k, String(v));
  }
  removeItem(k: string) {
    this.map.delete(k);
  }
  clear() {
    this.map.clear();
  }
}
