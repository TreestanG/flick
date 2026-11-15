// Small line icons on a 96 grid, drawn here so the app ships no image files.
// They take the current text color.

const line = 'fill="none" stroke="currentColor" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"';
const fill = 'fill="currentColor"';

const SHAPES = {
  grid: [14, 54].flatMap((x) => [14, 54].map((y) =>
    `<rect x="${x}" y="${y}" width="28" height="28" rx="7" ${line}/>`)).join(""),
  list: [26, 48, 70].map((y) =>
    `<circle cx="18" cy="${y}" r="6" ${fill}/><path d="M40 ${y}H84" ${line}/>`).join(""),
  sliders: [[24, 62], [48, 34], [72, 56]].map(([y, knob]) =>
    `<path d="M12 ${y}H84" ${line}/><circle cx="${knob}" cy="${y}" r="10" ${fill}/>`).join(""),
  search: `<circle cx="38" cy="38" r="24" ${line}/><path d="M58 58L82 82" ${line}/>`,
  chevrons: `<path d="M32 40L48 24L64 40M32 56L48 72L64 56" ${line}/>`,
  camera: `<rect x="10" y="28" width="76" height="52" rx="10" ${line}/>` +
    `<circle cx="48" cy="54" r="13" ${line}/><path d="M34 18H62" ${line}/>`,
  globe: `<circle cx="48" cy="48" r="36" ${line}/><ellipse cx="48" cy="48" rx="16" ry="36" ${line}/>` +
    `<path d="M14 48H82" ${line}/>`,
  monitor: `<rect x="10" y="16" width="76" height="50" rx="8" ${line}/><path d="M48 68V80M30 82H66" ${line}/>`,
  code: `<path d="M36 28L16 48L36 68M60 28L80 48L60 68" ${line}/>`,
  hand: `<rect x="26" y="40" width="46" height="46" rx="14" ${line}/>` +
    `<path d="M33 42V22M46 42V14M59 42V20M72 58L82 44" ${line}/>`,
  play: `<path d="M30 18L78 48L30 78Z" ${fill}/>`,
  pause: `<rect x="24" y="18" width="16" height="60" rx="5" ${fill}/><rect x="56" y="18" width="16" height="60" rx="5" ${fill}/>`,
  power: `<path d="M64 24.3A32 32 0 1 1 32 24.3M48 12V46" ${line}/>`,
  logo: `<rect width="96" height="96" rx="24" ${fill}/>` +
    `<rect x="30" y="44" width="38" height="36" rx="12" fill="#fff"/>` +
    [[36, 26], [48, 18], [60, 24]].map(([x, top]) =>
      `<rect x="${x - 5}" y="${top}" width="10" height="${56 - top}" rx="5" fill="#fff"/>`).join(""),
};

/** A <span class="icon"> holding the named icon. Size it with CSS. */
export function icon(name, className = "") {
  const span = document.createElement("span");
  span.className = "icon " + className;
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = `<svg viewBox="0 0 96 96">${SHAPES[name] || ""}</svg>`;
  return span;
}
