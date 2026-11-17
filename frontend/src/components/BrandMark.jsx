// The Arya mark from the landing site (landing/logo-mark.svg), inlined so
// it draws in currentColor and follows the theme. Decorative: the text next
// to it carries the name. Shared by the builder topbar and the call screen,
// so it must stay free of any admin-API import.
export default function BrandMark({ size = 28 }) {
  return (
    <svg
      className="brand-mark"
      viewBox="0 0 48 48"
      width={size}
      height={size}
      aria-hidden="true"
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth="3.2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M38.2 11.2 24 3 5.8 13.5v21L24 45l14.2-8.2" />
      <path d="M34 15.3 24 9.5 11.3 16.8v14.4L24 38.5l10-5.8" />
      <path d="M18.6 21.2v5.6M24 17.4v13.2M29.4 21.2v5.6" />
    </svg>
  );
}
