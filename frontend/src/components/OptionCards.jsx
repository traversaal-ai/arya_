import { useState } from "react";

/* Structured option groups (flight / hotel / rate searches, the order
   desk's lists, and the agent's own "choose one of the following") rendered
   as cards under the agent bubble that spoke them. One component for the
   live transcript (arya.options events) and History (messages.options),
   so the two can never drift.

   `onPick(item, group, groupIndex)` (live call only): each row becomes a
   button, and tapping it is the same as saying "option 2": the caller
   sends `item.pick` as a typed message and the agent's normal flow
   confirms and saves it. History passes no onPick and renders the plain
   informational rows, since there is no live call to select into. Rows
   carry no links by design: the caller's action here is to choose, not to
   leave.

   `picked` (live call only): { [groupIndex]: n | [n, ...] }. Once a group
   has a pick recorded, EVERY row in it renders as a static row (never a
   second button), the chosen one(s) get a check and the wash, the rest
   dim. This is what stops a second tap sending a second, contradictory
   pick.

   Per-kind layout: a flight/hotel/rate item that carries a `display`
   object renders a row built for its kind. `kind: "choice"` (the
   ask_choice tool) renders radio-style rows with the label and a one-line
   detail, or checkboxes plus a Done button when the group is `multi`. A
   group whose items carry pictures renders as a gallery of picture cards
   when two or more have one, and as a wide row with a larger thumbnail
   when only one does. An item with none of that falls back to the generic
   row: bold label, muted detail, right-aligned value. Nothing about an
   older stored shape stops rendering. */

function LegLine({ leg }) {
  return (
    <span className="optcard-leg">
      <span className="optcard-leg-time">{leg.dep}</span>
      <span className="optcard-leg-route">
        {leg.from}
        <svg className="optcard-leg-arrow" viewBox="0 0 24 24" aria-hidden="true">
          <path
            d="M5 12h14M13 6l6 6-6 6"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        {leg.to}
      </span>
      <span className="optcard-leg-time">
        {leg.arr}
        {leg.plus_days > 0 && <span className="optcard-plus">+{leg.plus_days}</span>}
      </span>
      {(leg.stops || leg.duration) && (
        <span className="optcard-leg-meta">
          {[leg.stops, leg.duration].filter(Boolean).join(" · ")}
        </span>
      )}
    </span>
  );
}

function PriceCol({ price, note }) {
  if (!price) return null;
  return (
    <span className="optcard-price-wrap">
      <span className="optcard-price">{price}</span>
      {note && <span className="optcard-price-note">{note}</span>}
    </span>
  );
}

function FlightBody({ it }) {
  const d = it.display;
  return (
    <>
      <span className="optcard-n">{it.n}</span>
      <span className="optcard-main">
        <b>{d.airline}</b>
        {d.airline_note && <span className="optcard-detail">{d.airline_note}</span>}
        {(d.legs || []).map((leg, i) => (
          <LegLine key={i} leg={leg} />
        ))}
      </span>
      <PriceCol price={d.price} note={d.price_note} />
    </>
  );
}

function HotelBody({ it }) {
  const d = it.display;
  const meta = [d.stars, [d.rating, d.reviews && `(${d.reviews})`].filter(Boolean).join(" "), d.area]
    .filter(Boolean)
    .join(" · ");
  return (
    <>
      <span className="optcard-n">{it.n}</span>
      <span className="optcard-main">
        <b>{d.name}</b>
        {meta && <span className="optcard-detail">{meta}</span>}
        {d.cancel && <span className="optcard-cancel">{d.cancel}</span>}
      </span>
      <PriceCol price={d.price} note={d.price_note} />
    </>
  );
}

function RateBody({ it }) {
  const d = it.display;
  const meta = [d.source, d.cancel].filter(Boolean).join(" · ");
  return (
    <>
      <span className="optcard-n">{it.n}</span>
      <span className="optcard-main">
        <b>{d.room}</b>
        {meta && <span className="optcard-detail">{meta}</span>}
      </span>
      <PriceCol price={d.price} note={d.price_note} />
    </>
  );
}

// A product picture where the desk supplies one. The hosted copy takes
// over if the first link fails, and a row with no working picture just
// loses the thumbnail.
function Pic({ it, className }) {
  return (
    <img
      className={className}
      src={it.image}
      alt=""
      loading="lazy"
      onError={(e) => {
        const img = e.currentTarget;
        if (it.image_fallback && img.src !== it.image_fallback) {
          img.src = it.image_fallback;
        } else {
          img.style.display = "none";
        }
      }}
    />
  );
}

function GenericBody({ it, bigThumb = false }) {
  return (
    <>
      <span className="optcard-n">{it.n}</span>
      {it.image && <Pic it={it} className={bigThumb ? "optcard-thumb lg" : "optcard-thumb"} />}
      <span className="optcard-main">
        <b>{it.label}</b>
        {it.detail && <span className="optcard-detail">{it.detail}</span>}
      </span>
      {it.value && <span className="optcard-val">{it.value}</span>}
    </>
  );
}

// "Choose one of the following": a radio circle (or a checkbox when the
// group allows several), the label, and its one-line detail.
function ChoiceBody({ it, multi, on }) {
  return (
    <>
      <span
        className={["optcard-radio", multi && "box", on && "on"].filter(Boolean).join(" ")}
        aria-hidden="true"
      />
      <span className="optcard-main">
        <b>{it.label}</b>
        {it.detail && <span className="optcard-detail">{it.detail}</span>}
      </span>
    </>
  );
}

function rowBody(kind, it, extra) {
  if (kind === "choice") return <ChoiceBody it={it} multi={extra.multi} on={extra.on} />;
  if (it.display) {
    if (kind === "flights") return <FlightBody it={it} />;
    if (kind === "hotels") return <HotelBody it={it} />;
    if (kind === "rates") return <RateBody it={it} />;
  }
  return <GenericBody it={it} bigThumb={extra.bigThumb} />;
}

function groupMeta(kind, display) {
  if (!display) return null;
  if (kind === "flights") {
    const route = display.from_code && display.to_code ? `${display.from_code} to ${display.to_code}` : "";
    return [route, display.dates, display.pax, display.cabin].filter(Boolean).join(" · ") || null;
  }
  if (kind === "hotels") {
    return [display.dates, display.pax, display.nights].filter(Boolean).join(" · ") || null;
  }
  return null;
}

const Chev = () => (
  <svg className="optcard-chev" viewBox="0 0 24 24" aria-hidden="true">
    <path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
const Check = () => (
  <svg className="optcard-check" viewBox="0 0 24 24" aria-hidden="true">
    <polyline points="20 6 9 17 4 12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);

const wasPicked = (pickedN, n) =>
  Array.isArray(pickedN) ? pickedN.includes(n) : pickedN != null && n === pickedN;

// Picture cards: the same items as a gallery, used when two or more rows
// carry a picture. The tap target is the whole card.
function Gallery({ g, gi, pickedN, onPick }) {
  const live = onPick && pickedN == null;
  return (
    <div className="optcard-gallery">
      {(g.items || []).map((it, ri) => {
        const isPicked = wasPicked(pickedN, it.n);
        const isDim = pickedN != null && !isPicked;
        const cls = ["optcard-pic", isPicked && "is-picked", isDim && "is-dim"].filter(Boolean).join(" ");
        const inner = (
          <>
            {it.image ? <Pic it={it} className="optcard-pic-img" /> : <span className="optcard-pic-img blank" />}
            <span className="optcard-pic-body">
              <b>{it.label}</b>
              {it.detail && <span className="optcard-detail">{it.detail}</span>}
              {it.value && <span className="optcard-pic-val">{it.value}</span>}
            </span>
            {isPicked && <Check />}
          </>
        );
        if (live) {
          return (
            <button
              type="button"
              className={cls}
              key={it.n}
              style={{ "--i": ri }}
              onClick={() => onPick(it, g, gi)}
              aria-label={`Option ${it.n}: ${it.label}.${it.detail ? ` ${it.detail}` : ""}`}
              title="Choose this option"
            >
              {inner}
            </button>
          );
        }
        return (
          <div className={cls} key={it.n} style={{ "--i": ri }}>
            {inner}
          </div>
        );
      })}
    </div>
  );
}

export default function OptionCards({ groups, onPick, picked = {} }) {
  // Multi-choice groups collect taps locally until Done sends them together.
  const [multiSel, setMultiSel] = useState({});
  if (!groups || groups.length === 0) return null;
  return groups.map((g, gi) => {
    const pickedN = picked[gi];
    const meta = groupMeta(g.kind, g.display);
    const items = g.items || [];
    const withPics = items.filter((it) => it.image).length;
    const isChoice = g.kind === "choice";
    const multi = isChoice && !!g.multi;
    const sel = multiSel[gi] || [];
    const live = onPick && pickedN == null;
    const cardCls = ["optcard", isChoice && "optcard-choice", withPics >= 2 && "has-gallery"]
      .filter(Boolean)
      .join(" ");
    return (
      <div className={cardCls} key={gi}>
        <div className={isChoice ? "optcard-question" : "optcard-title"}>{g.title}</div>
        {meta && <div className="optcard-meta">{meta}</div>}
        {withPics >= 2 ? (
          <Gallery g={g} gi={gi} pickedN={pickedN} onPick={onPick} />
        ) : (
          items.map((it, ri) => {
            const on = multi ? sel.includes(it.n) : false;
            const body = rowBody(g.kind, it, { multi, on, bigThumb: withPics === 1 });
            const isPicked = wasPicked(pickedN, it.n);
            const isDim = pickedN != null && !isPicked;
            const rowCls = ["optcard-row", isPicked && "is-picked", isDim && "is-dim", on && "is-on"]
              .filter(Boolean)
              .join(" ");
            const style = { "--i": ri };
            if (live && multi) {
              return (
                <button
                  type="button"
                  className={`${rowCls} optcard-pick`}
                  key={it.n}
                  style={style}
                  role="checkbox"
                  aria-checked={on}
                  onClick={() =>
                    setMultiSel((m) => ({
                      ...m,
                      [gi]: on ? sel.filter((n) => n !== it.n) : [...sel, it.n].sort((a, b) => a - b),
                    }))
                  }
                >
                  {body}
                </button>
              );
            }
            if (live) {
              return (
                <button
                  type="button"
                  className={`${rowCls} optcard-pick`}
                  key={it.n}
                  style={style}
                  onClick={() => onPick(it, g, gi)}
                  aria-label={
                    isChoice
                      ? `${it.label}${it.detail ? `. ${it.detail}` : ""}`
                      : `Option ${it.n}: ${it.label}.${it.detail ? ` ${it.detail}` : ""}`
                  }
                  title={isChoice ? "Choose this answer" : "Choose this option"}
                >
                  {body}
                  {!isChoice && <Chev />}
                </button>
              );
            }
            return (
              <div className={rowCls} key={it.n} style={style}>
                {body}
                {isPicked && <Check />}
              </div>
            );
          })
        )}
        {live && multi && (
          <div className="optcard-done">
            <button
              type="button"
              className="btn slim"
              disabled={sel.length === 0}
              onClick={() => {
                const chosen = items.filter((it) => sel.includes(it.n));
                const text = chosen.map((it) => it.pick || it.label).join(", ");
                onPick({ ...chosen[0], n: chosen[0].n, pick: text, label: text, multiPick: sel }, g, gi);
              }}
            >
              Done{sel.length > 0 ? ` (${sel.length})` : ""}
            </button>
          </div>
        )}
      </div>
    );
  });
}
