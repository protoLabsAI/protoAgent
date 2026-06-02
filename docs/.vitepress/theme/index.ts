// Docs theme = the shared protoLabs.studio VitePress theme
// (@protolabsai/vitepress-theme: maps VitePress --vp-* vars → @protolabsai/design
// --pl-* tokens, so every studio docs site is brand-consistent from one source)
// + a "Built by protoLabs.studio" footer on every page via the `layout-bottom`
// slot (the built-in themeConfig.footer hides on sidebar/doc layouts).
import DefaultTheme from "vitepress/theme";
import type { Theme } from "vitepress";
import { h } from "vue";
import protoTheme from "@protolabsai/vitepress-theme";
import StudioFooter from "./StudioFooter.vue";

export default {
  ...protoTheme,
  Layout() {
    return h(DefaultTheme.Layout, null, {
      "layout-bottom": () => h(StudioFooter),
    });
  },
} satisfies Theme;
