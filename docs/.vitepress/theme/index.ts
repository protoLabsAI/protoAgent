// Custom docs theme: the default VitePress theme + a "Built by protoLabs.studio"
// footer on every page. Injected via the `layout-bottom` slot (rather than
// themeConfig.footer, which VitePress hides on sidebar/doc layouts).
import DefaultTheme from "vitepress/theme";
import type { Theme } from "vitepress";
import { h } from "vue";
import StudioFooter from "./StudioFooter.vue";

export default {
  extends: DefaultTheme,
  Layout() {
    return h(DefaultTheme.Layout, null, {
      "layout-bottom": () => h(StudioFooter),
    });
  },
} satisfies Theme;
