// Identity icons (industries, templates, feature cards) — lucide line
// icons, replacing the emoji set the managers rejected. Chrome glyphs
// (send, x, check…) stay in Icon.jsx; these are the CONTENT icons.
//
// Frontend-only mapping on purpose: the server's emoji `icon` fields
// (catalog.py industries, template JSONs, saved cfg.icon) remain as dead
// data for backward compat but are never rendered again. The map keys on
// values the API already returns — industry `value` strings and template
// ids — so no backend contract changes.
import {
  Bot,
  Briefcase,
  CalendarCheck,
  Clapperboard,
  Dumbbell,
  GraduationCap,
  Headset,
  HeartPulse,
  House,
  Landmark,
  Laptop,
  Mic,
  Plane,
  Shapes,
  ShoppingBag,
  Utensils,
  Wand2,
} from "lucide-react";

// keyed by the server-owned industry `value` strings (backend catalog).
// Old values below the divider are no longer offered by the picker (the
// 12-card revamp renamed/split several categories) but stay mapped so any
// agent saved under the old value keeps a real icon instead of falling
// back to Shapes — those are frozen strings on saved agents, never
// migrated.
export const INDUSTRY_ICONS = {
  "retail and e-commerce": ShoppingBag,
  "food and beverage": Utensils,
  "hospitality and travel": Plane,
  "healthcare and medical": HeartPulse,
  "fitness and wellness": Dumbbell,
  "professional services": Briefcase,
  "housing and real estate": House,
  "technology and software": Laptop,
  "media and entertainment": Clapperboard,
  "education and training": GraduationCap,
  "banking and finance": Landmark,
  "": Shapes, // "Other": mixed shapes, not a star/sparkle

  // legacy values (pre-revamp), kept for existing saved agents only:
  "food and hospitality": Utensils,
  "healthcare and wellness": HeartPulse,
  "home services and real estate": House,
  "finance and insurance": Landmark,
};

export const TEMPLATE_ICONS = {
  travel: Plane,
  "travel-concierge": Plane,
  booking: CalendarCheck,
  appointments: CalendarCheck,
  support: Headset,
  "food-beverage": Utensils,
  fitness: Dumbbell,
  ticketing: Clapperboard,
};

// Card tints by industry: the landing site's voice-card families (sand,
// apricot, sage, blush, lavender, sky), applied through .card-tint-* in
// styles.css at reduced strength so text stays AA. "" (Other) and unknown
// values get no tint. Same key space as INDUSTRY_ICONS.
export const INDUSTRY_TINTS = {
  "retail and e-commerce": "sand",
  "food and beverage": "apricot",
  "hospitality and travel": "apricot",
  "healthcare and medical": "sage",
  "fitness and wellness": "sage",
  "professional services": "sky",
  "housing and real estate": "sand",
  "technology and software": "sky",
  "media and entertainment": "blush",
  "education and training": "lavender",
  "banking and finance": "sky",
  // legacy values, see INDUSTRY_ICONS
  "food and hospitality": "apricot",
  "healthcare and wellness": "sage",
  "home services and real estate": "sand",
  "finance and insurance": "sky",
};

export function industryTint(value) {
  const t = INDUSTRY_TINTS[value ?? ""];
  return t ? `card-tint-${t}` : "";
}

export function IndustryIcon({ value, size = 26, ...rest }) {
  // Unknown values (custom industries typed via "Other", legacy agents)
  // fall back to the Other icon rather than rendering nothing.
  const C = INDUSTRY_ICONS[value ?? ""] || Shapes;
  return <C size={size} strokeWidth={2} aria-hidden="true" {...rest} />;
}

export function TemplateIcon({ id, size = 26, ...rest }) {
  const C = TEMPLATE_ICONS[id] || Bot;
  return <C size={size} strokeWidth={2} aria-hidden="true" {...rest} />;
}

export { Bot, Mic, Wand2 };
