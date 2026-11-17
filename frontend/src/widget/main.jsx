import { createRoot } from "react-dom/client";
// Pre-paint theme: the loader forwards its data-arya-theme attribute as
// ?theme=dark on the iframe URL. Applied before render so there's no
// light flash. Default is light; deployment branding may still flip it
// after the agent info loads (see Widget.jsx).
try {
  const params = new URLSearchParams(location.search);
  if (params.get("theme") === "dark") {
    document.documentElement.dataset.theme = "dark";
  }
  // Per-embed orb colour (from the loader's data-arya-accent). Strict hex
  // check: this lands in a CSS custom property, never accept arbitrary text.
  const accent = params.get("accent");
  if (accent && /^#[0-9a-fA-F]{3,8}$/.test(accent)) {
    document.documentElement.style.setProperty("--orb-accent", accent);
  }
} catch {
  /* no search params: light, brand sage orb */
}
// Full builder stylesheet on purpose: the design system is one file and
// the call surface's rules live inside it; a widget-only split was judged
// not worth the fork risk of maintaining two copies of the same tokens.
// Geist only: the widget never renders the mono face, so --mono falls
// through to the system stack and the bundle stays one font file.
import "@fontsource-variable/geist/wght.css";
import "../styles.css";
import "./widget.css";
import Widget from "./Widget.jsx";

createRoot(document.getElementById("arya-widget")).render(<Widget />);
