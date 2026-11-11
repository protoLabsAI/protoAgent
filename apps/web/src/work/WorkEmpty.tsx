import { Empty } from "@protolabsai/ui/primitives";
import type { ComponentProps } from "react";

export function WorkEmpty(props: ComponentProps<typeof Empty>) {
  return <Empty {...props} className={`work-empty ${props.className ?? ""}`} />;
}
