import { Streamdown } from "streamdown";

/**
 * Assistant message text via **streamdown** — a streaming-markdown renderer built for AI
 * output. It HARDENS incomplete markdown (a half-written code block / table / link doesn't
 * flash broken mid-stream) and memoizes blocks, instead of re-parsing the whole answer on
 * every streamed token (the old react-markdown path was O(N²) per turn). XSS-safe
 * (rehype-sanitize/harden, no raw HTML); Shiki code highlighting. Wrapped in `.markdown`
 * for the chat theme.
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <Streamdown>{children}</Streamdown>
    </div>
  );
}
