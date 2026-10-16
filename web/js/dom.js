// Minimal DOM helpers. Text is always assigned as text, never parsed as HTML.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
      else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
      else if (key === 'text') el.textContent = value;
      else if (value === true) el.setAttribute(key, '');
      else el.setAttribute(key, String(value));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}

export function $$(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

export function replace(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

/** Small SVG icon from the inline sprite in index.html. */
export function icon(name, cls = '') {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', `icon ${cls}`.trim());
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(ns, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

export function badge(tone, label, title) {
  return h('span', { class: `badge tone-${tone}`, title: title || null }, h('i', { class: 'dot' }), label);
}

/** Copy text with a visible confirmation on the triggering element. */
export async function copyText(text, el) {
  try {
    await navigator.clipboard.writeText(text);
    if (el) {
      el.classList.add('copied');
      setTimeout(() => el.classList.remove('copied'), 900);
    }
  } catch {
    /* clipboard may be unavailable; the ID stays selectable */
  }
}
