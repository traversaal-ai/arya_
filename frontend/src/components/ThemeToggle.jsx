import { useState } from "react";
import { Moon, Sun } from "lucide-react";
import { getTheme, setTheme } from "../theme.js";

/* Light/dark switch. Lives in the topbar (builder and call screen). The
   widget/embed surface has no toggle — its theme is host-driven via the
   loader's data-arya-theme attribute. */
export default function ThemeToggle() {
  const [theme, set] = useState(getTheme);
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="icon-btn plain"
      onClick={() => {
        setTheme(next);
        set(next);
      }}
      aria-label={`Switch to ${next} theme`}
      aria-pressed={theme === "dark"}
      title={`Switch to ${next} theme`}
    >
      {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  );
}
