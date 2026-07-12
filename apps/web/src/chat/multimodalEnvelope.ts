// Multimodal tool-result envelope detection (#1947). A tool that wants the model to SEE an
// image returns `multimodal_tool_result(text, images)` (#1930): a sentinel-prefixed JSON
// string — `"\x1e[multimodal-tool-v1]" + {"text": …, "images": [{"b64": …, "mime": …}, …]}`
// (graph/multimodal.py) — that the graph middleware rewrites into content blocks before the
// MODEL reads it. The console's tool stream, though, carries the tool's RAW return value,
// so without detection the result expander dumps the sentinel plus megabytes of base64.
// This parses the envelope down to what a human wants: the text part and how many images
// rode along.
//
// Robustness: server-side tool previews truncate to 800 chars
// (server/chat.py::_TOOL_PREVIEW_CHARS), so the envelope routinely arrives CUT MID-BASE64
// and its JSON does not parse. The fallback pulls the `"text"` value off the front of the
// payload (json.dumps writes keys in insertion order — text first, images last), and if even
// the text was cut it degrades to a generic label. Never the raw sentinel/b64 dump.

// The record-separator sentinel graph/multimodal.py prepends. Also matched WITHOUT the
// leading \x1e, in case a transport stripped the control char.
const SENTINEL = "\x1e[multimodal-tool-v1]";
const SENTINEL_BARE = "[multimodal-tool-v1]";

export type MultimodalEnvelope = {
  /** The envelope's human-readable caption ("" when unrecoverable). */
  text: string;
  /** How many images rode along — null when the truncated preview made the array unreadable. */
  imageCount: number | null;
  /** True when the payload JSON did not parse (the 800-char preview cut it). */
  truncated: boolean;
};

/** Parse a multimodal tool-result envelope, or null for an ordinary (non-sentinel) result —
 *  one cheap startsWith, so plain string outputs are untouched by construction. */
export function parseMultimodalEnvelope(raw: string): MultimodalEnvelope | null {
  let payload: string;
  if (raw.startsWith(SENTINEL)) payload = raw.slice(SENTINEL.length);
  else if (raw.startsWith(SENTINEL_BARE)) payload = raw.slice(SENTINEL_BARE.length);
  else return null;

  try {
    const p = JSON.parse(payload) as { text?: unknown; images?: unknown };
    if (p && typeof p === "object") {
      return {
        text: typeof p.text === "string" ? p.text : "",
        imageCount: Array.isArray(p.images) ? p.images.length : 0,
        truncated: false,
      };
    }
  } catch {
    // Truncated preview — fall through to the tolerant text extraction.
  }
  // A complete `{"text": "…"` prefix survives any truncation that lands inside the images
  // array (the common case: captions are short, base64 is megabytes). Re-quote the matched
  // body through JSON.parse for correct unescaping (\n, \", \uXXXX).
  const m = payload.match(/^\s*\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"/);
  if (m) {
    try {
      return { text: JSON.parse(`"${m[1]}"`) as string, imageCount: null, truncated: true };
    } catch {
      // Unescape failed — degrade to the generic label below.
    }
  }
  return { text: "", imageCount: null, truncated: true };
}
