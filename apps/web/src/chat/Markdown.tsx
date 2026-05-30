import type { ComponentPropsWithoutRef } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

// GitHub-flavored markdown (tables, strikethrough, task lists, autolinks) +
// syntax-highlighted code blocks. No raw-HTML plugin — LLM output is untrusted,
// so we never render embedded HTML (XSS guard).
const REMARK = [remarkGfm];
const REHYPE = [rehypeHighlight];

const components = {
  // External links open in a new tab; never let markdown navigate the app.
  a: (props: ComponentPropsWithoutRef<"a">) => (
    <a {...props} target="_blank" rel="noreferrer noopener" />
  ),
};

/** Render assistant message text as markdown. Wrapped in `.markdown` for theming. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={REMARK} rehypePlugins={REHYPE} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
