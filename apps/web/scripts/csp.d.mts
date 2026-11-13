// Types for csp.mjs (a plain Node build helper), for vite.config.ts and the unit test.
export function previewScript(): string;
export function joinPreviewScript(idiomorph: string, receiver: string): string;
export function sha256Source(text: string): string;
export function inlineScriptHashes(html: string): string[];
export function cspDocument(): { directives: Record<string, string[]> };
export function cspHeader(directives: Record<string, string[]>): string;
export function consoleCspPlugin(): { name: string; configResolved(config: unknown): void; writeBundle(): void };
