// A heavy PDF for the artifact panel's off-main-thread render check (artifact-pdf-offthread.spec.ts).
//
// Its cost is in the work pdf.js does BEFORE drawing — inflating big Flate image streams and
// parsing long content streams — the same shape as a scanned or image-heavy rulebook (the stress
// test's 30.7 MB core-rules PDF). The mock server serves it over plain HTTP rather than the spec
// fulfilling it through page.route: WebKit delivers a routed body on its main thread, and a 26 MB
// one blocks for ~200 ms, which would measure Playwright instead of the panel.

import { deflateSync } from "node:zlib";

export const HEAVY_PDF_ID = "art-heavy-pdf";
export const HEAVY_PDF_PAGES = 6;

function build(pages, imgW, imgH, ops) {
  const parts = [];
  const offsets = [];
  let size = 0;
  const push = (b) => {
    const buf = typeof b === "string" ? Buffer.from(b, "latin1") : b;
    parts.push(buf);
    size += buf.length;
  };
  const obj = (n, body, stream) => {
    offsets[n] = size;
    push(`${n} 0 obj\n`);
    push(body);
    if (stream) {
      push("\nstream\n");
      push(stream);
      push("\nendstream");
    }
    push("\nendobj\n");
  };
  push("%PDF-1.7\n%\xe2\xe3\xcf\xd3\n");
  const kids = [];
  const raw = Buffer.alloc(imgW * imgH * 3);
  for (let p = 0; p < pages; p++) {
    const base = 3 + p * 3;
    kids.push(`${base} 0 R`);
    // A gradient with low-bit noise: it compresses (the file stays servable), but inflating it is
    // real work. A fixed LCG keeps the bytes identical run to run.
    let seed = 1234567 + p * 7919;
    for (let i = 0; i < raw.length; i++) {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      const x = (i / 3) % imgW;
      raw[i] = ((x + p * 37 + (i % 3) * 80) & 0xff) ^ (seed & 0x0f);
    }
    const img = deflateSync(raw, { level: 1 });
    let content = "q 612 0 0 792 0 0 cm /Im0 Do Q\n";
    for (let k = 0; k < ops; k++) content += `${(k * 7) % 600} ${(k * 13) % 780} 2 2 re f\n`;
    const cs = deflateSync(Buffer.from(content, "latin1"));
    obj(base, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im0 ${base + 2} 0 R >> >> /Contents ${base + 1} 0 R >>`);
    obj(base + 1, `<< /Length ${cs.length} /Filter /FlateDecode >>`, cs);
    obj(
      base + 2,
      `<< /Type /XObject /Subtype /Image /Width ${imgW} /Height ${imgH} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length ${img.length} >>`,
      img,
    );
  }
  obj(1, "<< /Type /Catalog /Pages 2 0 R >>");
  obj(2, `<< /Type /Pages /Kids [${kids.join(" ")}] /Count ${pages} >>`);
  const count = 3 + pages * 3;
  const xref = size;
  let table = `xref\n0 ${count}\n0000000000 65535 f \n`;
  for (let n = 1; n < count; n++) table += `${String(offsets[n]).padStart(10, "0")} 00000 n \n`;
  push(table);
  push(`trailer\n<< /Size ${count} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
  return Buffer.concat(parts);
}

let cached = null;
/** The fixture's bytes (~26 MB), built once per process. */
export function heavyPdf() {
  if (!cached) cached = build(HEAVY_PDF_PAGES, 2400, 3100, 40_000);
  return cached;
}
