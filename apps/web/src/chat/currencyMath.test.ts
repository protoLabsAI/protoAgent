import { describe, expect, it } from "vitest";

import { escapeCurrencyDollars } from "./currencyMath";

describe("escapeCurrencyDollars — currency isn't math", () => {
  it("escapes currency amounts so remark-math can't pair them into an inline-math span", () => {
    // The screenshot repro: two `$` in one line → everything between renders as KaTeX.
    expect(escapeCurrencyDollars("~A$180M total raised. Latest round A$63–90M")).toBe(
      "~A\\$180M total raised. Latest round A\\$63–90M",
    );
    expect(escapeCurrencyDollars("Valuation likely $600M–$1.1B.")).toBe(
      "Valuation likely \\$600M–\\$1.1B.",
    );
    expect(escapeCurrencyDollars("The cost is $5 and $10 per unit.")).toBe(
      "The cost is \\$5 and \\$10 per unit.",
    );
  });

  it("leaves genuine single-`$` inline math alone (opens on a letter/backslash, not a digit)", () => {
    expect(escapeCurrencyDollars("inline $x^2$ math")).toBe("inline $x^2$ math");
    expect(escapeCurrencyDollars("the ratio $\\pi$ appears")).toBe("the ratio $\\pi$ appears");
  });

  it("leaves `$$…$$` display math alone, even when it opens on a digit", () => {
    expect(escapeCurrencyDollars("$$5 + 3 = 8$$")).toBe("$$5 + 3 = 8$$");
    expect(escapeCurrencyDollars("Euler $$e^{i\\pi}+1=0$$ done")).toBe("Euler $$e^{i\\pi}+1=0$$ done");
  });

  it("does not double-escape an already-escaped `\\$`", () => {
    expect(escapeCurrencyDollars("already \\$5 escaped")).toBe("already \\$5 escaped");
  });

  it("leaves a `$` not followed by a digit untouched (spaced, trailing, or bare)", () => {
    expect(escapeCurrencyDollars("$ 5 has a space")).toBe("$ 5 has a space");
    expect(escapeCurrencyDollars("ends with a lone $")).toBe("ends with a lone $");
    expect(escapeCurrencyDollars("no dollars here")).toBe("no dollars here");
  });
});
