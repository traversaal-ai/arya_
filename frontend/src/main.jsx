import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
// Self-hosted fonts (bundled by Vite, no Google Fonts CDN at runtime).
// Variable Geist: one woff2 per subset covers every weight; registered as
// "Geist Variable" / "Geist Mono Variable" (see --sans and --mono).
import "@fontsource-variable/geist/wght.css";
import "@fontsource-variable/geist-mono/wght.css";
import "./styles.css";

createRoot(document.getElementById("root")).render(<App />);
