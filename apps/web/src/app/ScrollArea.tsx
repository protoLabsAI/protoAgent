import type { ReactNode } from "react";

type ScrollAreaProps = {
  ariaLabel: string;
  children: ReactNode;
  className?: string;
};

export function ScrollArea({ ariaLabel, children, className = "" }: ScrollAreaProps) {
  return (
    <div className={`scroll-area${className ? ` ${className}` : ""}`} role="region" aria-label={ariaLabel} tabIndex={0}>
      {children}
    </div>
  );
}
